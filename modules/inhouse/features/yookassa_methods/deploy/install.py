#!/usr/bin/env python3
"""Install/remove the inhouse payment feature on an existing SoloBot + Caddy pair.

Operator-side Python (Windows Git Bash/PowerShell or Linux), SSH/SCP, no extra packages.
PUBLIC_BASE_URL in ../settings.py is the single source of the pay domain.
See README.md for prerequisites, CLI examples, rollback and the destructive opt-ins.
config.py and label secrets are never read; pass the public --bot-link explicitly.
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
import uuid
import json
import ipaddress

from pathlib import Path, PurePosixPath


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
    # Caddy placeholders and quoted responses are not block braces.
    words = re.findall(r'"(?:\\.|[^"\\])*"|\{[^{}\s]+\}|[^\s]+', line.split(" #", 1)[0])
    if words and words[0].startswith("#"):
        return 0
    return words.count("{") - words.count("}")


def find_site_block(lines: list[str], site: str) -> tuple[int, int] | None:
    """(first, last) line index of the top-level block `site {` or `https://site {`."""
    heads = {f"{site} {{", f"https://{site} {{"}
    found = []
    outer = 0
    for i, line in enumerate(lines):
        if line.strip() in heads and outer == 0:
            depth = 0
            for j in range(i, len(lines)):
                depth += _brace_delta(lines[j])
                if depth == 0:
                    found.append((i, j))
                    break
            else:
                raise Stop(f"site block {site} is not closed")
        outer += _brace_delta(line)
    if len(found) > 1:
        raise Stop(f"duplicate site blocks for {site}")
    if not found and any(site in line and not line.lstrip().startswith("#") for line in lines):
        raise Stop(f"unsupported combined/imported site shape for {site}; split into its own site first")
    return found[0] if found else None


def render_ru_block(template: str, pay_domain: str, bot_domain: str, web_root: str = "/var/www/pay-omenso") -> str:
    text = template.replace(f"{TEMPLATE_PAY_DOMAIN} {{", f"{pay_domain} {{")
    text = text.replace(f"https://{TEMPLATE_BOT_DOMAIN}", f"https://{bot_domain}")
    text = text.replace("/var/www/pay-omenso", '"' + web_root + '"') if web_root != "/var/www/pay-omenso" else text
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
    # Keep surrounding comments: they may describe a different owner's site.
    remove_ru_block(caddyfile, pay_domain)  # ownership check; never overwrite another site
    while new_block and new_block[0].lstrip().startswith("#"):
        new_block.pop(0)
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
        if re.match(r"\s*(?:request_body|header|reverse_proxy|respond)\s+/inhouse/(?:pay|ping)(?:\s|$)", line):
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


def remove_ru_block(caddyfile: str, pay_domain: str) -> str:
    lines = caddyfile.split("\n")
    found = find_site_block(lines, pay_domain)
    if not found:
        return caddyfile
    start, end = found
    block = "\n".join(lines[start:end + 1])
    if "handle /p/*" not in block or "@pay path /inhouse/pay" not in block:
        raise Stop("pay domain is occupied by an unrelated site; refusing to remove/replace it")
    return "\n".join(lines[:start] + lines[end + 1:])


def remove_bot_lines(caddyfile: str, bot_domain: str) -> str:
    lines = caddyfile.split("\n")
    found = find_site_block(lines, bot_domain)
    if not found:
        raise Stop(f"no site block for {bot_domain}")
    start, end = found
    kept, removed, _ = _strip_ours(lines[start + 1:end])
    if not removed:
        return caddyfile
    return "\n".join(lines[:start + 1] + kept + lines[end:])


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
    from html import escape
    return html.replace("__BOT_LINK__", escape(link, quote=True))


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
    try:
        res = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop(f"command unavailable or timed out: {cmd[0]} ({type(exc).__name__})") from None
    out = res.stdout.decode("utf-8", "replace")
    if check and res.returncode != 0:
        err = res.stderr.decode("utf-8", "replace").strip()[-600:]
        target = f"ssh {cmd[-2]}" if cmd[0] == "ssh" else cmd[0]
        raise Stop(f"command failed ({res.returncode}): {target}\n{err}")
    return out


def ssh(host: str, command: str, *, input_bytes: bytes | None = None, check: bool = True) -> str:
    return run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
               input_bytes=input_bytes, check=check)


def git(*args: str) -> str:
    return run(["git", "-C", str(BOT_ROOT), *args]).strip()


def say(msg: str) -> None:
    print(msg, flush=True)


# ---------- steps ----------

def feature_marker(c) -> str:
    return c.a.bot_dir + "/modules/inhouse/.yookassa_methods.disabled"


def snapshot_file(c, host: str, path: str, reload: str = "") -> None:
    """Register restore before any write; cat preserves Caddy's bind-mounted inode."""
    p = shlex.quote(path)
    bak = shlex.quote(path + ".bak-inhouse-" + c.stamp)
    ssh(host, f"if test -L {p}; then exit 1; fi; if test -f {p}; then cp -p {p} {bak}; "
              f"elif test -e {p}; then exit 1; fi")
    restore = f"if test -f {bak}; then cat {bak} > {p} && chmod --reference={bak} {p}; else rm -f {p}; fi"
    if reload:
        restore += " && " + reload
    c.undo.append((host, restore))
    c.cleanup.append((host, f"rm -f {bak}"))


