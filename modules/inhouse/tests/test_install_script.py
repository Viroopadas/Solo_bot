"""Text logic of deploy/install.py: Caddyfile edits must be exact and idempotent.

No servers, no Caddy: only the pure helpers, on texts shaped like the live files.
"""

import importlib.util

from pathlib import Path

import pytest


DEPLOY = Path(__file__).resolve().parents[1] / "features" / "yookassa_methods" / "deploy"
_spec = importlib.util.spec_from_file_location("inhouse_install", DEPLOY / "install.py")
inst = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inst)

RU_TEMPLATE = (DEPLOY / "Caddyfile.pay.omenso.net").read_text(encoding="utf-8")
SNIPPET = (DEPLOY / "Caddyfile.store-snippet").read_text(encoding="utf-8")

RU_OTHER = """sub.potok-lubvi.ru {
    reverse_proxy 127.0.0.1:3010 {
        header_up Host {host}
    }
}

viroopa.com {
    respond "hi" 200
}
"""

BOT_FILE = """https://panel.potok-lubvi.ru {
    reverse_proxy remnawave:3000
}

https://store.potok-lubvi.ru {
    header Content-Type text/plain
    request_body /p2pguard2/hook {
        max_size 4KB
    }

    reverse_proxy /p2pguard2/hook 172.18.0.1:3002 {
        header_up Host {host}
    }

    reverse_proxy /reserved_host/webhook 172.18.0.1:3002 {
        header_up Host {host}
    }
}

:443 {
    respond 404
}
"""


def ru_block(pay="pay.omenso.net", bot="store.potok-lubvi.ru"):
    return inst.render_ru_block(RU_TEMPLATE, pay, bot)


def test_render_ru_block_sets_domains():
    block = ru_block("pay.example.org", "bot.example.org")
    assert "pay.example.org {" in block and "pay.omenso.net {" not in block
    assert "reverse_proxy https://bot.example.org {" in block
    assert "store.potok-lubvi.ru" not in block.split("pay.example.org {", 1)[1]


def test_ru_block_appended_once_and_idempotent():
    once = inst.upsert_ru_block(RU_OTHER, ru_block(), "pay.omenso.net")
    assert once.startswith(RU_OTHER.rstrip("\n"))
    assert once.count("pay.omenso.net {") == 1
    assert inst.upsert_ru_block(once, ru_block(), "pay.omenso.net") == once


def test_ru_block_replaced_in_place_others_untouched():
    old = inst.upsert_ru_block(RU_OTHER, ru_block().replace("health_fails 3", "health_fails 1"), "pay.omenso.net")
    new = inst.upsert_ru_block(old, ru_block(), "pay.omenso.net")
    assert "health_fails 3" in new and "health_fails 1" not in new
    assert new.count("# Site block for the Russian server") == 1
    lines = new.split("\n")
    s, e = inst.find_site_block(lines, "sub.potok-lubvi.ru")
    assert "\n".join(lines[s:e + 1]) in RU_OTHER
    assert "viroopa.com {" in new


def test_bot_lines_inserted_in_store_block_only():
    ours = inst.snippet_lines(SNIPPET, "172.18.0.1:3002")
    new = inst.upsert_bot_lines(BOT_FILE, "store.potok-lubvi.ru", ours)
    lines = new.split("\n")
    s, e = inst.find_site_block(lines, "store.potok-lubvi.ru")
    store = "\n".join(lines[s:e + 1])
    for line in ours:
        assert line in store
    assert "respond /inhouse/ping 204" in store
    assert new.split("https://store.potok-lubvi.ru {")[0] == BOT_FILE.split("https://store.potok-lubvi.ru {")[0]
    assert new.endswith(":443 {\n    respond 404\n}\n")
    assert inst.upsert_bot_lines(new, "store.potok-lubvi.ru", ours) == new


def test_bot_lines_replaced_where_they_are():
    ours = inst.snippet_lines(SNIPPET, "172.18.0.1:3002")
    first = inst.upsert_bot_lines(BOT_FILE, "store.potok-lubvi.ru", ours)
    older = [l for l in ours if ">Content-Type" not in l]  # the live file before Sol's fix
    stale = inst.upsert_bot_lines(BOT_FILE, "store.potok-lubvi.ru", older)
    fixed = inst.upsert_bot_lines(stale, "store.potok-lubvi.ru", ours)
    assert fixed.count("/inhouse/pay >Content-Type") == 1
    assert fixed.count("reverse_proxy /inhouse/pay") == 1
    assert fixed.count("request_body /inhouse/pay") == 1
    assert [l.strip() for l in fixed.split("\n")] == [l.strip() for l in first.split("\n")]


def test_upstream_substituted():
    ours = inst.snippet_lines(SNIPPET, "10.0.0.5:3002")
    assert any("reverse_proxy /inhouse/pay 10.0.0.5:3002 {" in l for l in ours)
    assert not any("172.18.0.1" in l for l in ours)


def test_missing_bot_site_stops():
    with pytest.raises(inst.Stop):
        inst.upsert_bot_lines(BOT_FILE, "other.example.org", ["respond /inhouse/ping 204"])


