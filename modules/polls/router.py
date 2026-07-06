from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, Message, PollAnswer
from sqlalchemy.ext.asyncio import AsyncSession

from logger import logger

from . import service
from .keyboards import build_poll_actions_keyboard, build_polls_list_keyboard
from .settings import CALLBACK_PREFIX, DEFAULT_ANONYMOUS
from .texts import (
    POLL_CLOSE_USAGE,
    POLL_STATS_USAGE,
    POLL_TEST_USAGE,
    format_poll_created,
    format_poll_list,
    format_poll_stats,
)


router = Router(name="polls_module")


async def check_is_admin(user_id: int) -> bool:
    try:
        from sqlalchemy import select

        from config import ADMIN_ID
        from database.db import async_session_maker
        from database.models import Admin

        admin_ids = set(ADMIN_ID) if isinstance(ADMIN_ID, (list, tuple, set)) else {ADMIN_ID}
        if user_id in admin_ids:
            return True

        async with async_session_maker() as session:
            result = await session.execute(select(Admin).where(Admin.tg_id == user_id))
            return result.scalar_one_or_none() is not None
    except Exception as e:
        logger.warning(f"[Polls] Ошибка проверки админа: {e}")
        return False


class PollsAdminFilter(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if not event.from_user:
            return False
        return await check_is_admin(event.from_user.id)


def _parse_poll_test_args(args: str | None) -> tuple[str, list[str], bool]:
    parts = [part.strip() for part in (args or "").split("|") if part.strip()]
    allows_multiple_answers = False

    if parts and parts[0].lower() in {"multi", "multiple", "несколько"}:
        allows_multiple_answers = True
        parts = parts[1:]
    elif parts and parts[0].lower() in {"single", "one", "один"}:
        parts = parts[1:]

    if not parts:
        return "Тестовый опрос", ["Да", "Нет"], allows_multiple_answers

    question = parts[0]
    options = parts[1:] or ["Да", "Нет"]
    return question, options, allows_multiple_answers


async def _handle_poll_answer(poll_answer: PollAnswer, session: AsyncSession) -> service.SavePollAnswerResult:
    tg_id = poll_answer.user.id if poll_answer.user else None
    option_ids = list(poll_answer.option_ids or [])

    logger.info(
        f"[Polls] PollAnswer получен: poll_id={poll_answer.poll_id!r}, "
        f"tg_id={tg_id}, option_ids={option_ids}"
    )
    result = await service.save_poll_answer(
        session,
        poll_id=poll_answer.poll_id,
        tg_id=tg_id,
        option_ids=option_ids,
    )
    logger.info(
        f"[Polls] PollAnswer обработан: status={result.status}, "
        f"poll_id={result.poll_id!r}, tg_id={result.tg_id}, option_ids={result.option_ids}"
    )
    return result


@router.poll_answer()
async def handle_poll_answer(poll_answer: PollAnswer, session: AsyncSession | None = None) -> None:
    if session is not None:
        await _handle_poll_answer(poll_answer, session)
        return

    from database.db import async_session_maker

    async with async_session_maker() as owned_session:
        try:
            await _handle_poll_answer(poll_answer, owned_session)
            await owned_session.commit()
        except Exception:
            await owned_session.rollback()
            raise


@router.message(Command("poll_test"), PollsAdminFilter())
async def cmd_poll_test(
    message: Message,
    command: CommandObject,
    bot: Bot,
    session: AsyncSession,
) -> None:
    question, options, allows_multiple_answers = _parse_poll_test_args(command.args)

    try:
        pending_poll = await service.create_poll_record(
            session,
            created_by_tg_id=message.from_user.id if message.from_user else None,
            question=question,
            options=options,
            allows_multiple_answers=allows_multiple_answers,
            is_anonymous=DEFAULT_ANONYMOUS,
        )
        await session.commit()
    except ValueError as e:
        await message.answer(f"{e}\n\n{POLL_TEST_USAGE}")
        return

    try:
        poll_message = await bot.send_poll(
            chat_id=message.chat.id,
            question=question,
            options=options,
            is_anonymous=False,
            allows_multiple_answers=allows_multiple_answers,
        )
        if not poll_message.poll:
            raise RuntimeError("Telegram не вернул poll в ответе send_poll")

        active_poll = await service.activate_poll_record(
            session,
            record_id=int(pending_poll["id"]),
            poll_id=poll_message.poll.id,
            chat_id=poll_message.chat.id,
            message_id=poll_message.message_id,
        )
        await session.commit()

        await message.answer(
            format_poll_created(active_poll or pending_poll),
            reply_markup=build_poll_actions_keyboard(active_poll or pending_poll),
        )
    except Exception as e:
        await service.mark_poll_failed(session, record_id=int(pending_poll["id"]))
        await session.commit()
        logger.error(f"[Polls] Не удалось отправить тестовый опрос: {e}", exc_info=True)
        await message.answer(f"Не удалось отправить опрос: {type(e).__name__}: {e}")


@router.message(Command("polls"), PollsAdminFilter())
async def cmd_polls(message: Message, session: AsyncSession) -> None:
    polls = await service.list_recent_polls(session)
    if not polls:
        await message.answer("Опросов пока нет.")
        return

    await message.answer(
        format_poll_list(polls),
        reply_markup=build_polls_list_keyboard(polls),
    )


@router.message(Command("poll_stats"), PollsAdminFilter())
async def cmd_poll_stats(message: Message, command: CommandObject, session: AsyncSession) -> None:
    selector = (command.args or "").strip()
    if not selector:
        await message.answer(POLL_STATS_USAGE)
        return

    stats = await service.get_poll_stats(session, selector)
    if not stats:
        await message.answer("Опрос не найден или ещё не активирован.")
        return

    await message.answer(
        format_poll_stats(stats),
        reply_markup=build_poll_actions_keyboard(stats["poll"]),
    )


async def _close_poll(
    *,
    poll: dict,
    bot: Bot,
    session: AsyncSession,
) -> tuple[dict | None, str | None]:
    stop_error = None
    chat_id = poll.get("chat_id")
    message_id = poll.get("message_id")

    if poll.get("status") == "active" and chat_id and message_id:
        try:
            await bot.stop_poll(chat_id=int(chat_id), message_id=int(message_id))
        except Exception as e:
            stop_error = f"{type(e).__name__}: {e}"
            logger.warning(f"[Polls] bot.stop_poll failed for poll #{poll.get('id')}: {e}", exc_info=True)

    closed_poll = await service.close_poll_record(session, record_id=int(poll["id"]))
    await session.commit()
    return closed_poll, stop_error


@router.message(Command("poll_close"), PollsAdminFilter())
async def cmd_poll_close(
    message: Message,
    command: CommandObject,
    bot: Bot,
    session: AsyncSession,
) -> None:
    selector = (command.args or "").strip()
    if not selector:
        await message.answer(POLL_CLOSE_USAGE)
        return

    poll = await service.get_poll_by_selector(session, selector)
    if not poll:
        await message.answer("Опрос не найден.")
        return
    if poll.get("status") == "closed":
        await message.answer("Опрос уже закрыт.")
        return

    closed_poll, stop_error = await _close_poll(poll=poll, bot=bot, session=session)
    suffix = f"\nTelegram stop_poll вернул ошибку: <code>{stop_error}</code>" if stop_error else ""
    await message.answer(
        f"Опрос #{poll.get('id')} закрыт локально.{suffix}",
        reply_markup=build_poll_actions_keyboard(closed_poll or poll),
    )


@router.callback_query(F.data.startswith(CALLBACK_PREFIX), PollsAdminFilter())
async def cb_polls(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    data = callback.data or ""
    payload = data.removeprefix(CALLBACK_PREFIX)
    action, _, value = payload.partition(":")

    if action == "stats" and value:
        stats = await service.get_poll_stats(session, value)
        if not stats:
            await callback.answer("Опрос не найден", show_alert=True)
            return
        await callback.answer()
        await callback.message.edit_text(
            format_poll_stats(stats),
            reply_markup=build_poll_actions_keyboard(stats["poll"]),
        )
        return

    if action == "close" and value:
        poll = await service.get_poll_by_selector(session, value)
        if not poll:
            await callback.answer("Опрос не найден", show_alert=True)
            return
        if poll.get("status") == "closed":
            await callback.answer("Опрос уже закрыт", show_alert=False)
            return

        closed_poll, stop_error = await _close_poll(poll=poll, bot=bot, session=session)
        stats = await service.get_poll_stats(session, str(poll["id"]))
        if stats is None:
            stats = {
                "poll": closed_poll or poll,
                "options": [],
                "counts": {},
                "respondents": 0,
                "withdrawn": 0,
                "allows_multiple_answers": False,
            }
        await callback.answer("Опрос закрыт" if not stop_error else "Опрос закрыт локально", show_alert=False)
        await callback.message.edit_text(
            format_poll_stats(stats),
            reply_markup=build_poll_actions_keyboard(closed_poll or poll),
        )
        return

    await callback.answer("Неизвестное действие", show_alert=True)
