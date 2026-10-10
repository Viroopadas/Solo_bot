"""Outgoing-request layer: provider buttons become link buttons."""

import json

import pytest

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    EditMessageCaption,
    EditMessageMedia,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    SendPhoto,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from modules.inhouse.features.yookassa_methods import link_buttons, sdk_patch, state, token
from modules.inhouse.features.yookassa_methods import settings as yk_settings
from modules.inhouse.tests.helpers import SECRET, activate, make_deps


TG = 777000222
SBP_CB = "choose_payment_provider|YOOKASSA_SBP"
CARD_CB = "choose_payment_provider|YOOKASSA_CARD"
STOCK_CB = "choose_payment_provider|YOOKASSA"


def purchase_markup(order=(SBP_CB, CARD_CB, STOCK_CB)):
    rows = [[InlineKeyboardButton(text=cb.split("|")[1], callback_data=cb)] for cb in order]
    rows.append([InlineKeyboardButton(text="🎟️ Активировать купон", callback_data="fastflow_coupon")])
    rows.append([InlineKeyboardButton(text="👤 Личный кабинет", callback_data="profile")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@pytest.fixture
def active():
    deps = make_deps(None, None)
    activate(deps)
    return deps


def flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


def check_links(markup):
    buttons = flat(markup)
    sbp, card, other = buttons[0], buttons[1], buttons[2]
    for b in (sbp, card, other):
        assert b.callback_data is None
        assert b.url.startswith("https://pay.omenso.net/p/")
    assert sbp.text == "СБП"
    assert sbp.model_dump(exclude_none=True)["icon_custom_emoji_id"] == "5265074015868822600"
    assert card.text == "💳 Банковская карта"
    assert other.text == "SberPay и другие способы"
    methods = [token.verify(SECRET, b.url.rsplit("/", 1)[1], 1800).method for b in (sbp, card, other)]
    assert methods == ["sbp", "card", "other"]
    claims = [token.verify(SECRET, b.url.rsplit("/", 1)[1], 1800) for b in (sbp, card, other)]
    assert {c.tg_id for c in claims} == {TG}
    assert len({c.issued_ms for c in claims}) == 1  # one message = one issue time
    assert buttons[3].callback_data == "fastflow_coupon" and buttons[3].url is None
    assert buttons[4].callback_data == "profile"


@pytest.mark.parametrize(
    "make",
    [
        lambda m: SendMessage(chat_id=TG, text="Не хватает 199 ₽", reply_markup=m),
        lambda m: EditMessageText(chat_id=TG, message_id=10, text="Не хватает 199 ₽", reply_markup=m),
        lambda m: EditMessageCaption(chat_id=TG, message_id=10, caption="x", reply_markup=m),
        lambda m: EditMessageMedia(chat_id=TG, message_id=10, media=InputMediaPhoto(media="file-id"), reply_markup=m),
        lambda m: SendPhoto(chat_id=TG, photo="file-id", caption="x", reply_markup=m),
        lambda m: EditMessageReplyMarkup(chat_id=TG, message_id=10, reply_markup=m),
    ],
    ids=["SendMessage", "EditMessageText", "EditMessageCaption", "EditMessageMedia", "SendPhoto", "EditReplyMarkup"],
)
def test_methods_rewritten(active, make):
    method = make(purchase_markup())
    assert link_buttons.LinkButtonsMiddleware.apply(method) is True
    check_links(method.reply_markup)


def test_order_fixed_whatever_admin_order(active):
    method = SendMessage(chat_id=TG, text="x", reply_markup=purchase_markup((STOCK_CB, CARD_CB, SBP_CB)))
    link_buttons.LinkButtonsMiddleware.apply(method)
    check_links(method.reply_markup)


def test_only_exact_callbacks(active):
    rows = [
        [InlineKeyboardButton(text="a", callback_data="choose_payment_provider|YOOMONEY")],
        [InlineKeyboardButton(text="b", callback_data="choose_payment_provider|YOOKASSA_X")],
        [InlineKeyboardButton(text="c", callback_data="choose_payment_provider|yookassa")],
        [InlineKeyboardButton(text="d", callback_data="pay_yookassa")],
    ]
    method = SendMessage(chat_id=TG, text="x", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    before = method.reply_markup
    assert link_buttons.LinkButtonsMiddleware.apply(method) is False
    assert method.reply_markup is before


@pytest.mark.parametrize("chat_id", [-1001234567890, -42])
def test_groups_untouched(active, chat_id):
    method = SendMessage(chat_id=chat_id, text="x", reply_markup=purchase_markup())
    assert link_buttons.LinkButtonsMiddleware.apply(method) is False
    assert flat(method.reply_markup)[0].callback_data == SBP_CB


def test_inline_message_untouched(active):
    method = EditMessageText(inline_message_id="abc", text="x", reply_markup=purchase_markup())
    assert link_buttons.LinkButtonsMiddleware.apply(method) is False


def test_module_off_untouched(active, monkeypatch):
    monkeypatch.setattr(state, "module_enabled", lambda: False)
    method = SendMessage(chat_id=TG, text="x", reply_markup=purchase_markup())
    assert link_buttons.LinkButtonsMiddleware.apply(method) is False
    assert flat(method.reply_markup)[0].callback_data == SBP_CB


def test_feature_inactive_untouched():
    method = SendMessage(chat_id=TG, text="x", reply_markup=purchase_markup())
    assert link_buttons.LinkButtonsMiddleware.apply(method) is False


def test_no_icon_label(active, monkeypatch):
    monkeypatch.setattr(yk_settings, "SBP_ICON_CUSTOM_EMOJI_ID", "")
    method = SendMessage(chat_id=TG, text="x", reply_markup=purchase_markup())
    link_buttons.LinkButtonsMiddleware.apply(method)
    sbp = flat(method.reply_markup)[0]
    assert sbp.text == "🏦 СБП" and "icon_custom_emoji_id" not in sbp.model_dump(exclude_none=True)


def test_link_failure_keeps_callback(active, monkeypatch):
    active.secret = b"short"  # token.issue refuses -> button stays a normal button
    method = SendMessage(chat_id=TG, text="x", reply_markup=purchase_markup())
    link_buttons.LinkButtonsMiddleware.apply(method)
    buttons = flat(method.reply_markup)
    assert [b.callback_data for b in buttons[:3]] == [SBP_CB, CARD_CB, STOCK_CB]
    assert all(b.url is None for b in buttons[:3])
    assert buttons[0].text == "СБП"


def test_topup_menu_dedupe(active):
    rows = [
        [InlineKeyboardButton(text="YOOKASSA_SBP", callback_data="pay_yookassa")],
        [InlineKeyboardButton(text="YOOKASSA_CARD", callback_data="pay_yookassa")],
        [InlineKeyboardButton(text="💳 ЮКасса: быстрая оплата", callback_data="pay_yookassa")],
        [InlineKeyboardButton(text="⭐ Оплата Звездами", callback_data="pay_stars")],
        [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="profile")],
    ]
    method = EditMessageText(
        chat_id=TG, message_id=1, text="x", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    assert link_buttons.LinkButtonsMiddleware.apply(method) is True
    buttons = flat(method.reply_markup)
    assert [b.callback_data for b in buttons] == ["pay_yookassa", "pay_stars", "profile"]
    assert buttons[0].text == "💳 ЮКасса: быстрая оплата" and buttons[0].url is None
    assert len(method.reply_markup.inline_keyboard) == 3


def test_single_topup_button_untouched(active):
    rows = [[InlineKeyboardButton(text="ЮКасса", callback_data="pay_yookassa")]]
    method = SendMessage(chat_id=TG, text="x", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    assert link_buttons.LinkButtonsMiddleware.apply(method) is False


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def make_request(self, bot, method, timeout=None):
        files = {}
        payload = {k: self.prepare_value(v, bot=bot, files=files) for k, v in method.model_dump(warnings=False).items()}
        self.sent.append((type(method).__name__, {k: v for k, v in payload.items() if v is not None}))
        return True

    async def stream_content(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def close(self):
        pass


async def test_through_real_bot_session(active):
    """The middleware registered on bot.session changes what is actually sent."""
    session = RecordingSession()
    bot = Bot(token="42:TEST", session=session)
    mw = link_buttons.LinkButtonsMiddleware()
    bot.session.middleware(mw)
    try:
        await bot(SendMessage(chat_id=TG, text="Не хватает 199 ₽", reply_markup=purchase_markup()))
    finally:
        bot.session.middleware.unregister(mw)
    name, payload = session.sent[0]
    keyboard = json.loads(payload["reply_markup"])["inline_keyboard"]
    sbp = keyboard[0][0]
    assert "callback_data" not in sbp and sbp["url"].startswith("https://pay.omenso.net/p/")
    assert sbp["icon_custom_emoji_id"] == "5265074015868822600" and sbp["text"] == "СБП"
    assert keyboard[3][0]["callback_data"] == "fastflow_coupon"


async def test_layer_error_never_blocks_sending(active, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(link_buttons, "rewrite_markup", boom)
    called = []

    async def make_request(bot, method):
        called.append(method)
        return "ok"

    method = SendMessage(chat_id=TG, text="x", reply_markup=purchase_markup())
    assert await link_buttons.LinkButtonsMiddleware()(make_request, None, method) == "ok"
    assert called and flat(method.reply_markup)[0].callback_data == SBP_CB


def test_rewrite_prewarms_yookassa_connection(active, monkeypatch):
    calls = []
    monkeypatch.setattr(sdk_patch, "prewarm", lambda: calls.append(1) or True)
    method = SendMessage(chat_id=TG, text="x", reply_markup=purchase_markup())
    assert link_buttons.LinkButtonsMiddleware.apply(method) is True
    assert calls == [1]
    other = SendMessage(chat_id=TG, text="x", reply_markup=InlineKeyboardMarkup(inline_keyboard=[]))
    link_buttons.LinkButtonsMiddleware.apply(other)
    assert calls == [1]
