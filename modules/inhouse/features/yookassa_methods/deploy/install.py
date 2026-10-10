#!/usr/bin/env python3
"""Install the yookassa_methods feature (SBP / card / "SberPay и другие способы" link buttons).

Runs on the operator's machine (Windows Git Bash or Linux) from the fork checkout and talks to
both servers over ssh. Every address lives in one place: the CLI options below (defaults are the
current production). The pay domain itself is PUBLIC_BASE_URL in ../settings.py, so the bot and
the Russian Caddy can never disagree about it.

    python install.py check      # read everything, print what would change, change nothing
    python install.py install    # apply: code, pages, both Caddy files, restart, smoke test
    python install.py disable    # rollback: feature off + bot restart (old "Оплатить" screen)
    python install.py enable     # feature back on + bot restart

Steps of `install` (each is skipped when the server already matches):
 1. local checks: the fork has no uncommitted changes in modules/inhouse; DNS of the pay domain
    points at the Russian server;
 2. bot code: the server's git HEAD must be an ancestor of the local HEAD and the tracked tree
    clean; then a git bundle is fast-forwarded (server = fork). On a server whose bot is not our
    fork, pass --copy-module to copy modules/inhouse (without tests) instead;
 3. Russian server: pay page files into --web-root (the bot link is substituted), our site block
    in its Caddyfile (replaced in place or appended; other sites untouched);
 4. bot server: our /inhouse/ lines inside the bot domain's site block of the Caddy file (the
    file is bind-mounted into the container: written in place, inode kept);
 5. every Caddy change: backup next to the file (*.bak-inhouse-<stamp>), validate the candidate,
    write, reload; the backup is put back if anything fails;
 6. bot restart when the code changed (or --restart), then wait for "[inhouse.yk] feature on";
 7. smoke test over the internet: page 200, GET /inhouse/pay 405, invalid POST 400 with one
    text/html Content-Type, the bot Caddy's /inhouse/ping 204.

No payments are made. Secrets are never read or printed: the only value taken from config.py is
the public t.me link REDIRECT_LINK (or pass --bot-link).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from pathlib import Path


DEPLOY_DIR = Path(__file__).resolve().parent
FEATURE_DIR = DEPLOY_DIR.parent
BOT_ROOT = FEATURE_DIR.parents[3]  # .../Solo_bot
PAGE_FILES = ("index.html", "unavailable.html", "static/pay.js", "static/pay.css")
RU_TEMPLATE = DEPLOY_DIR / "Caddyfile.pay.omenso.net"
BOT_SNIPPET = DEPLOY_DIR / "Caddyfile.store-snippet"
TEMPLATE_PAY_DOMAIN = "pay.omenso.net"
TEMPLATE_BOT_DOMAIN = "store.potok-lubvi.ru"
TEMPLATE_UPSTREAM = "172.18.0.1:3002"
MARK = "inhouse"


class Stop(Exception):
    """A check failed: nothing more is done; the message says why."""


# ---------- pure text helpers (unit-tested in tests/test_install_script.py) ----------

def _brace_delta(line: str) -> int:
    if line.lstrip().startswith("#"):
        return 0
    return line.count("{") - line.count("}")


def find_site_block(lines: list[str], site: str) -> tuple[int, int] | None:
    """(first, last) line index of the top-level block `site {` or `https://site {`."""
    heads = {f"{site} {{", f"https://{site} {{"}
    for i, line in enumerate(lines):
        if line.strip() in heads and not line.startswith((" ", "\t")):
            depth = 0
            for j in range(i, len(lines)):
                depth += _brace_delta(lines[j])
                if depth == 0:
                    return i, j
            raise Stop(f"site block {site} is not closed")
    return None


def render_ru_block(template: str, pay_domain: str, bot_domain: str) -> str:
    text = template.replace(f"{TEMPLATE_PAY_DOMAIN} {{", f"{pay_domain} {{")
    text = text.replace(f"https://{TEMPLATE_BOT_DOMAIN}", f"https://{bot_domain}")
    return text.rstrip("\n") + "\n"


def upsert_ru_block(caddyfile: str, block: str, pay_domain: str) -> str:
    """Replace our site block (with the comment lines right above it) or append it."""
    lines = caddyfile.split("\n")
    found = find_site_block(lines, pay_domain)
    new_block = block.rstrip("\n").split("\n")
    if found is None:
        body = caddyfile.rstrip("\n")
        return body + "\n\n" + "\n".join(new_block) + "\n"
    start, end = found
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    out = lines[:start] + new_block + lines[end + 1:]
    return "\n".join(out)


def snippet_lines(snippet: str, upstream: str) -> list[str]:
    out = []
    for line in snippet.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        out.append(line.replace("\t", "    ").replace(TEMPLATE_UPSTREAM, upstream).rstrip())
    return out


def _strip_ours(body: list[str]) -> tuple[list[str], list[str], int | None]:
    """Remove our /inhouse/ directives from a site body; return (kept, removed, first_index)."""
    kept, removed, first = [], [], None
    i = 0
    while i < len(body):
        line = body[i]
        if f"/{MARK}/" in line:
            if first is None:
                first = len(kept)
            depth = _brace_delta(line)
            removed.append(line)
            i += 1
            while depth > 0 and i < len(body):
                depth += _brace_delta(body[i])
                removed.append(body[i])
                i += 1
            continue
        kept.append(line)
        i += 1
    return kept, removed, first


def upsert_bot_lines(caddyfile: str, bot_domain: str, ours: list[str]) -> str:
    """Put our lines into the bot domain's site block: same place if present, else before `}`."""
    lines = caddyfile.split("\n")
    found = find_site_block(lines, bot_domain)
    if found is None:
        raise Stop(f"no site block for {bot_domain} in the bot Caddyfile")
    start, end = found
    body = lines[start + 1:end]
    kept, removed, first = _strip_ours(body)
    if [l.strip() for l in removed] == [l.strip() for l in ours]:
        return caddyfile
    if first is None:
        while kept and not kept[-1].strip():
            kept.pop()
        insert_at = len(kept)
        new_body = kept[:insert_at] + [""] + ours
    else:
        new_body = kept[:first] + ours + kept[first:]
    return "\n".join(lines[:start + 1] + new_body + lines[end:])


