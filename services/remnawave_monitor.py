import asyncio
from html import escape as html_escape
from typing import Any

from config import ADMIN_ID, REMNAWAVE_LOGIN, REMNAWAVE_PASSWORD, REMNAWAVE_TOKEN_LOGIN_ENABLED
from core.settings.remnawave_config import (
    REMNAWAVE_CONFIG,
    get_host_rotation_allowed,
    is_host_rotation_enabled,
    is_node_health_enabled,
    update_remnawave_config,
)
from database import async_session_maker, get_servers
from logger import logger
from panels.remnawave import RemnawaveAPI


NODE_HEALTH_DEFAULT_INTERVAL_MIN = 5
HOST_ROTATION_DEFAULT_INTERVAL_MIN = 60
TICK_SLEEP_SEC = 30


async def _collect_remnawave_panels() -> list[str]:
    async with async_session_maker() as session:
        servers = await get_servers(session, include_enabled=True)

    seen: set[str] = set()
    panels: list[str] = []
    for cluster in servers.values():
        for srv in cluster:
            if srv.get("panel_type") != "remnawave":
                continue
            api_url = (srv.get("api_url") or "").strip()
            if not api_url or api_url in seen:
                continue
            seen.add(api_url)
            panels.append(api_url)
    return panels


async def _login_api(api_url: str) -> RemnawaveAPI | None:
    api = RemnawaveAPI(api_url)
    try:
        if REMNAWAVE_TOKEN_LOGIN_ENABLED:
            return api
        ok = await api.login(REMNAWAVE_LOGIN, REMNAWAVE_PASSWORD)
        if not ok:
            await api.aclose()
            return None
        return api
    except Exception as exc:
        logger.warning("[Remnawave-Monitor] Логин на {} провалился: {}", api_url, exc)
        try:
            await api.aclose()
        except Exception:
            pass
        return None


def _build_servers_kb():
    from aiogram.types import InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from handlers.admin.panel.keyboard import AdminPanelCallback

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🖥️ К серверам",
            callback_data=AdminPanelCallback(action="clusters").pack(),
        )
    )
    return builder.as_markup()


async def _send_to_admins(bot, text: str, reply_markup=None) -> None:
    if not ADMIN_ID:
        return
    for admin_id in ADMIN_ID:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as exc:
            logger.warning("[Remnawave-Monitor] Не удалось отправить уведомление {}: {}", admin_id, exc)


def _is_node_alive(node: dict[str, Any]) -> bool:
    if node.get("isDisabled"):
        return True
    return bool(node.get("isConnected"))


async def _node_health_tick(bot) -> None:
    panels = await _collect_remnawave_panels()
    if not panels:
        return

    last_states: dict[str, dict[str, Any]] = dict(REMNAWAVE_CONFIG.get("NODE_HEALTH_LAST_STATES") or {})
    next_states: dict[str, dict[str, Any]] = {}
    alerts: list[tuple[str, dict[str, Any], bool]] = []

    for api_url in panels:
        api = await _login_api(api_url)
        if api is None:
            continue
        try:
            nodes = await api.get_all_nodes() or []
        finally:
            try:
                await api.aclose()
            except Exception:
                pass

        for node in nodes:
            uuid = node.get("uuid")
            if not uuid:
                continue
            alive = _is_node_alive(node)
            state_key = f"{api_url}::{uuid}"
            next_states[state_key] = {
                "alive": alive,
                "name": node.get("name") or "",
                "address": node.get("address") or "",
                "isDisabled": bool(node.get("isDisabled")),
                "lastStatusMessage": node.get("lastStatusMessage") or "",
            }
            previous = last_states.get(state_key)
            previously_alive = previous.get("alive", True) if previous else True
            if alive != previously_alive:
                alerts.append((state_key, next_states[state_key], alive))

    if alerts:
        lines: list[str] = ["<b>🌀 Remnawave: изменение состояния нод</b>"]
        for _, info, alive in alerts:
            name = html_escape(info.get("name") or "—")
            address = html_escape(info.get("address") or "—")
            if alive:
                lines.append(f"✅ <b>{name}</b> ({address}) — снова на связи")
            else:
                reason = info.get("lastStatusMessage") or "соединение потеряно"
                lines.append(f"⚠️ <b>{name}</b> ({address}) — {html_escape(str(reason))}")
        await _send_to_admins(bot, "\n".join(lines), reply_markup=_build_servers_kb())

    if next_states != last_states:
        new_cfg = dict(REMNAWAVE_CONFIG)
        new_cfg["NODE_HEALTH_LAST_STATES"] = next_states
        async with async_session_maker() as session:
            await update_remnawave_config(session, new_cfg)