def snapshot_module(c) -> None:
    d = shlex.quote(c.a.bot_dir + "/modules")
    bak = shlex.quote(c.a.bot_dir + f"/modules/.inhouse-backup-{c.stamp}.tar")
    ssh(c.a.bot_host, f"if test -d {d}/inhouse; then tar -cf {bak} -C {d} inhouse; fi")
    c.undo.append((c.a.bot_host, f"rm -rf {d}/inhouse && if test -f {bak}; then tar -xf {bak} -C {d}; fi"))
    c.cleanup.append((c.a.bot_host, f"rm -f {bak}"))


def cleanup(c) -> None:
    for host, command in c.cleanup:
        try:
            ssh(host, command)
        except Stop:
            say(f"[cleanup] could not remove temporary backup/lock on {host}; state already committed")
    c.cleanup.clear()


def rollback(c) -> None:
    errors = []
    if c.ff_attempted and not c.ff_applied:
        try:
            head = ssh(c.a.bot_host, f"git -C {shlex.quote(c.a.bot_dir)} rev-parse HEAD").strip()
            c.ff_applied = head == c.ff_target
        except Stop:
            errors.append("cannot determine git HEAD after interrupted fast-forward")
    for host, command in reversed(c.undo):
        try:
            ssh(host, command)
        except Stop:
            errors.append(f"{host}: restore failed (backup suffix .bak-inhouse-{c.stamp})")
    # A fast-forward is intentionally not rewritten: leave the new reviewed code off.
    if c.ff_applied:
        try:
            ssh(c.a.bot_host, f"touch {shlex.quote(feature_marker(c))}")
        except Stop:
            errors.append("could not leave fast-forwarded code disabled")
    if c.mutated:
        try:
            ssh(c.a.bot_host, f"systemctl restart {shlex.quote(c.a.bot_service)} && "
                              f"systemctl is-active --quiet {shlex.quote(c.a.bot_service)}")
        except Stop:
            errors.append("bot restart after restore failed")
    if errors:
        say("RESTORE INCOMPLETE: " + "; ".join(errors) + "; backups retained")
    else:
        cleanup(c)
        say("[restore] saved files restored; " +
            ("new git HEAD retained, feature disabled" if c.ff_applied else "previous state retained"))


