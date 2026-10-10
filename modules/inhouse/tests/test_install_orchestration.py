"""Owner 2026-10-10: install, re-run, failure restore, uninstall with fake SSH/SCP.

Pure transport fakes record every command; the generated POSIX scripts themselves
run through Git Bash / Linux bash on fixture files. No Caddy/browser/network.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from modules.inhouse.tests.installer_shim import posix

DEPLOY = Path(__file__).resolve().parents[1] / "features/yookassa_methods/deploy"
SHIM = Path(__file__).with_name("installer_shim.py")

CS = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
class Shim {
 static string Q(string s) { return "\"" + s.Replace("\"", "\\\"") + "\""; }
 static int Main(string[] args) {
  string name = Path.GetFileNameWithoutExtension(Environment.GetCommandLineArgs()[0]);
  string a = Q(Environment.GetEnvironmentVariable("INHOUSE_FAKE_SCRIPT")) + " " + Q(name) + " --encoded";
  foreach (string arg in args) a += " " + Convert.ToBase64String(Encoding.UTF8.GetBytes(arg));
  var info = new ProcessStartInfo(Environment.GetEnvironmentVariable("INHOUSE_FAKE_PYTHON"), a);
  info.UseShellExecute = false;
  var p = Process.Start(info); p.WaitForExit(); return p.ExitCode;
 }
}
'''


def git(root, *args):
    res = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=20)
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    return res.stdout.decode().strip()


@pytest.fixture(scope="module")
def shim_bin(tmp_path_factory):
    directory = tmp_path_factory.mktemp("executables")
    if os.name == "nt":
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        assert compiler.is_file(), "Windows transport tests require .NET Framework csc"
        source = directory / "Shim.cs"
        source.write_text(CS, encoding="utf-8")
        result = subprocess.run([str(compiler), "/nologo", "/out:" + str(directory / "ssh.exe"), str(source)],
                                capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout
        shutil.copyfile(directory / "ssh.exe", directory / "scp.exe")
    else:
        for name in ("ssh", "scp"):
            script = directory / name
            script.write_text("#!/bin/sh\nexec " + __import__("shlex").quote(sys.executable) + " " +
                              __import__("shlex").quote(str(SHIM)) + " " + name + ' "$@"\n')
            script.chmod(0o755)
    return directory


@pytest.fixture
def pair(tmp_path, monkeypatch, shim_bin):
    bash = shutil.which("bash")
    assert bash, "POSIX orchestration tests require bash / Windows Git Bash"
    local = tmp_path / "fork"
    module = local / "modules/inhouse"
    shutil.copytree(DEPLOY.parents[2], module, ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))
    git(local, "init", "-q")
    git(local, "config", "user.email", "fixture@example.invalid")
    git(local, "config", "user.name", "Fixture")
    git(local, "add", "modules")
    git(local, "commit", "-qm", "fixture module from reviewed working files")
    for host in ("bot-fixture", "ru-fixture"):
        (tmp_path / host / "srv").mkdir(parents=True)
        (tmp_path / host / "tmp").mkdir()
        (tmp_path / host / "etc/caddy").mkdir(parents=True)
    bot = tmp_path / "bot-fixture/srv/bot"
    (bot / "modules").mkdir(parents=True)
    (bot / "utils").mkdir()
    (bot / "utils/modules_loader.py").write_text("fixture")
    (bot / "storage").mkdir()
    bot_caddy = tmp_path / "bot-fixture/srv/bot-caddy"
    bot_caddy.write_text('other.example {\n respond "{}" 200\n}\nstore.potok-lubvi.ru {\n header Content-Type text/plain\n}\n')
    ru_caddy = tmp_path / "ru-fixture/srv/ru-caddy"
    ru_caddy.write_text('other.example {\n respond "hi" 200\n}\n')
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    remote_bin = tmp_path / "remote-bin"
    remote_bin.mkdir()
    import shlex
    for name in ("caddy", "docker", "systemctl", "journalctl", "hostname", "python3", "psql", "pg_dump", "git"):
        script = remote_bin / name
        script.write_text("#!/bin/sh\nexec " + shlex.quote(posix(sys.executable)) + " " +
                          shlex.quote(str(SHIM).replace("\\", "/")) + " " + name + ' "$@"\n', encoding="utf-8")
        script.chmod(0o755)
    for key, value in {"INHOUSE_FAKE_ROOT": str(tmp_path), "INHOUSE_FAKE_BASH": bash,
                       "INHOUSE_FAKE_PYTHON": sys.executable, "INHOUSE_FAKE_SCRIPT": str(SHIM),
                       "INHOUSE_FAKE_REAL_GIT": shutil.which("git")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", str(shim_bin) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("MSYS_NO_PATHCONV", "1")
    spec = importlib.util.spec_from_file_location("installer_orchestration", os.environ.get("INHOUSE_INSTALLER_UNDER_TEST", DEPLOY / "install.py"))
    inst = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inst)
    monkeypatch.setattr(inst, "BOT_ROOT", local)
    monkeypatch.setattr(inst.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("192.0.2.10", 443))])

    def http(url, method="GET", data=None):
        if method == "POST":
            return 400, ["text/html; charset=utf-8"], "invalid link"
        if url.endswith("/ping"):
            return 204, [], ""
        if url.endswith("/pay"):
            return 405, [], ""
        return 200, ["text/html"], '<form action="/inhouse/pay">'
    monkeypatch.setattr(inst, "_http", http)
    args = ["--bot-host", "bot-fixture", "--ru-host", "ru-fixture", "--bot-dir", "/srv/bot",
            "--bot-caddyfile", "/srv/bot-caddy", "--ru-caddyfile", "/srv/ru-caddy",
            "--web-root", "/srv/pay page", "--bot-link", "https://t.me/fixture_bot", "--wait-seconds", "2"]

    class Pair:
        def run(self, command="install", *extra, copy=True):
            return inst.main([command, *args, *(["--copy-module"] if copy else []), *extra])

        def files(self):
            return {str(p.relative_to(tmp_path)): p.read_bytes() for host in ("bot-fixture", "ru-fixture")
                    for p in (tmp_path / host).rglob("*") if p.is_file() and ".git" not in p.parts}

        def calls(self):
            return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]

    result = Pair()
    result.root, result.bot, result.bot_caddy, result.ru_caddy = tmp_path, bot, bot_caddy, ru_caddy
    result.state, result.inst, result.local = state, inst, local
    return result


def test_install_empty_pair_rerun_uninstall(pair):
    inode = pair.bot_caddy.stat().st_ino
    before = pair.files()
    assert pair.run("check") == 0
    assert pair.files() == before
    assert pair.run() == 0
    assert pair.bot_caddy.stat().st_ino == inode
    assert "other.example" in pair.ru_caddy.read_text()
    assert 'root * "/srv/pay page"' in pair.ru_caddy.read_text()
    assert (pair.root / "ru-fixture/srv/pay page/index.html").is_file()
    assert (pair.bot / "modules/inhouse/router.py").is_file()
    installed = pair.files()
    call_count = len(pair.calls())
    assert pair.run() == 0
    assert pair.files() == installed
    second = pair.calls()[call_count:]
    assert not any(a[0] == "caddy" and a[2][0] in ("reload", "validate") for a in second)
    assert not any(a[0] == "systemctl" and a[2][0] == "restart" for a in second)
    assert pair.run("uninstall") == 0
    assert "/inhouse/pay" not in pair.bot_caddy.read_text()
    assert "pay.omenso.net {" not in pair.ru_caddy.read_text()
    assert "other.example" in pair.ru_caddy.read_text()
    assert not (pair.root / "ru-fixture/srv/pay page/index.html").exists()
    assert (pair.bot / "modules/inhouse/.yookassa_methods.disabled").is_file()
    uninstalled = pair.files()
    assert pair.run("uninstall") == 0
    assert pair.files() == uninstalled
    assert before != installed


@pytest.mark.parametrize("failure", ["bot-fixture:validate", "ru-fixture:validate", "bot-fixture:reload", "ru-fixture:reload"])
def test_failure_restores_pair_and_stops(pair, failure, capsys):
    before = pair.files()
    inode = pair.bot_caddy.stat().st_ino
    pair.state.write_text(json.dumps({"failure": failure}))
    assert pair.run() == 2
    assert pair.files() == before
    assert pair.bot_caddy.stat().st_ino == inode
    assert "saved files restored" in capsys.readouterr().out
    assert not list(pair.root.rglob("*.bak-inhouse-*"))
    assert not list(pair.root.rglob("inhouse-install.lock"))


def test_disable_enable_does_not_dirty_settings(pair):
    assert pair.run() == 0
    settings = pair.bot / "modules/inhouse/settings.py"
    before = settings.read_bytes()
    assert pair.run("disable") == 0
    assert settings.read_bytes() == before
    assert (settings.parent / ".yookassa_methods.disabled").exists()
    assert pair.run("enable") == 0
    assert settings.read_bytes() == before
    assert not (settings.parent / ".yookassa_methods.disabled").exists()


def test_restart_failure_restores_all_files(pair):
    before = pair.files()
    pair.state.write_text(json.dumps({"fail_restart": True}))
    assert pair.run() == 2
    assert pair.files() == before


def test_uninstall_optional_module_and_table(pair):
    assert pair.run() == 0
    assert pair.run("uninstall", "--remove-module", "--drop-table", "--postgres-container", "db",
                    "--database-name", "fixture_db") == 0
    assert not (pair.bot / "modules/inhouse").exists()
    assert json.loads(pair.state.read_text())["table"] is False
    assert len(list((pair.bot / "storage").glob("inhouse_yk_links-*.sql"))) == 1
    assert pair.run("uninstall", "--remove-module", "--drop-table", "--postgres-container", "db",
                    "--database-name", "fixture_db") == 0


def test_git_bundle_fast_forward_and_scp(pair):
    git(pair.bot, "init", "-q")
    git(pair.bot, "fetch", str(pair.local), "HEAD")
    git(pair.bot, "merge", "--ff-only", "FETCH_HEAD")
    old = git(pair.bot, "rev-parse", "HEAD")
    (pair.local / "modules/inhouse/new-file.txt").write_text("owner's new module release")
    git(pair.local, "add", "modules/inhouse/new-file.txt")
    git(pair.local, "commit", "-qm", "next module release")
    assert pair.run(copy=False) == 0
    assert git(pair.bot, "rev-parse", "HEAD") == git(pair.local, "rev-parse", "HEAD") != old
    assert any(call[0] == "scp" for call in pair.calls())
    assert not list((pair.root / "bot-fixture/tmp").glob("*.bundle"))


def test_reload_exit_code_regression_real_transport(pair):
    """Also run against 557409ac: failure without the word error must still stop."""
    i = pair.inst
    old = pair.bot_caddy.read_text()
    pair.state.write_text(json.dumps({"failure": "bot-fixture:reload"}))
    c = i.Ctx(i.parse(["install", "--bot-host", "bot-fixture", "--bot-dir", "/srv/bot"]), True)
    with pytest.raises(i.Stop):
        i._apply_caddy(c, "bot-fixture", "/srv/bot-caddy", old, old + "# new\n", docker="caddy", label="bot")
    i.rollback(c)
    assert pair.bot_caddy.read_text() == old


def test_smoke_failure_restores_pair(pair, monkeypatch):
    before = pair.files()
    monkeypatch.setattr(pair.inst, "_http", lambda *a, **k: (502, [], "unavailable"))
    assert pair.run() == 2
    assert pair.files() == before


def test_wrong_dns_stops_without_writes(pair, monkeypatch):
    before = pair.files()
    monkeypatch.setattr(pair.inst.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("192.0.2.99", 443))])
    assert pair.run() == 2
    assert pair.files() == before


def test_git_merge_failure_not_hidden_by_bundle_cleanup(pair):
    git(pair.bot, "init", "-q")
    git(pair.bot, "fetch", str(pair.local), "HEAD")
    git(pair.bot, "merge", "--ff-only", "FETCH_HEAD")
    old = git(pair.bot, "rev-parse", "HEAD")
    (pair.local / "modules/inhouse/new-file.txt").write_text("new release")
    git(pair.local, "add", "modules/inhouse/new-file.txt")
    git(pair.local, "commit", "-qm", "next release")
    pair.state.write_text(json.dumps({"fail_merge": True}))
    assert pair.run(copy=False) == 2
    assert git(pair.bot, "rev-parse", "HEAD") == old
    assert not (pair.bot / "modules/inhouse/new-file.txt").exists()


def test_fast_forward_then_smoke_failure_keeps_new_head_disabled(pair, monkeypatch):
    """Owner forbids history rewrite; README promises reviewed new code left off."""
    git(pair.bot, "init", "-q")
    git(pair.bot, "fetch", str(pair.local), "HEAD")
    git(pair.bot, "merge", "--ff-only", "FETCH_HEAD")
    before_bot, before_ru = pair.bot_caddy.read_bytes(), pair.ru_caddy.read_bytes()
    (pair.local / "modules/inhouse/new-file.txt").write_text("new release")
    git(pair.local, "add", "modules/inhouse/new-file.txt")
    git(pair.local, "commit", "-qm", "next release")
    monkeypatch.setattr(pair.inst, "_http", lambda *a, **k: (502, [], "unavailable"))
    assert pair.run(copy=False) == 2
    assert git(pair.bot, "rev-parse", "HEAD") == git(pair.local, "rev-parse", "HEAD")
    assert (pair.bot / "modules/inhouse/.yookassa_methods.disabled").exists()
    assert git(pair.bot, "status", "--porcelain") == "?? utils/"
    assert pair.bot_caddy.read_bytes() == before_bot
    assert pair.ru_caddy.read_bytes() == before_ru


def test_history_with_reverted_outside_change_refused(pair):
    """The bundle must not transfer commits modifying other modules/configs."""
    git(pair.bot, "init", "-q")
    git(pair.bot, "fetch", str(pair.local), "HEAD")
    git(pair.bot, "merge", "--ff-only", "FETCH_HEAD")
    before = pair.files()
    outside = pair.local / "outside.txt"
    outside.write_text("unrelated public data")
    git(pair.local, "add", "outside.txt")
    git(pair.local, "commit", "-qm", "outside change")
    outside.unlink()
    git(pair.local, "add", "outside.txt")
    git(pair.local, "commit", "-qm", "remove outside change")
    assert git(pair.local, "diff", "--name-only", git(pair.bot, "rev-parse", "HEAD"), "HEAD") == ""
    assert pair.run(copy=False) == 2
    assert pair.files() == before
    assert not any(call[0] == "scp" for call in pair.calls())


def test_drop_failure_restores_table_from_retained_dump(pair):
    assert pair.run() == 0
    bot_caddy, ru_caddy = pair.bot_caddy.read_bytes(), pair.ru_caddy.read_bytes()
    pair.state.write_text(json.dumps({"fail_drop": True, "table": True}))
    assert pair.run("uninstall", "--drop-table", "--postgres-container", "db", "--database-name", "fixture_db") == 2
    assert json.loads(pair.state.read_text())["table"] is True
    assert pair.bot_caddy.read_bytes() == bot_caddy
    assert pair.ru_caddy.read_bytes() == ru_caddy
    assert len(list((pair.bot / "storage").glob("inhouse_yk_links-*.sql"))) == 1
    assert not (pair.bot / "modules/inhouse/.yookassa_methods.disabled").exists()