def substitute_bot_link(html: str, link: str) -> str:
    return html.replace("__BOT_LINK__", link)


def read_redirect_link(config_text: str) -> str | None:
    m = re.search(r'^REDIRECT_LINK\s*=\s*["\']([^"\']+)["\']', config_text, re.M)
    return m.group(1) if m else None


def read_pay_domain(settings_text: str) -> str:
    m = re.search(r'^PUBLIC_BASE_URL\s*=\s*["\']https://([^/"\']+)/?["\']', settings_text, re.M)
    if not m:
        raise Stop("PUBLIC_BASE_URL in settings.py is not an https address")
    return m.group(1)


# ---------- shell helpers ----------

def run(cmd: list[str], *, input_bytes: bytes | None = None, check: bool = True) -> str:
    res = subprocess.run(cmd, input=input_bytes, capture_output=True)
    out = res.stdout.decode("utf-8", "replace")
    if check and res.returncode != 0:
        err = res.stderr.decode("utf-8", "replace").strip()[-600:]
        raise Stop(f"command failed ({res.returncode}): {' '.join(cmd[:3])} …\n{err}")
    return out


def ssh(host: str, command: str, *, input_bytes: bytes | None = None, check: bool = True) -> str:
    return run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
               input_bytes=input_bytes, check=check)


def git(*args: str) -> str:
    return run(["git", "-C", str(BOT_ROOT), *args]).strip()


def say(msg: str) -> None:
    print(msg, flush=True)


# ---------- steps ----------

class Ctx:
    def __init__(self, a: argparse.Namespace, apply: bool):
        self.a = a
        self.apply = apply
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.code_changed = False
        self.pay_domain = read_pay_domain((FEATURE_DIR / "settings.py").read_text(encoding="utf-8"))
        if a.pay_domain and a.pay_domain != self.pay_domain:
            raise Stop(f"--pay-domain {a.pay_domain} differs from settings.py ({self.pay_domain}): "
                       "change PUBLIC_BASE_URL in settings.py, commit, then run again")


def step_local(c: Ctx) -> None:
    dirty = git("status", "--porcelain", "--", "modules/inhouse", ".gitignore")
    if dirty:
        raise Stop("the fork has uncommitted changes in modules/inhouse — commit first:\n" + dirty)
    say(f"[local] fork HEAD {git('rev-parse', '--short', 'HEAD')}, pay domain {c.pay_domain}")
    try:
        resolved = socket.gethostbyname(c.pay_domain)
    except OSError:
        raise Stop(f"DNS: {c.pay_domain} does not resolve — add an A record to the Russian server") from None
    ru_ips = ssh(c.a.ru_host, "hostname -I").split()
    if resolved not in ru_ips:
        # A server behind NAT does not list its public address: warn, do not stop.
        say(f"[local] WARNING: {c.pay_domain} -> {resolved}, {c.a.ru_host} lists {ru_ips}; "
            "make sure the A record points at the Russian server, or Caddy gets no certificate")
    else:
        say(f"[local] DNS {c.pay_domain} -> {resolved} (Russian server) ok")