def preflight(c) -> None:
    a = c.a
    for name in ("bot_dir", "bot_caddyfile", "ru_caddyfile", "web_root"):
        value = getattr(a, name)
        if (not value.startswith("/") or value in ("/", "/root", "/var", "/var/www", "/etc", "/opt")
                or any(ch in value for ch in '\\"\r\n\x00') or ".." in PurePosixPath(value).parts):
            raise Stop(f"unsafe absolute path: --{name.replace('_', '-')}")
    for name in ("bot_host", "ru_host", "bot_service", "bot_caddy_container", "postgres_container",
                 "database_name", "database_user"):
        value = getattr(a, name)
        if value and (value.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", value)):
            raise Stop(f"unsafe identifier: {name}")
    for domain in (c.pay_domain, a.bot_domain):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]", domain):
            raise Stop("invalid domain")
    if not re.fullmatch(r"[A-Za-z0-9.:-]+", a.bot_upstream):
        raise Stop("invalid bot upstream")
    if a.remove_module or a.drop_table:
        if a.command != "uninstall":
            raise Stop("--remove-module / --drop-table require uninstall")
    if a.drop_table and not (a.database_name and a.postgres_container):
        raise Stop("--drop-table needs --database-name and --postgres-container (local PostgreSQL, no passwords)")
    d = shlex.quote(a.bot_dir)
    def require(host, command, message):
        try:
            return ssh(host, command)
        except Stop as exc:
            raise Stop(f"[preflight] {host}: {message}; {exc}") from None

    require(a.bot_host, f"test -d {d}/modules && test -f {d}/utils/modules_loader.py && "
                    f"test -w {d}/modules && systemctl is-active --quiet {shlex.quote(a.bot_service)} && "
                    "command -v tar >/dev/null && command -v sha256sum >/dev/null && "
                    "command -v python3 >/dev/null", "need an existing writable SoloBot, active bot service, tar/sha256sum/python3")
    if not a.copy_module and a.command in ("check", "install"):
        ssh(a.bot_host, "command -v git >/dev/null")
    for host, path, container in ((a.ru_host, a.ru_caddyfile, ""),
                                  (a.bot_host, a.bot_caddyfile, a.bot_caddy_container)):
        p = shlex.quote(path)
        require(host, f"test -f {p} && test ! -L {p} && test -w {p} && "
                  f"test -w {shlex.quote(str(PurePosixPath(path).parent))} && "
                  "command -v sha256sum >/dev/null", f"need writable Caddyfile and parent, no symlink: {path}")
        if container:
            version = require(host, f"docker exec {shlex.quote(container)} caddy version", "need running Docker Caddy container")
            mounts = json.loads(ssh(host, f"docker inspect {shlex.quote(container)} --format '{{{{json .Mounts}}}}'"))
            if not any(m.get("Source") == path and m.get("Destination") == "/etc/caddy/Caddyfile"
                       and m.get("RW") for m in mounts):
                raise Stop("bot Caddyfile must be a writable bind mount at /etc/caddy/Caddyfile")
        else:
            version = require(host, "command -v caddy >/dev/null && systemctl is-active --quiet caddy && caddy version",
                              "need installed Caddy and active system caddy service")
        parsed = re.search(r"v?(\d+)\.(\d+)\.(\d+)", version)
        minimum = (2, 11, 1) if host == a.ru_host else (2, 9, 1)
        if not parsed or tuple(map(int, parsed.groups())) < minimum or parsed.group(1) != "2":
            raise Stop(f"Caddy on {host} must be version {'.'.join(map(str, minimum))} or newer in major 2")
    # Check the nearest existing parent and reject symlinks along the page path.
    root = shlex.quote(a.web_root)
    require(a.ru_host, f"ancestor={root}; while test ! -e \"$ancestor\"; do "
                   "test ! -L \"$ancestor\" || exit 1; ancestor=${ancestor%/*}; "
                   "test -n \"$ancestor\" || ancestor=/; done; "
                   "test -d \"$ancestor\" && test -w \"$ancestor\" || exit 1; "
                   f"ancestor={root}; while test \"$ancestor\" != /; do "
                   "test ! -L \"$ancestor\" || exit 1; ancestor=${ancestor%/*}; "
                   "test -n \"$ancestor\" || ancestor=/; done", "web root needs a writable directory ancestor and no symlinks")
    # This reads only the public module switch, never bot config or payment keys.
    state = ssh(a.bot_host, f"if test -f {d}/storage/modules_state.json; "
                            f"then cat {d}/storage/modules_state.json; else printf '{{}}'; fi")
    if a.command in ("check", "install", "enable") and "inhouse" in json.loads(state).get("disabled", []):
        raise Stop("enable inhouse in the bot admin Modules page first; custom MODULES_STATE_FILE must also be checked there")
    if a.command in ("disable", "enable"):
        require(a.bot_host, f"grep -q DISABLE_FILE {d}/modules/inhouse/settings.py", "module is too old for local overrides; install reviewed version first")
    if a.drop_table:
        ssh(a.bot_host, f"docker exec {shlex.quote(a.postgres_container)} sh -c "
                       "'command -v pg_dump >/dev/null && command -v psql >/dev/null'")
    say("[preflight] SSH, paths, bot service, Caddy and module switch ok; "
        "bot Python dependencies/YooKassa settings are checked by feature startup")

