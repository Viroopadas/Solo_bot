"""Payment site watch (spec 2026-10-10 "pay site watch", edition 2, sections 3 and 4.3).

What it does, in one task started from the module's startup hook:
- every WATCH_INTERVAL_SECONDS: one check round over PUBLIC_BASE_URL (section 3.3):
  GET /p/probe-<nonce> (the page with the label filled in and pay.js linked) and
  POST /inhouse/pay probe=<nonce> (the bot's exact answer, through the Russian server);
- states OK / SUSPECT / DOWN of the round results and OK / DOWN / UNKNOWN of the signals of
  the check from Russia (tools/pay-probe, section 4.3), menu mode links <-> fallback
  (section 3.4); the watch is the only owner of mode transitions and never calls
  feature.start/stop;
- short plain-text Telegram messages through a persistent queue with retries (section 3.5);
- passive count of real link outcomes (3.6), daily certificate/domain dates (3.7),
  hourly comparison of the page files on the Russian server with deploy/static (3.3);
- state in storage/inhouse/watch.json, written atomically (3.8).

The owner always wins (T9): with FEATURES off, the DISABLE_FILE present or the module off
in the admin panel the watch does no rounds, switches nothing and sends nothing.
Log: state changes and errors only, never a line per round.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import socket
import ssl
import time

from collections import Counter, deque
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from logger import logger

from . import (
    compat,
    expiry,
    menu_mode,
    settings as feature_settings,
    state,
)


PREFIX = "[inhouse.watch]"
SCHEMA = 1
TICK_SECONDS = 5
READ_LIMIT = 16 * 1024
BODY_MAX = 2048
QUEUE_MAX = 50
RETRY_STEPS = (60, 120, 300, 600)  # then every 600 s
RESTART_DELAY = 60
DAY = 86400.0
SIGNAL_MAX_AGE = 300
NOT_STARTED_WARN_AFTER = 600
CREATE_WINDOW = 600
CREATE_ERRORS = 3
STATIC_EVERY = 3600
DELIVERY_TIMEOUT = 20
DEPLOY_STATIC = Path(__file__).resolve().parent / "deploy" / "static"
STATIC_FILES = ("pay.js", "pay.css")

OK, SUSPECT, DOWN, UNKNOWN = "OK", "SUSPECT", "DOWN", "UNKNOWN"

# Reason codes of a failed round (A) and of the signals of the check from Russia (B).
REASONS: dict[str, str] = {
    "dns": "имя {host} не находится",
    "server": "сервер переходника не отвечает",
    "cert": "проблема с сертификатом",
    "page": "страница оплаты отдаётся неправильно",
    "bridge": "переходник не достучался до бота",
    "bot": "бот ответил не то",
}
SIGNAL_REASONS = frozenset(REASONS) | {"ok"}

PROBE_RE = re.compile(rb"probe=([0-9a-f]{16,32})")
SIGNAL_KEYS = ("observer", "seq", "ts", "state", "reason", "sig")
SIGNAL_FORMATS = {
    "observer": re.compile(r"[a-z0-9][a-z0-9.-]{0,31}"),
    "seq": re.compile(r"[0-9]{1,18}"),
    "ts": re.compile(r"[0-9]{1,12}"),
    "state": re.compile(r"ok|down"),
    "reason": re.compile(r"[a-z]{1,16}"),
    "sig": re.compile(r"[0-9a-f]{64}"),
}
# deploy/unavailable.html of the Russian server: shown when the bot did not answer it.
RU_UNAVAILABLE_MARKERS = ("временно недоступна", "/static/pay.css")


class Clock:
    """Time seam: tests replace `watch.clock` with a fake."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


clock = Clock()


def host() -> str:
    return urlsplit(feature_settings.PUBLIC_BASE_URL).hostname or feature_settings.PUBLIC_BASE_URL


def reason_text(code: str | None) -> str:
    template = REASONS.get(code or "")
    return template.format(host=host()) if template else "причина не названа"


def owner_allows() -> bool:
    """T9: FEATURES on, no DISABLE_FILE, module on in the admin panel."""
    from .feature import feature_flag

    try:
        return bool(feature_flag()) and bool(state.module_enabled())
    except Exception:
        return False


def fresh() -> dict:
    return {
        "schema": SCHEMA,
        "mode": menu_mode.LINKS,
        "incident": None,
        "entries": [],
        "hold_until": 0.0,
        "a": {"state": OK, "fails": 0, "oks": 0, "last_round": 0.0, "reason": ""},
        "observers": {},
        "queue": [],
        "warned": {},
        "expiry_fails": {},
        "daily_last": 0.0,
    }


