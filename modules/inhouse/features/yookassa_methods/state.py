"""Runtime state of the feature, shared by all its parts.

`feature.start()` fills Deps only after every start condition passed and every part is
installed. Each part asks `current()` on every call: it returns Deps only while the
feature is active AND the inhouse module is enabled in the admin panel.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from . import compat


@dataclass
class Deps:
    settings: ModuleType
    secret: bytes
    session_maker: Callable[[], Any]
    create_payment: Callable[..., Awaitable[Any]]
    read_checkout: Callable[..., Awaitable[Any]] = compat.read_checkout
    payment_status: Callable[..., Awaitable[Any]] = compat.payment_status
    bot_link: str = ""
    standard_yookassa_label: str = "ЮKassa"
    last_purge: float = 0.0


@dataclass
class State:
    active: bool = False
    starting: bool = False
    deps: Deps | None = None
    reason: str = "not started"
    undo: list[tuple[str, Callable[[], None]]] = field(default_factory=list)


STATE = State()

# Seam for tests; production asks the module manager.
module_enabled: Callable[[], bool] = compat.module_enabled


def current() -> Deps | None:
    if not STATE.active or STATE.deps is None:
        return None
    if not module_enabled():
        return None
    return STATE.deps


def is_active() -> bool:
    return current() is not None