class Ctx:
    def __init__(self, a: argparse.Namespace, apply: bool):
        self.a = a
        self.apply = apply
        self.stamp = uuid.uuid4().hex[:12]
        self.code_changed = False
        self.undo = []
        self.cleanup = []
        self.changed = False
        self.mutated = False
        self.ff_applied = False
        self.ff_attempted = False
        self.ff_target = ""
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
        resolved = {r[4][0] for r in socket.getaddrinfo(c.pay_domain, 443, type=socket.SOCK_STREAM)}
    except OSError:
        raise Stop(f"DNS: {c.pay_domain} does not resolve — add an A record to the Russian server") from None
    ru_ips = set(ssh(c.a.ru_host, "hostname -I").split())
    if c.a.ru_public_ip:
        ru_ips.add(str(ipaddress.ip_address(c.a.ru_public_ip)))
    if not resolved.issubset(ru_ips):
        raise Stop(f"DNS {c.pay_domain}: {sorted(resolved)} not on Russian host; fix DNS, "
                   "or specify --ru-public-ip for NAT")
    say(f"[local] DNS {c.pay_domain} -> {sorted(resolved)} ok")


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
                               capture_output=True, timeout=180).returncode == 0
        if not known:
            raise Stop(f"bot server HEAD {server_head[:8]} is not an ancestor of the fork HEAD — "
                       "compare the server with the fork first (or use --copy-module on a non-fork bot)")
        # Inspect every transferred commit, including merges and changes later reverted.
        paths = [p for p in git("log", "-m", "--format=", "--name-only", f"{server_head}..{local_head}").splitlines() if p]
        if any(not p.startswith("modules/inhouse/") for p in paths):
            raise Stop("fast-forward changes files outside modules/inhouse; use --copy-module after review")
        say(f"[code] fast-forward {server_head[:8]} -> {local_head[:8]}")
        if not c.apply:
            c.code_changed = True
            return
        remote_bundle = f"/tmp/inhouse-{c.stamp}.bundle"
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "inhouse.bundle"
            git("bundle", "create", str(bundle), f"{server_head}..HEAD")
            run(["scp", "-q", "-o", "BatchMode=yes", str(bundle), f"{a.bot_host}:{remote_bundle}"])
        d = shlex.quote(a.bot_dir)
        c.cleanup.append((a.bot_host, f"rm -f {remote_bundle}"))
        c.mutated = True
        c.ff_attempted = True
        c.ff_target = local_head
        ssh(a.bot_host, f"git -C {d} fetch -q {remote_bundle} HEAD && "
                        f"git -C {d} merge --ff-only -q FETCH_HEAD")
        c.ff_applied = True
        c.changed = True
        c.code_changed = True
        return
    if not a.copy_module:
        raise Stop("bot is not a readable fork git repository: pass --copy-module explicitly")
    # Explicit copy mode: replace only our module, keeping an exact rollback archive.
    res = subprocess.run(["git", "-C", str(BOT_ROOT), "archive", "--format=tar", "HEAD", "--",
                          "modules/inhouse", ":(exclude)modules/inhouse/tests"], capture_output=True, timeout=180)
    if res.returncode != 0:
        raise Stop("git archive failed")
    archive = res.stdout
    remote_sum = ssh(a.bot_host, f"cd {shlex.quote(a.bot_dir)} 2>/dev/null && "
                                 "find modules/inhouse -type f ! -path '*/tests/*' ! -path '*/__pycache__/*' ! -name '.yookassa_methods.disabled' -exec sha256sum {} + "
                                 "2>/dev/null | sort", check=False)
    local_sum = _archive_sums(archive)
    if _norm_sums(remote_sum) == local_sum:
        say("[code] modules/inhouse on the server already equals the fork (copy mode)")
        return
    say("[code] copy modules/inhouse to the server (copy mode)")
    c.code_changed = True
    if c.apply:
        snapshot_module(c)
        marker = feature_marker(c)
        off = ssh(a.bot_host, f"if test -f {shlex.quote(marker)}; then printf off; fi") == "off"
        c.mutated = True
        ssh(a.bot_host, f"rm -rf {shlex.quote(a.bot_dir + '/modules/inhouse')} && tar -x -C {shlex.quote(a.bot_dir)}",
            input_bytes=archive)
        if off:
            ssh(a.bot_host, f"touch {shlex.quote(marker)}")
        c.changed = True


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
    link = c.a.bot_link
    if not re.fullmatch(r"https://t\.me/[A-Za-z0-9_]+", link):
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
                snapshot_file(c, a.ru_host, root + '/' + rel)
                c.mutated = True
                ssh(a.ru_host, f"umask 022; mkdir -p {shlex.quote(str(PurePosixPath(root + '/' + rel).parent))} && "
                               f"cat > {shlex.quote(root + '/' + rel)} && chmod 644 {shlex.quote(root + '/' + rel)}",
                    input_bytes=data)
                c.changed = True
    say(f"[pages] {('updated: ' + ', '.join(changed)) if changed else 'up to date'}")


