"""Settings of the yookassa_methods feature: everything the owner may want to change.

Changes take effect after a bot restart.
"""

from pathlib import Path


# --- Link address -------------------------------------------------------------------------
# The button opens PUBLIC_BASE_URL + LINK_PATH_PREFIX + <signed label>. The page there posts
# a form to PUBLIC_BASE_URL + WEBHOOK_PATH; the Russian Caddy forwards it to the bot.
PUBLIC_BASE_URL = "https://pay.omenso.net"
LINK_PATH_PREFIX = "/p/"
WEBHOOK_PATH = "/inhouse/pay"

# Origins accepted in the Origin header of the form post (when a browser sends one).
# "null" is always accepted: with Referrer-Policy no-referrer browsers send Origin: null.
ALLOWED_ORIGINS: tuple[str, ...] = ("https://pay.omenso.net",)

# --- Signed label -------------------------------------------------------------------------
TOKEN_TTL_SECONDS = 30 * 60
# Secret of the label (256 bit). Created on first start, kept on the server, never in git.
# storage/ of the bot is outside git and survives module redeploys.
SECRET_FILE = Path(__file__).resolve().parents[4] / "storage" / "inhouse" / "yookassa_methods.secret"

# --- Limits -------------------------------------------------------------------------------
MAX_AMOUNT_RUB = 50_000  # the top-up taken from the saved purchase must not exceed this
NEW_PAYMENTS_PER_HOUR = 10  # new YooKassa payments per person per hour via links
STORE_RETENTION_DAYS = 7  # rows of inhouse_yk_links older than this are deleted

# --- Buttons ------------------------------------------------------------------------------
# Custom emoji on the SBP button (Bot API 9.4 icon_custom_emoji_id). Empty string = no icon.
SBP_ICON_CUSTOM_EMOJI_ID = "5265074015868822600"
LABEL_SBP = "СБП"  # used together with the icon
LABEL_SBP_NO_ICON = "🏦 СБП"  # used when SBP_ICON_CUSTOM_EMOJI_ID is empty
LABEL_CARD = "💳 Банковская карта"
LABEL_OTHER = "SberPay и другие способы"

# Display order inside the bot (smaller = higher). The link layer also keeps
# SBP -> card -> other among the three buttons whatever the admin order is.
ORDER_SBP = -30
ORDER_CARD = -20
ORDER_OTHER = -10

# --- Pages --------------------------------------------------------------------------------
# Link back to the bot on every page. Empty = REDIRECT_LINK from the bot config.
BOT_LINK = ""
BOT_LINK_TEXT = "Вернуться в бот"

PAGE_TITLE = "Оплата"
PAGE_STALE = "Оформление устарело, вернитесь в бот и откройте покупку заново."
PAGE_PAID = "Эта покупка уже оплачена."
PAGE_ERROR = "Не получилось создать оплату, попробуйте ещё раз или вернитесь в бот."
PAGE_ERROR_RETRY = "Попробовать ещё раз"
PAGE_UNAVAILABLE = "Оплата временно недоступна. Вернитесь в бот и откройте покупку заново."
PAGE_INVALID = "Ссылка недействительна. Вернитесь в бот и откройте покупку заново."
PAGE_LIMITED = "Слишком много попыток оплаты. Подождите немного или вернитесь в бот."

# --- Redirect safety ----------------------------------------------------------------------
# The endpoint redirects only to these YooKassa hosts (and their subdomains), https only.
YOOKASSA_REDIRECT_HOSTS: tuple[str, ...] = ("yoomoney.ru", "yookassa.ru")
