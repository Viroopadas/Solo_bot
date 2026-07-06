from __future__ import annotations

import asyncio
import json

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from logger import logger


SCHEMA_VERSION = 1

_storage_lock = asyncio.Lock()
_storage_ready = False


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _row(row: Any) -> dict | None:
    return dict(row) if row is not None else None


async def ensure_storage(session: AsyncSession) -> None:
    global _storage_ready
    if _storage_ready:
        return

    async with _storage_lock:
        if _storage_ready:
            return

        await _create_v1(session)

        version = await get_schema_version(session)
        if version is None:
            await session.execute(
                text(
                    """
                    INSERT INTO module_polls_meta (key, schema_version, applied_at)
                    VALUES ('schema', :schema_version, now())
                    ON CONFLICT (key) DO NOTHING
                    """
                ),
                {"schema_version": SCHEMA_VERSION},
            )
        elif version > SCHEMA_VERSION:
            raise RuntimeError(
                f"[Polls] Версия схемы хранилища {version} новее версии модуля {SCHEMA_VERSION}"
            )
        elif version < SCHEMA_VERSION:
            await _apply_migrations(session, version)

        _storage_ready = True
        logger.info(f"[Polls] Хранилище готово, schema_version={SCHEMA_VERSION}")


async def _create_v1(session: AsyncSession) -> None:
    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS module_polls_meta (
                key text PRIMARY KEY,
                schema_version integer NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    )
    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS module_polls (
                id bigserial PRIMARY KEY,
                poll_id text UNIQUE NULL,
                chat_id bigint NULL,
                message_id bigint NULL,
                created_by_tg_id bigint NULL,
                question text NOT NULL,
                options_json jsonb NOT NULL,
                allows_multiple_answers boolean NOT NULL DEFAULT false,
                is_anonymous boolean NOT NULL DEFAULT false,
                status text NOT NULL DEFAULT 'pending',
                created_at timestamptz NOT NULL DEFAULT now(),
                closed_at timestamptz NULL
            )
            """
        )
    )
    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS module_poll_answers (
                poll_id text NOT NULL REFERENCES module_polls(poll_id) ON DELETE CASCADE,
                tg_id bigint NOT NULL,
                option_ids jsonb NOT NULL,
                is_withdrawn boolean NOT NULL DEFAULT false,
                answered_at timestamptz NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (poll_id, tg_id)
            )
            """
        )
    )
    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS module_poll_events (
                id bigserial PRIMARY KEY,
                poll_id text NOT NULL,
                tg_id bigint NULL,
                raw_option_ids jsonb NOT NULL,
                event_type text NOT NULL,
                received_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    )
    await session.execute(text("CREATE INDEX IF NOT EXISTS ix_module_poll_answers_poll_id ON module_poll_answers (poll_id)"))
    await session.execute(text("CREATE INDEX IF NOT EXISTS ix_module_poll_answers_tg_id ON module_poll_answers (tg_id)"))
    await session.execute(text("CREATE INDEX IF NOT EXISTS ix_module_polls_status ON module_polls (status)"))
    await session.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_module_polls_poll_id_not_null
            ON module_polls (poll_id)
            WHERE poll_id IS NOT NULL
            """
        )
    )


async def _apply_migrations(session: AsyncSession, current_version: int) -> None:
    if current_version < SCHEMA_VERSION:
        await session.execute(
            text(
                """
                UPDATE module_polls_meta
                SET schema_version = :schema_version, applied_at = now()
                WHERE key = 'schema'
                """
            ),
            {"schema_version": SCHEMA_VERSION},
        )


async def get_schema_version(session: AsyncSession) -> int | None:
    result = await session.execute(
        text("SELECT schema_version FROM module_polls_meta WHERE key = 'schema'")
    )
    version = result.scalar_one_or_none()
    return int(version) if version is not None else None


async def create_poll_record(
    session: AsyncSession,
    *,
    created_by_tg_id: int | None,
    question: str,
    options: Sequence[str],
    allows_multiple_answers: bool,
    is_anonymous: bool,
) -> dict:
    await ensure_storage(session)
    result = await session.execute(
        text(
            """
            INSERT INTO module_polls (
                created_by_tg_id,
                question,
                options_json,
                allows_multiple_answers,
                is_anonymous,
                status
            )
            VALUES (
                :created_by_tg_id,
                :question,
                CAST(:options_json AS jsonb),
                :allows_multiple_answers,
                :is_anonymous,
                'pending'
            )
            RETURNING *
            """
        ),
        {
            "created_by_tg_id": created_by_tg_id,
            "question": question,
            "options_json": _json(list(options)),
            "allows_multiple_answers": allows_multiple_answers,
            "is_anonymous": is_anonymous,
        },
    )
    return dict(result.mappings().one())


async def activate_poll_record(
    session: AsyncSession,
    *,
    record_id: int,
    poll_id: str,
    chat_id: int,
    message_id: int,
) -> dict | None:
    await ensure_storage(session)
    result = await session.execute(
        text(
            """
            UPDATE module_polls
            SET poll_id = :poll_id,
                chat_id = :chat_id,
                message_id = :message_id,
                status = 'active'
            WHERE id = :record_id
            RETURNING *
            """
        ),
        {
            "record_id": record_id,
            "poll_id": poll_id,
            "chat_id": chat_id,
            "message_id": message_id,
        },
    )
    return _row(result.mappings().one_or_none())


async def mark_poll_failed(session: AsyncSession, *, record_id: int) -> dict | None:
    await ensure_storage(session)
    result = await session.execute(
        text(
            """
            UPDATE module_polls
            SET status = 'failed'
            WHERE id = :record_id
            RETURNING *
            """
        ),
        {"record_id": record_id},
    )
    return _row(result.mappings().one_or_none())


async def close_poll_record(session: AsyncSession, *, record_id: int) -> dict | None:
    await ensure_storage(session)
    result = await session.execute(
        text(
            """
            UPDATE module_polls
            SET status = 'closed',
                closed_at = now()
            WHERE id = :record_id
            RETURNING *
            """
        ),
        {"record_id": record_id},
    )
    return _row(result.mappings().one_or_none())


async def get_poll_by_id(session: AsyncSession, record_id: int) -> dict | None:
    await ensure_storage(session)
    result = await session.execute(
        text("SELECT * FROM module_polls WHERE id = :record_id"),
        {"record_id": record_id},
    )
    return _row(result.mappings().one_or_none())


async def get_poll_by_poll_id(session: AsyncSession, poll_id: str) -> dict | None:
    await ensure_storage(session)
    result = await session.execute(
        text("SELECT * FROM module_polls WHERE poll_id = :poll_id"),
        {"poll_id": poll_id},
    )
    return _row(result.mappings().one_or_none())


async def list_recent_polls(session: AsyncSession, limit: int) -> list[dict]:
    await ensure_storage(session)
    result = await session.execute(
        text(
            """
            SELECT *
            FROM module_polls
            ORDER BY id DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]