def _apply_caddy(c: Ctx, host: str, path: str, old: str, new: str, *, docker: str | None, label: str) -> None:
    if old == new:
        say(f"[{label}] Caddyfile up to date")
        return
    say(f"[{label}] Caddyfile will change ({path})")
    if not c.apply:
        return
    p = shlex.quote(path)
    # Relative `import` paths are resolved relative to the candidate's directory.
    candidate_dir = "/etc/caddy" if docker else str(PurePosixPath(path).parent)
    candidate = candidate_dir + f"/.inhouse-{c.stamp}-candidate"
    if docker:
        validate = (f"docker exec -i {shlex.quote(docker)} sh -c "
                    + shlex.quote(f"trap 'rm -f {candidate}' EXIT; cat > {candidate} && "
                                  f"caddy validate --config {candidate} --adapter caddyfile"))
        reload = f"docker exec {shlex.quote(docker)} caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile"
    else:
        validate = (f"trap 'rm -f {candidate}' EXIT; cat > {candidate} && "
                    f"caddy validate --config {candidate} --adapter caddyfile")
        reload = f"caddy reload --config {p} --adapter caddyfile"
    snapshot_file(c, host, path, reload)
    try:
        ssh(host, validate, input_bytes=new.encode("utf-8"))
    except Stop as exc:
        raise Stop(f"[{label}] Caddy validation failed; candidate not installed: {exc}") from None
    # Write in place (keeps the inode of a bind-mounted file), then reload; restore on failure.
    c.mutated = True
    try:
        ssh(host, f"cat > {p}", input_bytes=new.encode("utf-8"))
        ssh(host, reload)
    except Stop as exc:
        raise Stop(f"[{label}] write/reload failed; restoring saved files: {exc}") from None
    c.changed = True
    say(f"[{label}] reloaded")


def step_caddy_ru(c: Ctx) -> None:
    a = c.a
    old = ssh(a.ru_host, f"cat {shlex.quote(a.ru_caddyfile)}")
    block = render_ru_block(RU_TEMPLATE.read_text(encoding="utf-8"), c.pay_domain, a.bot_domain, a.web_root)
    _apply_caddy(c, a.ru_host, a.ru_caddyfile, old, upsert_ru_block(old, block, c.pay_domain),
                 docker=None, label="caddy-ru")


def step_caddy_bot(c: Ctx) -> None:
    a = c.a
    old = ssh(a.bot_host, f"cat {shlex.quote(a.bot_caddyfile)}")
    ours = snippet_lines(BOT_SNIPPET.read_text(encoding="utf-8"), a.bot_upstream)
    new = upsert_bot_lines(old, a.bot_domain, ours)
    _apply_caddy(c, a.bot_host, a.bot_caddyfile, old, new, docker=a.bot_caddy_container or None, label="caddy-bot")


