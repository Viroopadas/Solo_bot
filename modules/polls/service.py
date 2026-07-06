from __future__ import annotations

import asyncio

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from logger import logger

from . import storage
from .settings import (
    DEFAULT_ANONYMOUS,
    MAX_POLL_OPTIONS,
    MAX_POLL_OPTION_LEN,
    MAX_POLL_QUESTION_LEN,
    MIN_POLL_OPTIONS,
    RECENT_POLLS_LIMIT,
    STORE_EVENTS,
    UNKNOWN_POLL_RETRY_SEC,
    UNKNOWN_POLL_RETRY_STEP_SEC,
)


@dataclass(frozen=True)
class SavePollAnswerResult:
    status: str
    poll_id: str
    tg_id: int | None
    option_ids: list[int]
    is_withdrawn: bool
    row: dict | None = None
    poll: dict | None = None


def _normalize_option_ids(option_ids: Sequence[int] | None) -> list[int]:
    normalized: list[int] = []
    for value in option_ids or []:
        normalized.append(int(value))
    return normalized


def _normalize_options(options: Sequence[str]) -> list[str]:
    return [str(option).strip() for option in options if str(option).strip()]


def validate_poll_payload(question: str, options: Sequence[str]) -> tuple[str, list[str]]:
    question = (question or "").strip()
    options = _normalize_options(options)

    if not question:
        raise ValueError("Вопрос не должен быть пустым.")
    if len(question) > MAX_POLL_QUESTION_LEN:
        raise ValueError(f"Вопрос длиннее {MAX_POLL_QUESTION_LEN} символов.")
    if len(options) < MIN_POLL_OPTIONS:
        raise ValueError(f"Нужно минимум {MIN_POLL_OPTIONS} варианта.")
    if len(options) > MAX_POLL_OPTIONS:
        raise ValueError(f"Можно указать максимум {MAX_POLL_OPTIONS} вариантов.")

    too_long = [option for option in options if len(option) > MAX_POLL_OPTION_LEN]
    if too_long:
        raise ValueError(f"Вариант ответа длиннее {MAX_POLL_OPTION_LEN} символов: {too_long[0]!r}")

    return question, options


async def create_poll_record(
    session: AsyncSession,
    *,
    created_by_tg_id: int | None,
    question: str,
    options: Sequence[str],
    allows_multiple_answers: bool = False,
    is_anonymous: bool = DEFAULT_ANONYMOUS,
) -> dict:
    question, options = validate_poll_payload(question, options)
    return await storage.create_poll_record(
        session,
        created_by_tg_id=created_by_tg_id,
        question=question,
        options=options,
        allows_multiple_answers=allows_multiple_answers,
        is_anonymous=is_anonymous,
    )


async def activate_poll_record(
    session: AsyncSession,
    *,
    record_id: int,
    poll_id: str,
    chat_id: int,
    message_id: int,
) -> dict | None:
    return await storage.activate_poll_record(
        session,
        record_id=record_id,
        poll_id=poll_id,
        chat_id=chat_id,
        message_id=message_id,
    )


async def mark_poll_failed(session: AsyncSession, *, record_id: int) -> dict | None:
    return await storage.mark_poll_failed(session, record_id=record_id)


async def close_poll_record(session: AsyncSession, *, record_id: int) -> dict | None:
    return await storage.close_poll_record(session, record_id=record_id)


async def get_poll_by_selector(session: AsyncSession, selector: str) -> dict | None:
    selector = (selector or "").strip()
    if not selector:
        return None

    poll = None
    if selector.isdigit():
        poll = await storage.get_poll_by_id(session, int(selector))
        if poll:
            return poll

    return await storage.get_poll_by_poll_id(session, selector)


async def list_recent_polls(session: AsyncSession, limit: int = RECENT_POLLS_LIMIT) -> list[dict]:
    return await storage.list_recent_polls(session, limit=limit)


async def _wait_for_poll_ready(session: AsyncSession, poll_id: str) -> dict | None:
    deadline = asyncio.get_running_loop().time() + UNKNOWN_POLL_RETRY_SEC
    last_poll: dict | None = None

    while True:
        poll = await storage.get_poll_by_poll_id(session, poll_id)
        if poll:
            last_poll = poll
            if poll.get("status") != "pending":
                return poll

        if asyncio.get_running_loop().time() >= deadline:
            return last_poll

        await asyncio.sleep(UNKNOWN_POLL_RETRY_STEP_SEC)


