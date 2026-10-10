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

# --- Payment site watch (watch.py) -------------------------------------------------------
# The watch checks PUBLIC_BASE_URL every few minutes. When the site is broken it switches
# NEW menus to the stock YooKassa button ("fallback") and writes to the owner in Telegram;
# when the site works again it brings the three buttons back. It never stops the feature.
WATCH_ENABLED = True  # False: no checks, no messages, menus always show the three buttons
AUTO_FALLBACK = True  # False: only messages, menus always show the three buttons
WATCH_INTERVAL_SECONDS = 120  # pause between checks while everything works (and while broken)
WATCH_RETRY_SECONDS = 30  # pause after a failed check while not yet "broken"
WATCH_JITTER_SECONDS = 10  # random 0..N seconds added to the normal pause
WATCH_REQUEST_TIMEOUT = 10  # seconds per request of a check
WATCH_FAILS = 3  # failed checks in a row -> broken
WATCH_OKS = 3  # good checks in a row -> works again
WATCH_MIN_FALLBACK_MINUTES = 10  # the stock button stays at least this long
WATCH_FLAP_ENTRIES_PER_HOUR = 3  # this many switches to the stock button within an hour ...
WATCH_FLAP_HOLD_MINUTES = 60  # ... keep the stock button at least this long
WATCH_REMIND_FIRST_MINUTES = 60  # first reminder while broken
WATCH_REMIND_EVERY_HOURS = 6  # later reminders (and repeated warnings)
OBSERVER_STALE_MINUTES = 6  # a signal of the check from Russia older than this is "unknown"
OBSERVER_SILENT_ALERT_MINUTES = 15  # warn when the check from Russia is silent this long
OBSERVER_SCOPE_TEXT = "Из сети Timeweb в России"  # where the check from Russia looks from
CERT_WARN_DAYS = 14
DOMAIN_WARN_DAYS = 30
WATCH_DOMAINS: tuple[str, ...] = ("omenso.net", "potok-lubvi.ru")
# Certificates checked once a day: host -> where to look for the renewal.
WATCH_CERT_HOSTS: dict[str, str] = {
    "pay.omenso.net": "test-bg-ru1 (Caddy переходника)",
    "store.potok-lubvi.ru": "solobot-remnawave (Caddy бота)",
}
# Telegram chat ids for the watch messages; empty = ADMIN_ID from the bot config.
WATCH_ALERT_CHAT_IDS: tuple[int, ...] = ()
# Outside git, next to the label secret; the installer never copies these files.
WATCH_STATE_FILE = Path(__file__).resolve().parents[4] / "storage" / "inhouse" / "watch.json"
OBSERVER_SECRET_FILE = Path(__file__).resolve().parents[4] / "storage" / "inhouse" / "observer.secret"