def restart_and_wait(c: Ctx, *, on: bool = True, removed: bool = False) -> None:
    a = c.a
    cursor = ssh(a.bot_host, f"journalctl -u {shlex.quote(a.bot_service)} -n 0 --show-cursor --no-pager").strip()
    match = re.search(r"-- cursor: (\S+)", cursor)
    if not match:
        raise Stop("cannot get bot journal cursor before restart")
    ssh(a.bot_host, f"systemctl restart {shlex.quote(a.bot_service)}")
    say(f"[bot] {a.bot_service} restarted, waiting for the feature line…")
    deadline = time.monotonic() + c.a.wait_seconds
    while time.monotonic() < deadline:
        ssh(a.bot_host, f"systemctl is-active --quiet {shlex.quote(a.bot_service)}")
        if removed:
            return
        out = ssh(a.bot_host, f"journalctl -u {shlex.quote(a.bot_service)} --after-cursor={shlex.quote(match.group(1))} "
                              "--no-pager -n 1 --grep='inhouse.yk\\] feature (on|off|stays off)'")
        if (on and "feature on:" in out) or (not on and "feature off:" in out):
            say("[bot] expected feature state confirmed")
            return
        if "stays off" in out:
            raise Stop("[bot] feature did not start; check startup conditions in bot journal")
        time.sleep(1)
    raise Stop("[bot] expected feature state not seen within --wait-seconds; check admin module switch and journal")


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
    marker = feature_marker(c)
    exists = ssh(a.bot_host, f"if test -f {shlex.quote(marker)}; then printf off; fi") == "off"
    if exists == (not on):
        return
    module = a.bot_dir + "/modules/inhouse"
    if ssh(a.bot_host, f"if test -d {shlex.quote(module)}; then printf yes; fi") != "yes":
        if on:
            raise Stop("module missing; run install first")
        return
    snapshot_file(c, a.bot_host, marker)
    c.mutated = True
    ssh(a.bot_host, f"rm -f {shlex.quote(marker)}" if on else f"touch {shlex.quote(marker)}")
    c.changed = True
    say(f"[bot] local override {'on' if on else 'off'} (tracked settings untouched)")


def uninstall(c: Ctx) -> None:
    a = c.a
    set_feature(c, False)
    step_caddy_remove(c)
    for rel in PAGE_FILES:
        path = a.web_root.rstrip('/') + '/' + rel
        if ssh(a.ru_host, f"if test -f {shlex.quote(path)}; then printf yes; fi") == "yes":
            snapshot_file(c, a.ru_host, path)
            c.mutated = True
            ssh(a.ru_host, f"rm -f {shlex.quote(path)}")
            c.changed = True
    if a.remove_module:
        module = a.bot_dir + "/modules/inhouse"
        if ssh(a.bot_host, f"if test -d {shlex.quote(module)}; then printf yes; fi") == "yes":
            snapshot_module(c)
            c.mutated = True
            ssh(a.bot_host, f"rm -rf {shlex.quote(module)}")
            c.changed = True
            say("[uninstall] module removed; tracked deletion is intentional, reinstall with --copy-module")
    if c.changed or a.restart:
        missing = ssh(a.bot_host, f"if test -d {shlex.quote(a.bot_dir + '/modules/inhouse')}; then printf yes; fi") != "yes"
        restart_and_wait(c, on=False, removed=missing)
    # Last operation: explicit destructive option, with a durable dump before DROP.
    if a.drop_table:
        table_exists = ssh(a.bot_host, postgres(c, "psql") +
                           " -Atqc \"SELECT to_regclass('public.inhouse_yk_links') IS NOT NULL\"").strip()
        if table_exists == "t":
            backup = a.bot_dir + f"/storage/inhouse_yk_links-{c.stamp}.sql"
            ssh(a.bot_host, f"umask 077; {postgres(c, 'pg_dump')} -t public.inhouse_yk_links > {shlex.quote(backup)}")
            say(f"[uninstall] table backup saved: {a.bot_host}:{backup}")
            query = postgres(c, "psql") + " -Atqc \"SELECT to_regclass('public.inhouse_yk_links') IS NOT NULL\""
            restore = (f"present=$({query}); rc=$?; test \"$rc\" -eq 0 || exit \"$rc\"; "
                       f"case \"$present\" in f) {postgres(c, 'psql', stdin=True)} -v ON_ERROR_STOP=1 < {shlex.quote(backup)};; "
                       "t) :;; *) exit 1;; esac")
            c.undo.append((a.bot_host, restore))
            c.mutated = True
            ssh(a.bot_host, f"{postgres(c, 'psql')} -v ON_ERROR_STOP=1 -c 'DROP TABLE IF EXISTS public.inhouse_yk_links'")
            say(f"[uninstall] table removed; retained backup {a.bot_host}:{backup}")
        elif table_exists != "f":
            raise Stop("cannot determine table existence")
    # Empty directories are harmless; never delete unrelated contents recursively.
    ssh(a.ru_host, f"rmdir {shlex.quote(a.web_root + '/static')} {shlex.quote(a.web_root)} 2>/dev/null || true")


