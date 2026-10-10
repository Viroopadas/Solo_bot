"""Spec 4.3/5: v6 keeps Telegram identity but keys temporary data by users.id.

Schema source: v6 ca1b8ae database/models/users.py:64-71;
write/commit contract: database/temporary_data.py:12-31.
"""

import json
from datetime import datetime

from sqlalchemy import text

from modules.inhouse.features.yookassa_methods import compat


async def test_v6_checkout_by_user_when_legacy_tg_id_is_null(db, monkeypatch):
    monkeypatch.setitem(compat._flavor_cache, "flavor", compat.V6)
    when = datetime(2026, 10, 10, 10, 0)
    async with db() as session:
        await session.execute(text("DROP TABLE temporary_data"))
        await session.execute(text("CREATE TABLE users (id BIGINT PRIMARY KEY, tg_id BIGINT UNIQUE)"))
        await session.execute(text("CREATE TABLE temporary_data (user_id BIGINT PRIMARY KEY, tg_id BIGINT, data JSON, updated_at TIMESTAMP)"))
        await session.execute(text("INSERT INTO users VALUES (7, 555000111)"))
        await session.execute(text("INSERT INTO temporary_data VALUES (7, NULL, :d, :ts)"),
                              {"d": json.dumps({"required_amount": 199}), "ts": when})
        await session.commit()
    async with db() as session:
        checkout = await compat.read_checkout(session, 555000111)
        assert checkout.data == {"required_amount": 199}
        assert checkout.updated_at == when
        assert await compat.read_checkout(session, 7) is None  # internal id is not Telegram id


async def test_payment_status_reads_committed_stock_row(db):
    async with db() as session:
        await session.execute(text("INSERT INTO payments (payment_id, status) VALUES ('p-1', 'success')"))
        await session.commit()
    async with db() as session:
        assert await compat.payment_status(session, "p-1") == "success"
        assert await compat.payment_status(session, "absent") is None
