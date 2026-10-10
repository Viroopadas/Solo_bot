"""Wrapper over yookassa.Payment.create (open SDK, version 3.9.0).

Acts only when the request carries a method label (context.py) and the feature is active
with the module enabled. Then it:
- copies the request (dict or PaymentRequest) and adds one field:
  payment_method_data = {"type": "sbp"} or {"type": "bank_card"}; for "other" nothing is
  added (the general YooKassa page);
- if the request already has payment_method_data, payment_method_id or payment_token,
  changes nothing and writes a warning: the buyer gets the general page (T2 not met then,
  and it is visible in the log);
- passes the provider's idempotence key and all other arguments unchanged, keeps the
  classmethod shape, returns the SDK response unchanged;
- puts the payment id and confirmation URL into the per-call holder.
It never retries a request itself.
"""

from __future__ import annotations

import copy
import functools
import threading
import time

from typing import Any

from logger import logger

from . import context, state


METHOD_DATA: dict[str, dict[str, str]] = {"sbp": {"type": "sbp"}, "card": {"type": "bank_card"}}
CONFLICT_KEYS: tuple[str, ...] = ("payment_method_data", "payment_method_id", "payment_token")
MARK = "__inhouse_yk_patch__"

_installed: dict[str, Any] = {"cls": None, "original": None}


class PatchError(Exception):
    pass


def check_patchable(payment_cls) -> None:
    raw = payment_cls.__dict__.get("create")
    if not isinstance(raw, classmethod):
        raise PatchError("yookassa.Payment.create is not a classmethod")


def is_installed() -> bool:
    return _installed["cls"] is not None


def _to_dict(params: Any) -> dict | None:
    if isinstance(params, dict):
        return copy.deepcopy(params)
    try:
        from yookassa.domain.request import PaymentRequest
    except Exception:
        PaymentRequest = None  # noqa: N806
    if PaymentRequest is not None and isinstance(params, PaymentRequest):
        return copy.deepcopy(dict(params))
    return None


def _conflicts(params: Any) -> list[str]:
    if isinstance(params, dict):
        return [k for k in CONFLICT_KEYS if params.get(k)]
    return [k for k in CONFLICT_KEYS if getattr(params, k, None)]


def _capture(holder: context.Holder, response: Any) -> None:
    try:
        pid = getattr(response, "id", None)
        confirmation = getattr(response, "confirmation", None)
        url = getattr(confirmation, "confirmation_url", None)
        if url is None and isinstance(response, dict):
            pid = response.get("id", pid)
            url = (response.get("confirmation") or {}).get("confirmation_url")
        if pid:
            holder.payment_id = str(pid)
        if url:
            holder.confirmation_url = str(url)
    except Exception as exc:  # never break the stock flow
        holder.notes.append(f"capture failed: {type(exc).__name__}")


def _make_wrapper(original_func):
    @functools.wraps(original_func)
    def create(cls, params, idempotency_key=None, *args: object, **kwargs: object):
        selection = context.current()
        if selection is None or state.current() is None:
            return original_func(cls, params, idempotency_key, *args, **kwargs)
        holder = selection.holder
        holder.calls += 1
        send = params
        extra = METHOD_DATA.get(selection.method)
        if extra is not None:
            found = _conflicts(params)
            if found:
                holder.conflict = True
                logger.warning(
                    f"[inhouse.yk] payment request already has {','.join(found)}; "
                    f"method {selection.method} not applied, buyer gets the general page"
                )
            else:
                as_dict = _to_dict(params)
                if as_dict is None:
                    holder.notes.append("unknown params type")
                    logger.warning(
                        f"[inhouse.yk] unknown payment request type {type(params).__name__}; "
                        f"method {selection.method} not applied"
                    )
                else:
                    as_dict["payment_method_data"] = dict(extra)
                    send = as_dict
                    holder.applied = True
        response = original_func(cls, send, idempotency_key, *args, **kwargs)
        _capture(holder, response)
        return response

    setattr(create, MARK, True)
    return create


def install(payment_cls) -> None:
    if is_installed():
        raise PatchError("already installed")
    check_patchable(payment_cls)
    raw = payment_cls.__dict__["create"]
    if getattr(raw.__func__, MARK, False):
        raise PatchError("Payment.create already wrapped by another copy")
    payment_cls.create = classmethod(_make_wrapper(raw.__func__))
    _installed["cls"] = payment_cls
    _installed["original"] = raw


def uninstall() -> None:
    cls, original = _installed["cls"], _installed["original"]
    if cls is not None and original is not None:
        cls.create = original
    _installed["cls"] = None
    _installed["original"] = None


# ---- Shared HTTP connection to the YooKassa API --------------------------------------------
# The SDK opens a new requests.Session for every call and closes it right after, so each
# payment pays a fresh TLS handshake to api.yookassa.ru (~0.1 s). While the feature is on,
# ApiClient.get_session returns one shared session whose close() does nothing, and the link
# layer warms that connection when it shows the purchase screen, shortly before the click.
# Retries and timeouts stay exactly as the SDK configures them.

PREWARM_INTERVAL_SECONDS = 40.0

_session: dict[str, Any] = {"cls": None, "original": None, "shared": {}, "warmed_at": 0.0}
_session_lock = threading.Lock()


def _shared_get_session(original_func):
    @functools.wraps(original_func)
    def get_session(self, *args: object, **kwargs: object):
        key = (getattr(self, "max_attempts", None), getattr(self, "timeout", None))
        with _session_lock:
            shared = _session["shared"].get(key)
            if shared is None:
                shared = original_func(self, *args, **kwargs)
                shared.close = lambda: None  # the SDK closes the session after each call
                _session["shared"][key] = shared
        return shared

    setattr(get_session, MARK, True)
    return get_session


def install_session_reuse(api_client_cls) -> None:
    if _session["cls"] is not None:
        raise PatchError("session reuse already installed")
    raw = api_client_cls.__dict__.get("get_session")
    if not callable(raw) or getattr(raw, MARK, False):
        raise PatchError("ApiClient.get_session not patchable")
    api_client_cls.get_session = _shared_get_session(raw)
    _session["cls"] = api_client_cls
    _session["original"] = raw


def uninstall_session_reuse() -> None:
    cls, original = _session["cls"], _session["original"]
    if cls is not None and original is not None:
        cls.get_session = original
    with _session_lock:
        sessions = list(_session["shared"].values())
        _session["shared"].clear()
    for s in sessions:
        try:
            type(s).close(s)  # the real close; the instance attribute is the no-op
        except Exception:
            pass
    _session["cls"] = None
    _session["original"] = None
    _session["warmed_at"] = 0.0


def _prewarm_blocking() -> None:
    cls = _session["cls"]
    if cls is None:
        return
    try:
        client = cls()
        client.get_session().head(client.endpoint, timeout=5, verify=client.configuration.verify)
    except Exception as exc:
        logger.debug(f"[inhouse.yk] prewarm skipped: {type(exc).__name__}")


def prewarm() -> bool:
    """Open the shared connection in the background; at most once per PREWARM_INTERVAL_SECONDS."""
    if _session["cls"] is None:
        return False
    now = time.monotonic()
    with _session_lock:
        if now - _session["warmed_at"] < PREWARM_INTERVAL_SECONDS:
            return False
        _session["warmed_at"] = now
    threading.Thread(target=_prewarm_blocking, name="inhouse-yk-prewarm", daemon=True).start()
    return True
