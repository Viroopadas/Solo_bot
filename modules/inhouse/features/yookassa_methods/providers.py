"""providers_config hook: two more cash desks over the stock YooKassa.

YOOKASSA_SBP and YOOKASSA_CARD get the stock YooKassa fields: module="yookassa" (so the
fallback path finds handlers.payments.yookassa.handlers), fast="process_custom_amount_input",
value="pay_yookassa" (the hidden top-up menu reads cfg["value"]), currency="RUB".
Order -30 / -20; the stock YOOKASSA gets -10. Added only when YooKassa is enabled.
The new names are appended to USE_NEW_PAYMENT_FLOW in place, without duplicates.
"""

from __future__ import annotations

from typing import Any

from . import state


SBP = "YOOKASSA_SBP"
CARD = "YOOKASSA_CARD"
STOCK = "YOOKASSA"
NEW_PROVIDERS: tuple[str, ...] = (SBP, CARD)
PROVIDER_METHODS: dict[str, str] = {SBP: "sbp", CARD: "card", STOCK: "other"}

DEFAULTS = {
    "module": "yookassa",
    "fast": "process_custom_amount_input",
    "value": "pay_yookassa",
    "currency": "RUB",
}


def providers_patch(providers: dict[str, dict[str, Any]] | None, flags: dict[str, bool] | None) -> dict:
    deps = state.current()
    if deps is None:
        return {}
    providers = providers or {}
    stock = providers.get(STOCK)
    if not stock:
        return {}
    enabled = bool((flags or {}).get(STOCK)) or bool(stock.get("enabled"))
    if not enabled:
        return {}
    s = deps.settings
    common = {
        "module": DEFAULTS["module"],
        "fast": stock.get("fast") or DEFAULTS["fast"],
        "value": stock.get("value") or DEFAULTS["value"],
        "currency": DEFAULTS["currency"],
        "enabled": True,
    }
    return {
        SBP: {**common, "order": s.ORDER_SBP},
        CARD: {**common, "order": s.ORDER_CARD},
        STOCK: {"order": s.ORDER_OTHER},
    }


def extend_new_flow(flow: list) -> list[str]:
    """Append YOOKASSA_SBP / YOOKASSA_CARD to USE_NEW_PAYMENT_FLOW in place; returns what was added."""
    if not isinstance(flow, list):
        raise TypeError("USE_NEW_PAYMENT_FLOW is not a list")
    present = {str(p).upper() for p in flow}
    added: list[str] = []
    for name in NEW_PROVIDERS:
        if name not in present:
            flow.append(name)
            present.add(name)
            added.append(name)
    return added


def revert_new_flow(flow: list, added: list[str]) -> None:
    for name in added:
        while name in flow:
            flow.remove(name)