async def _store_event(
    session: AsyncSession,
    *,
    poll_id: str,
    tg_id: int | None,
    option_ids: Sequence[int],
    event_type: str,
) -> None:
    if not STORE_EVENTS:
        return
    await storage.insert_event(
        session,
        poll_id=poll_id,
        tg_id=tg_id,
        raw_option_ids=option_ids,
        event_type=event_type,
    )


async def save_poll_answer(
    session: AsyncSession,
    *,
    poll_id: str,
    tg_id: int | None,
    option_ids: Sequence[int] | None,
) -> SavePollAnswerResult:
    await storage.ensure_storage(session)
    normalized_option_ids = _normalize_option_ids(option_ids)
    is_withdrawn = len(normalized_option_ids) == 0

    if tg_id is None:
        await _store_event(
            session,
            poll_id=poll_id,
            tg_id=None,
            option_ids=normalized_option_ids,
            event_type="missing_user",
        )
        logger.warning(f"[Polls] Ответ без user: poll_id={poll_id!r}, option_ids={normalized_option_ids}")
        return SavePollAnswerResult(
            status="missing_user",
            poll_id=poll_id,
            tg_id=None,
            option_ids=normalized_option_ids,
            is_withdrawn=is_withdrawn,
        )

    poll = await _wait_for_poll_ready(session, poll_id)
    if poll is None:
        await _store_event(
            session,
            poll_id=poll_id,
            tg_id=tg_id,
            option_ids=normalized_option_ids,
            event_type="unknown_poll_id",
        )
        logger.warning(f"[Polls] Неизвестный poll_id={poll_id!r}, tg_id={tg_id}")
        return SavePollAnswerResult(
            status="unknown_poll_id",
            poll_id=poll_id,
            tg_id=tg_id,
            option_ids=normalized_option_ids,
            is_withdrawn=is_withdrawn,
        )

    status = str(poll.get("status") or "")
    if status == "closed":
        await _store_event(
            session,
            poll_id=poll_id,
            tg_id=tg_id,
            option_ids=normalized_option_ids,
            event_type="answer_after_close",
        )
        logger.info(f"[Polls] Ответ после закрытия: poll_id={poll_id!r}, tg_id={tg_id}")
        return SavePollAnswerResult(
            status="answer_after_close",
            poll_id=poll_id,
            tg_id=tg_id,
            option_ids=normalized_option_ids,
            is_withdrawn=is_withdrawn,
            poll=poll,
        )

    if status != "active":
        await _store_event(
            session,
            poll_id=poll_id,
            tg_id=tg_id,
            option_ids=normalized_option_ids,
            event_type="poll_not_active",
        )
        logger.warning(f"[Polls] Ответ на неактивный опрос: poll_id={poll_id!r}, status={status!r}, tg_id={tg_id}")
        return SavePollAnswerResult(
            status="poll_not_active",
            poll_id=poll_id,
            tg_id=tg_id,
            option_ids=normalized_option_ids,
            is_withdrawn=is_withdrawn,
            poll=poll,
        )

    row = await storage.upsert_poll_answer(
        session,
        poll_id=poll_id,
        tg_id=tg_id,
        option_ids=normalized_option_ids,
    )
    result_status = "withdrawn" if is_withdrawn else "saved"
    return SavePollAnswerResult(
        status=result_status,
        poll_id=poll_id,
        tg_id=tg_id,
        option_ids=normalized_option_ids,
        is_withdrawn=is_withdrawn,
        row=row,
        poll=poll,
    )


async def get_poll_stats(session: AsyncSession, selector: str) -> dict[str, Any] | None:
    poll = await get_poll_by_selector(session, selector)
    if not poll or not poll.get("poll_id"):
        return None

    options = poll.get("options_json") or []
    if not isinstance(options, list):
        options = []

    counts = dict.fromkeys(range(len(options)), 0)
    answers = await storage.list_poll_answers(session, poll_id=str(poll["poll_id"]))
    withdrawn = 0
    respondents = 0

    for answer in answers:
        if bool(answer.get("is_withdrawn")):
            withdrawn += 1
            continue
        respondents += 1
        option_ids = answer.get("option_ids") or []
        if not isinstance(option_ids, list):
            continue
        for option_id in option_ids:
            option_id = int(option_id)
            if option_id in counts:
                counts[option_id] += 1

    return {
        "poll": poll,
        "options": options,
        "counts": counts,
        "respondents": respondents,
        "withdrawn": withdrawn,
        "answers_total": len(answers),
        "allows_multiple_answers": bool(poll.get("allows_multiple_answers")),
    }
