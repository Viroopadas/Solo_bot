"""Test harness for modules/inhouse.

Run from OUTSIDE the bot folder (so the bot's config.py with live keys can never be
imported), in a throwaway venv with aiogram==3.24.0, yookassa==3.9.0, SQLAlchemy 2.0,
aiosqlite, pytest, pytest-asyncio:

    cd <somewhere outside the bot>
    python -m pytest <bot>/modules/inhouse/tests -p no:cacheprovider

The bot root is NOT put on sys.path: a synthetic `modules` package points at
<bot>/modules; `logger`, `hooks.hooks` and `config` are fakes (tests/helpers.py). The
closed YooKassa provider is replaced by a fake service that calls the real yookassa SDK
with a faked HTTP client.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys

from pathlib import Path

import pytest


MODULES_DIR = Path(__file__).resolve().parents[2]

if not getattr(sys.modules.get("modules"), "__inhouse_fake__", False):
    _spec = importlib.machinery.ModuleSpec("modules", None, is_package=True)
    _pkg = importlib.util.module_from_spec(_spec)
    _pkg.__path__ = [str(MODULES_DIR)]
    _pkg.__inhouse_fake__ = True
    sys.modules["modules"] = _pkg

from modules.inhouse.tests import helpers  # noqa: E402  (installs logger/hooks/config fakes)
from modules.inhouse.features.yookassa_methods import compat, menu_mode, sdk_patch, state, store, watch  # noqa: E402
from modules.inhouse.features.yookassa_methods import settings as yk_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    helpers.FAKE_LOGGER.records.clear()
    state.STATE.active = False
    state.STATE.starting = False
    state.STATE.deps = None
    state.STATE.undo = []
    store.LOCKS._locks.clear()
    monkeypatch.setattr(state, "module_enabled", lambda: True)
    compat._flavor_cache.clear()
    compat._flavor_cache["flavor"] = compat.V512
    # The watch never touches the bot's real storage/ in tests.
    monkeypatch.setattr(yk_settings, "WATCH_STATE_FILE", tmp_path / "watch-storage" / "watch.json")
    monkeypatch.setattr(yk_settings, "OBSERVER_SECRET_FILE", tmp_path / "watch-storage" / "observer.secret")
    menu_mode.set_mode(menu_mode.LINKS)
    watch.reset()
    yield
    menu_mode.set_mode(menu_mode.LINKS)
    watch.reset()
    sdk_patch.uninstall()
    state.STATE.active = False
    state.STATE.deps = None


@pytest.fixture
def logs():
    return helpers.FAKE_LOGGER


@pytest.fixture
async def db(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'bot.db').as_posix()}")
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(
            text(
                "CREATE TABLE payments (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id BIGINT, amount FLOAT, "
                "payment_system TEXT, status TEXT, payment_id TEXT, currency TEXT DEFAULT 'RUB')"
            )
        )
        await session.execute(
            text("CREATE TABLE temporary_data (tg_id BIGINT PRIMARY KEY, state TEXT, data JSON, updated_at TIMESTAMP)")
        )
        await store.ensure_table(session)
        await session.commit()
    yield maker
    await engine.dispose()


@pytest.fixture
def yk_api(monkeypatch):
    from yookassa import Configuration
    from yookassa.client import ApiClient

    Configuration.configure("100500", "test_not_a_real_key")
    api = helpers.FakeYooKassaApi()
    monkeypatch.setattr(ApiClient, "request", api)
    return api
