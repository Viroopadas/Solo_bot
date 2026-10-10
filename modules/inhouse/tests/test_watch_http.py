"""Probe answer, observer signals and the menu mode through the real endpoint / link layer.

Spec 2026-10-10 "pay site watch" edition 2, sections 3.1, 3.2, 3.8, 4.3 and 9.
A real aiohttp test server on a local socket of the test process (like test_endpoint_http);
database and YooKassa are fakes and must not be called by a probe or a signal.
"""

import asyncio
import hashlib
import hmac
import json
import types

import pytest

from aiogram import Bot
from aiogram.methods import SendMessage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yookassa import Payment

from modules.inhouse import settings as module_settings
from modules.inhouse.features.yookassa_methods import (
    compat,
    feature,
    link_buttons,
    menu_mode,
    pay_endpoint,
    providers,
    sdk_patch,
    settings as yk_settings,
    state,
    token,
    watch,
)
from modules.inhouse.tests.helpers import SECRET, FakeStockService, activate, count_rows, make_deps, past, save_checkout
from modules.inhouse.tests.test_link_buttons import STOCK_CB, RecordingSession, flat, purchase_markup
from modules.inhouse.tests.test_providers import base_providers


TG = 888000444
NONCE = "0123456789abcdef"


@pytest.fixture
async def client():
    app = web.Application()
    app.router.add_post("/inhouse/pay", pay_endpoint.handle)
    async with TestClient(TestServer(app)) as c:
        yield c


async def post_raw(client, body: bytes, ctype="application/x-www-form-urlencoded", **headers):
    return await client.post(
        "/inhouse/pay", data=body, headers={"Content-Type": ctype, **headers}, allow_redirects=False
    )


# --- probe (3.2) ---------------------------------------------------------------------------


@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize("mode", ["links", "fallback"])
async def test_probe_exact_body(client, ready, mode, db):
    svc = FakeStockService()
    if ready:
        activate(make_deps(db, svc))
    menu_mode.set_mode(mode)
    resp = await post_raw(client, f"probe={NONCE}".encode(), Origin="https://evil.example")
    assert resp.status == 200
    assert await resp.text() == f"inhouse-probe {NONCE} {'ready' if ready else 'off'} {mode} -1"
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert svc.calls == 0 and await count_rows(db, "payments") == 0


async def test_probe_reports_round_age(client, monkeypatch):
    clock = types.SimpleNamespace(now=lambda: 1000.0)
    monkeypatch.setattr(watch, "clock", clock)
    watch.WATCH.data["a"]["last_round"] = 958.0
    resp = await post_raw(client, b"probe=" + b"a" * 32)
    assert await resp.text() == "inhouse-probe " + "a" * 32 + " off links 42"


@pytest.mark.parametrize(
    "body",
    [
        b"probe=0123456789ABCDEF",  # upper case
        b"probe=0123456789abcde",  # 15
        b"probe=" + b"a" * 33,  # 33
        b"probe=0123456789abcdef&t=1",  # extra field
        b"t=1&probe=0123456789abcdef",
        b"probe=0123456789abcdef&probe=0123456789abcdef",
        b"probe=0123456789abcdef\n",
        b"probe=0123456789abcdef" + b"&x=" + b"a" * 2100,  # over 2 KB
    ],
    ids=["upper", "short", "long", "extra", "extra-first", "twice", "newline", "big"],
)
async def test_not_a_probe_goes_normal_branch(client, db, body, logs):
    svc = FakeStockService()
    activate(make_deps(db, svc))
    resp = await post_raw(client, body)
    assert resp.status in (400, 410)
    assert "inhouse-probe" not in await resp.text()
    assert svc.calls == 0 and await count_rows(db, "payments") == 0


async def test_probe_wrong_content_type_goes_normal_branch(client, db):
    activate(make_deps(db, FakeStockService()))
    resp = await post_raw(client, f"probe={NONCE}".encode(), ctype="text/plain")
    assert resp.status == 400 and "inhouse-probe" not in await resp.text()


