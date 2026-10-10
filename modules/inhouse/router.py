"""SoloBot module entry of inhouse (modules/inhouse/router.py).

Contract of the SoloBot module loader (utils/modules_loader.py, web/__init__.py):
- `router`: aiogram Router included into modules_hub;
- `get_webhook_data()`: one POST path served by the bot's web server;
- hooks registered with hooks.hooks.register_hook; run_hooks skips them while the module is
  disabled in the admin panel (owner = "inhouse").

Features live in features/<name>/ and are switched in settings.py (FEATURES). Nothing
closed (compiled) is imported here: every feature imports bot internals lazily at start.
Feature start runs as a background task from the startup hook, so it is not bound by the
4-second hook timeout; until it finishes the bot shows the stock screens.
"""

from __future__ import annotations

import asyncio

from aiogram import Router

from hooks.hooks import register_hook
from logger import logger

from .features.yookassa_methods import (
    feature as yk_feature,
    pay_endpoint as yk_endpoint,
    providers as yk_providers,
    settings as yk_settings,
)


router = Router(name="inhouse")

_tasks: dict[str, asyncio.Task] = {}


def get_webhook_data():
    return {"path": yk_settings.WEBHOOK_PATH, "handler": yk_endpoint.handle}


async def _start_features(bot) -> None:
    try:
        await yk_feature.start(bot)
    except Exception as exc:
        logger.error(f"[inhouse] feature start crashed: {type(exc).__name__}")


@register_hook("startup")
async def inhouse_startup(bot=None, **kwargs: object):
    task = _tasks.get("start")
    if task is not None and not task.done():
        return
    _tasks["start"] = asyncio.create_task(_start_features(bot))


@register_hook("shutdown")
async def inhouse_shutdown(**kwargs: object):
    task = _tasks.pop("start", None)
    if task is not None and not task.done():
        task.cancel()
    try:
        yk_feature.stop()
    except Exception as exc:
        logger.error(f"[inhouse] feature stop failed: {type(exc).__name__}")


@register_hook("providers_config")
async def inhouse_providers_config(providers=None, flags=None, **kwargs: object):
    return yk_providers.providers_patch(providers, flags)
