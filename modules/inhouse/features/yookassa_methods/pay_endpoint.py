"""POST /inhouse/pay: open a link button -> YooKassa payment page.

Order of checks (section 4.2 of the spec):
0. a probe of the pay-site watch or a signal of the check from Russia (watch.answer_fast):
   answered at once, before everything else, without database or YooKassa;
1. feature active and module enabled, else "payment temporarily unavailable";
2. Origin (only if the browser sent one; "null" is what no-referrer pages send);
3. label: signature, time, method from the white list - before any database access;
4. a payment created through this very link (one message) is already paid -> "already paid";
5. the saved purchase: a temporary_data row of this person saved not later than the label
   was issued (updated_at is the checkout number), else "checkout is stale";
6. amount = required_amount: int, > 0, <= MAX_AMOUNT_RUB;
7. under the lock "person + checkout + method": any payment of this checkout paid ->
   "already paid"; an unpaid payment of this method -> its page again; cancelled/expired
   -> a new one; at most NEW_PAYMENTS_PER_HOUR new payments per person;
8. the stock create_and_store_yk_payment(session, tg_id, amount) with the method label, in
   our own session; commit together with our row, rollback on any error;
9. redirect only to https YooKassa hosts.
One log line per opening; no labels, URLs or keys in the log.
"""

from __future__ import annotations

import time

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from logger import logger

from . import context, pages, state, store, token, watch


BODY_MAX = 2048
FORM_FIELD = "t"


@dataclass
class Outcome:
    kind: str  # created | reused | stale | paid | error | unavailable | invalid | limited
    url: str | None = None
    tg_id: int | None = None
    method: str | None = None
    checkout_ms: int | None = None
    payment_id: str | None = None
    reason: str = ""
    retry_token: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def extract_token(raw: str | None, prefix: str) -> str:
    """The form carries either the bare label or the page path /p/<label>."""
    value = (raw or "").strip()
    if value.startswith(prefix):
        value = value[len(prefix) :]
    return value.strip("/")


def origin_allowed(origin: str | None, allowed: tuple[str, ...]) -> bool:
    if origin is None or origin == "" or origin == "null":
        return True
    return origin.rstrip("/") in {a.rstrip("/") for a in allowed}


def redirect_allowed(url: str | None, hosts: tuple[str, ...]) -> bool:
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    host = parts.hostname.lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