def _normalise_address(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().lower().rstrip(".")


async def _host_rotation_tick() -> None:
    allowed = get_host_rotation_allowed()
    if not allowed:
        return

    panels = await _collect_remnawave_panels()
    if not panels:
        return

    for api_url in panels:
        api = await _login_api(api_url)
        if api is None:
            continue
        try:
            nodes = await api.get_all_nodes() or []
            hosts_data = await api.get_hosts() or []

            if not isinstance(hosts_data, list) or not hosts_data:
                continue

            node_load_by_address: dict[str, int] = {}
            for node in nodes:
                address = _normalise_address(node.get("address"))
                if not address:
                    continue
                online = int(node.get("usersOnline") or 0)
                current = node_load_by_address.get(address)
                node_load_by_address[address] = online if current is None else min(current, online)

            hosts_sorted = sorted(hosts_data, key=lambda h: int(h.get("viewPosition") or 0))

            movable: list[dict[str, Any]] = []
            movable_positions: list[int] = []
            new_layout: list[dict[str, Any] | None] = []
            for idx, host in enumerate(hosts_sorted):
                host_uuid = host.get("uuid")
                if not host_uuid:
                    continue
                if host_uuid in allowed:
                    movable.append(host)
                    movable_positions.append(idx)
                    new_layout.append(None)
                else:
                    new_layout.append(host)

            if len(movable) < 2:
                continue

            def host_load(host: dict[str, Any]) -> int:
                host_addr = _normalise_address(host.get("address"))
                sni_addr = _normalise_address(host.get("sni"))
                for candidate in (host_addr, sni_addr):
                    if candidate and candidate in node_load_by_address:
                        return node_load_by_address[candidate]
                return 10**9

            movable_sorted = sorted(movable, key=host_load)

            for slot_idx, host in zip(movable_positions, movable_sorted):
                new_layout[slot_idx] = host

            reorder_payload: list[dict[str, Any]] = []
            changed = False
            for idx, host in enumerate(new_layout):
                if host is None:
                    continue
                new_view_position = idx + 1
                reorder_payload.append({"uuid": host["uuid"], "viewPosition": new_view_position})
                if int(host.get("viewPosition") or 0) != new_view_position:
                    changed = True

            if not changed:
                continue

            ok = await api.reorder_hosts(reorder_payload)
            if ok:
                logger.info(
                    "[Remnawave-Monitor] hosts reordered on {}: payload {} элементов",
                    api_url,
                    len(reorder_payload),
                )
        finally:
            try:
                await api.aclose()
            except Exception:
                pass


async def remnawave_monitor_loop(bot, _sessionmaker) -> None:
    last_node_tick = 0.0
    last_rotation_tick = 0.0

    loop = asyncio.get_event_loop()
    while True:
        try:
            now = loop.time()
            node_interval = max(1, int(REMNAWAVE_CONFIG.get("NODE_HEALTH_INTERVAL_MIN") or NODE_HEALTH_DEFAULT_INTERVAL_MIN)) * 60
            rotation_interval = max(5, int(REMNAWAVE_CONFIG.get("HOST_ROTATION_INTERVAL_MIN") or HOST_ROTATION_DEFAULT_INTERVAL_MIN)) * 60

            if is_node_health_enabled() and (now - last_node_tick) >= node_interval:
                last_node_tick = now
                try:
                    await _node_health_tick(bot)
                except Exception as exc:
                    logger.error("[Remnawave-Monitor] Ошибка node health tick: {}", exc)

            if is_host_rotation_enabled() and (now - last_rotation_tick) >= rotation_interval:
                last_rotation_tick = now
                try:
                    await _host_rotation_tick()
                except Exception as exc:
                    logger.error("[Remnawave-Monitor] Ошибка host rotation tick: {}", exc)
        except Exception as exc:
            logger.error("[Remnawave-Monitor] Внешняя ошибка: {}", exc)

        await asyncio.sleep(TICK_SLEEP_SEC)
