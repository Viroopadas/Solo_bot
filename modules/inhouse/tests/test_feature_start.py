"""Feature start: checks first, then install; failure undoes everything; stop undoes too."""

import types

import pytest

from aiogram import Bot
from yookassa import Payment

from modules.inhouse import settings as module_settings
from modules.inhouse.features.yookassa_methods import compat, feature, link_buttons, sdk_patch, state
from modules.inhouse.features.yookassa_methods import settings as yk_settings
from modules.inhouse.tests.helpers import FakeStockService


@pytest.fixture
def env(monkeypatch, tmp_path, db):
    cfg = types.SimpleNamespace(
        YOOKASSA_SBP_ONLY=False, USE_NEW_PAYMENT_FLOW=["YOOKASSA"], REDIRECT_LINK="https://t.me/example_bot"
    )
    buttons = types.SimpleNamespace(YOOKASSA="💳 ЮКасса: быстрая оплата")
    svc = FakeStockService()
    monkeypatch.setattr(compat, "load_config", lambda: cfg)
    monkeypatch.setattr(compat, "load_buttons", lambda: buttons)
    monkeypatch.setattr(compat, "load_create_payment", lambda: svc)
    monkeypatch.setattr(compat, "load_yookassa_payment", lambda: Payment)
    monkeypatch.setattr(compat, "session_maker", lambda: db)
    monkeypatch.setattr(yk_settings, "SECRET_FILE", tmp_path / "storage" / "yk.secret")
    monkeypatch.setitem(module_settings.FEATURES, "yookassa_methods", True)
    bot = Bot(token="42:TEST")
    return types.SimpleNamespace(cfg=cfg, buttons=buttons, svc=svc, bot=bot)


def nothing_installed(env):
    assert env.cfg.USE_NEW_PAYMENT_FLOW == ["YOOKASSA"]
    assert not hasattr(env.buttons, "YOOKASSA_SBP")
    assert not sdk_patch.is_installed()
    assert not any(isinstance(m, link_buttons.LinkButtonsMiddleware) for m in env.bot.session.middleware)
    assert state.STATE.active is False and state.current() is None


async def test_start_installs_all_parts_and_stop_undoes(env, tmp_path):
    assert await feature.start(env.bot) is True
    assert state.is_active()
    assert env.cfg.USE_NEW_PAYMENT_FLOW == ["YOOKASSA", "YOOKASSA_SBP", "YOOKASSA_CARD"]
    assert env.buttons.YOOKASSA_SBP == "🏦 СБП" and env.buttons.YOOKASSA_CARD == "💳 Банковская карта"
    assert sdk_patch.is_installed()
    assert any(isinstance(m, link_buttons.LinkButtonsMiddleware) for m in env.bot.session.middleware)
    assert (tmp_path / "storage" / "yk.secret").exists()
    assert state.STATE.deps.bot_link == "https://t.me/example_bot"
    assert await feature.start(env.bot) is True  # second startup call is a no-op
    assert env.cfg.USE_NEW_PAYMENT_FLOW.count("YOOKASSA_SBP") == 1
    feature.stop()
    nothing_installed(env)


async def test_feature_flag_off(env, monkeypatch, logs):
    monkeypatch.setitem(module_settings.FEATURES, "yookassa_methods", False)
    assert await feature.start(env.bot) is False
    nothing_installed(env)
    assert "feature off" in logs.text()


@pytest.mark.parametrize(
    "breakage,expected",
    [
        (lambda e, mp: setattr(e.cfg, "YOOKASSA_SBP_ONLY", True), "YOOKASSA_SBP_ONLY is on"),
        (lambda e, mp: setattr(e.cfg, "USE_NEW_PAYMENT_FLOW", ("YOOKASSA",)), "USE_NEW_PAYMENT_FLOW is not a list"),
        (lambda e, mp: mp.setattr(compat, "load_create_payment", _raise_compat), "yookassa service"),
        (lambda e, mp: mp.setattr(yk_settings, "PUBLIC_BASE_URL", ""), "link address"),
        (lambda e, mp: mp.setattr(compat, "session_maker", _raise_compat), "database"),
    ],
    ids=["sbp_only", "flow_not_list", "no_service", "no_address", "no_db"],
)
async def test_start_condition_fails_nothing_installed(env, monkeypatch, logs, breakage, expected):
    breakage(env, monkeypatch)
    flow_before = list(env.cfg.USE_NEW_PAYMENT_FLOW)
    assert await feature.start(env.bot) is False
    assert list(env.cfg.USE_NEW_PAYMENT_FLOW) == flow_before
    assert not sdk_patch.is_installed() and state.current() is None
    assert not any(isinstance(m, link_buttons.LinkButtonsMiddleware) for m in env.bot.session.middleware)
    errors = logs.at("error")
    assert errors and "feature stays off" in errors[-1] and expected in errors[-1]


def _raise_compat():
    raise compat.CompatError("not importable")


async def test_failure_mid_install_undoes_installed_parts(env, monkeypatch, logs):
    def refuse(mw):
        raise RuntimeError("cannot register")

    bot = types.SimpleNamespace(session=types.SimpleNamespace(middleware=refuse))
    assert await feature.start(bot) is False
    assert env.cfg.USE_NEW_PAYMENT_FLOW == ["YOOKASSA"]  # flow entries removed again
    assert not hasattr(env.buttons, "YOOKASSA_SBP")  # labels removed again
    assert not sdk_patch.is_installed()  # SDK wrapper removed again
    assert state.current() is None
    assert "link layer" in logs.at("error")[-1]


async def test_table_created_at_start(env, db):
    from sqlalchemy import text

    async with db() as session:
        await session.execute(text("DROP TABLE inhouse_yk_links"))
        await session.commit()
    assert await feature.start(env.bot) is True
    async with db() as session:
        await session.execute(text("SELECT COUNT(*) FROM inhouse_yk_links"))
    feature.stop()


async def test_router_contract():
    """The module entry offers what utils/modules_loader.py and web/__init__.py expect."""
    from aiogram import Router

    from modules.inhouse import router as entry
    from modules.inhouse.features.yookassa_methods import pay_endpoint
    from modules.inhouse.tests.helpers import REGISTERED_HOOKS

    assert isinstance(entry.router, Router)
    data = entry.get_webhook_data()
    assert data == {"path": "/inhouse/pay", "handler": pay_endpoint.handle}
    names = {name for name, f in REGISTERED_HOOKS if f.__module__ == "modules.inhouse.router"}
    assert {"startup", "shutdown", "providers_config"} <= names
    # hook owner as hooks.owner() computes it
    assert all(f.__module__.split(".")[1] == "inhouse" for _, f in REGISTERED_HOOKS)
