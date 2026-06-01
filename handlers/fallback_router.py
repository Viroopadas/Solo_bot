from typing import Any

from aiogram import F, Router
from aiogram.types import InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import SUPPORT_CHAT_URL
from core.bootstrap import MODES_CONFIG
from handlers.buttons import HAVE_PROBLEM, MAIN_MENU, SUPPORT
from handlers.support_triage import TriageCallback
from handlers.texts import FALLBACK_MESSAGE
from hooks.hooks import run_hooks


fallback_router = Router()


@fallback_router.message(F.text)
async def handle_unhandled_messages(message: Message, session: Any):
    await run_hooks(
        "user_message",
        user_id=message.from_user.id,
        message_text=message.text,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        session=session,
        message=message,
    )

    keyboard = InlineKeyboardBuilder()
    if bool((MODES_CONFIG or {}).get("SUPPORT_TRIAGE_ENABLED", False)):
        keyboard.row(
            InlineKeyboardButton(text=HAVE_PROBLEM, callback_data=TriageCallback(action="root").pack())
        )
    else:
        keyboard.row(InlineKeyboardButton(text=SUPPORT, url=SUPPORT_CHAT_URL))
    keyboard.row(InlineKeyboardButton(text=MAIN_MENU, callback_data="profile"))
    await message.answer(
        FALLBACK_MESSAGE,
        reply_markup=keyboard.as_markup(),
    )
