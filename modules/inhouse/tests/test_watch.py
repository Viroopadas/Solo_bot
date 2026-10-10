"""Pay-site watch: states, transitions, messages, delivery, watch.json, dates, supervision.

Spec 2026-10-10 "pay site watch" edition 2, sections 3.3-3.10 and 9. Fakes only: clock,
HTTP requests of a round, Telegram bot, TLS/RDAP/whois. No servers, no network.
Independent expectations: main project .ai-engine/decisions/2026-10-10-pay-watch-verification-invariants.md.
"""

import asyncio
import json
import os
import socket
import ssl
import types

import pytest

from modules.inhouse import settings as module_settings
from modules.inhouse.features.yookassa_methods import (
    compat,
    expiry,
    menu_mode,
    settings as yk_settings,
    state,
    watch,
)
from modules.inhouse.tests.helpers import activate, make_deps


ADMIN = 111


class FakeClock:
    def __init__(self, t=1_800_000_000.0) -> None:
        self.t = t
        self.sleeps = []

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)


class FakeBot:
    def __init__(self) -> None:
        self.sent = []
        self.fail = []  # exceptions raised by the next calls, in order

    async def send_message(self, chat_id, text, parse_mode="HTML", link_preview_options=None, **kwargs):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
        return types.SimpleNamespace(message_id=len(self.sent))


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(watch, "clock", c)
    return c


@pytest.fixture
def env(monkeypatch, tmp_path, clock):
    monkeypatch.setattr(module_settings, "DISABLE_FILE", tmp_path / ".disabled")
    monkeypatch.setitem(module_settings.FEATURES, "yookassa_methods", True)
    monkeypatch.setattr(yk_settings, "WATCH_ALERT_CHAT_IDS", (ADMIN,))
    monkeypatch.setattr(yk_settings, "WATCH_JITTER_SECONDS", 0)
    activate(make_deps(None, None))
    bot = FakeBot()
    w = watch.WATCH
    w.start(bot)
    bot.sent.clear()
    w.data["queue"].clear()  # the first-start greeting is tested separately
    w.save()
    return types.SimpleNamespace(w=w, bot=bot, clock=clock, tmp=tmp_path)


def texts(bot):
    return [m["text"] for m in bot.sent]


async def fail_rounds(env, n, code="server"):
    for _ in range(n):
        env.w.apply_round(False, code)
        await env.w.evaluate()
        env.clock.t += 30


async def ok_rounds(env, n):
    for _ in range(n):
        env.w.apply_round(True, "")
        await env.w.evaluate()
        env.clock.t += 120


def signal_body(key, seq, ts, st="down", reason="server", observer="br-tw-ru1", sig=None):
    import hashlib
    import hmac

    msg = f"observer={observer}&seq={seq}&ts={ts}&state={st}&reason={reason}"
    sig = sig or hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}&sig={sig}".encode()


# --- round (3.3) ---------------------------------------------------------------------------


def page(label):
    return (f'<form><input type="hidden" id="t" name="t" value="{label}"></form>'
            '<script src="/static/pay.js"></script>').encode()


def requester(get=None, post=None):
    calls = []

    async def request(method, url, data=None):
        calls.append((method, url, data))
        handler = get if method == "GET" else post
        result = handler(url, data)
        if isinstance(result, BaseException):
            raise result
        return result

    request.calls = calls
    return request


async def test_round_success_exact_steps():
    nonce = "0123456789abcdef"
    req = requester(
        get=lambda url, data: (200, page(f"/p/probe-{nonce}")),
        post=lambda url, data: (200, f"inhouse-probe {nonce} ready links 12".encode()),
    )
    assert await watch.run_round(req, nonce) == (True, "")
    assert req.calls[0] == ("GET", f"https://pay.omenso.net/p/probe-{nonce}", None)
    assert req.calls[1] == ("POST", "https://pay.omenso.net/inhouse/pay", f"probe={nonce}".encode())


@pytest.mark.parametrize(
    "get,post,code",
    [
        (socket.gaierror(-2, "Name or service not known"), None, "dns"),
        (ssl.SSLCertVerificationError("bad cert"), None, "cert"),
        (TimeoutError(), None, "server"),
        (ConnectionRefusedError(), None, "server"),
        ((200, b"<html>no template</html>"), None, "page"),
        ((404, b""), None, "page"),
        ("ok", (502, b"Bad Gateway"), "bridge"),
        ("ok", (200, "<p>Оплата временно недоступна.</p><link href=\"/static/pay.css\">".encode()), "bridge"),
        ("ok", (200, b"inhouse-probe ffffffffffffffff ready links 1"), "bot"),
        ("ok", (400, "Ссылка недействительна".encode()), "bot"),
    ],
    ids=["dns", "cert", "timeout", "refused", "no-template", "404", "502", "ru-unavailable", "wrong-nonce", "invalid"],
)
async def test_round_failure_reasons(get, post, code):
    nonce = "0123456789abcdef"
    good_page = (200, page(f"/p/probe-{nonce}"))
    req = requester(get=lambda u, d: good_page if get == "ok" else get, post=lambda u, d: post)
    assert await watch.run_round(req, nonce) == (False, code)
    assert watch.reason_text(code)  # every code has a plain-words phrase
    assert "pay.omenso.net" in watch.reason_text("dns")