def step_code(c: Ctx) -> None:
    a = c.a
    local_head = git("rev-parse", "HEAD")
    is_repo = ssh(a.bot_host, f"git -C {shlex.quote(a.bot_dir)} rev-parse HEAD", check=False).strip()
    if re.fullmatch(r"[0-9a-f]{40}", is_repo or "") and not a.copy_module:
        server_head = is_repo
        dirty = ssh(a.bot_host, f"git -C {shlex.quote(a.bot_dir)} status --porcelain --untracked-files=no").strip()
        if dirty:
            raise Stop("bot server has changed tracked files (run `enable` if you used `disable`):\n" + dirty)
        if server_head == local_head:
            say("[code] bot server already at the fork HEAD")
            return
        known = subprocess.run(["git", "-C", str(BOT_ROOT), "merge-base", "--is-ancestor", server_head, local_head],
                               capture_output=True).returncode == 0
        if not known:
            raise Stop(f"bot server HEAD {server_head[:8]} is not an ancestor of the fork HEAD — "
                       "compare the server with the fork first (or use --copy-module on a non-fork bot)")
        say(f"[code] fast-forward {server_head[:8]} -> {local_head[:8]}")
        if not c.apply:
            c.code_changed = True
            return
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "inhouse.bundle"
            git("bundle", "create", str(bundle), f"{server_head}..HEAD")
            run(["scp", "-q", "-o", "BatchMode=yes", str(bundle), f"{a.bot_host}:/tmp/inhouse.bundle"])
        d = shlex.quote(a.bot_dir)
        ssh(a.bot_host, f"git -C {d} fetch -q /tmp/inhouse.bundle HEAD:refs/remotes/inhouse-bundle/main && "
                        f"git -C {d} merge --ff-only -q inhouse-bundle/main && "
                        f"git -C {d} update-ref -d refs/remotes/inhouse-bundle/main; rm -f /tmp/inhouse.bundle")
        c.code_changed = True
        return
    # Not our fork (or --copy-module): copy modules/inhouse without tests.
    res = subprocess.run(["git", "-C", str(BOT_ROOT), "archive", "--format=tar", "HEAD", "--",
                          "modules/inhouse", ":(exclude)modules/inhouse/tests"], capture_output=True)
    if res.returncode != 0:
        raise Stop("git archive failed")
    archive = res.stdout
    remote_sum = ssh(a.bot_host, f"cd {shlex.quote(a.bot_dir)} 2>/dev/null && "
                                 "find modules/inhouse -type f ! -path '*/tests/*' ! -name '*.pyc' -exec sha256sum {} + "
                                 "2>/dev/null | sort", check=False)
    local_sum = _archive_sums(archive)
    if _norm_sums(remote_sum) == local_sum:
        say("[code] modules/inhouse on the server already equals the fork (copy mode)")
        return
    say("[code] copy modules/inhouse to the server (copy mode)")
    c.code_changed = True
    if c.apply:
        ssh(a.bot_host, f"test -d {shlex.quote(a.bot_dir)}/modules && tar -x -C {shlex.quote(a.bot_dir)}",
            input_bytes=archive)


def _archive_sums(archive: bytes) -> list[tuple[str, str]]:
    import io
    import tarfile
    out = []
    with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
        for m in tf.getmembers():
            if m.isfile():
                out.append((m.name, hashlib.sha256(tf.extractfile(m).read()).hexdigest()))
    return sorted(out)


def _norm_sums(text: str) -> list[tuple[str, str]]:
    out = []
    for line in text.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            out.append((parts[1].strip().lstrip("*"), parts[0]))
    return sorted(out)


def bot_link(c: Ctx) -> str:
    link = c.a.bot_link or read_redirect_link((BOT_ROOT / "config.py").read_text(encoding="utf-8")
                                              if (BOT_ROOT / "config.py").exists() else "")
    if not link or not link.startswith("https://t.me/"):
        raise Stop("bot link unknown: pass --bot-link https://t.me/<bot>")
    return link


