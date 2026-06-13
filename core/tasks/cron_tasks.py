import asyncio

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from database import async_session_maker, cancel_expired_pending_payments
from logger import logger


async def scheduled_audit_drain() -> None:
    from audit import drain_audit_redis_to_db

    try:
        drained = await drain_audit_redis_to_db(async_session_maker)
        logger.info("[AuditDrain] Ночной drain завершён, записано событий: {}", drained)
    except Exception as error:
        logger.error("[AuditDrain] Ошибка ночного drain: {}", error)


async def scheduled_stats_report() -> None:
    from handlers.admin.stats.stats_handler import send_daily_stats_report

    async with async_session_maker() as session:
        await send_daily_stats_report(session)


async def sweep_stale_payments_job() -> None:
    async with async_session_maker() as session:
        await cancel_expired_pending_payments(session)
        await session.commit()


def scheduled_audit_drain_process_runner() -> None:
    asyncio.run(scheduled_audit_drain())


def scheduled_stats_report_process_runner() -> None:
    asyncio.run(scheduled_stats_report())


def sweep_stale_payments_process_runner() -> None:
    asyncio.run(sweep_stale_payments_job())


async def cleanup_expired_gifts_job() -> None:
    from datetime import datetime

    from sqlalchemy import update as sa_update

    from database.models import Gift

    async with async_session_maker() as session:
        try:
            result = await session.execute(
                sa_update(Gift).where(Gift.expiry_time < datetime.utcnow(), Gift.is_used is False).values(is_used=True)
            )
            count = result.rowcount
            await session.commit()
            if count:
                logger.info("[GiftCleanup] Просроченных подарков помечено использованными: {}", count)
        except Exception as error:
            logger.error("[GiftCleanup] Ошибка очистки подарков: {}", error)


def cleanup_expired_gifts_process_runner() -> None:
    asyncio.run(cleanup_expired_gifts_job())


WEB_ANALYTICS_RETENTION_DAYS = 90
WEB_ERROR_RETENTION_DAYS = 30


async def cleanup_web_analytics_job() -> None:
    """Удаляет старую веб-аналитику, чтобы таблицы не росли бесконечно."""
    from datetime import datetime, timedelta, timezone as _tz

    from sqlalchemy import delete as sa_delete

    from database.models import KeyTrafficHistory
    from database.models.web import WebErrorReport, WebFlowEvent, WebPageView

    now = datetime.now(_tz.utc)
    analytics_cutoff = now - timedelta(days=WEB_ANALYTICS_RETENTION_DAYS)
    error_cutoff = now - timedelta(days=WEB_ERROR_RETENTION_DAYS)
    traffic_cutoff = (now - timedelta(days=180)).date()

    async with async_session_maker() as session:
        try:
            pv = await session.execute(sa_delete(WebPageView).where(WebPageView.created_at < analytics_cutoff))
            fe = await session.execute(sa_delete(WebFlowEvent).where(WebFlowEvent.created_at < analytics_cutoff))
            er = await session.execute(
                sa_delete(WebErrorReport).where(
                    WebErrorReport.resolved.is_(True),
                    WebErrorReport.last_seen_at < error_cutoff,
                )
            )
            th = await session.execute(sa_delete(KeyTrafficHistory).where(KeyTrafficHistory.snapshot_date < traffic_cutoff))
            from sqlalchemy import text as _sa_text

            rl_cutoff = int(now.timestamp()) - 3600
            rl = await session.execute(_sa_text("DELETE FROM rate_limit_counters WHERE window_start < :c"), {"c": rl_cutoff})
            await session.commit()
            logger.info(
                "[WebAnalyticsCleanup] удалено page_views={} flow_events={} error_reports={} traffic_history={} rate_limit={}",
                pv.rowcount,
                fe.rowcount,
                er.rowcount,
                th.rowcount,
                rl.rowcount,
            )
        except Exception as error:
            logger.error("[WebAnalyticsCleanup] ошибка очистки: {}", error)


def cleanup_web_analytics_process_runner() -> None:
    asyncio.run(cleanup_web_analytics_job())


async def abandoned_checkout_reminder_job() -> None:
    """Напоминает пользователям о незавершённой оплате (брошенный checkout)."""
    from services.abandoned_checkout import send_abandoned_checkout_reminders

    async with async_session_maker() as session:
        try:
            count = await send_abandoned_checkout_reminders(session)
            await session.commit()
            if count:
                logger.info("[AbandonedCheckout] Напоминаний отправлено: {}", count)
        except Exception as error:
            logger.error("[AbandonedCheckout] Ошибка отправки напоминаний: {}", error)


def abandoned_checkout_reminder_process_runner() -> None:
    asyncio.run(abandoned_checkout_reminder_job())


async def snapshot_key_traffic_job() -> None:
    """Дневной снапшот использованного трафика по ключам (для графиков использования)."""
    from services.traffic_history import snapshot_all_key_traffic

    async with async_session_maker() as session:
        try:
            count = await snapshot_all_key_traffic(session)
            await session.commit()
            if count:
                logger.info("[TrafficHistory] Снапшотов трафика записано: {}", count)
        except Exception as error:
            logger.error("[TrafficHistory] Ошибка снапшота трафика: {}", error)


def snapshot_key_traffic_process_runner() -> None:
    asyncio.run(snapshot_key_traffic_job())


async def log_db_pool_status() -> None:
    """Раз в минуту логирует состояние пула соединений: даёт видимость «упираемся ли в лимит»."""
    try:
        from database.db import engine

        pool = engine.pool
        size = pool.size()
        checked_out = pool.checkedout()
        checked_in = getattr(pool, "checkedin", lambda: size - checked_out)()
        overflow = getattr(pool, "overflow", lambda: -1)()
        logger.info(
            "[DBPool] size={} in_use={} idle={} overflow={}",
            size,
            checked_out,
            checked_in,
            overflow,
        )
    except Exception as error:
        logger.debug("[DBPool] не удалось получить статус пула: {}", error)


AUDIT_DRAIN_TRIGGER = CronTrigger(hour=0, minute=0, timezone="Europe/Moscow")
DAILY_STATS_REPORT_TRIGGER = CronTrigger(hour=0, minute=1, timezone="Europe/Moscow")
STALE_PAYMENTS_SWEEP_TRIGGER = CronTrigger(minute=0, timezone="Europe/Moscow")
EXPIRED_GIFTS_CLEANUP_TRIGGER = CronTrigger(hour=3, minute=0, timezone="Europe/Moscow")
WEB_ANALYTICS_CLEANUP_TRIGGER = CronTrigger(hour=3, minute=30, timezone="Europe/Moscow")
ABANDONED_CHECKOUT_TRIGGER = CronTrigger(minute=20, timezone="Europe/Moscow")
KEY_TRAFFIC_SNAPSHOT_TRIGGER = CronTrigger(hour=0, minute=10, timezone="Europe/Moscow")
DB_POOL_STATUS_TRIGGER = IntervalTrigger(minutes=1)
