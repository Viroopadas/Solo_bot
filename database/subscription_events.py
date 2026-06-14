from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    DailySubscriptionMetric,
    Key,
    Payment,
    SubscriptionEvent,
)
from logger import logger


async def record_subscription_event(
    session: AsyncSession,
    *,
    event_type: str,
    user_id: int | None = None,
    tg_id: int | None = None,
    client_id: str | None = None,
    tariff_id: int | None = None,
    server_id: str | None = None,
    price_rub: float | None = None,
    duration_days: int | None = None,
    expiry_time: int | None = None,
    was_expired: bool | None = None,
    source: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Добавляет событие в журнал. Никогда не бросает исключение — логирование
    подписок не должно ломать операции с ключами. Коммит — за вызывающим (контракт транзакций)."""
    try:
        session.add(
            SubscriptionEvent(
                event_type=event_type,
                user_id=user_id,
                tg_id=tg_id,
                client_id=str(client_id)[:128] if client_id else None,
                tariff_id=tariff_id,
                server_id=server_id,
                price_rub=float(price_rub) if price_rub is not None else None,
                duration_days=duration_days,
                expiry_time=expiry_time,
                was_expired=was_expired,
                source=source,
                metadata_=metadata,
            )
        )
    except Exception as e:
        logger.warning("[sub-events] не удалось записать событие {}: {}", event_type, e)


async def backfill_from_payments(session: AsyncSession) -> int:
    """Ретроспективно засеивает журнал из истории платежей: первый успешный платёж
    юзера → created, последующие → renewed. Идемпотентно (пропускает, если уже сеяли)."""
    already = await session.scalar(
        select(func.count()).select_from(SubscriptionEvent).where(SubscriptionEvent.source == "backfill")
    )
    if already:
        return 0

    rows = (
        await session.execute(
            select(Payment.user_id, Payment.tg_id, Payment.amount, Payment.created_at)
            .where(Payment.status == "success")
            .order_by(Payment.user_id, Payment.created_at)
        )
    ).all()

    seen: set[int] = set()
    count = 0
    for r in rows:
        uid = r.user_id
        is_first = uid not in seen
        if uid is not None:
            seen.add(uid)
        ev = SubscriptionEvent(
            event_type="created" if is_first else "renewed",
            user_id=uid,
            tg_id=r.tg_id,
            price_rub=float(r.amount) if r.amount is not None else None,
            source="backfill",
            created_at=r.created_at,
        )
        session.add(ev)
        count += 1
    logger.info("[sub-events] backfill из платежей: засеяно {} событий", count)
    return count


async def snapshot_daily_metrics(session: AsyncSession) -> None:
    """Дневной снапшот: активные подписки сейчас + события за прошедшие сутки (UTC).
    Пишет/обновляет строку за вчерашний день."""
    now = datetime.utcnow()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    target_date = (now - timedelta(days=1)).date()
    day_start = datetime(target_date.year, target_date.month, target_date.day)
    day_end = day_start + timedelta(days=1)

    active = (await session.scalar(
        select(func.count()).select_from(Key).where(Key.expiry_time > now_ms)
    )) or 0

    ev_rows = (
        await session.execute(
            select(SubscriptionEvent.event_type, func.count().label("cnt"))
            .where(SubscriptionEvent.created_at >= day_start)
            .where(SubscriptionEvent.created_at < day_end)
            .group_by(SubscriptionEvent.event_type)
        )
    ).all()
    ev = {r.event_type: int(r.cnt) for r in ev_rows}

    revenue = (await session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0))
        .where(Payment.status == "success")
        .where(Payment.created_at >= day_start)
        .where(Payment.created_at < day_end)
    )) or 0

    tariff_rows = (
        await session.execute(
            select(Key.tariff_id, func.count().label("cnt"))
            .where(Key.expiry_time > now_ms).group_by(Key.tariff_id)
        )
    ).all()
    server_rows = (
        await session.execute(
            select(Key.server_id, func.count().label("cnt"))
            .where(Key.expiry_time > now_ms).group_by(Key.server_id)
        )
    ).all()
    by_tariff = {str(r.tariff_id): int(r.cnt) for r in tariff_rows}
    by_server = {str(r.server_id): int(r.cnt) for r in server_rows}

    values = dict(
        snapshot_date=target_date,
        active=int(active),
        created=ev.get("created", 0),
        renewed=ev.get("renewed", 0),
        expired=ev.get("expired", 0),
        deleted=ev.get("deleted", 0),
        revenue_rub=float(revenue),
        by_tariff=by_tariff,
        by_server=by_server,
    )
    stmt = pg_insert(DailySubscriptionMetric).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[DailySubscriptionMetric.snapshot_date],
        set_={k: v for k, v in values.items() if k != "snapshot_date"},
    )
    await session.execute(stmt)


async def get_subscription_dynamics(session: AsyncSession, days: int) -> dict:
    """Данные для аналитики: события подписок по дням (из журнала, есть backfill-история)
    + тренд активных подписок по дням (из снапшотов, накапливается с момента деплоя)."""
    since = datetime.utcnow() - timedelta(days=days)

    day = func.date_trunc("day", SubscriptionEvent.created_at).label("day")
    ev_rows = (
        await session.execute(
            select(day, SubscriptionEvent.event_type, func.count().label("cnt"))
            .where(SubscriptionEvent.created_at >= since)
            .group_by(day, SubscriptionEvent.event_type)
            .order_by(day)
        )
    ).all()
    daily_map: dict[str, dict[str, int]] = {}
    for r in ev_rows:
        d = r.day.strftime("%Y-%m-%d")
        cur = daily_map.setdefault(d, {"created": 0, "renewed": 0, "expired": 0, "deleted": 0})
        if r.event_type in cur:
            cur[r.event_type] = int(r.cnt)

    since_date = since.date()
    snap_rows = (
        await session.execute(
            select(DailySubscriptionMetric.snapshot_date, DailySubscriptionMetric.active)
            .where(DailySubscriptionMetric.snapshot_date >= since_date)
            .order_by(DailySubscriptionMetric.snapshot_date)
        )
    ).all()

    return {
        "dailyEvents": [
            {
                "date": d,
                "created": v["created"],
                "renewed": v["renewed"],
                "expired": v["expired"] + v["deleted"],
            }
            for d, v in sorted(daily_map.items())
        ],
        "activeTrend": [
            {"date": r.snapshot_date.strftime("%Y-%m-%d"), "active": int(r.active)}
            for r in snap_rows
        ],
    }