def step_pages(c: Ctx) -> None:
    a = c.a
    link = bot_link(c)
    root = a.web_root.rstrip("/")
    changed = []
    for rel in PAGE_FILES:
        data = (DEPLOY_DIR / rel).read_bytes()
        if rel == "unavailable.html":
            data = substitute_bot_link(data.decode("utf-8"), link).encode("utf-8")
        remote = ssh(a.ru_host, f"sha256sum {shlex.quote(root + '/' + rel)} 2>/dev/null | cut -c1-64", check=False).strip()
        if remote != hashlib.sha256(data).hexdigest():
            changed.append(rel)
            if c.apply:
                ssh(a.ru_host, f"mkdir -p {shlex.quote(str(Path(root + '/' + rel).parent.as_posix()))} && "
                               f"cat > {shlex.quote(root + '/' + rel)} && chmod 644 {shlex.quote(root + '/' + rel)}",
                    input_bytes=data)
    say(f"[pages] {('updated: ' + ', '.join(changed)) if changed else 'up to date'}")


def _apply_caddy(c: Ctx, host: str, path: str, old: str, new: str, *, docker: str | None, label: str) -> None:
    if old == new:
        say(f"[{label}] Caddyfile up to date")
        return
    say(f"[{label}] Caddyfile will change ({path})")
    if not c.apply:
        return
    p = shlex.quote(path)
    bak = shlex.quote(f"{path}.bak-inhouse-{c.stamp}")
    if docker:
        validate = (f"docker exec -i {shlex.quote(docker)} sh -c "
                    "'cat > /tmp/inhouse-candidate && caddy validate --config /tmp/inhouse-candidate --adapter caddyfile'")
        reload = f"docker exec {shlex.quote(docker)} caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile"
    else:
        validate = ("cat > /tmp/inhouse-candidate && caddy validate --config /tmp/inhouse-candidate "
                    "--adapter caddyfile; rc=$?; rm -f /tmp/inhouse-candidate; exit $rc")
        reload = f"caddy reload --config {p} --adapter caddyfile"
    ssh(host, validate, input_bytes=new.encode("utf-8"))
    ssh(host, f"cp -a {p} {bak}")
    say(f"[{label}] backup {path}.bak-inhouse-{c.stamp}")
    # Write in place (keeps the inode of a bind-mounted file), then reload; restore on failure.
    ssh(host, f"cat > {p}", input_bytes=new.encode("utf-8"))
    out = ssh(host, reload + " 2>&1", check=False)
    if "error" in out.lower() and "not formatted" not in out.lower():
        ssh(host, f"cat {bak} > {p} && {reload}", check=False)
        raise Stop(f"[{label}] reload failed, backup restored:\n{out[-600:]}")
    say(f"[{label}] reloaded")


def step_caddy_ru(c: Ctx) -> None:
    a = c.a
    old = ssh(a.ru_host, f"cat {shlex.quote(a.ru_caddyfile)}")
    block = render_ru_block(RU_TEMPLATE.read_text(encoding="utf-8"), c.pay_domain, a.bot_domain)
    _apply_caddy(c, a.ru_host, a.ru_caddyfile, old, upsert_ru_block(old, block, c.pay_domain),
                 docker=None, label="caddy-ru")


def step_caddy_bot(c: Ctx) -> None:
    a = c.a
    old = ssh(a.bot_host, f"cat {shlex.quote(a.bot_caddyfile)}")
    ours = snippet_lines(BOT_SNIPPET.read_text(encoding="utf-8"), a.bot_upstream)
    new = upsert_bot_lines(old, a.bot_domain, ours)
    _apply_caddy(c, a.bot_host, a.bot_caddyfile, old, new, docker=a.bot_caddy_container or None, label="caddy-bot")


def restart_and_wait(c: Ctx) -> None:
    a = c.a
    since = ssh(a.bot_host, "date '+%Y-%m-%d %H:%M:%S'").strip()
    ssh(a.bot_host, f"systemctl restart {shlex.quote(a.bot_service)}")
    say(f"[bot] {a.bot_service} restarted, waiting for the feature line…")
    for _ in range(24):
        time.sleep(5)
        out = ssh(a.bot_host, f"journalctl -u {shlex.quote(a.bot_service)} --since {shlex.quote(since)} --no-pager "
                              "| grep -E 'inhouse.yk\\] feature (on|stays off)' | tail -1", check=False)
        if "feature on" in out:
            say("[bot] " + out.strip().split("|")[-1].strip())
            return
        if "stays off" in out:
            raise Stop("[bot] feature did not start: " + out.strip().split("|")[-1].strip())
    raise Stop("[bot] no feature line within 2 minutes — check the bot journal")