async def test_probe_not_logged_but_counted(client, logs):
    for _ in range(3):
        await post_raw(client, f"probe={NONCE}".encode())
    assert logs.records == []
    assert watch.WATCH.probe_count == 3


async def test_normal_label_still_works_after_fast_path(client, db, yk_api):
    """The fast path reads the body; the normal branch must still see the same form."""
    svc = FakeStockService()
    activate(make_deps(db, svc))
    sdk_patch.install(Payment)
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    resp = await post_raw(client, f"t={token.issue(SECRET, TG, 'card', 1800)}".encode())
    assert resp.status == 303 and yk_api.bodies[0]["payment_method_data"] == {"type": "bank_card"}


# --- observer signals (4.3) ----------------------------------------------------------------


def signed(key, seq, ts, st="ok", reason="ok", observer="br-tw-ru1"):
    msg = f"observer={observer}&seq={seq}&ts={ts}&state={st}&reason={reason}"
    return f"{msg}&sig={hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()}".encode()


async def test_signal_ack_and_refusals_go_invalid(client, db, monkeypatch):
    import time

    svc = FakeStockService()
    activate(make_deps(db, svc))
    w = watch.WATCH
    w.start(None)
    key = yk_settings.OBSERVER_SECRET_FILE.read_text(encoding="ascii").strip().encode()
    now = int(time.time())
    resp = await post_raw(client, signed(key, 100, now))
    assert resp.status == 200 and await resp.text() == "observer-ack 100"
    for body in (signed(key, 100, now), signed(key, 101, now - 400), signed(b"wrong", 102, now)):
        resp = await post_raw(client, body)
        assert resp.status == 400 and "Ссылка недействительна" in await resp.text()
    assert svc.calls == 0 and await count_rows(db, "payments") == 0


async def test_signal_refused_when_watch_never_started(client):
    resp = await post_raw(client, signed(b"x" * 64, 1, 1))
    assert resp.status == 503  # feature off -> the normal "unavailable" page, no ack


# --- menu mode (3.1) -----------------------------------------------------------------------


def test_fallback_providers_patch_is_empty():
    activate(make_deps(None, None))
    assert providers.providers_patch(base_providers(), {"YOOKASSA": True})
    menu_mode.set_mode(menu_mode.FALLBACK)
    assert providers.providers_patch(base_providers(), {"YOOKASSA": True}) == {}


def test_fallback_strips_sbp_card_and_keeps_stock_button():
    activate(make_deps(None, None))
    menu_mode.set_mode(menu_mode.FALLBACK)
    markup = purchase_markup()
    stock_before = flat(markup)[2]
    method = SendMessage(chat_id=TG, text="Не хватает 199 ₽", reply_markup=markup)
    assert link_buttons.LinkButtonsMiddleware.apply(method) is True
    buttons = flat(method.reply_markup)
    assert [b.callback_data for b in buttons] == [STOCK_CB, "fastflow_coupon", "profile"]
    assert buttons[0].text == stock_before.text == "YOOKASSA" and buttons[0].url is None
    assert len(method.reply_markup.inline_keyboard) == 3  # emptied rows dropped
    # a menu built in fallback (stock button only) is left untouched
    only_stock = SendMessage(chat_id=TG, text="x", reply_markup=purchase_markup((STOCK_CB,)))
    before = only_stock.reply_markup
    assert link_buttons.LinkButtonsMiddleware.apply(only_stock) is False
    assert only_stock.reply_markup is before


def test_back_to_links_rewrites_again():
    activate(make_deps(None, None))
    menu_mode.set_mode(menu_mode.FALLBACK)
    menu_mode.set_mode(menu_mode.LINKS)
    method = SendMessage(chat_id=TG, text="x", reply_markup=purchase_markup())
    assert link_buttons.LinkButtonsMiddleware.apply(method) is True
    assert flat(method.reply_markup)[0].url.startswith("https://pay.omenso.net/p/")