async def test_round_through_do_round_and_hourly_static(env, monkeypatch):
    import hashlib

    js = (watch.DEPLOY_STATIC / "pay.js").read_bytes()

    def get(url, data):
        if url.endswith("/static/pay.js"):
            return 200, js
        if url.endswith("/static/pay.css"):
            return 200, b"old css"
        return 200, page(url.split("pay.omenso.net", 1)[1])

    def post(url, data):
        nonce = data.decode().split("=", 1)[1]
        return 200, f"inhouse-probe {nonce} ready links -1".encode()

    req = requester(get=get, post=post)

    async def make(timeout):
        assert timeout == 10
        return req, _noop

    monkeypatch.setattr(watch, "make_requester", make)
    env.w.data["daily_last"] = env.clock.t
    await env.w.do_round()
    await env.w.maintenance()
    assert env.w.data["a"]["fails"] == 0 and env.w.data["a"]["oks"] == 1
    assert sum(1 for c in req.calls if "/static/" in c[1]) == 2
    queued = [q["text"] for q in env.w.data["queue"]]
    assert len(queued) == 1 and "другая версия страницы оплаты" in queued[0] and "pay.css" in queued[0]
    assert hashlib.sha256(js).hexdigest()  # pay.js matched: not named
    assert "pay.js" not in queued[0]
    env.clock.t += 130
    await env.w.do_round()  # not again within the hour
    await env.w.maintenance()
    assert sum(1 for c in req.calls if "/static/" in c[1]) == 2


async def _noop():
    return None


