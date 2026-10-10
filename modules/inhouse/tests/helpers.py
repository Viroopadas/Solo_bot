"""Fakes and helpers for the inhouse tests. Importing this installs the fakes."""

from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid

from datetime import datetime, timedelta


class FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _add(self, level: str, msg) -> None:
        self.records.append((level, str(msg)))

    def info(self, msg, *a, **k):
        self._add("info", msg)

    def warning(self, msg, *a, **k):
        self._add("warning", msg)

    def error(self, msg, *a, **k):
        self._add("error", msg)

    def exception(self, msg, *a, **k):
        self._add("error", msg)

    def debug(self, msg, *a, **k):
        self._add("debug", msg)

    def text(self) -> str:
        return "\n".join(m for _, m in self.records)

    def at(self, level: str) -> list[str]:
        return [m for lvl, m in self.records if lvl == level]


FAKE_LOGGER = FakeLogger()
REGISTERED_HOOKS: list[tuple[str, object]] = []


def _install_fakes() -> None:
    logger_mod = types.ModuleType("logger")
    logger_mod.logger = FAKE_LOGGER
    sys.modules["logger"] = logger_mod

    hooks_pkg = types.ModuleType("hooks")
    hooks_pkg.__path__ = []
    hooks_mod = types.ModuleType("hooks.hooks")

    def register_hook(name, func=None):
        if func is None:

            def deco(f):
                REGISTERED_HOOKS.append((name, f))
                return f

            return deco
        REGISTERED_HOOKS.append((name, func))
        return func

    hooks_mod.register_hook = register_hook
    hooks_pkg.hooks = hooks_mod
    sys.modules["hooks"] = hooks_pkg
    sys.modules["hooks.hooks"] = hooks_mod

    # Guard: the real config.py must never load in tests.
    cfg = types.ModuleType("config")
    cfg.__inhouse_fake__ = True
    sys.modules["config"] = cfg


_install_fakes()

from modules.inhouse.features.yookassa_methods import settings as yk_settings  # noqa: E402
from modules.inhouse.features.yookassa_methods import state  # noqa: E402


SECRET = bytes(range(32))
CONFIRM_URL = "https://yoomoney.ru/checkout/payments/v2/contract?orderId={pid}"


async def save_checkout(maker, tg_id: int, data: dict, updated_at: datetime | None = None) -> datetime:
    """Mimics database.temporary_data.create_temporary_data (naive UTC updated_at, commit)."""
    from sqlalchemy import text

    updated_at = updated_at or datetime.utcnow()
    async with maker() as session:
        await session.execute(text("DELETE FROM temporary_data WHERE tg_id = :tg"), {"tg": tg_id})
        await session.execute(
            text("INSERT INTO temporary_data (tg_id, state, data, updated_at) VALUES (:tg, :st, :d, :u)"),
            {"tg": tg_id, "st": "waiting_for_payment", "d": json.dumps(data), "u": updated_at},
        )
        await session.commit()
    return updated_at


async def clear_checkout(maker, tg_id: int) -> None:
    from sqlalchemy import text

    async with maker() as session:
        await session.execute(text("DELETE FROM temporary_data WHERE tg_id = :tg"), {"tg": tg_id})
        await session.commit()


async def set_payment_status(maker, payment_id: str, status: str) -> None:
    from sqlalchemy import text

    async with maker() as session:
        await session.execute(
            text("UPDATE payments SET status = :s WHERE payment_id = :p"), {"s": status, "p": payment_id}
        )
        await session.commit()


async def count_rows(maker, table: str) -> int:
    from sqlalchemy import text

    async with maker() as session:
        return int((await session.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar())


class FakeYooKassaApi:
    """Replaces yookassa ApiClient.request: records bodies, answers like YooKassa."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.idempotency_keys: list[str] = []

    def __call__(self, method="", path="", query_params=None, headers=None, body=None):
        from yookassa.domain.common import RequestObject

        if isinstance(body, RequestObject):
            body.validate()
            body = dict(body)
        self.bodies.append(body)
        self.idempotency_keys.append((headers or {}).get("Idempotence-Key"))
        pid = f"2f{len(self.bodies):06d}-000f-5000-8000-1a2b3c4d5e6f"
        return {
            "id": pid,
            "status": "pending",
            "paid": False,
            "amount": body.get("amount"),
            "confirmation": {"type": "redirect", "confirmation_url": CONFIRM_URL.format(pid=pid)},
            "created_at": "2026-10-10T10:00:00.000Z",
            "test": True,
            "refundable": False,
            "recipient": {"account_id": "1", "gateway_id": "1"},
        }


def stock_payload(tg_id: int, amount: int) -> dict:
    return {
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/example_bot"},
        "capture": True,
        "description": "Пополнение баланса",
        "metadata": {"user_id": str(tg_id)},
        "receipt": {
            "customer": {"email": "user@example.com"},
            "items": [
                {
                    "description": "Пополнение баланса",
                    "quantity": "1.00",
                    "amount": {"value": f"{amount}.00", "currency": "RUB"},
                    "vat_code": 1,
                }
            ],
        },
    }


class FakeStockService:
    """Stands in for the closed create_and_store_yk_payment(session, tg_id, amount):
    builds a request like the provider, calls Payment.create in a thread with a uuid4
    idempotence key, adds a pending payments row WITHOUT commit, returns a dict."""

    def __init__(
        self,
        delay: float = 0.0,
        fail: Exception | None = None,
        return_none: bool = False,
        payload_extra: dict | None = None,
    ) -> None:
        self.calls = 0
        self.delay = delay
        self.fail = fail
        self.return_none = return_none
        self.payload_extra = payload_extra or {}

    async def __call__(self, session, tg_id: int, amount: int):
        from sqlalchemy import text
        from yookassa import Payment

        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        payload = stock_payload(tg_id, amount)
        payload.update(self.payload_extra)
        response = await asyncio.to_thread(Payment.create, payload, uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO payments (tg_id, amount, payment_system, status, payment_id) "
                "VALUES (:t, :a, 'YOOKASSA', 'pending', :p)"
            ),
            {"t": tg_id, "a": amount, "p": response.id},
        )
        if self.return_none:
            return None
        return {"id": response.id, "confirmation_url": response.confirmation.confirmation_url}


def make_deps(maker, create_payment, **overrides) -> state.Deps:
    deps = state.Deps(
        settings=yk_settings,
        secret=SECRET,
        session_maker=maker,
        create_payment=create_payment,
        bot_link="https://t.me/example_bot",
        standard_yookassa_label="💳 ЮКасса: быстрая оплата",
    )
    for k, v in overrides.items():
        setattr(deps, k, v)
    return deps


def activate(deps: state.Deps) -> None:
    state.STATE.deps = deps
    state.STATE.active = True


def past(minutes: float) -> datetime:
    return datetime.utcnow() - timedelta(minutes=minutes)
