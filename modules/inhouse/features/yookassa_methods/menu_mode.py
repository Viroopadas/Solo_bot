"""Menu mode: what NEW payment menus show (spec 2026-10-10 pay-site watch, section 3.1).

- "links": the three link buttons (SBP, card, "SberPay and other") through PUBLIC_BASE_URL;
- "fallback": the stock YooKassa button; the bot creates the payment itself and gives a
  yoomoney.ru link, the Russian pay site is not needed.

Only the menu changes. The /inhouse/pay handler, the YooKassa SDK wrapper, the connection
pools, the USE_NEW_PAYMENT_FLOW entries and the button labels stay installed, so a payment
already being opened finishes with its chosen method and old links keep working.

The watch (watch.py) is the only owner of transitions after start; feature start restores
the saved mode before the link layer is installed, so the very first menu is right.
"""

from __future__ import annotations

import threading


LINKS = "links"
FALLBACK = "fallback"
MODES: tuple[str, ...] = (LINKS, FALLBACK)

_lock = threading.Lock()
_mode: list[str] = [LINKS]


def current() -> str:
    return _mode[0]


def is_fallback() -> bool:
    return _mode[0] == FALLBACK


def set_mode(mode: str) -> bool:
    """Set the mode; returns True when it changed. Callers check the owner's permission."""
    if mode not in MODES:
        raise ValueError(f"unknown menu mode {mode!r}")
    with _lock:
        changed = _mode[0] != mode
        _mode[0] = mode
        return changed
