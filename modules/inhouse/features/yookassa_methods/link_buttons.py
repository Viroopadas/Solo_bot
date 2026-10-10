"""Outgoing-request layer (aiogram bot.session.middleware): provider buttons -> link buttons.

In any outgoing method of a private chat whose reply_markup has the exact callbacks
choose_payment_provider|YOOKASSA_SBP / |YOOKASSA_CARD / |YOOKASSA, the callback_data is
replaced by a link with a signed label (token.py), the label text is set, and the SBP
button gets its custom-emoji icon. Other buttons are not touched. The three buttons keep
the order SBP -> card -> other in the slots they occupy.

In the top-up menu (hidden in production) the three providers would show three identical
"pay_yookassa" buttons; one stock YooKassa button is kept.

If a link cannot be made, the button stays a normal button: pressing it goes to the stock
YooKassa handler and opens today's "Pay" screen, so payment never breaks. Any error in
this layer is logged and the request is sent unchanged.
"""

from __future__ import annotations

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.types import InlineKeyboardMarkup

from logger import logger

from . import state, token
from .providers import CARD, SBP, STOCK


CALLBACK_PREFIX = "choose_payment_provider|"
TARGETS: dict[str, str] = {
    f"{CALLBACK_PREFIX}{SBP}": "sbp",
    f"{CALLBACK_PREFIX}{CARD}": "card",
    f"{CALLBACK_PREFIX}{STOCK}": "other",
}
RANK = {"sbp": 0, "card": 1, "other": 2}
TOPUP_CALLBACK = "pay_yookassa"


def private_chat_id(method) -> int | None:
    """The user id when the method targets a private chat (positive chat id), else None."""
    if getattr(method, "inline_message_id", None):
        return None
    chat_id = getattr(method, "chat_id", None)
    if isinstance(chat_id, bool):
        return None
    if isinstance(chat_id, int):
        return chat_id if chat_id > 0 else None
    if isinstance(chat_id, str) and chat_id.isdigit():
        value = int(chat_id)
        return value if value > 0 else None
    return None


def _label(deps: state.Deps, method_key: str) -> tuple[str, str | None]:
    s = deps.settings
    if method_key == "sbp":
        icon = (s.SBP_ICON_CUSTOM_EMOJI_ID or "").strip()
        return (s.LABEL_SBP, icon) if icon else (s.LABEL_SBP_NO_ICON, None)
    if method_key == "card":
        return s.LABEL_CARD, None
    return s.LABEL_OTHER, None


def build_url(deps: state.Deps, tg_id: int, method_key: str, issued_ms: int) -> str:
    s = deps.settings
    label = token.issue(deps.secret, tg_id, method_key, s.TOKEN_TTL_SECONDS, issued_ms=issued_ms)
    return f"{s.PUBLIC_BASE_URL.rstrip('/')}{s.LINK_PATH_PREFIX}{label}"


def _link_button(button, deps: state.Deps, tg_id: int, method_key: str, issued_ms: int):
    data = button.model_dump(exclude_none=True)
    text, icon = _label(deps, method_key)
    data["text"] = text
    if icon:
        data["icon_custom_emoji_id"] = icon
    try:
        url = build_url(deps, tg_id, method_key, issued_ms)
    except Exception as exc:
        logger.warning(f"[inhouse.yk] link not built for {method_key}, button stays a callback: {type(exc).__name__}")
        return type(button)(**data)
    data.pop("callback_data", None)
    data["url"] = url
    return type(button)(**data)


def rewrite_markup(markup: InlineKeyboardMarkup, deps: state.Deps, tg_id: int, issued_ms: int):
    """New markup, or None when nothing in it concerns us."""
    rows = markup.inline_keyboard or []
    callbacks = [getattr(b, "callback_data", None) for row in rows for b in row]
    has_targets = any(cd in TARGETS for cd in callbacks)
    topup_dupes = sum(1 for cd in callbacks if cd == TOPUP_CALLBACK) > 1
    if not has_targets and not topup_dupes:
        return None

    new_rows: list[list] = []
    target_slots: list[tuple[int, int]] = []
    target_buttons: list[tuple[int, object]] = []
    topup_seen = False
    for row in rows:
        new_row = []
        for button in row:
            cd = getattr(button, "callback_data", None)
            if cd in TARGETS:
                method_key = TARGETS[cd]
                new_button = _link_button(button, deps, tg_id, method_key, issued_ms)
                target_slots.append((len(new_rows), len(new_row)))
                target_buttons.append((RANK[method_key], new_button))
                new_row.append(new_button)
            elif topup_dupes and cd == TOPUP_CALLBACK:
                if topup_seen:
                    continue
                topup_seen = True
                data = button.model_dump(exclude_none=True)
                data["text"] = deps.standard_yookassa_label
                data.pop("icon_custom_emoji_id", None)
                new_row.append(type(button)(**data))
            else:
                new_row.append(button)
        if new_row:
            new_rows.append(new_row)

    ordered = [b for _, b in sorted(target_buttons, key=lambda item: item[0])]
    for (r, c), button in zip(target_slots, ordered, strict=True):
        new_rows[r][c] = button
    return InlineKeyboardMarkup(inline_keyboard=new_rows)


class LinkButtonsMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):  # noqa: ANN204 (aiogram protocol)
        try:
            self.apply(method)
        except Exception as exc:
            logger.warning(f"[inhouse.yk] link layer skipped: {type(exc).__name__}")
        return await make_request(bot, method)

    @staticmethod
    def apply(method) -> bool:
        deps = state.current()
        if deps is None:
            return False
        markup = getattr(method, "reply_markup", None)
        if not isinstance(markup, InlineKeyboardMarkup):
            return False
        tg_id = private_chat_id(method)
        if tg_id is None:
            return False
        new_markup = rewrite_markup(markup, deps, tg_id, token.now_ms())
        if new_markup is None:
            return False
        method.reply_markup = new_markup
        return True
