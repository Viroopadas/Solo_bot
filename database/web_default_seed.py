import json
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models.web import WebFlow, WebPage, WebPageVariant, WebPageVariantBlock

DEFAULT_VARIANT_KEY = "default"
DEFAULT_VARIANT_NAME = "Основной"

_SITE_FILE = Path(__file__).with_name("default_site.json")


def _load_site() -> tuple[dict, dict, list]:
    """Возвращает (theme_tokens, pages, flows) из default_site.json.
    pages: {slug: [{type, order, data}, ...]}. '_theme' — токены темы, '_flows' — пути клиента."""
    try:
        raw = json.loads(_SITE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}, []
    theme = raw.get("_theme") or {}
    pages = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, list)}
    flows = raw.get("_flows") or []
    return theme, pages, flows


BLACK_ORANGE_THEME = {
    "primary": "#FF7A1A",
    "background": "#0A0A0A",
    "foreground": "#F2F2F2",
    "surfaceOpacity": 1,
}


def _apply_runtime_links(theme_tokens: dict) -> dict:
    try:
        from config import SUPPORT_CHAT_URL, USERNAME_BOT
    except Exception:
        return theme_tokens
    footer = theme_tokens.get("footer")
    if not isinstance(footer, dict):
        return theme_tokens
    bot_url = f"https://t.me/{USERNAME_BOT}" if USERNAME_BOT else ""
    support_url = SUPPORT_CHAT_URL or bot_url
    links = footer.get("links")
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            label = str(link.get("label", "")).strip().lower()
            if label in ("бот", "bot") and bot_url:
                link["href"] = bot_url
            elif label in ("поддержка", "support") and support_url:
                link["href"] = support_url
    return theme_tokens


async def seed_default_site(session: AsyncSession, force: bool = False) -> bool:
    """Засевает дефолтный сайт. По умолчанию (force=False) — только в пустую БД
    (идемпотентно). При force=True перезаписывает страницы дефолта (кнопка
    «Установить дефолтный дизайн»). Возвращает True, если что-то записано."""
    if not force:
        existing = await session.execute(select(func.count(WebPageVariantBlock.id)))
        if (existing.scalar() or 0) > 0:
            return False

    theme, pages, flows = _load_site()
    theme_tokens = theme or BLACK_ORANGE_THEME
    theme_tokens = _apply_runtime_links(theme_tokens)

    seeded = False
    for slug, blocks in pages.items():
        page = (
            await session.execute(select(WebPage).where(WebPage.slug == slug))
        ).scalar_one_or_none()
        if page is None:
            session.add(WebPage(slug=slug, title=slug))
            await session.flush()

        variant = (
            await session.execute(
                select(WebPageVariant).where(
                    WebPageVariant.page_slug == slug,
                    WebPageVariant.variant_key == DEFAULT_VARIANT_KEY,
                )
            )
        ).scalar_one_or_none()
        if variant is None:
            variant = WebPageVariant(
                page_slug=slug,
                variant_key=DEFAULT_VARIANT_KEY,
                name=DEFAULT_VARIANT_NAME,
                is_active=True,
                theme_tokens=dict(theme_tokens),
            )
            session.add(variant)
            await session.flush()
        else:
            variant.is_active = True
            variant.theme_tokens = dict(theme_tokens)
            if force:
                await session.execute(
                    delete(WebPageVariantBlock).where(
                        WebPageVariantBlock.variant_id == variant.id
                    )
                )

        if force:
            await session.execute(
                update(WebPageVariant)
                .where(
                    WebPageVariant.page_slug == slug,
                    WebPageVariant.variant_key != DEFAULT_VARIANT_KEY,
                )
                .values(is_active=False)
            )

        for order, block in enumerate(blocks):
            session.add(
                WebPageVariantBlock(
                    variant_id=variant.id,
                    order=order,
                    type=block["type"],
                    data=dict(block["data"]),
                )
            )
        seeded = True

    for flow in flows:
        if not isinstance(flow, dict):
            continue
        fid = str(flow.get("id") or "").strip()
        if not fid:
            continue
        nodes = list(flow.get("nodes") or [])
        edges = list(flow.get("edges") or [])
        name = flow.get("name") or fid
        entry = flow.get("entry_node_id")
        existing = await session.get(WebFlow, fid)
        if existing is None:
            session.add(
                WebFlow(id=fid, name=name, nodes=nodes, edges=edges, entry_node_id=entry, version=1)
            )
            seeded = True
        elif force:
            existing.name = name
            existing.nodes = nodes
            existing.edges = edges
            existing.entry_node_id = entry
            existing.version = (existing.version or 0) + 1
            seeded = True

    await session.flush()
    return seeded
