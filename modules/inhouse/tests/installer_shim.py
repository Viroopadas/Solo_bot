"""Executable fake SSH/SCP and fake service CLIs; never imported by the bot.

Only fixture hosts bot-fixture/ru-fixture exist. Real POSIX file commands execute
inside the test directory. Caddy, Docker, systemd and journal are canned contracts.
Expectation source: owner's installer mandate 2026-10-10, deploy/README.md.
"""
import json
import base64
import os
from pathlib import Path
import shlex
import subprocess
import sys


def posix(path):
    value = str(path).replace("\\", "/")
    if len(value) > 2 and value[1] == ":":
        return "/" + value[0].lower() + value[2:]
    return value


def main():
    name = sys.argv[1]
    args = sys.argv[2:]
    if args and args[0] == "--encoded":
        args = [base64.b64decode(a).decode("utf-8") for a in args[1:]]
    root = Path(os.environ["INHOUSE_FAKE_ROOT"])
    host = os.environ.get("INHOUSE_FAKE_HOST", "")
    state_file = root / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    with (root / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps([name, host, args], ensure_ascii=False) + "\n")
    if name == "ssh":
        host, command = args[-2:]
        if host not in ("bot-fixture", "ru-fixture"):
            raise SystemExit("unknown fake host: real network forbidden")
        server = root / host
        command = command.replace("/tmp", posix(server / "tmp"))
        command = command.replace("/srv", posix(server / "srv"))
        command = command.replace("/etc/caddy", posix(server / "etc/caddy"))
        env = dict(os.environ, INHOUSE_FAKE_HOST=host)
        remote_bin = posix(root / "remote-bin")
        prelude = "export PATH=" + shlex.quote(remote_bin) + ":/usr/bin:/bin; "
        result = subprocess.run([os.environ["INHOUSE_FAKE_BASH"], "-c", prelude + command],
                                input=sys.stdin.buffer.read(), env=env, capture_output=True, timeout=20)
        if result.returncode:
            with (root / "failures.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps([command, result.returncode, result.stderr.decode("utf-8", "replace")[-500:]]) + "\n")
        sys.stdout.buffer.write(result.stdout)
        sys.stderr.buffer.write(result.stderr)
        return result.returncode
    if name == "scp":
        source, destination = args[-2:]
        host, path = destination.split(":", 1)
        if host not in ("bot-fixture", "ru-fixture"):
            return 91
        target = root / host / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(source).read_bytes())
        return 0
    if name == "hostname":
        print("192.0.2.10")
        return 0
    if name == "git":
        if "merge" in args and state.get("fail_merge"):
            state["fail_merge"] = False
            state_file.write_text(json.dumps(state), encoding="utf-8")
            return 38
        if os.name == "nt":
            args = [a[1].upper() + ":" + a[2:] if len(a) > 3 and a[0] == "/" and a[2] == "/" else a for a in args]
        result = subprocess.run([os.environ["INHOUSE_FAKE_REAL_GIT"], *args], capture_output=True, timeout=15)
        sys.stdout.buffer.write(result.stdout)
        sys.stderr.buffer.write(result.stderr)
        return result.returncode
    if name == "systemctl":
        if state.get("fail_restart") and args[0] == "restart":
            state["fail_restart"] = False
            state_file.write_text(json.dumps(state), encoding="utf-8")
            return 7
        return 0
    if name == "journalctl":
        if "--show-cursor" in args:
            print("-- cursor: fixture-cursor")
        else:
            marker = root / host / "srv/bot/modules/inhouse/.yookassa_methods.disabled"
            print("[inhouse.yk] feature off: local override" if marker.exists()
                  else "[inhouse.yk] feature on: fixture")
        return 0
    if name == "docker":
        if args[0] == "inspect":
            print(json.dumps([{"Source": "/srv/bot-caddy", "Destination": "/etc/caddy/Caddyfile", "RW": True}]))
            return 0
        args = args[1:]
        if args[0] == "-i":
            args = args[1:]
        args = args[1:]  # container
        if args[0] in ("sh", "caddy"):
            args = [str(root / "remote-bin" / args[0]), *args[1:]] if args[0] == "caddy" else args
            result = subprocess.run([os.environ["INHOUSE_FAKE_BASH"], "-c", shlex.join(args)],
                                    input=sys.stdin.buffer.read(), capture_output=True, timeout=10)
            sys.stdout.buffer.write(result.stdout)
            sys.stderr.buffer.write(result.stderr)
            return result.returncode
        if args[0] == "psql":
            if "to_regclass" in " ".join(args):
                print("t" if state.get("table", True) else "f")
            else:
                data = sys.stdin.buffer.read()
                state["table"] = b"CREATE TABLE" in data
                if state.get("fail_drop"):
                    state["fail_drop"] = False
                    state_file.write_text(json.dumps(state), encoding="utf-8")
                    return 7
                state_file.write_text(json.dumps(state), encoding="utf-8")
            return 0
        if args[0] == "pg_dump":
            print("CREATE TABLE public.inhouse_yk_links (id integer);")
            return 0
        return 92
    if name == "caddy":
        if args[0] == "version":
            print("v2.9.1" if host == "bot-fixture" else "v2.11.1")
            return 0
        failure = state.get("failure")
        if failure == host + ":" + args[0]:
            state["failure"] = ""
            state_file.write_text(json.dumps(state), encoding="utf-8")
            print("connection refused")  # deliberately no substring 'error'
            return 9
        if args[0] == "reload":
            print("zero errors")  # successful output deliberately contains 'error'
        return 0
    return 93


if __name__ == "__main__":
    raise SystemExit(main())