def postgres(c: Ctx, tool: str, *, stdin: bool = False) -> str:
    a = c.a
    return (f"docker exec {'-i ' if stdin else ''}{shlex.quote(a.postgres_container)} {tool} "
            f"-U {shlex.quote(a.database_user)} -d {shlex.quote(a.database_name)}")


def step_caddy_remove(c: Ctx) -> None:
    a = c.a
    for host, path, docker, domain, label, remover in (
        (a.bot_host, a.bot_caddyfile, a.bot_caddy_container or None, a.bot_domain, "caddy-bot", remove_bot_lines),
        (a.ru_host, a.ru_caddyfile, None, c.pay_domain, "caddy-ru", remove_ru_block),
    ):
        old = ssh(host, f"cat {shlex.quote(path)}")
        _apply_caddy(c, host, path, old, remover(old, domain), docker=docker, label=label)


# ---------- main ----------

def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["check", "install", "disable", "enable", "uninstall"])
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
    p.add_argument("--bot-link", default="", help="required for check/install: public https://t.me/<bot>; config.py is never read")
    p.add_argument("--ru-public-ip", default="", help="explicit public Russian IP when behind NAT")
    p.add_argument("--wait-seconds", type=int, default=120)
    p.add_argument("--remove-module", action="store_true", help="uninstall only: remove whole inhouse module")
    p.add_argument("--drop-table", action="store_true", help="uninstall only: dump then drop feature table")
    p.add_argument("--postgres-container", default="")
    p.add_argument("--database-name", default="")
    p.add_argument("--database-user", default="postgres")
    p.add_argument("--copy-module", action="store_true", help="copy modules/inhouse instead of git fast-forward")
    p.add_argument("--restart", action="store_true", help="restart the bot even if the code did not change")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    a = parse(argv)
    c = None
    try:
        c = Ctx(a, apply=a.command != "check")
        preflight(c)
        if a.wait_seconds < 1 or a.wait_seconds > 600:
            raise Stop("--wait-seconds must be between 1 and 600")
        if a.command in ("check", "install"):
            bot_link(c)
            step_local(c)
            # Read and calculate BOTH Caddy edits before code/page changes.
            was_apply = c.apply
            c.apply = False
            step_code(c)
            step_caddy_bot(c)
            step_caddy_ru(c)
            c.apply = was_apply
        if c.apply:
            # Stable lock paths; interrupted runs leave a visible lock, never auto-steal.
            for host in dict.fromkeys((a.bot_host, a.ru_host)):
                ssh(host, "mkdir /tmp/inhouse-install.lock")
                c.cleanup.append((host, "rmdir /tmp/inhouse-install.lock"))
        if a.command in ("disable", "enable"):
            set_feature(c, a.command == "enable")
            if c.changed or a.restart:
                restart_and_wait(c, on=a.command == "enable")
            c.undo.clear()
            cleanup(c)
            return 0
        if a.command == "uninstall":
            uninstall(c)
            c.undo.clear()
            cleanup(c)
            say("=== uninstall finished ===")
            return 0
        say("=== check only: nothing will be changed ===" if not c.apply else "=== install ===")
        step_code(c)
        step_pages(c)
        step_caddy_bot(c)
        step_caddy_ru(c)
        if c.apply:
            set_feature(c, True)
            if c.changed or a.restart:
                restart_and_wait(c)
            smoke(c)
            c.undo.clear()
            cleanup(c)
            say("=== done ===")
        else:
            say("=== check finished; run `install` to apply ===")
        return 0
    except (Stop, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        say(f"STOP: {exc}")
        if c is not None and c.apply and (c.undo or c.cleanup):
            rollback(c)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
