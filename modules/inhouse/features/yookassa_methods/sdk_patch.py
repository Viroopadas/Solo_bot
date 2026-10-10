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