def valid_amount(value: Any, max_amount: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > int(max_amount):
        return None
    return value


def _pick(holder: context.Holder, result: Any) -> tuple[str | None, str | None]:
    """Id and URL from the SDK response (holder); the stock function's dict is the fallback."""
    pid, url = holder.payment_id, holder.confirmation_url
    if isinstance(result, dict):
        if not pid:
            for key in ("payment_id", "id", "yookassa_id"):
                if result.get(key):
                    pid = str(result[key])
                    break
        if not url:
            for key in ("confirmation_url", "url", "payment_url", "pay_url"):
                if result.get(key):
                    url = str(result[key])
                    break
    return pid, url


class _RefusedError(Exception):
    """Our own refusal; its message is safe to log."""


def _safe_reason(exc: BaseException) -> str:
    # Foreign exception texts may carry SQL parameters (payment URLs); log only their type.
    if isinstance(exc, _RefusedError):
        return str(exc)
    return type(exc).__name__


async def _create(deps: state.Deps, claims: token.Claims, checkout_ms: int, amount: int) -> Outcome:
    # The hourly quota belongs to the person, not to a payment method. Keep its
    # check and committed increment together even for simultaneous SBP/card opens.
    async with store.LOCKS.hold(("new-payment", claims.tg_id)):
        return await _create_locked(deps, claims, checkout_ms, amount)


async def _create_locked(deps: state.Deps, claims: token.Claims, checkout_ms: int, amount: int) -> Outcome:
    s = deps.settings
    base = {"tg_id": claims.tg_id, "method": claims.method, "checkout_ms": checkout_ms}
    async with deps.session_maker() as session:
        try:
            # A second method may have waited for this person's creation lock.
            checkout = await deps.read_checkout(session, claims.tg_id)
            if checkout is None or checkout.checkout_ms != checkout_ms:
                return Outcome("stale", reason="checkout changed while waiting", **base)
            if await store.count_recent(session, claims.tg_id) >= int(s.NEW_PAYMENTS_PER_HOUR):
                return Outcome("limited", reason="hourly limit", **base)
            with context.method_scope(claims.method) as holder:
                result = await deps.create_payment(session, claims.tg_id, amount)
            if result is None:  # the stock function's own failure signal; its row may be rolled back
                raise _RefusedError("stock function returned None")
            pid, url = _pick(holder, result)
            if not pid or not url:
                raise _RefusedError("no payment id or url")
            if not redirect_allowed(url, s.YOOKASSA_REDIRECT_HOSTS):
                raise _RefusedError("redirect host not allowed")
            await store.insert(
                session,
                tg_id=claims.tg_id,
                checkout_ms=checkout_ms,
                issued_ms=claims.issued_ms,
                method=claims.method,
                payment_id=pid,
                confirmation_url=url,
                amount=amount,
            )
            await session.commit()
        except Exception as exc:
            try:
                await session.rollback()
            except Exception as rollback_exc:
                logger.warning(f"[inhouse.yk] rollback failed: {type(rollback_exc).__name__}")
            return Outcome("error", reason=_safe_reason(exc), **base)
    reason = ""
    if claims.method != "other" and not holder.applied:
        reason = "method not applied (conflict)" if holder.conflict else "method not applied (sdk not called)"
    return Outcome("created", url=url, payment_id=pid, reason=reason, **base)


async def open_link(deps: state.Deps, raw_token: str) -> Outcome:
    s = deps.settings
    try:
        claims = token.verify(deps.secret, raw_token, s.TOKEN_TTL_SECONDS)
    except token.TokenExpiredError:
        return Outcome("stale", reason="label expired")
    except token.TokenError as exc:
        return Outcome("invalid", reason=f"label: {exc}")

    async with deps.session_maker() as session:
        for row in await store.rows_for_link(session, claims.tg_id, claims.issued_ms):
            if (await deps.payment_status(session, row.payment_id) or "").lower() in store.PAID_STATUSES:
                return Outcome(
                    "paid",
                    tg_id=claims.tg_id,
                    method=claims.method,
                    payment_id=row.payment_id,
                    checkout_ms=row.checkout_ms,
                    reason="link already paid",
                )
        checkout = await deps.read_checkout(session, claims.tg_id)
    if checkout is None:
        return Outcome("stale", tg_id=claims.tg_id, method=claims.method, reason="no checkout")
    checkout_ms = checkout.checkout_ms
    base = {"tg_id": claims.tg_id, "method": claims.method, "checkout_ms": checkout_ms}
    if checkout_ms > claims.issued_ms:
        return Outcome("stale", reason="checkout newer than label", **base)
    amount = valid_amount(checkout.data.get("required_amount"), s.MAX_AMOUNT_RUB)
    if amount is None:
        return Outcome("stale", reason="amount out of bounds", **base)

    async with store.LOCKS.hold((claims.tg_id, checkout_ms, claims.method)):
        async with deps.session_maker() as session:
            current_checkout = await deps.read_checkout(session, claims.tg_id)
            if current_checkout is None or current_checkout.checkout_ms != checkout_ms:
                return Outcome("stale", reason="checkout changed while waiting", **base)
            rows = await store.rows_for_checkout(session, claims.tg_id, checkout_ms)
            reuse = None
            for row in rows:
                status = (await deps.payment_status(session, row.payment_id) or "").lower()
                if status in store.PAID_STATUSES:
                    return Outcome("paid", payment_id=row.payment_id, reason="checkout already paid", **base)
                if reuse is None and row.method == claims.method and status in store.REUSABLE_STATUSES:
                    reuse = row
        if reuse is not None and redirect_allowed(reuse.confirmation_url, s.YOOKASSA_REDIRECT_HOSTS):
            return Outcome("reused", url=reuse.confirmation_url, payment_id=reuse.payment_id, **base)
        outcome = await _create(deps, claims, checkout_ms, amount)
    if outcome.kind == "error":
        outcome.retry_token = raw_token
    return outcome


async def _maybe_purge(deps: state.Deps) -> None:
    now = time.time()
    if now - deps.last_purge < 3600:
        return
    deps.last_purge = now
    try:
        async with deps.session_maker() as session:
            await store.purge_old(session, deps.settings.STORE_RETENTION_DAYS)
            await session.commit()
    except Exception as exc:
        logger.warning(f"[inhouse.yk] purge failed: {type(exc).__name__}")


def _log(outcome: Outcome, started: float) -> None:
    ms = int((time.monotonic() - started) * 1000)
    line = (
        f"[inhouse.yk] open result={outcome.kind} method={outcome.method or '-'} user={outcome.tg_id or '-'} "
        f"checkout={outcome.checkout_ms or '-'} payment={outcome.payment_id or '-'} ms={ms}"
    )
    if outcome.reason:
        line += f" reason={outcome.reason}"
    try:
        watch.WATCH.note_outcome(outcome.kind, outcome.reason)
    except Exception as exc:  # the watch must never affect a payment
        logger.warning(f"[inhouse.yk] watch outcome note failed: {type(exc).__name__}")
    if outcome.kind == "error":
        logger.error(line)
    elif outcome.kind in ("created", "reused", "paid", "stale"):
        logger.info(line)
    else:
        logger.warning(line)


def _redirect(url: str) -> web.Response:
    response = web.Response(status=303, headers={"Location": url})
    pages.apply_headers(response)
    return response


async def handle(request: web.Request) -> web.Response:
    # Public Request.clone API: bound reading before either fast or normal form parsing,
    # including chunked requests with no Content-Length. Both branches share this cache.
    request = request.clone(client_max_size=BODY_MAX)
    started = time.monotonic()
    # Probe of the pay-site watch and signals of the check from Russia are answered first,
    # before the activity and Origin checks, without database or YooKassa (watch.py).
    try:
        fast = await watch.answer_fast(request)
    except web.HTTPRequestEntityTooLarge:
        from . import settings as feature_settings

        _log(Outcome("invalid", reason="body too large"), started)
        return pages.render("invalid", feature_settings, _fallback_bot_link(feature_settings))
    if fast is not None:
        return fast
    deps = state.current()
    if deps is None:
        from . import settings as feature_settings

        outcome = Outcome("unavailable", reason="feature off")
        _log(outcome, started)
        return pages.render("unavailable", feature_settings, _fallback_bot_link(feature_settings))
    s = deps.settings
    if not origin_allowed(request.headers.get("Origin"), s.ALLOWED_ORIGINS):
        outcome = Outcome("invalid", reason="origin not allowed")
        _log(outcome, started)
        return pages.render("invalid", s, deps.bot_link)
    if request.content_length is not None and request.content_length > BODY_MAX:
        outcome = Outcome("invalid", reason="body too large")
        _log(outcome, started)
        return pages.render("invalid", s, deps.bot_link)
    try:
        form = await request.post()
        raw = form.get(FORM_FIELD)
        raw = raw if isinstance(raw, str) else ""
    except Exception:
        raw = ""
    raw = extract_token(raw, s.LINK_PATH_PREFIX)
    try:
        outcome = await open_link(deps, raw)
    except Exception as exc:
        outcome = Outcome("error", reason=f"unexpected {type(exc).__name__}", retry_token=raw)
    _log(outcome, started)
    # A forged label must not reach the database, even via maintenance work.
    if outcome.kind != "invalid" and outcome.reason != "label expired":
        await _maybe_purge(deps)
    if outcome.kind in ("created", "reused") and outcome.url:
        return _redirect(outcome.url)
    return pages.render(
        outcome.kind if outcome.kind in pages.KINDS else "error", s, deps.bot_link, retry_token=outcome.retry_token
    )


def _fallback_bot_link(feature_settings) -> str:
    try:
        from . import compat

        return compat.bot_link(compat.load_config(), feature_settings.BOT_LINK)
    except Exception:
        return ""
