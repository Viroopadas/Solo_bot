from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .settings import CALLBACK_PREFIX


def _callback(action: str, value: int | str) -> str:
    return f"{CALLBACK_PREFIX}{action}:{value}"


def build_poll_actions_keyboard(poll: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    poll_id = poll.get("id")
    builder.row(
        InlineKeyboardButton(text="Статистика", callback_data=_callback("stats", poll_id)),
    )
    if poll.get("status") == "active":
        builder.row(
            InlineKeyboardButton(text="Закрыть", callback_data=_callback("close", poll_id)),
        )
    return builder.as_markup()


def build_polls_list_keyboard(polls: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for poll in polls:
        poll_id = poll.get("id")
        builder.row(
            InlineKeyboardButton(text=f"Статистика #{poll_id}", callback_data=_callback("stats", poll_id)),
        )
        if poll.get("status") == "active":
            builder.row(
                InlineKeyboardButton(text=f"Закрыть #{poll_id}", callback_data=_callback("close", poll_id)),
            )
    return builder.as_markup()
