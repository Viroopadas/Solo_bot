"""Adapter SoloBot 5.1.2 <-> v6.

Everything that differs between the versions goes through here, and every import of the
bot or of the closed YooKassa provider is lazy (inside a function), so importing our
module never imports closed code.

                    5.1.2                                   v6
YooKassa service    handlers.payments.yookassa.service      services.payments.yookassa.service
config              config                                  settings.config
buttons             handlers.buttons                        settings.buttons
temporary_data      keyed by tg_id                          keyed by users.id (tg_id kept too)
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, String, text


V512 = "5.1.2"
V6 = "v6"

SERVICE_MODULES = {
    V512: "handlers.payments.yookassa.service",
    V6: "services.payments.yookassa.service",
}
CONFIG_MODULES = {V512: "config", V6: "settings.config"}
BUTTON_MODULES = {V512: "handlers.buttons", V6: "settings.buttons"}
CREATE_FUNCTION = "create_and_store_yk_payment"
MODULE_NAME = "inhouse"


class CompatError(Exception):
    """A start condition is not met; the message is safe to log."""


def _try_import(name: str):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


_flavor_cache: dict[str, str] = {}


def flavor() -> str:
    """Which SoloBot this is, decided by where the YooKassa service lives."""
    if "flavor" in _flavor_cache:
        return _flavor_cache["flavor"]
    found = V512
    for name, module in ((V512, SERVICE_MODULES[V512]), (V6, SERVICE_MODULES[V6])):
        try:
            if importlib.util.find_spec(module) is not None:
                found = name
                break
        except (ImportError, ValueError):
            continue
    _flavor_cache["flavor"] = found
    return found


def load_config():
    module = _try_import(CONFIG_MODULES[flavor()])
    if module is None:
        raise CompatError(f"config module {CONFIG_MODULES[flavor()]} not importable")
    return module


def load_buttons():
    return _try_import(BUTTON_MODULES[flavor()])


def load_create_payment():
    """The stock create_and_store_yk_payment(session, tg_id, amount) -> dict | None."""
    name = SERVICE_MODULES[flavor()]
    try:
        module = importlib.import_module(name)
    except Exception as exc:  # closed .so: ImportError, or anything its import raises
        raise CompatError(f"YooKassa service {name} not importable ({type(exc).__name__})") from None
    func = getattr(module, CREATE_FUNCTION, None)
    if not callable(func):
        raise CompatError(f"{name}.{CREATE_FUNCTION} not found")
    try:
        params = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        params = None  # compiled functions may hide their signature; the call itself decides
    if params is not None:
        positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        has_var = any(p.kind == p.VAR_POSITIONAL for p in params)
        if len(positional) < 3 and not has_var:
            raise CompatError(f"{CREATE_FUNCTION} signature changed: {[p.name for p in params]}")
    return func


def load_yookassa_payment():
    try:
        from yookassa import Payment
    except Exception as exc:
        raise CompatError(f"yookassa SDK not importable ({type(exc).__name__})") from None
    return Payment


def load_yookassa_api_client():
    try:
        from yookassa.client import ApiClient
    except Exception as exc:
        raise CompatError(f"yookassa ApiClient not importable ({type(exc).__name__})") from None
    return ApiClient


def session_maker():
    module = _try_import("database")
    maker = getattr(module, "async_session_maker", None) if module else None
    if maker is None:
        module = _try_import("database.db")
        maker = getattr(module, "async_session_maker", None) if module else None
    if maker is None:
        raise CompatError("database.async_session_maker not found")
    return maker


def load_bot():
    module = _try_import("bot")
    bot = getattr(module, "bot", None) if module else None
    if bot is None:
        raise CompatError("bot object not available")
    return bot


def module_enabled() -> bool:
    """The admin-panel switch of the inhouse module. Unknown -> enabled (as run_hooks does)."""
    try:
        from utils.modules_manager import manager
    except Exception:
        return True
    try:
        return bool(manager.is_enabled(MODULE_NAME))
    except Exception:
        return True


def bot_link(cfg: Any, override: str) -> str:
    link = (override or getattr(cfg, "REDIRECT_LINK", "") or "").strip()
    if link.startswith(("https://t.me/", "tg://")):
        return link
    return ""


# --- temporary_data ------------------------------------------------------------------------


@dataclass(frozen=True)
class Checkout:
    data: dict
    updated_at: datetime  # naive UTC, as SoloBot writes it (datetime.utcnow())

    @property
    def checkout_ms(self) -> int:
        return to_ms(self.updated_at)


def to_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


_SQL_512 = "SELECT data, updated_at FROM temporary_data WHERE tg_id = :tg_id"
_SQL_V6 = (
    "SELECT data, updated_at FROM temporary_data "
    "WHERE tg_id = :tg_id OR user_id IN (SELECT id FROM users WHERE tg_id = :tg_id) "
    "ORDER BY updated_at DESC LIMIT 1"
)


async def read_checkout(session, tg_id: int) -> Checkout | None:
    """The saved purchase of this person and its save time (the checkout number)."""
    sql = _SQL_V6 if flavor() == V6 else _SQL_512
    stmt = text(sql).columns(data=JSON, updated_at=DateTime)
    row = (await session.execute(stmt, {"tg_id": int(tg_id)})).first()
    if row is None:
        return None
    data, updated_at = row[0], row[1]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if isinstance(updated_at, str):
        updated_at = datetime.fromisoformat(updated_at)
    if not isinstance(data, dict) or not isinstance(updated_at, datetime):
        return None
    return Checkout(data=data, updated_at=updated_at)


async def payment_status(session, payment_id: str) -> str | None:
    """Status of the payments row written by the stock provider for this YooKassa id."""
    stmt = text("SELECT status FROM payments WHERE payment_id = :pid ORDER BY id DESC LIMIT 1").columns(status=String)
    row = (await session.execute(stmt, {"pid": str(payment_id)})).first()
    return None if row is None else (str(row[0]) if row[0] is not None else None)


# --- button labels -------------------------------------------------------------------------


def set_button_labels(buttons_module, labels: dict[str, str]) -> list[str]:
    """Define missing button-module attributes (e.g. YOOKASSA_SBP) so stock code that does
    getattr(btn, provider, provider) shows a human label. Returns the names it set."""
    if buttons_module is None:
        return []
    added: list[str] = []
    for name, value in labels.items():
        if not hasattr(buttons_module, name):
            setattr(buttons_module, name, value)
            added.append(name)
    return added


def unset_button_labels(buttons_module, names: list[str]) -> None:
    if buttons_module is None:
        return
    for name in names:
        if hasattr(buttons_module, name):
            delattr(buttons_module, name)