async def insert_event(
    session: AsyncSession,
    *,
    poll_id: str,
    tg_id: int | None,
    raw_option_ids: Sequence[int],
    event_type: str,
) -> None:
    await ensure_storage(session)
    await session.execute(
        text(
            """
            INSERT INTO module_poll_events (poll_id, tg_id, raw_option_ids, event_type)
            VALUES (:poll_id, :tg_id, CAST(:raw_option_ids AS jsonb), :event_type)
            """
        ),
        {
            "poll_id": poll_id,
            "tg_id": tg_id,
            "raw_option_ids": _json(list(raw_option_ids)),
            "event_type": event_type,
        },
    )


async def upsert_poll_answer(
    session: AsyncSession,
    *,
    poll_id: str,
    tg_id: int,
    option_ids: Sequence[int],
    answered_at: datetime | None = None,
) -> dict:
    await ensure_storage(session)
    if answered_at is None:
        answered_at = datetime.now(timezone.utc)
    is_withdrawn = len(option_ids) == 0

    result = await session.execute(
        text(
            """
            INSERT INTO module_poll_answers (
                poll_id,
                tg_id,
                option_ids,
                is_withdrawn,
                answered_at,
                updated_at
            )
            VALUES (
                :poll_id,
                :tg_id,
                CAST(:option_ids AS jsonb),
                :is_withdrawn,
                :answered_at,
                now()
            )
            ON CONFLICT (poll_id, tg_id) DO UPDATE
            SET option_ids = EXCLUDED.option_ids,
                is_withdrawn = EXCLUDED.is_withdrawn,
                answered_at = EXCLUDED.answered_at,
                updated_at = now()
            WHERE module_poll_answers.answered_at IS NULL
               OR EXCLUDED.answered_at >= module_poll_answers.answered_at
            RETURNING *
            """
        ),
        {
            "poll_id": poll_id,
            "tg_id": tg_id,
            "option_ids": _json(list(option_ids)),
            "is_withdrawn": is_withdrawn,
            "answered_at": answered_at,
        },
    )
    row = result.mappings().one_or_none()
    if row is not None:
        return dict(row)

    existing = await get_poll_answer(session, poll_id=poll_id, tg_id=tg_id)
    if existing is None:
        raise RuntimeError(f"[Polls] Upsert ответа не вернул строку: poll_id={poll_id!r}, tg_id={tg_id}")
    return existing


async def get_poll_answer(session: AsyncSession, *, poll_id: str, tg_id: int) -> dict | None:
    await ensure_storage(session)
    result = await session.execute(
        text(
            """
            SELECT *
            FROM module_poll_answers
            WHERE poll_id = :poll_id AND tg_id = :tg_id
            """
        ),
        {"poll_id": poll_id, "tg_id": tg_id},
    )
    return _row(result.mappings().one_or_none())


async def list_poll_answers(session: AsyncSession, *, poll_id: str) -> list[dict]:
    await ensure_storage(session)
    result = await session.execute(
        text(
            """
            SELECT *
            FROM module_poll_answers
            WHERE poll_id = :poll_id
            ORDER BY answered_at ASC, tg_id ASC
            """
        ),
        {"poll_id": poll_id},
    )
    return [dict(row) for row in result.mappings().all()]