def test_small_readers():
    assert inst.read_redirect_link('A = 1\nREDIRECT_LINK = "https://t.me/some_bot"\n') == "https://t.me/some_bot"
    assert inst.read_redirect_link("X = 1\n") is None
    assert inst.read_pay_domain('PUBLIC_BASE_URL = "https://pay.omenso.net"\n') == "pay.omenso.net"
    with pytest.raises(inst.Stop):
        inst.read_pay_domain('PUBLIC_BASE_URL = "http://pay.omenso.net"\n')
    assert inst.substitute_bot_link('<a href="__BOT_LINK__">', "https://t.me/b") == '<a href="https://t.me/b">'


def test_settings_pay_domain_is_single_source(tmp_path, monkeypatch):
    """Owner 2026-10-10: domain comes from settings, templates must follow it."""
    (tmp_path / "settings.py").write_text('PUBLIC_BASE_URL = "https://payments.example.org"\n')
    monkeypatch.setattr(inst, "FEATURE_DIR", tmp_path)
    context = inst.Ctx(inst.parse(["check"]), False)
    assert context.pay_domain == "payments.example.org"
    block = inst.render_ru_block(RU_TEMPLATE, context.pay_domain, "bot.example.org")
    assert "payments.example.org {" in block
    assert "pay.omenso.net {" not in block


def test_custom_web_root_used_in_all_ru_handlers():
    """Owner mandate: install on another pair with its own web root."""
    rendered = inst.render_ru_block(RU_TEMPLATE, "pay.example.org", "bot.example.org", "/srv/pay page")
    assert rendered.count('root * "/srv/pay page"') == 3
    assert "/var/www/pay-omenso" not in rendered


def test_site_braces_ignore_placeholders_and_quoted_responses():
    text = ['  site.example {', ' respond "{" 200', ' header X-Foo {host}', '}']
    assert inst.find_site_block(text, "site.example") == (0, 3)


def test_duplicate_and_combined_sites_refused():
    with pytest.raises(inst.Stop, match="duplicate"):
        inst.find_site_block(["site.example {", "}", "site.example {", "}"], "site.example")
    with pytest.raises(inst.Stop, match="unsupported"):
        inst.find_site_block(["site.example, other.example {", "}"], "site.example")


def test_uninstall_preserves_other_sites_and_comments():
    """Owner mandate: remove only our block and lines, preserve other sites."""
    ru = RU_OTHER + "\n# unrelated note\n" + ru_block()
    removed = inst.remove_ru_block(ru, "pay.omenso.net")
    assert RU_OTHER in removed
    assert "# unrelated note" in removed
    assert "pay.omenso.net {" not in removed
    assert inst.remove_ru_block(removed, "pay.omenso.net") == removed
    bot = inst.upsert_bot_lines(BOT_FILE, "store.potok-lubvi.ru", inst.snippet_lines(SNIPPET, "172.18.0.1:3002"))
    removed_bot = inst.remove_bot_lines(bot, "store.potok-lubvi.ru")
    assert "/inhouse/pay" not in removed_bot
    assert "/p2pguard2/hook" in removed_bot
    assert "/reserved_host/webhook" in removed_bot
    assert inst.remove_bot_lines(removed_bot, "store.potok-lubvi.ru") == removed_bot


def test_unrelated_pay_site_refused():
    text = 'pay.omenso.net {\n respond "another owner" 200\n}\n'
    with pytest.raises(inst.Stop, match="unrelated"):
        inst.upsert_ru_block(text, ru_block(), "pay.omenso.net")


def test_caddy_reload_nonzero_without_error_word_restored(monkeypatch):
    """External CLI exit status + owner mandate, not log-substring matching."""
    calls = []
    def fake(host, command, **kwargs):
        calls.append(command)
        if command.startswith("caddy reload"):
            raise inst.Stop("connection refused")
        return ""
    monkeypatch.setattr(inst, "ssh", fake)
    c = inst.Ctx(inst.parse(["install"]), True)
    with pytest.raises(inst.Stop):
        inst._apply_caddy(c, "fixture", "/srv/Caddyfile", "old", "new", docker=None, label="test")
    inst.rollback(c)
    assert any("cat /srv/Caddyfile.bak-inhouse-" in command for command in calls)


def test_success_output_containing_error_is_success(monkeypatch):
    monkeypatch.setattr(inst, "ssh", lambda *a, **k: "zero errors")
    c = inst.Ctx(inst.parse(["install"]), True)
    inst._apply_caddy(c, "fixture", "/srv/Caddyfile", "old", "new", docker=None, label="test")
    assert c.changed


def test_subprocess_nonzero_stderr_is_checked(monkeypatch):
    import subprocess
    monkeypatch.setattr(inst.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 7, b"", b"refused"))
    with pytest.raises(inst.Stop, match=r"command failed \(7\)"):
        inst.run(["fixture"])


def test_shell_quoting_uses_posix_directory_on_windows():
    from pathlib import PurePosixPath
    assert str(PurePosixPath("/srv/pay page/static/pay.js").parent) == "/srv/pay page/static"