@pytest.fixture
def started(monkeypatch, tmp_path, db):
    cfg = types.SimpleNamespace(
        YOOKASSA_SBP_ONLY=False, USE_NEW_PAYMENT_FLOW=["YOOKASSA"], REDIRECT_LINK="https://t.me/example_bot"
    )
    buttons = types.SimpleNamespace(YOOKASSA="💳 ЮКасса: быстрая оплата")
    svc = FakeStockService(delay=0.2)
    monkeypatch.setattr(compat, "load_config", lambda: cfg)
    monkeypatch.setattr(compat, "load_buttons", lambda: buttons)
    monkeypatch.setattr(compat, "load_create_payment", lambda: svc)
    monkeypatch.setattr(compat, "load_yookassa_payment", lambda: Payment)
    monkeypatch.setattr(compat, "session_maker", lambda: db)
    monkeypatch.setattr(yk_settings, "SECRET_FILE", tmp_path / "storage" / "yk.secret")
    monkeypatch.setattr(module_settings, "DISABLE_FILE", tmp_path / ".disabled")
    monkeypatch.setitem(module_settings.FEATURES, "yookassa_methods", True)
    bot = Bot(token="42:TEST", session=RecordingSession())
    yield types.SimpleNamespace(cfg=cfg, buttons=buttons, svc=svc, bot=bot)
    feature.stop()


async def test_switch_keeps_handler_wrapper_and_in_flight_method(started, client, db, yk_api, monkeypatch):
    assert await feature.start(started.bot)
    monkeypatch.setattr(yk_settings, "WATCH_ALERT_CHAT_IDS", (1,))
    w = watch.WATCH
    w.start(None)
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    secret = state.STATE.deps.secret  # the feature's own label secret
    opening = asyncio.create_task(post_raw(client, f"t={token.issue(secret, TG, 'sbp', 1800)}".encode()))
    await asyncio.sleep(0.05)  # the stock service is now inside its 0.2 s delay
    assert await w.switch(menu_mode.FALLBACK) is True
    resp = await opening
    assert resp.status == 303
    assert yk_api.bodies[0]["payment_method_data"] == {"type": "sbp"}  # chosen method kept
    # nothing was uninstalled by the switch
    assert sdk_patch.is_installed() and state.is_active()
    assert "YOOKASSA_SBP" in started.cfg.USE_NEW_PAYMENT_FLOW and started.buttons.YOOKASSA_SBP == "🏦 СБП"
    assert any(isinstance(m, link_buttons.LinkButtonsMiddleware) for m in started.bot.session.middleware)
    # old links keep working in fallback
    await save_checkout(db, TG, {"required_amount": 299}, past(0.5))
    resp = await post_raw(client, f"t={token.issue(secret, TG, 'card', 1800)}".encode())
    assert resp.status == 303


async def test_saved_fallback_read_before_first_menu(started):
    path = yk_settings.WATCH_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data = watch.fresh()
    data.update(mode="fallback", incident={"id": "x", "since": 1.0, "source": "a", "reason": "server"})
    path.write_text(json.dumps(data), encoding="utf-8")
    assert await feature.start(started.bot)
    assert menu_mode.is_fallback()
    await started.bot(SendMessage(chat_id=TG, text="Не хватает 199 ₽", reply_markup=purchase_markup()))
    keyboard = json.loads(started.bot.session.sent[0][1]["reply_markup"])["inline_keyboard"]
    callbacks = [b.get("callback_data") for row in keyboard for b in row]
    assert callbacks == [STOCK_CB, "fastflow_coupon", "profile"]
    assert all("url" not in b for row in keyboard for b in row)


async def test_damaged_state_file_does_not_block_start(started, logs):
    path = yk_settings.WATCH_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00garbage")
    assert await feature.start(started.bot)
    assert menu_mode.current() == menu_mode.LINKS and "damaged" in logs.text()