async def test_daily_check_cannot_delay_outage_alert(env, monkeypatch):
    """Architecture T2, 3.4/3.5: alert path must not wait for certificate/whois checks."""
    env.w.data["a"].update(state=watch.SUSPECT, fails=2)
    env.w.next_static_at = env.clock.t + 3600

    async def request(method, url, data=None):
        if method == "GET":
            return 200, page(url.split("pay.omenso.net", 1)[1])
        return 502, b"Bad Gateway"

    async def make(timeout):
        return request, _noop

    async def blocked_daily():
        await asyncio.Event().wait()

    monkeypatch.setattr(watch, "make_requester", make)
    monkeypatch.setattr(env.w, "daily", blocked_daily)
    try:
        await asyncio.wait_for(env.w.tick(), 0.3)
        assert menu_mode.current() == menu_mode.FALLBACK
        assert len(env.bot.sent) == 1 and env.bot.sent[0]["text"].startswith("🔴")
    finally:
        task = getattr(env.w, "_maintenance_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_owner_disable_during_round_prevents_alert_and_switch(env, monkeypatch):
    """Architecture T9 and 3.1: owner's disable is rechecked after network awaits."""
    env.w.data["a"].update(state=watch.SUSPECT, fails=2)
    env.w.next_static_at = env.clock.t + 3600
    env.w.data["daily_last"] = env.clock.t

    async def request(method, url, data=None):
        if method == "GET":
            return 200, page(url.split("pay.omenso.net", 1)[1])
        monkeypatch.setitem(module_settings.FEATURES, "yookassa_methods", False)
        return 502, b"Bad Gateway"

    async def make(timeout):
        return request, _noop

    monkeypatch.setattr(watch, "make_requester", make)
    await env.w.tick()
    assert menu_mode.current() == menu_mode.LINKS
    assert env.w.data["incident"] is None and env.bot.sent == []


async def test_alert_deadline_with_slow_rounds_and_maintenance(env, monkeypatch):
    """Architecture T2: measured through tick/do_round/evaluate/deliver, maximum 300 s."""
    started = env.clock.t
    env.w.next_round_at = started + 130  # maximum healthy interval including jitter
    env.w.next_static_at = started + 3600

    async def request(method, url, data=None):
        env.clock.t += 10  # each request consumes the full configured budget
        if method == "GET":
            return 200, page(url.split("pay.omenso.net", 1)[1])
        return 502, b"Bad Gateway"

    async def make(timeout):
        return request, _noop

    async def blocked_daily():
        await asyncio.Event().wait()

    monkeypatch.setattr(watch, "make_requester", make)
    monkeypatch.setattr(env.w, "daily", blocked_daily)
    try:
        for _ in range(61):
            await env.w.tick()
            if env.bot.sent:
                break
            env.clock.t += 5
            await asyncio.sleep(0)
        assert env.bot.sent and env.clock.t - started <= 300
        assert menu_mode.current() == menu_mode.FALLBACK
    finally:
        await env.w.stop_maintenance()
        assert env.w._maintenance_task is None


async def test_delivery_budget_preserves_queue_and_cancels_send(env, monkeypatch):
    """Architecture 3.4/3.5: bounded delivery, retained message after timeout."""
    cancelled = asyncio.Event()

    async def send(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(env.bot, "send_message", send)
    monkeypatch.setattr(watch, "DELIVERY_TIMEOUT", 0.03)
    env.w.enqueue("warn", "test")
    await asyncio.wait_for(env.w.deliver(), 0.3)
    assert cancelled.is_set()
    assert len(env.w.data["queue"]) == 1
    assert env.w.data["queue"][0]["next_at"] == env.clock.t + 60


async def test_run_cancellation_stops_maintenance(env, monkeypatch):
    """Architecture 3.9: all watch-owned tasks finish at shutdown."""
    started, stopped = asyncio.Event(), asyncio.Event()
    env.w.next_round_at = env.clock.t + 3600
    env.w.next_static_at = env.clock.t + 3600

    async def daily():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(env.w, "daily", daily)
    task = asyncio.create_task(watch.run(env.bot))
    await asyncio.wait_for(started.wait(), 0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 0.3)
    assert task.done() and stopped.is_set() and env.w._maintenance_task is None


# --- states and transitions (3.4) ----------------------------------------------------------


async def test_three_failures_switch_to_fallback_with_one_message(env):
    await fail_rounds(env, 2)
    assert env.w.data["a"]["state"] == watch.SUSPECT and menu_mode.current() == menu_mode.LINKS
    await fail_rounds(env, 1, "bridge")
    assert env.w.data["a"]["state"] == watch.DOWN
    assert menu_mode.current() == menu_mode.FALLBACK
    await env.w.deliver()
    assert texts(env.bot) == [
        ("🔴 Оплата через pay.omenso.net не работает: переходник не достучался до бота. "
        "Включил запасную кнопку ЮKassa — новые покупки идут по-старому.")
    ]
    assert env.bot.sent[0]["parse_mode"] is None and env.bot.sent[0]["chat_id"] == ADMIN
    await fail_rounds(env, 3)  # still broken: no new message
    await env.w.deliver()
    assert len(env.bot.sent) == 1


async def test_pauses_follow_state(env):
    env.w.apply_round(True, "")
    assert env.w.next_round_at - env.clock.t == 120
    env.w.apply_round(False, "server")
    assert env.w.next_round_at - env.clock.t == 30
    env.w.apply_round(False, "server")
    env.w.apply_round(False, "server")
    assert env.w.data["a"]["state"] == watch.DOWN and env.w.next_round_at - env.clock.t == 120


async def test_recovery_needs_three_successes_and_ten_minutes(env):
    await fail_rounds(env, 3)
    await env.w.deliver()
    start = env.w.data["incident"]["since"]
    await ok_rounds(env, 2)
    assert menu_mode.is_fallback()
    env.w.apply_round(True, "")
    assert env.w.data["a"]["state"] == watch.OK
    await env.w.evaluate()
    assert menu_mode.is_fallback(), "less than 10 minutes in fallback"
    env.clock.t = start + 10 * 60
    await env.w.evaluate()
    assert menu_mode.current() == menu_mode.LINKS
    await env.w.deliver()
    assert texts(env.bot)[-1] == "🟢 pay.omenso.net снова работает (простой 10 мин). Вернул кнопки СБП и карты."
    assert len(env.bot.sent) == 2


async def test_flapping_holds_fallback_and_says_so_once(env):
    for episode in range(3):
        await fail_rounds(env, 3)
        await ok_rounds(env, 3)
        env.clock.t += 10 * 60
        await env.w.evaluate()
        if episode < 2:
            assert menu_mode.current() == menu_mode.LINKS
    assert menu_mode.is_fallback(), "third entry within the hour: held"
    hold = env.w.data["hold_until"]
    assert hold - env.w.data["incident"]["since"] == 60 * 60
    await env.w.deliver()
    unstable = [t for t in texts(env.bot) if "нестабильна" in t]
    assert len(unstable) == 1 and "не меньше 60 мин" in unstable[0]
    env.clock.t = hold - 1
    await env.w.evaluate()
    assert menu_mode.is_fallback()
    env.clock.t = hold
    await env.w.evaluate()
    assert menu_mode.current() == menu_mode.LINKS


async def test_observer_down_switches_to_fallback(env):
    ack = env.w.accept_signal(signal_body(env.w.observer_key, 5, int(env.clock.t), "down", "dns"))
    assert ack == "observer-ack 5"
    assert env.w.b_state(env.clock.t) == watch.DOWN
    await env.w.evaluate()
    assert menu_mode.is_fallback()
    await env.w.deliver()
    assert texts(env.bot) == [
        ("🔴 Из сети Timeweb в России не открывается pay.omenso.net: имя pay.omenso.net не находится. "
        "Включил запасную кнопку ЮKassa — новые покупки идут по-старому.")
    ]
    # back: A OK, B ok, 10 minutes
    env.clock.t += 10 * 60
    env.w.accept_signal(signal_body(env.w.observer_key, 6, int(env.clock.t), "ok", "ok"))
    await env.w.evaluate()
    assert menu_mode.current() == menu_mode.LINKS


async def test_observer_silence_unknown_then_warning(env):
    env.w.accept_signal(signal_body(env.w.observer_key, 1, int(env.clock.t), "ok", "ok"))
    env.clock.t += 6 * 60
    assert env.w.b_state(env.clock.t) == watch.UNKNOWN
    env.w.check_observer_silence(env.clock.t)
    assert not env.w.data["queue"]
    env.clock.t += 9 * 60
    env.w.check_observer_silence(env.clock.t)
    env.w.check_observer_silence(env.clock.t + 60)
    assert [q["text"] for q in env.w.data["queue"]] == [
        "⚠️ Проверка оплаты из России молчит уже 15 мин — сторож в боте работает, мост проверьте."
    ]
    env.clock.t += 6 * 3600
    env.w.check_observer_silence(env.clock.t)
    assert len(env.w.data["queue"]) == 2  # repeated after 6 hours


async def test_observer_never_installed_no_silence_warning(env):
    env.clock.t += 24 * 3600
    env.w.check_observer_silence(env.clock.t)
    assert env.w.b_state(env.clock.t) == watch.UNKNOWN and not env.w.data["queue"]


async def test_owner_turned_off_means_silence(env, monkeypatch):
    calls = []

    async def make(timeout):
        calls.append(1)
        return requester(), _noop

    monkeypatch.setattr(watch, "make_requester", make)
    env.w.data["a"].update(state=watch.DOWN, fails=3, reason="server")
    env.w.data["daily_last"] = env.clock.t
    for off in ("features", "file", "admin"):
        if off == "features":
            monkeypatch.setitem(module_settings.FEATURES, "yookassa_methods", False)
        elif off == "file":
            monkeypatch.setitem(module_settings.FEATURES, "yookassa_methods", True)
            module_settings.DISABLE_FILE.touch()
        else:
            module_settings.DISABLE_FILE.unlink()
            monkeypatch.setattr(state, "module_enabled", lambda: False)
        await env.w.tick()
        assert calls == [] and menu_mode.current() == menu_mode.LINKS
        assert env.bot.sent == [] and env.w.data["incident"] is None
    # the switch itself also re-checks the owner's permission
    assert await env.w.switch(menu_mode.FALLBACK) is False and menu_mode.current() == menu_mode.LINKS


async def test_enabled_but_not_started_warns_after_ten_minutes(env):
    state.STATE.active = False
    state.STATE.deps = None
    state.STATE.reason = "yookassa service: not importable"
    env.w.data["daily_last"] = env.clock.t
    await env.w.tick()
    env.clock.t += 9 * 60
    await env.w.tick()
    assert env.bot.sent == []
    env.clock.t += 60
    await env.w.tick()
    assert texts(env.bot) == [
        ("⚠️ Кнопки СБП и карты не запустились: yookassa service: not importable. "
        "Покупки идут через обычную кнопку ЮKassa.")
    ]
    env.clock.t += 3600
    await env.w.tick()
    assert len(env.bot.sent) == 1  # next one only after 6 hours
    env.clock.t += 6 * 3600
    await env.w.tick()
    assert len(env.bot.sent) == 2


async def test_reminders_first_after_hour_then_every_six(env):
    await fail_rounds(env, 3)
    since = env.w.data["incident"]["since"]
    env.clock.t = since + 59 * 60
    await env.w.evaluate()
    assert [q["kind"] for q in env.w.data["queue"]] == ["down"]
    env.clock.t = since + 60 * 60
    await env.w.evaluate()
    assert [q["kind"] for q in env.w.data["queue"]] == ["down", "remind"]
    assert "всё ещё не работает (60 мин)" in env.w.data["queue"][-1]["text"]
    env.clock.t += 5 * 3600
    await env.w.evaluate()
    assert len(env.w.data["queue"]) == 2
    env.clock.t += 3600
    await env.w.evaluate()
    assert len(env.w.data["queue"]) == 3


# --- passive counter (3.6) -----------------------------------------------------------------


async def test_three_create_errors_without_success_warn_once(env):
    env.w.note_outcome("error", "RuntimeError")
    env.w.note_outcome("stale", "")
    env.w.note_outcome("error", "stock function returned None")
    assert not env.w.data["queue"]
    env.w.note_outcome("error", "RuntimeError")
    assert [q["text"] for q in env.w.data["queue"]] == [
        "⚠️ Не получается создать оплату в ЮKassa: ошибка при обращении к ЮKassa (RuntimeError)."
    ]
    env.w.note_outcome("error", "RuntimeError")
    assert len(env.w.data["queue"]) == 1  # once a day
    assert menu_mode.current() == menu_mode.LINKS  # mode unchanged


async def test_errors_with_a_success_do_not_warn(env):
    for kind in ("error", "created", "error", "error"):
        env.w.note_outcome(kind, "RuntimeError")
    assert not env.w.data["queue"]
    env.clock.t += 11 * 60  # the success left the window
    for _ in range(3):
        env.w.note_outcome("error", "RuntimeError")
    assert len(env.w.data["queue"]) == 1


# --- delivery (3.5) ------------------------------------------------------------------------


async def test_telegram_error_is_retried_on_schedule(env):
    env.bot.fail = [RuntimeError("network")] * 5
    env.w.enqueue("warn", "⚠️ test")
    steps = []
    for _ in range(5):
        await env.w.deliver()
        item = env.w.data["queue"][0]
        steps.append(item["next_at"] - env.clock.t)
        env.clock.t = item["next_at"]
    assert steps == [60, 120, 300, 600, 600]
    await env.w.deliver()
    assert texts(env.bot) == ["⚠️ test"] and env.w.data["queue"] == []


async def test_telegram_429_waits_given_time(env):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    env.bot.fail = [TelegramRetryAfter(SendMessage(chat_id=1, text="x"), "Too Many Requests", 37)]
    env.w.enqueue("warn", "one")
    env.w.enqueue("warn", "two")
    await env.w.deliver()
    assert env.bot.sent == []
    assert all(q["next_at"] == env.clock.t + 37 for q in env.w.data["queue"])
    env.clock.t += 36
    await env.w.deliver()
    assert env.bot.sent == []
    env.clock.t += 1
    await env.w.deliver()
    assert texts(env.bot) == ["one", "two"]


async def test_unsent_messages_survive_restart(env):
    env.bot.fail = [RuntimeError("down")]
    await fail_rounds(env, 3)
    await env.w.deliver()
    assert env.bot.sent == [] and len(env.w.data["queue"]) == 1
    # bot restarts: a new watch reads watch.json and sends what is left
    new = watch.reset()
    menu_mode.set_mode(menu_mode.LINKS)
    assert new.restore_mode() == menu_mode.FALLBACK  # first menu after restart already right
    bot2 = FakeBot()
    new.start(bot2)
    env.clock.t += 60
    await new.deliver()
    assert len(bot2.sent) == 1 and bot2.sent[0]["text"].startswith("🔴 Оплата через pay.omenso.net не работает")


async def test_fixed_before_broken_was_sent_gives_one_summary(env):
    env.bot.fail = [RuntimeError("telegram down")]
    await fail_rounds(env, 3, "dns")
    await env.w.deliver()  # failed, retry in 60 s
    await ok_rounds(env, 3)
    env.clock.t = env.w.data["incident"]["since"] + 12 * 60
    await env.w.evaluate()
    assert menu_mode.current() == menu_mode.LINKS
    assert [q["kind"] for q in env.w.data["queue"]] == ["summary"]
    await env.w.deliver()
    assert texts(env.bot) == [
        "🟡 Оплата через pay.omenso.net была недоступна 12 мин (имя pay.omenso.net не находится), уже работает."
    ]


async def test_partial_delivery_merges_per_recipient_and_keeps_retry_after(env, monkeypatch):
    """Architecture 3.5: each recipient gets the recovery/summary; 429 remains binding."""
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    monkeypatch.setattr(yk_settings, "WATCH_ALERT_CHAT_IDS", (ADMIN, ADMIN + 1))
    await fail_rounds(env, 3)
    first = env.bot.send_message

    async def send(**kwargs):
        if kwargs["chat_id"] == ADMIN + 1:
            raise TelegramRetryAfter(SendMessage(chat_id=ADMIN + 1, text="x"), "limit", 3600)
        return await first(**kwargs)

    monkeypatch.setattr(env.bot, "send_message", send)
    await env.w.deliver()
    assert len(env.bot.sent) == 1 and env.bot.sent[0]["chat_id"] == ADMIN
    due = env.w.data["queue"][0]["next_at"]
    await ok_rounds(env, 3)
    env.clock.t = env.w.data["incident"]["since"] + 12 * 60
    await env.w.evaluate()
    assert {q["kind"] for q in env.w.data["queue"]} == {"summary", "up"}
    summary = next(q for q in env.w.data["queue"] if q["kind"] == "summary")
    assert summary["chats"] == [ADMIN + 1] and summary["next_at"] == due
    monkeypatch.setattr(env.bot, "send_message", first)
    await env.w.deliver()
    assert len(env.bot.sent) == 1  # recovery cannot bypass the token-wide 429 delay
    env.clock.t = due
    await env.w.deliver()
    second_admin = [m["text"] for m in env.bot.sent if m["chat_id"] == ADMIN + 1]
    assert len(second_admin) == 1 and "была недоступна" in second_admin[0]
    assert not env.w.data["queue"]


def test_unconfirmed_messages_not_discarded_after_fifty(env):
    """Architecture T4/3.5: undelivered messages must survive a long Telegram outage."""
    for n in range(51):
        env.w.enqueue("warn", f"warning {n}")
    saved = json.loads(yk_settings.WATCH_STATE_FILE.read_text(encoding="utf-8"))
    assert len(saved["queue"]) == 51 and saved["queue"][0]["text"] == "warning 0"


@pytest.mark.parametrize("body,expected", [
    (b"probe=0123456789abcdef", 200),
    (b"probe=0123456789abcdef" + b"x" * 2048, 400),
    (b"t=1.123.s.1.1800.forged", 400),
], ids=["valid", "oversized", "normal-label"])
async def test_streamed_body_is_bounded_without_starting_server(env, body, expected):
    """Architecture 3.2: body limit before parsing, including HTTP chunked transport."""
    from unittest.mock import AsyncMock, Mock
    from aiohttp.test_utils import make_mocked_request
    from modules.inhouse.features.yookassa_methods import pay_endpoint

    payload = Mock()
    chunks = [body, b""]
    if len(body) > 2048:
        from modules.inhouse.features.yookassa_methods import token
        # An oversized prefix must not let a valid signed label in a later chunk reach payment code.
        tail = f"t={token.issue(state.STATE.deps.secret, ADMIN, 'sbp', 1800)}".encode()
        chunks = [body, tail, b""]
    payload.readany = AsyncMock(side_effect=chunks)
    request = make_mocked_request(
        "POST", "/inhouse/pay", headers={"Content-Type": "application/x-www-form-urlencoded"}, payload=payload,
    )
    assert request.content_length is None
    response = await pay_endpoint.handle(request)
    assert response.status == expected
    if expected == 200:
        assert response.text == "inhouse-probe 0123456789abcdef ready links -1"


async def test_no_recipients_logs_and_drops(env, monkeypatch, logs):
    monkeypatch.setattr(yk_settings, "WATCH_ALERT_CHAT_IDS", ())
    monkeypatch.setattr(compat, "load_config", lambda: types.SimpleNamespace())
    env.w.enqueue("warn", "x")
    assert env.w.data["queue"] == [] and "no recipients" in logs.text()


def test_recipients_from_admin_id_number_or_list(monkeypatch):
    w = watch.WATCH
    monkeypatch.setattr(yk_settings, "WATCH_ALERT_CHAT_IDS", ())
    monkeypatch.setattr(compat, "load_config", lambda: types.SimpleNamespace(ADMIN_ID=42))
    assert w.recipients() == [42]
    monkeypatch.setattr(compat, "load_config", lambda: types.SimpleNamespace(ADMIN_ID=[42, "43", True, 42]))
    assert w.recipients() == [42, 43]
    monkeypatch.setattr(compat, "load_config", lambda: types.SimpleNamespace(ADMIN_IDS=(7,)))
    assert w.recipients() == [7]
    monkeypatch.setattr(yk_settings, "WATCH_ALERT_CHAT_IDS", (5, 6))
    assert w.recipients() == [5, 6]


# --- AUTO_FALLBACK / WATCH_ENABLED (3.8) ---------------------------------------------------


async def test_auto_fallback_off_messages_only(env, monkeypatch):
    monkeypatch.setattr(yk_settings, "AUTO_FALLBACK", False)
    await fail_rounds(env, 3)
    assert menu_mode.current() == menu_mode.LINKS
    await env.w.deliver()
    await ok_rounds(env, 3)
    env.clock.t += 10 * 60
    await env.w.evaluate()
    await env.w.deliver()
    assert texts(env.bot) == [
        "🔴 Оплата через pay.omenso.net не работает: сервер переходника не отвечает.",
        "🟢 pay.omenso.net снова работает (простой 16 мин).",
    ]


def test_auto_fallback_off_drops_saved_fallback(monkeypatch, logs):
    path = yk_settings.WATCH_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data = watch.fresh()
    data.update(mode="fallback", incident={"id": "x", "since": 1.0, "source": "a", "reason": "server"})
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(yk_settings, "AUTO_FALLBACK", False)
    assert watch.WATCH.restore_mode() == menu_mode.LINKS
    assert json.loads(path.read_text(encoding="utf-8"))["mode"] == "links"
    assert "saved fallback mode dropped" in logs.text()


async def test_watch_disabled_links_and_no_file(monkeypatch, logs):
    path = yk_settings.WATCH_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data = watch.fresh()
    data.update(mode="fallback", incident={"id": "x", "since": 1.0, "source": "a", "reason": "server"})
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(yk_settings, "WATCH_ENABLED", False)
    assert watch.WATCH.restore_mode() == menu_mode.LINKS
    await watch.run(FakeBot())  # returns at once
    assert "watch off" in logs.text() and menu_mode.current() == menu_mode.LINKS
    assert path.read_bytes() == before
    assert not yk_settings.OBSERVER_SECRET_FILE.exists()


# --- watch.json (3.8) ----------------------------------------------------------------------


async def test_state_file_written_atomically(env, monkeypatch, logs):
    await fail_rounds(env, 3)
    path = yk_settings.WATCH_STATE_FILE
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema"] == 1 and saved["mode"] == "fallback" and saved["a"]["state"] == "DOWN"
    assert saved["incident"]["source"] == "a" and len(saved["entries"]) == 1
    assert {p.name for p in path.parent.iterdir()} == {"observer.secret", "watch.json"}
    before = path.read_bytes()

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(watch.os, "replace", broken_replace)
    await ok_rounds(env, 3)
    env.clock.t += 600
    await env.w.evaluate()  # switching and queueing still happen
    assert menu_mode.current() == menu_mode.LINKS and env.w.data["queue"]
    assert path.read_bytes() == before  # old file intact, no half-written file
    assert {p.name for p in path.parent.iterdir()} == {"observer.secret", "watch.json"}
    assert len([m for m in logs.at("error") if "state file not written" in m]) == 1


def test_damaged_file_means_links_and_a_warning(monkeypatch, logs):
    monkeypatch.setattr(yk_settings, "WATCH_ALERT_CHAT_IDS", (ADMIN,))
    path = yk_settings.WATCH_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema": 1, "mode": "fallb', encoding="utf-8")
    w = watch.WATCH
    assert w.restore_mode() == menu_mode.LINKS
    w.start(FakeBot())
    assert "damaged" in logs.text()
    assert [q["text"] for q in w.data["queue"]] == [
        "⚠️ Файл состояния сторожа оплаты был повреждён — начал заново, кнопки СБП и карты включены."
    ]
    assert json.loads(path.read_text(encoding="utf-8"))["mode"] == "links"


def test_missing_file_links_and_one_greeting(monkeypatch, logs):
    monkeypatch.setattr(yk_settings, "WATCH_ALERT_CHAT_IDS", (ADMIN,))
    w = watch.WATCH
    assert w.restore_mode() == menu_mode.LINKS
    w.start(FakeBot())
    assert [q["text"] for q in w.data["queue"]] == [
        "🟢 Сторож оплаты pay.omenso.net включён. Напишу сюда, если оплата через него сломается."
    ]
    assert "watch on:" in logs.text()
    secret = yk_settings.OBSERVER_SECRET_FILE
    assert len(bytes.fromhex(secret.read_text(encoding="ascii"))) == 32
    # next start: file exists, no second greeting
    w2 = watch.reset()
    w2.start(FakeBot())
    assert len(w2.data["queue"]) == 1  # the unsent greeting itself, not a new one


def test_wrong_shapes_are_damage():
    for bad in ({"schema": 2}, {"schema": 1, "mode": "x"}, {"schema": 1, "queue": [1]}, {"schema": 1, "a": {"state": "?"}}):
        with pytest.raises(ValueError):
            watch.validate(bad)
    assert watch.validate({"schema": 1, "mode": "fallback"})["mode"] == "links"  # no incident -> links


@pytest.mark.parametrize("field,value", [
    ("a", {"state": "OK", "fails": "broken"}),
    ("observers", {"br-tw-ru1": {"at": "broken"}}),
    ("queue", [{"text": "alert", "chats": [ADMIN]}]),
    ("entries", [{}]),
    ("hold_until", float("nan")),
    ("warned", {"static": "broken"}),
], ids=["a-counter", "observer", "queue", "entries", "nan", "warned"])
def test_nested_damaged_state_is_rejected_before_restore(field, value, logs):
    """Architecture 3.8: damaged state must start fresh, not break the running watch."""
    path = yk_settings.WATCH_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data = watch.fresh()
    data[field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    assert watch.WATCH.restore_mode() == menu_mode.LINKS
    assert watch.WATCH.corrupted and "damaged" in logs.text()


# --- observer signals (4.3) ----------------------------------------------------------------


async def test_signal_rules(env, logs):
    key, now = env.w.observer_key, int(env.clock.t)
    assert env.w.accept_signal(signal_body(key, 10, now, "ok", "ok")) == "observer-ack 10"
    assert "signals accepted" in logs.text()
    assert env.w.accept_signal(signal_body(key, 10, now, "ok", "ok")) is None  # repeated seq
    assert env.w.accept_signal(signal_body(key, 9, now, "ok", "ok")) is None  # older seq
    assert env.w.accept_signal(signal_body(key, 11, now - 301, "ok", "ok")) is None  # old ts
    assert env.w.accept_signal(signal_body(key, 12, now, "ok", "ok", sig="0" * 64)) is None  # bad signature
    assert env.w.accept_signal(signal_body(b"other-secret", 13, now, "ok", "ok")) is None
    assert env.w.accept_signal(signal_body(key, 14, now, "ok", "weird")) is None  # unknown reason
    assert env.w.accept_signal(signal_body(key, 15, now, "ok", "ok") + b"&x=1") is None  # extra field
    assert env.w.accept_signal(signal_body(key, 16, now + 200, "down", "page")) == "observer-ack 16"
    assert env.w.data["observers"]["br-tw-ru1"]["seq"] == 16
    saved = json.loads(yk_settings.WATCH_STATE_FILE.read_text(encoding="utf-8"))
    assert saved["observers"]["br-tw-ru1"]["state"] == "down"


# --- dates (3.7) ---------------------------------------------------------------------------


def test_parsers_on_forged_answers():
    rdap = json.dumps({"events": [{"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"},
                                  {"eventAction": "expiration", "eventDate": "2027-01-15T10:00:00Z"}]})
    assert expiry.parse_rdap_expiry(rdap) == 1800007200.0
    whois = "domain:        POTOK-LUBVI.RU\nstate:         REGISTERED\npaid-till:     2027-01-15T10:00:00Z\n"
    assert expiry.parse_whois_paid_till(whois) == 1800007200.0
    assert expiry.cert_not_after({"notAfter": "Jan 15 10:00:00 2027 GMT"}) == 1800007200.0
    for bad in ("{}", "not json", json.dumps({"events": [{"eventAction": "expiration", "eventDate": "?"}]})):
        with pytest.raises(expiry.ExpiryError):
            expiry.parse_rdap_expiry(bad)
    with pytest.raises(expiry.ExpiryError):
        expiry.parse_whois_paid_till("No entries found")


async def test_cert_days_from_forged_connection(monkeypatch):
    seen = {}

    class Writer:
        def get_extra_info(self, name):
            return {"notAfter": "Jan 15 10:00:00 2027 GMT"} if name == "peercert" else None

        def close(self):
            seen["closed"] = True

        async def wait_closed(self):
            return None

    async def fake_open(host, port, ssl=None, server_hostname=None):
        seen.update(host=host, port=port, hostname=server_hostname, verify=ssl.verify_mode)
        return None, Writer()

    monkeypatch.setattr(expiry, "open_connection", fake_open)
    monkeypatch.setattr(expiry, "now", lambda: 1800007200.0 - 5 * 86400)
    assert await expiry.cert_days("pay.omenso.net") == 5.0
    assert seen == {"host": "pay.omenso.net", "port": 443, "hostname": "pay.omenso.net",
                    "verify": ssl.CERT_REQUIRED, "closed": True}

    async def expired(*a, **k):
        err = ssl.SSLCertVerificationError("certificate has expired")
        err.verify_code = 10
        raise err

    monkeypatch.setattr(expiry, "open_connection", expired)
    assert await expiry.cert_days("pay.omenso.net") < 0


async def test_domain_days_routes_by_zone(monkeypatch):
    queries = []

    class Reader:
        def __init__(self) -> None:
            self.parts = [b"paid-till:     2027-01-15T10:00:00Z\n", b""]

        async def read(self, n):
            return self.parts.pop(0)

    class Writer:
        def write(self, data):
            queries.append(data)

        async def drain(self):
            return None

        def close(self):
            pass

        async def wait_closed(self):
            return None

    async def fake_open(host, port, **kwargs):
        queries.append((host, port))
        return Reader(), Writer()

    async def fake_rdap(url, timeout=10.0):
        queries.append(url)
        return json.dumps({"events": [{"eventAction": "expiration", "eventDate": "2027-01-15T10:00:00Z"}]}).encode()

    monkeypatch.setattr(expiry, "open_connection", fake_open)
    monkeypatch.setattr(expiry, "rdap_json", fake_rdap)
    monkeypatch.setattr(expiry, "now", lambda: 1800007200.0 - 20 * 86400)
    assert await expiry.domain_days("potok-lubvi.ru") == 20.0
    assert await expiry.domain_days("omenso.net") == 20.0
    assert queries == [("whois.tcinet.ru", 43), b"potok-lubvi.ru\r\n",
                       "https://rdap.verisign.com/net/v1/domain/omenso.net"]


async def test_daily_warnings_and_unknown_three_days(env, monkeypatch):
    days = {"pay.omenso.net": 5.0, "store.potok-lubvi.ru": 80.0}

    async def cert(name, timeout):
        return days[name]

    async def domain(name, timeout):
        if name == "potok-lubvi.ru":
            raise expiry.ExpiryError("whois down")
        return 12.0

    monkeypatch.setattr(expiry, "cert_days", cert)
    monkeypatch.setattr(expiry, "domain_days", domain)
    await env.w.daily()
    assert [q["text"] for q in env.w.data["queue"]] == [
        "⚠️ Сертификат pay.omenso.net истекает через 5 дн. — проверьте продление на test-bg-ru1 (Caddy переходника).",
        "⚠️ Домен omenso.net истекает через 12 дн. — продлите.",
    ]
    env.w.data["queue"].clear()
    for _ in range(2):
        env.clock.t += 86400
        await env.w.daily()
    unknown = [q["text"] for q in env.w.data["queue"] if "Не могу" in q["text"]]
    assert unknown == ["⚠️ Не могу проверить срок домена potok-lubvi.ru третьи сутки подряд."]
    env.clock.t += 86400
    await env.w.daily()
    assert len([q for q in env.w.data["queue"] if "Не могу" in q["text"]]) == 1  # only once


# --- task and supervisor (3.9) -------------------------------------------------------------


async def test_supervisor_restarts_after_unexpected_end_and_cancel_stops(env, monkeypatch, logs):
    runs = []

    async def run(bot=None):
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("bug")
        await asyncio.Event().wait()

    monkeypatch.setattr(watch, "run", run)
    task = asyncio.create_task(watch.supervise(env.bot))
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(runs) == 2 and env.clock.sleeps == [60]
    assert "ended unexpectedly (RuntimeError), restart in 60 s" in logs.text()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_loop_survives_failing_step(env, monkeypatch, logs):
    steps = []

    async def tick():
        steps.append(1)
        if len(steps) == 1:
            raise ValueError("round broke")
        if len(steps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(env.w, "tick", tick)
    monkeypatch.setattr(env.w, "start", lambda bot: None)
    with pytest.raises(asyncio.CancelledError):
        await watch.run(env.bot)
    assert len(steps) == 3 and "step failed: ValueError" in logs.text()


async def test_router_starts_and_stops_watch_task(monkeypatch):
    from modules.inhouse import router as entry
    from modules.inhouse.features.yookassa_methods import feature

    started = asyncio.Event()

    async def supervise(bot=None):
        started.set()
        await asyncio.Event().wait()

    async def start(bot=None):
        return False

    monkeypatch.setattr(watch, "supervise", supervise)
    monkeypatch.setattr(feature, "start", start)
    await entry.inhouse_startup(bot=None)
    await asyncio.wait_for(started.wait(), 1)
    task = entry._tasks["watch"]
    await entry.inhouse_shutdown()
    assert task.cancelled() and "watch" not in entry._tasks
