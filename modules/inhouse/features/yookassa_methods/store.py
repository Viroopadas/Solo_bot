"""Table inhouse_yk_links: which YooKassa payment was created by which link.

One row per payment created through a link button: person, checkout number (save time of
the purchase in temporary_data, ms), issue time of the link (ms), method, YooKassa id,
YooKassa URL, amount, creation time. The status of a payment is read from `payments` by
its id (the stock provider and its webhook keep it). The table is created by the module
(CREATE TABLE IF NOT EXISTS), no core migration. Rows older than STORE_RETENTION_DAYS
are deleted.

The lock "checkout + method" is in memory: the bot is one process (bot.service).
"""
# ruff: noqa: S608  (the only interpolated name is the TABLE constant; values are bound parameters)

from __future__ import annotations

import asyncio
import time

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import BigInteger, Integer, String, text


TABLE = "inhouse_yk_links"

PAID_STATUSES = frozenset({"success", "succeeded"})
REUSABLE_STATUSES = frozenset({"pending", "waiting_for_capture"})


@dataclass(frozen=True)
class LinkRow:
    id: int
    tg_id: int
    checkout_ms: int
    issued_ms: int
    method: str
    payment_id: str
    confirmation_url: str
    amount: int
    created_at: int  # Unix seconds


def _ddl(dialect: str) -> list[str]:
    pk = "id INTEGER PRIMARY KEY AUTOINCREMENT" if dialect == "sqlite" else "id BIGSERIAL PRIMARY KEY"
    return [
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            {pk},
            tg_id BIGINT NOT NULL,
            checkout_ms BIGINT NOT NULL,
            issued_ms BIGINT NOT NULL,
            method VARCHAR(16) NOT NULL,
            payment_id VARCHAR(128) NOT NULL,
            confirmation_url TEXT NOT NULL,
            amount INTEGER NOT NULL,
            created_at BIGINT NOT NULL
        )
        """,
        f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_checkout ON {TABLE} (tg_id, checkout_ms, method)",
        f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_issued ON {TABLE} (tg_id, issued_ms)",
        f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_created ON {TABLE} (created_at)",
    ]


async def ensure_table(session) -> None:
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    for statement in _ddl(dialect):
        await session.execute(text(statement))


async def purge_old(session, retention_days: int, now_s: int | None = None) -> int:
    cutoff = int(now_s if now_s is not None else time.time()) - int(retention_days) * 86400
    result = await session.execute(text(f"DELETE FROM {TABLE} WHERE created_at < :cutoff"), {"cutoff": cutoff})
    return int(result.rowcount or 0)


_COLUMNS = "id, tg_id, checkout_ms, issued_ms, method, payment_id, confirmation_url, amount, created_at"
_TYPES = {
    "id": Integer,
    "tg_id": BigInteger,
    "checkout_ms": BigInteger,
    "issued_ms": BigInteger,
    "method": String,
    "payment_id": String,
    "confirmation_url": String,
    "amount": Integer,
    "created_at": BigInteger,
}


def _row(r) -> LinkRow:
    return LinkRow(*[r[i] for i in range(9)])


async def rows_for_checkout(session, tg_id: int, checkout_ms: int) -> list[LinkRow]:
    """All rows of this checkout, newest first (any method)."""
    stmt = text(f"SELECT {_COLUMNS} FROM {TABLE} WHERE tg_id = :tg AND checkout_ms = :co ORDER BY id DESC").columns(
        **_TYPES
    )
    result = await session.execute(stmt, {"tg": int(tg_id), "co": int(checkout_ms)})
    return [_row(r) for r in result.all()]


async def rows_for_link(session, tg_id: int, issued_ms: int) -> list[LinkRow]:
    """All rows created through links of one message (one issue time), newest first."""
    stmt = text(f"SELECT {_COLUMNS} FROM {TABLE} WHERE tg_id = :tg AND issued_ms = :iss ORDER BY id DESC").columns(
        **_TYPES
    )
    result = await session.execute(stmt, {"tg": int(tg_id), "iss": int(issued_ms)})
    return [_row(r) for r in result.all()]


async def count_recent(session, tg_id: int, window_s: int = 3600, now_s: int | None = None) -> int:
    since = int(now_s if now_s is not None else time.time()) - int(window_s)
    stmt = text(f"SELECT COUNT(*) AS n FROM {TABLE} WHERE tg_id = :tg AND created_at >= :since").columns(n=Integer)
    result = await session.execute(stmt, {"tg": int(tg_id), "since": since})
    return int(result.scalar() or 0)


async def insert(
    session,
    *,
    tg_id: int,
    checkout_ms: int,
    issued_ms: int,
    method: str,
    payment_id: str,
    confirmation_url: str,
    amount: int,
    now_s: int | None = None,
) -> None:
    """Adds the row; does not commit (the endpoint commits together with the payments row)."""
    await session.execute(
        text(
            f"INSERT INTO {TABLE} (tg_id, checkout_ms, issued_ms, method, payment_id, confirmation_url, amount, created_at) "
            "VALUES (:tg, :co, :iss, :m, :pid, :url, :amt, :ts)"
        ),
        {
            "tg": int(tg_id),
            "co": int(checkout_ms),
            "iss": int(issued_ms),
            "m": method,
            "pid": str(payment_id),
            "url": str(confirmation_url),
            "amt": int(amount),
            "ts": int(now_s if now_s is not None else time.time()),
        },
    )


class KeyedLocks:
    """asyncio locks per key; an entry lives only while someone holds or waits for it."""

    def __init__(self) -> None:
        self._locks: dict[tuple, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: tuple) -> AsyncIterator[None]:
        lock, users = self._locks.get(key, (None, 0))
        if lock is None:
            lock = asyncio.Lock()
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock_now, users_now = self._locks[key]
            if users_now <= 1:
                self._locks.pop(key, None)
            else:
                self._locks[key] = (lock_now, users_now - 1)

    def __len__(self) -> int:
        return len(self._locks)


LOCKS = KeyedLocks()