def validate(data) -> dict:
    """Saved state merged over defaults; raises ValueError when the shape is wrong."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError("schema")
    out = fresh()
    for key, default in out.items():
        if key not in data:
            continue
        value = data[key]
        if key == "incident":
            if value is not None and not (isinstance(value, dict) and isinstance(value.get("since"), (int, float))):
                raise ValueError(key)
        elif key == "hold_until" or key == "daily_last":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(key)
        elif not isinstance(value, type(default)):
            raise ValueError(key)
        out[key] = value
    if out["mode"] not in menu_mode.MODES:
        raise ValueError("mode")
    if out["a"].get("state") not in (OK, SUSPECT, DOWN):
        raise ValueError("a.state")
    for item in out["queue"]:
        if not (isinstance(item, dict) and isinstance(item.get("text"), str) and isinstance(item.get("chats"), list)):
            raise ValueError("queue")
    if out["mode"] == menu_mode.FALLBACK and out["incident"] is None:
        out["mode"] = menu_mode.LINKS
    return out


# --- HTTP of a round ----------------------------------------------------------------------


def classify_error(exc: BaseException) -> str:
    try:
        import aiohttp
    except Exception:  # pragma: no cover - aiohttp is a bot dependency
        aiohttp = None
    if aiohttp is not None:
        dns_error = getattr(aiohttp, "ClientConnectorDNSError", None)
        if dns_error is not None and isinstance(exc, dns_error):
            return "dns"
        if isinstance(exc, aiohttp.ClientConnectorError) and isinstance(getattr(exc, "os_error", None), socket.gaierror):
            return "dns"
        if isinstance(exc, aiohttp.ClientSSLError):
            return "cert"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, ssl.SSLError):
        return "cert"
    return "server"


async def make_requester(timeout: float):
    """(request, close): one aiohttp session per round, no redirects, at most READ_LIMIT read."""
    import aiohttp

    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(use_dns_cache=False, force_close=True),
        timeout=aiohttp.ClientTimeout(total=timeout),
        headers={"User-Agent": "inhouse-watch"},
    )

    async def request(method: str, url: str, data: bytes | None = None) -> tuple[int, bytes]:
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if data is not None else None
        async with session.request(method, url, data=data, headers=headers, allow_redirects=False) as resp:
            return resp.status, await resp.content.read(READ_LIMIT)

    return request, session.close


def probe_body_re(nonce: str) -> re.Pattern:
    return re.compile(rf"inhouse-probe {re.escape(nonce)} (ready|off) (links|fallback) (-1|[0-9]+)")


async def run_round(request, nonce: str | None = None) -> tuple[bool, str]:
    """Two steps of section 3.3. Returns (ok, reason code or '')."""
    s = feature_settings
    nonce = nonce or secrets.token_hex(8)
    base = s.PUBLIC_BASE_URL.rstrip("/")
    label_path = f"{s.LINK_PATH_PREFIX}probe-{nonce}"
    try:
        status, body = await request("GET", base + label_path)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return False, classify_error(exc)
    page = body.decode("utf-8", "replace")
    if status != 200 or f'value="{label_path}"' not in page or "/static/pay.js" not in page:
        return False, "page"
    try:
        status, body = await request("POST", base + s.WEBHOOK_PATH, f"probe={nonce}".encode("ascii"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return False, classify_error(exc)
    text = body.decode("utf-8", "replace")
    if status == 200 and probe_body_re(nonce).fullmatch(text):
        return True, ""
    if status in (502, 503, 504) or all(m in text for m in RU_UNAVAILABLE_MARKERS):
        return False, "bridge"
    return False, "bot"


# --- the watch ----------------------------------------------------------------------------


class Watch:
    def __init__(self) -> None:
        self.loaded = False
        self.data = fresh()
        self.fresh_file = False
        self.corrupted = False
        self.started = False
        self.bot = None
        self.observer_key: bytes | None = None
        self.next_round_at = 0.0
        self.next_static_at = 0.0
        self.not_ready_since: float | None = None
        self.outcomes: deque = deque()
        self.rejects: Counter = Counter()
        self.rejects_logged_at = 0.0
        self.signal_logged = False
        self.save_error_logged_at = 0.0
        self.no_bot_logged = False
        self.probe_count = 0
        self._lock: asyncio.Lock | None = None
        self._maintenance_task: asyncio.Task | None = None

    # --- state file -----------------------------------------------------------------------

    def ensure_loaded(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        path = Path(feature_settings.WATCH_STATE_FILE)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.data = fresh()
            self.fresh_file = True
            logger.info(f"{PREFIX} state file not found, menu mode links")
            return
        except Exception as exc:
            raw = None
            reason = type(exc).__name__
        else:
            reason = ""
        if raw is not None:
            try:
                self.data = validate(json.loads(raw))
                return
            except Exception as exc:
                reason = type(exc).__name__
        self.data = fresh()
        self.corrupted = True
        logger.warning(f"{PREFIX} state file damaged ({reason}), menu mode links")

    def save(self) -> None:
        if not feature_settings.WATCH_ENABLED:
            return
        path = Path(feature_settings.WATCH_STATE_FILE)
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:
            now = clock.now()
            if now - self.save_error_logged_at >= 3600:
                self.save_error_logged_at = now
                logger.error(f"{PREFIX} state file not written: {type(exc).__name__}")
            with contextlib.suppress(Exception):
                tmp.unlink()

    def restore_mode(self) -> str:
        """Feature start, before the link layer: the saved mode becomes the menu mode."""
        s = feature_settings
        self.ensure_loaded()
        mode = self.data["mode"]
        if not s.WATCH_ENABLED:
            mode = menu_mode.LINKS
        elif mode == menu_mode.FALLBACK and not s.AUTO_FALLBACK:
            logger.info(f"{PREFIX} AUTO_FALLBACK is off: saved fallback mode dropped, menu mode links")
            mode = menu_mode.LINKS
            self.data["mode"] = mode
            self.save()
        menu_mode.set_mode(mode)
        return mode

    # --- start ----------------------------------------------------------------------------

    def start(self, bot) -> None:
        s = feature_settings
        self.bot = bot
        self.ensure_loaded()
        if self.started:
            return
        self.started = True
        try:
            from . import token

            self.observer_key = token.load_or_create_secret(s.OBSERVER_SECRET_FILE).hex().encode("ascii")
        except Exception as exc:
            self.observer_key = None
            logger.error(f"{PREFIX} observer secret not available, signals refused: {type(exc).__name__}")
        if not s.AUTO_FALLBACK and self.data["mode"] == menu_mode.FALLBACK:
            self.data["mode"] = menu_mode.LINKS
            menu_mode.set_mode(menu_mode.LINKS)
        if self.fresh_file:
            self.enqueue("info", f"🟢 Сторож оплаты {host()} включён. Напишу сюда, если оплата через него сломается.")
        if self.corrupted:
            self.enqueue(
                "warn",
                "⚠️ Файл состояния сторожа оплаты был повреждён — начал заново, кнопки СБП и карты включены.",
            )
        self.save()
        logger.info(
            f"{PREFIX} watch on: {s.PUBLIC_BASE_URL}, mode {menu_mode.current()}, "
            f"auto fallback {'on' if s.AUTO_FALLBACK else 'off'}, "
            f"observer signals {'on' if self.observer_key else 'off'}"
        )

    # --- messages -------------------------------------------------------------------------

    def recipients(self) -> list[int]:
        ids = compat.parse_ids(tuple(feature_settings.WATCH_ALERT_CHAT_IDS or ()))
        if ids:
            return ids
        try:
            return compat.admin_ids()
        except Exception:
            return []

    def enqueue(self, kind: str, text: str, incident: str | None = None) -> None:
        chats = self.recipients()
        if not chats:
            logger.error(f"{PREFIX} no recipients (WATCH_ALERT_CHAT_IDS / ADMIN_ID), message dropped: {kind}")
            return
        queue = self.data["queue"]
        if kind == "up" and incident:
            unsent_down = [
                q for q in queue if q.get("incident") == incident and q["kind"] == "down" and len(q["chats"]) == q.get("all")
            ]
            if unsent_down:
                # "fixed" before "broken" was delivered: one summary instead of the pair.
                self.data["queue"] = queue = [q for q in queue if q.get("incident") != incident]
                text = unsent_down[0].get("summary") or text
                kind = "summary"
        queue.append(
            {
                "id": secrets.token_hex(4),
                "kind": kind,
                "incident": incident,
                "text": text,
                "chats": chats,
                "all": len(chats),
                "attempts": 0,
                "next_at": 0.0,
                "created": clock.now(),
            }
        )
        while len(queue) > QUEUE_MAX:
            dropped = queue.pop(0)
            logger.error(f"{PREFIX} message queue full, oldest dropped: {dropped.get('kind')}")
        self.save()

    def warn(self, topic: str, text: str, every: float = DAY) -> bool:
        now = clock.now()
        last = self.data["warned"].get(topic, 0.0)
        if last and now - last < every:
            return False
        self.data["warned"][topic] = now
        self.enqueue("warn", text)
        return True

    def _bot(self):
        if self.bot is None:
            try:
                self.bot = compat.load_bot()
            except Exception:
                if not self.no_bot_logged:
                    self.no_bot_logged = True
                    logger.error(f"{PREFIX} bot object not available, messages wait in the queue")
        return self.bot

    async def deliver(self) -> None:
        if not owner_allows() or not self.data["queue"]:
            return
        # The entire batch, not each recipient, has the alert delivery budget of section 3.4.
        budget = DELIVERY_TIMEOUT
        if self.next_round_at > clock.now():
            budget = min(budget, max(0.1, self.next_round_at - clock.now()))
        try:
            async with asyncio.timeout(budget):
                await self._deliver()
        except TimeoutError:
            now = clock.now()
            for item in self.data["queue"]:
                if item.get("next_at", 0.0) <= now:
                    item["attempts"] = int(item.get("attempts", 0)) + 1
                    step = RETRY_STEPS[min(item["attempts"], len(RETRY_STEPS)) - 1]
                    item["next_at"] = now + step
            logger.warning(f"{PREFIX} delivery time budget reached, pending messages retained")
        finally:
            self.save()

    async def _deliver(self) -> None:
        queue = self.data["queue"]
        if not queue:
            return
        bot = self._bot()
        if bot is None:
            return
        try:
            from aiogram.exceptions import TelegramRetryAfter
            from aiogram.types import LinkPreviewOptions
        except Exception:  # pragma: no cover - aiogram is a bot dependency
            TelegramRetryAfter = ()  # noqa: N806
            LinkPreviewOptions = None  # noqa: N806
        changed = False
        for item in list(queue):
            now = clock.now()
            if item.get("next_at", 0.0) > now:
                continue
            for chat in list(item["chats"]):
                if not owner_allows():
                    return
                try:
                    kwargs = {"chat_id": chat, "text": item["text"], "parse_mode": None}
                    if LinkPreviewOptions is not None:
                        kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=True)
                    sent = await bot.send_message(**kwargs)
                    if getattr(sent, "message_id", None) is None:
                        raise RuntimeError("telegram returned no message")
                    item["chats"].remove(chat)
                    changed = True
                except asyncio.CancelledError:
                    raise
                except TelegramRetryAfter as exc:
                    wait = max(1, int(getattr(exc, "retry_after", 1) or 1))
                    for other in queue:
                        other["next_at"] = max(other.get("next_at", 0.0), now + wait)
                    logger.warning(f"{PREFIX} telegram asks to wait {wait} s")
                    self.save()
                    return
                except Exception as exc:
                    item["attempts"] = int(item.get("attempts", 0)) + 1
                    step = RETRY_STEPS[min(item["attempts"], len(RETRY_STEPS)) - 1]
                    item["next_at"] = now + step
                    changed = True
                    logger.warning(
                        f"{PREFIX} message not sent ({type(exc).__name__}), attempt {item['attempts']}, retry in {step} s"
                    )
                    break
            if not item["chats"]:
                with contextlib.suppress(ValueError):
                    queue.remove(item)
                changed = True
        if changed:
            self.save()

    # --- signals of the check from Russia (4.3) -------------------------------------------

    def _reject(self, why: str) -> None:
        self.rejects[why] += 1
        now = clock.now()
        if now - self.rejects_logged_at >= 3600:
            self.rejects_logged_at = now
            detail = " ".join(f"{k}={v}" for k, v in sorted(self.rejects.items()))
            logger.warning(f"{PREFIX} observer signals refused: {detail}")
            self.rejects.clear()

    def accept_signal(self, body: bytes) -> str | None:
        """Ack text for a valid signal; None sends the request down the normal branch."""
        if not feature_settings.WATCH_ENABLED or not self.observer_key:
            self._reject("watch off")
            return None
        try:
            pairs = parse_qsl(body.decode("ascii"), keep_blank_values=True, strict_parsing=True)
        except (UnicodeDecodeError, ValueError):
            self._reject("shape")
            return None
        fields = dict(pairs)
        if len(pairs) != len(SIGNAL_KEYS) or set(fields) != set(SIGNAL_KEYS):
            self._reject("shape")
            return None
        for key, pattern in SIGNAL_FORMATS.items():
            if not pattern.fullmatch(fields[key]):
                self._reject("shape")
                return None
        if fields["reason"] not in SIGNAL_REASONS:
            self._reject("shape")
            return None
        message = "&".join(f"{k}={fields[k]}" for k in SIGNAL_KEYS[:-1])
        expected = hmac.new(self.observer_key, message.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, fields["sig"]):
            self._reject("signature")
            return None
        now = clock.now()
        ts, seq = int(fields["ts"]), int(fields["seq"])
        if abs(now - ts) > SIGNAL_MAX_AGE:
            self._reject("time")
            return None
        name = fields["observer"]
        previous = self.data["observers"].get(name) or {}
        if seq <= int(previous.get("seq", 0)):
            self._reject("seq")
            return None
        self.data["observers"][name] = {
            "seq": seq,
            "ts": ts,
            "state": fields["state"],
            "reason": fields["reason"],
            "at": now,
        }
        if not self.signal_logged:
            self.signal_logged = True
            logger.info(f"{PREFIX} observer {name} signals accepted (state {fields['state']})")
        elif previous.get("state") and previous.get("state") != fields["state"]:
            logger.warning(f"{PREFIX} observer {name} state {previous.get('state')} -> {fields['state']} ({fields['reason']})")
        if self.data["warned"].pop("observer_silent", None):
            logger.info(f"{PREFIX} observer {name} is back")
            self.enqueue("info", "🟢 Проверка оплаты из России снова на связи.")
        self.save()
        return f"observer-ack {seq}"

    def last_signal(self) -> dict | None:
        observers = [o for o in self.data["observers"].values() if isinstance(o, dict)]
        return max(observers, key=lambda o: o.get("at", 0.0)) if observers else None

    def b_state(self, now: float) -> str:
        last = self.last_signal()
        if last is None or now - float(last.get("at", 0.0)) >= feature_settings.OBSERVER_STALE_MINUTES * 60:
            return UNKNOWN
        return DOWN if last.get("state") == "down" else OK

    # --- passive outcomes (3.6) -----------------------------------------------------------

    def note_outcome(self, kind: str, reason: str = "") -> None:
        if not feature_settings.WATCH_ENABLED or not self.started:
            return
        now = clock.now()
        self.outcomes.append((now, kind, reason))
        while self.outcomes and now - self.outcomes[0][0] > CREATE_WINDOW:
            self.outcomes.popleft()
        errors = [r for _, k, r in self.outcomes if k == "error"]
        good = any(k in ("created", "reused") for _, k, _ in self.outcomes)
        if len(errors) >= CREATE_ERRORS and not good and owner_allows():
            self.warn("create", f"⚠️ Не получается создать оплату в ЮKassa: {safe_create_reason(errors[-1])}.")

    # --- probe answer (3.2) ---------------------------------------------------------------

    def round_age(self) -> int:
        last = float(self.data["a"].get("last_round") or 0.0)
        if not last or not feature_settings.WATCH_ENABLED:
            return -1
        return max(0, int(clock.now() - last))

    # --- rounds and transitions (3.3, 3.4) ------------------------------------------------

    async def do_round(self) -> None:
        s = feature_settings
        request, close = await make_requester(float(s.WATCH_REQUEST_TIMEOUT))
        try:
            ok, code = await run_round(request)
        finally:
            with contextlib.suppress(Exception):
                await close()
        self.apply_round(ok, code)

    async def maintenance(self) -> None:
        """Hourly/daily network work cannot delay rounds, switching or alert delivery (T2)."""
        try:
            if owner_allows() and state.current() is not None and clock.now() >= self.next_static_at:
                self.next_static_at = clock.now() + STATIC_EVERY
                request, close = await make_requester(float(feature_settings.WATCH_REQUEST_TIMEOUT))
                try:
                    await self.check_static(request)
                finally:
                    await close()
            if owner_allows() and clock.now() - float(self.data.get("daily_last") or 0.0) >= DAY:
                await self.daily()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"{PREFIX} maintenance failed: {type(exc).__name__}")

    async def stop_maintenance(self) -> None:
        task, self._maintenance_task = self._maintenance_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def apply_round(self, ok: bool, code: str) -> None:
        s = feature_settings
        a = self.data["a"]
        before = a["state"]
        now = clock.now()
        if ok:
            a["fails"] = 0
            a["oks"] = int(a.get("oks", 0)) + 1
            if before == SUSPECT or (before == DOWN and a["oks"] >= int(s.WATCH_OKS)):
                a["state"] = OK
        else:
            a["oks"] = 0
            a["fails"] = int(a.get("fails", 0)) + 1
            a["reason"] = code
            if a["fails"] >= int(s.WATCH_FAILS):
                a["state"] = DOWN
            elif before == OK:
                a["state"] = SUSPECT
        a["last_round"] = now
        if a["state"] != before:
            line = f"{PREFIX} check {before} -> {a['state']}" + ("" if ok else f" ({code})")
            (logger.info if a["state"] == OK else logger.warning)(line)
        if a["state"] == SUSPECT:
            pause = float(s.WATCH_RETRY_SECONDS)
        elif a["state"] == OK:
            pause = float(s.WATCH_INTERVAL_SECONDS) + random.uniform(0, float(s.WATCH_JITTER_SECONDS))
        else:
            pause = float(s.WATCH_INTERVAL_SECONDS)
        self.next_round_at = now + pause
        self.save()

    async def check_static(self, request) -> None:
        mismatched = []
        base = feature_settings.PUBLIC_BASE_URL.rstrip("/")
        for name in STATIC_FILES:
            local = DEPLOY_STATIC / name
            try:
                expected = hashlib.sha256(local.read_bytes()).hexdigest()
            except OSError:
                return
            try:
                status, body = await request("GET", f"{base}/static/{name}")
            except asyncio.CancelledError:
                raise
            except Exception:
                return
            if status != 200:
                return
            if hashlib.sha256(body).hexdigest() != expected:
                mismatched.append(name)
        if mismatched:
            logger.warning(f"{PREFIX} page files differ on the pay server: {','.join(mismatched)}")
            self.warn("static", f"⚠️ На переходнике {host()} другая версия страницы оплаты ({', '.join(mismatched)}).")

    def _lock_obj(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def switch(self, mode: str) -> bool:
        """The only mode transition: under a lock, owner's permission checked again."""
        async with self._lock_obj():
            if not owner_allows():
                return False
            changed = menu_mode.set_mode(mode)
            self.data["mode"] = mode
            self.save()
            if changed:
                logger.warning(f"{PREFIX} menu mode -> {mode}")
            return True

    def _current_reason(self) -> tuple[str, str]:
        a = self.data["a"]
        if a["state"] == DOWN:
            return "a", a.get("reason") or ""
        last = self.last_signal() or {}
        return "b", str(last.get("reason") or "")

    def _down_text(self, source: str, code: str, switched: bool) -> str:
        why = reason_text(code)
        if source == "b":
            text = f"🔴 {feature_settings.OBSERVER_SCOPE_TEXT} не открывается {host()}: {why}."
        else:
            text = f"🔴 Оплата через {host()} не работает: {why}."
        if switched:
            text += " Включил запасную кнопку ЮKassa — новые покупки идут по-старому."
        return text

    async def evaluate(self) -> None:
        s = feature_settings
        d = self.data
        now = clock.now()
        a_state = d["a"]["state"]
        b_state = self.b_state(now)
        incident = d["incident"]
        if incident is None:
            if a_state == DOWN or b_state == DOWN:
                await self.open_incident("a" if a_state == DOWN else "b")
            return
        since = float(incident["since"])
        if (
            a_state == OK
            and b_state != DOWN
            and now - since >= float(s.WATCH_MIN_FALLBACK_MINUTES) * 60
            and now >= float(d.get("hold_until", 0.0))
        ):
            await self.close_incident()
            return
        last = incident.get("last_remind")
        due = since + float(s.WATCH_REMIND_FIRST_MINUTES) * 60 if not last else last + float(s.WATCH_REMIND_EVERY_HOURS) * 3600
        if now >= due:
            incident["last_remind"] = now
            _, code = self._current_reason()
            minutes = max(1, int((now - since) // 60))
            text = f"🔴 {host()} всё ещё не работает ({minutes} мин): {reason_text(code or incident.get('reason'))}."
            if menu_mode.is_fallback():
                text += " Запасная кнопка ЮKassa включена."
            self.enqueue("remind", text, incident=incident["id"])

    async def open_incident(self, source: str) -> None:
        s = feature_settings
        d = self.data
        now = clock.now()
        _, code = self._current_reason() if source == "b" else ("a", d["a"].get("reason") or "")
        entries = [t for t in d["entries"] if now - float(t) < 3600] + [now]
        d["entries"] = entries
        flap = len(entries) >= int(s.WATCH_FLAP_ENTRIES_PER_HOUR)
        if flap:
            d["hold_until"] = now + float(s.WATCH_FLAP_HOLD_MINUTES) * 60
        incident = {"id": secrets.token_hex(4), "since": now, "source": source, "reason": code, "last_remind": None}
        d["incident"] = incident
        switched = False
        if s.AUTO_FALLBACK:
            switched = await self.switch(menu_mode.FALLBACK)
        logger.warning(f"{PREFIX} pay site broken (source {source}, {code or '-'}), fallback {'on' if switched else 'off'}")
        text = self._down_text(source, code, switched)
        if flap:
            text += f" Связь нестабильна: сбоев за час — {len(entries)}"
            text += (
                f", держу запасную кнопку не меньше {int(s.WATCH_FLAP_HOLD_MINUTES)} мин." if switched else "."
            )
        self.enqueue("down", text, incident=incident["id"])

    async def close_incident(self) -> None:
        d = self.data
        now = clock.now()
        incident = d["incident"]
        minutes = max(1, int(round((now - float(incident["since"])) / 60)))
        switched_back = False
        if menu_mode.is_fallback() or d["mode"] == menu_mode.FALLBACK:
            switched_back = await self.switch(menu_mode.LINKS)
        d["incident"] = None
        logger.info(f"{PREFIX} pay site works again after {minutes} min, menu mode {menu_mode.current()}")
        text = f"🟢 {host()} снова работает (простой {minutes} мин)."
        if switched_back:
            text += " Вернул кнопки СБП и карты."
        # Unsent reminders of a finished incident are no longer true.
        d["queue"] = [
            q for q in d["queue"]
            if not (q.get("incident") == incident["id"] and q["kind"] == "remind" and len(q["chats"]) == q.get("all"))
        ]
        for q in d["queue"]:
            if q.get("incident") == incident["id"] and q["kind"] == "down":
                q["summary"] = (
                    f"🟡 Оплата через {host()} была недоступна {minutes} мин "
                    f"({reason_text(incident.get('reason'))}), уже работает."
                )
        self.enqueue("up", text, incident=incident["id"])
        self.save()

    # --- owner-visible states without a breakage alarm ------------------------------------

    def check_not_started(self, now: float) -> None:
        if self.not_ready_since is None:
            self.not_ready_since = now
            return
        if now - self.not_ready_since >= NOT_STARTED_WARN_AFTER:
            self.warn(
                "not_started",
                f"⚠️ Кнопки СБП и карты не запустились: {state.STATE.reason}. Покупки идут через обычную кнопку ЮKassa.",
                every=float(feature_settings.WATCH_REMIND_EVERY_HOURS) * 3600,
            )

    def check_observer_silence(self, now: float) -> None:
        last = self.last_signal()
        if last is None:
            return  # the check from Russia was never installed: nothing to miss
        if now - float(last.get("at", 0.0)) >= float(feature_settings.OBSERVER_SILENT_ALERT_MINUTES) * 60:
            minutes = int((now - float(last.get("at", 0.0))) // 60)
            self.warn(
                "observer_silent",
                f"⚠️ Проверка оплаты из России молчит уже {minutes} мин — сторож в боте работает, мост проверьте.",
                every=float(feature_settings.WATCH_REMIND_EVERY_HOURS) * 3600,
            )

    # --- daily dates (3.7) ----------------------------------------------------------------

    def _expiry_unknown(self, key: str, name: str) -> None:
        fails = self.data["expiry_fails"]
        fails[key] = int(fails.get(key, 0)) + 1
        if fails[key] == 3:
            self.enqueue("warn", f"⚠️ Не могу проверить срок {name} третьи сутки подряд.")

    async def daily(self) -> None:
        s = feature_settings
        self.data["daily_last"] = clock.now()
        self.save()
        timeout = float(s.WATCH_REQUEST_TIMEOUT)
        for name, server in dict(s.WATCH_CERT_HOSTS).items():
            if not owner_allows():
                return
            key = f"cert:{name}"
            try:
                days = await expiry.cert_days(name, timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"{PREFIX} certificate date of {name} unknown: {type(exc).__name__}")
                self._expiry_unknown(key, f"сертификата {name}")
                continue
            self.data["expiry_fails"].pop(key, None)
            if days < float(s.CERT_WARN_DAYS):
                left = int(days)
                text = (
                    f"⚠️ Сертификат {name} уже истёк — проверьте продление на {server}."
                    if days <= 0
                    else f"⚠️ Сертификат {name} истекает через {left} дн. — проверьте продление на {server}."
                )
                self.warn(key, text)
        for name in tuple(s.WATCH_DOMAINS):
            if not owner_allows():
                return
            key = f"domain:{name}"
            try:
                days = await expiry.domain_days(name, timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"{PREFIX} domain date of {name} unknown: {type(exc).__name__}")
                self._expiry_unknown(key, f"домена {name}")
                continue
            self.data["expiry_fails"].pop(key, None)
            if days < float(s.DOMAIN_WARN_DAYS):
                self.warn(key, f"⚠️ Домен {name} истекает через {max(0, int(days))} дн. — продлите.")
        self.save()

    # --- one step of the loop -------------------------------------------------------------

    async def tick(self) -> None:
        now = clock.now()
        if not owner_allows():
            await self.stop_maintenance()
            return  # T9: the owner turned the feature off - silence, no switching
        if state.current() is not None:
            self.not_ready_since = None
            if now >= self.next_round_at:
                await self.do_round()
            if not owner_allows():
                await self.stop_maintenance()
                return
            await self.evaluate()
        elif not state.STATE.starting:
            self.check_not_started(now)
        self.check_observer_silence(clock.now())
        await self.deliver()
        if not owner_allows():
            return
        due = (state.current() is not None and clock.now() >= self.next_static_at) or (
            clock.now() - float(self.data.get("daily_last") or 0.0) >= DAY
        )
        if due and (self._maintenance_task is None or self._maintenance_task.done()):
            self._maintenance_task = asyncio.create_task(self.maintenance())


WATCH = Watch()


def reset() -> Watch:
    """Tests: a fresh watch object."""
    global WATCH
    WATCH = Watch()
    return WATCH


def safe_create_reason(reason: str) -> str:
    text = reason or ""
    if "redirect host" in text:
        return "ЮKassa вернула неожиданный адрес оплаты"
    if "no payment id or url" in text:
        return "ЮKassa не вернула ссылку на оплату"
    if "returned None" in text:
        return "штатное создание оплаты вернуло ошибку"
    if re.fullmatch(r"(unexpected )?[A-Za-z_][A-Za-z0-9_.]{0,60}", text):
        return f"ошибка при обращении к ЮKassa ({text})"
    return "ошибка при обращении к ЮKassa"


# --- web: probe and observer signal before the normal branch ------------------------------


async def answer_fast(request):
    """Probe answer (3.2) or observer ack (4.3); None = the request goes the normal branch.

    Size is checked before reading; only a form body of known length up to 2 KB is read
    (aiohttp caches it, so the normal branch can still parse the same body).
    """
    try:
        if request.method != "POST" or request.content_type != "application/x-www-form-urlencoded":
            return None
        length = request.content_length
        if length is None or length > BODY_MAX:
            return None
        body = await request.read()
    except Exception:
        return None
    if len(body) > BODY_MAX:
        return None
    from aiohttp import web

    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    match = PROBE_RE.fullmatch(body)
    if match:
        w = WATCH
        w.probe_count += 1
        ready = "ready" if state.current() is not None else "off"
        text = f"inhouse-probe {match.group(1).decode('ascii')} {ready} {menu_mode.current()} {w.round_age()}"
        return web.Response(status=200, text=text, content_type="text/plain", headers=headers)
    if body.startswith(b"observer="):
        ack = WATCH.accept_signal(body)
        if ack is not None:
            return web.Response(status=200, text=ack, content_type="text/plain", headers=headers)
    return None


# --- task and its supervisor (3.9) --------------------------------------------------------


async def run(bot=None) -> None:
    if not feature_settings.WATCH_ENABLED:
        logger.info(f"{PREFIX} watch off (WATCH_ENABLED = False), menu mode links")
        menu_mode.set_mode(menu_mode.LINKS)
        return
    w = WATCH
    w.start(bot)
    try:
        while True:
            try:
                await w.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a failed step must not stop the watch
                logger.error(f"{PREFIX} step failed: {type(exc).__name__}")
            await clock.sleep(TICK_SECONDS)
    finally:
        await w.stop_maintenance()


async def supervise(bot=None) -> None:
    """Restart the watch task 60 s after an unexpected end; cancellation ends it."""
    while True:
        inner = asyncio.create_task(run(bot))
        try:
            await inner
            return  # run() returns only when the watch is turned off in settings
        except asyncio.CancelledError:
            inner.cancel()
            with contextlib.suppress(BaseException):
                await inner
            with contextlib.suppress(Exception):
                WATCH.save()
            raise
        except Exception as exc:
            logger.error(f"{PREFIX} watch task ended unexpectedly ({type(exc).__name__}), restart in {RESTART_DELAY} s")
        await clock.sleep(RESTART_DELAY)
