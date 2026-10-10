"""Start and stop of the yookassa_methods feature.

Start = all checks first (section 4.3 "start conditions"), then installation of all parts.
A failure at any step undoes the parts already installed, leaves the feature off and
writes one log line with the reason; other features keep working.

Start conditions:
- FEATURES["yookassa_methods"] is True in modules/inhouse/settings.py;
- YOOKASSA_SBP_ONLY = False;
- the stock YooKassa service and create_and_store_yk_payment import;
- USE_NEW_PAYMENT_FLOW is a list;
- the link address is set (https) and the feature table exists (created if missing);
- plus what the parts need: the SDK Payment.create is a classmethod, the secret is readable
  or creatable, the bot object is available.

Parts installed (and undone in reverse order on failure or stop):
USE_NEW_PAYMENT_FLOW entries, button labels, SDK wrapper, outgoing-request layer.
The providers hook and the web endpoint are registered by the module at import time and
do nothing while the feature is not active.
"""

from __future__ import annotations

import asyncio

from logger import logger

from . import (
    compat,
    link_buttons,
    providers,
    sdk_patch,
    settings as feature_settings,
    state,
    store,
    token,
)


FEATURE = "yookassa_methods"
PREFIX = "[inhouse.yk]"


class StartError(Exception):
    """A start condition is not met; the message is safe to log."""


def feature_flag() -> bool:
    from ... import settings as module_settings

    return bool(getattr(module_settings, "FEATURES", {}).get(FEATURE))


async def start(bot=None) -> bool:
    st = state.STATE
    if st.active or st.starting:
        logger.info(f"{PREFIX} start skipped: already {'active' if st.active else 'starting'}")
        return st.active
    if not feature_flag():
        st.reason = "turned off in modules/inhouse/settings.py"
        logger.info(f"{PREFIX} feature off: {st.reason}")
        return False
    st.starting = True
    try:
        return await _start(bot)
    finally:
        st.starting = False


async def _start(bot) -> bool:
    st = state.STATE
    s = feature_settings
    step = "checks"
    try:
        step = "config"
        cfg = compat.load_config()
        if bool(getattr(cfg, "YOOKASSA_SBP_ONLY", False)):
            raise StartError("YOOKASSA_SBP_ONLY is on")
        flow = getattr(cfg, "USE_NEW_PAYMENT_FLOW", None)
        if not isinstance(flow, list):
            raise StartError("USE_NEW_PAYMENT_FLOW is not a list")
        step = "yookassa service"
        create_payment = compat.load_create_payment()
        step = "yookassa sdk"
        payment_cls = compat.load_yookassa_payment()
        sdk_patch.check_patchable(payment_cls)
        step = "link address"
        if not str(s.PUBLIC_BASE_URL or "").startswith("https://"):
            raise StartError("PUBLIC_BASE_URL is not an https address")
        step = "secret"
        secret = token.load_or_create_secret(s.SECRET_FILE)
        step = "database"
        session_maker = compat.session_maker()
        step = "bot"
        if bot is None:
            bot = compat.load_bot()
        if not hasattr(getattr(bot, "session", None), "middleware"):
            raise StartError("bot.session.middleware not available")
        step = "table"
        async with session_maker() as session:
            await store.ensure_table(session)
            await store.purge_old(session, s.STORE_RETENTION_DAYS)
            await session.commit()
        step = "buttons"
        buttons = compat.load_buttons()
    except (StartError, compat.CompatError, sdk_patch.PatchError) as exc:
        return _fail(f"{step}: {exc}")
    except Exception as exc:
        return _fail(f"{step}: {type(exc).__name__}")

    deps = state.Deps(
        settings=s,
        secret=secret,
        session_maker=session_maker,
        create_payment=create_payment,
        bot_link=compat.bot_link(cfg, s.BOT_LINK),
        standard_yookassa_label=str(getattr(buttons, "YOOKASSA", "ЮKassa")),
    )
    undo: list[tuple[str, object]] = []
    step = "install"
    try:
        step = "new payment flow"
        added = providers.extend_new_flow(flow)
        undo.append((step, lambda: providers.revert_new_flow(flow, added)))
        step = "button labels"
        labels = compat.set_button_labels(buttons, {providers.SBP: s.LABEL_SBP_NO_ICON, providers.CARD: s.LABEL_CARD})
        undo.append((step, lambda: compat.unset_button_labels(buttons, labels)))
        step = "sdk wrapper"
        sdk_patch.install(payment_cls)
        undo.append((step, sdk_patch.uninstall))
        step = "link layer"
        middleware = link_buttons.LinkButtonsMiddleware()
        bot.session.middleware(middleware)
        undo.append((step, lambda: bot.session.middleware.unregister(middleware)))
    except BaseException as exc:
        _undo(undo)
        _fail(f"{step}: {type(exc).__name__}")
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        return False

    st.deps = deps
    st.undo = undo
    st.active = True
    st.reason = "active"
    logger.info(
        f"{PREFIX} feature on: {compat.flavor()}, links {s.PUBLIC_BASE_URL}{s.LINK_PATH_PREFIX}…, "
        f"flow {list(flow)}, icon {'yes' if s.SBP_ICON_CUSTOM_EMOJI_ID else 'no'}"
    )
    return True


def _fail(reason: str) -> bool:
    st = state.STATE
    st.active = False
    st.deps = None
    st.reason = reason
    logger.error(f"{PREFIX} feature stays off: {reason}")
    return False


def _undo(undo: list) -> None:
    for name, action in reversed(undo):
        try:
            action()
        except Exception as exc:
            logger.error(f"{PREFIX} undo of {name} failed: {type(exc).__name__}")


def stop() -> None:
    st = state.STATE
    was_active = st.active
    st.active = False
    _undo(st.undo)
    st.undo = []
    st.deps = None
    st.reason = "stopped"
    if was_active:
        logger.info(f"{PREFIX} feature stopped")