def _http(url: str, method: str = "GET", data: bytes | None = None) -> tuple[int, list[str], str]:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None

    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": "inhouse-install-smoke"})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, r.headers.get_all("Content-Type") or [], r.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get_all("Content-Type") or [], e.read(4000).decode("utf-8", "replace")


def smoke(c: Ctx) -> None:
    base = f"https://{c.pay_domain}"
    checks = []
    st, _, body = _http(f"{base}/p/install-smoke")
    checks.append(("page 200 with form", st == 200 and 'action="/inhouse/pay"' in body))
    st, _, _ = _http(f"{base}/inhouse/pay")
    checks.append(("GET /inhouse/pay 405", st == 405))
    st, ctypes, body = _http(f"{base}/inhouse/pay", "POST", b"t=install.smoke")
    checks.append(("invalid POST 400, one text/html", st == 400 and len(ctypes) == 1 and "text/html" in ctypes[0]))
    st, _, _ = _http(f"https://{c.a.bot_domain}/{MARK}/ping")
    checks.append(("bot Caddy /inhouse/ping 204", st == 204))
    for name, ok in checks:
        say(f"[smoke] {'ok  ' if ok else 'FAIL'} {name}")
    if not all(ok for _, ok in checks):
        raise Stop("smoke test failed")


def set_feature(c: Ctx, on: bool) -> None:
    a = c.a
    f = shlex.quote(f"{a.bot_dir}/modules/inhouse/settings.py")
    old, new = ('False', 'True') if on else ('True', 'False')
    ssh(a.bot_host, f"sed -i 's/\"yookassa_methods\": {old}/\"yookassa_methods\": {new}/' {f} && "
                    f"grep -n '\"yookassa_methods\"' {f}")
    say(f"[bot] feature {'on' if on else 'off'} in settings.py")
    ssh(a.bot_host, f"systemctl restart {shlex.quote(a.bot_service)}")
    say(f"[bot] {a.bot_service} restarted")


# ---------- main ----------

def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["check", "install", "disable", "enable"])
    p.add_argument("--bot-host", default="solobot-remnawave", help="ssh host of the bot server")
    p.add_argument("--bot-dir", default="/root/Solo_bot")
    p.add_argument("--bot-service", default="bot")
    p.add_argument("--bot-domain", default=TEMPLATE_BOT_DOMAIN, help="public domain of the bot web server")
    p.add_argument("--bot-upstream", default=TEMPLATE_UPSTREAM, help="bot web address as seen by its Caddy")
    p.add_argument("--bot-caddyfile", default="/opt/remnawave/caddy/Caddyfile")
    p.add_argument("--bot-caddy-container", default="caddy", help="docker container of the bot Caddy; '' = system Caddy")
    p.add_argument("--ru-host", default="test-bg-ru1", help="ssh host of the Russian server")
    p.add_argument("--ru-caddyfile", default="/etc/caddy/Caddyfile")
    p.add_argument("--web-root", default="/var/www/pay-omenso")
    p.add_argument("--pay-domain", default="", help="must equal PUBLIC_BASE_URL in settings.py (checked)")
    p.add_argument("--bot-link", default="", help="https://t.me/<bot>; default REDIRECT_LINK from config.py")
    p.add_argument("--copy-module", action="store_true", help="copy modules/inhouse instead of git fast-forward")
    p.add_argument("--restart", action="store_true", help="restart the bot even if the code did not change")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    a = parse(argv)
    try:
        if a.command in ("disable", "enable"):
            set_feature(Ctx(a, apply=True), a.command == "enable")
            return 0
        c = Ctx(a, apply=a.command == "install")
        say("=== check only: nothing will be changed ===" if not c.apply else "=== install ===")
        step_local(c)
        step_code(c)
        step_pages(c)
        step_caddy_bot(c)
        step_caddy_ru(c)
        if c.apply:
            if c.code_changed or a.restart:
                restart_and_wait(c)
            smoke(c)
            say("=== done ===")
        else:
            say("=== check finished; run `install` to apply ===")
        return 0
    except Stop as exc:
        say(f"STOP: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
