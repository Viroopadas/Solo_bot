from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from config import DATABASE_URL
from logger import logger


def _is_postgresql() -> bool:
    u = (DATABASE_URL or "").lower()
    return "+asyncpg" in u or u.startswith("postgresql")


async def _table_exists(conn: AsyncConnection, table: str) -> bool:
    r = await conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = :t
            """
        ),
        {"t": table},
    )
    return r.first() is not None


async def _ensure_migrations_table(conn: AsyncConnection) -> None:
    if not await _table_exists(conn, "schema_migrations"):
        await conn.execute(
            text(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    description TEXT
                )
                """
            )
        )


async def _get_current_version(conn: AsyncConnection) -> int:
    await _ensure_migrations_table(conn)
    r = await conn.execute(text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations"))
    row = r.first()
    return int(row[0]) if row else 0


async def _mark_migration_applied(conn: AsyncConnection, version: int, description: str) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO schema_migrations (version, description)
            VALUES (:v, :d)
            ON CONFLICT (version) DO NOTHING
            """
        ),
        {"v": version, "d": description},
    )


async def _users_pk_columns(conn: AsyncConnection) -> list[str]:
    r = await conn.execute(
        text(
            """
            SELECT a.attname
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord) ON true
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = u.attnum
            WHERE n.nspname = 'public'
              AND t.relname = 'users'
              AND c.contype = 'p'
            ORDER BY u.ord
            """
        )
    )
    return [row[0] for row in r.all()]


async def _column_exists(conn: AsyncConnection, table: str, column: str) -> bool:
    r = await conn.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :t AND column_name = :c
            """
        ),
        {"t": table, "c": column},
    )
    return r.first() is not None


async def _column_is_identity(conn: AsyncConnection, table: str, column: str) -> bool:
    r = await conn.execute(
        text(
            """
            SELECT is_identity
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :t AND column_name = :c
            """
        ),
        {"t": table, "c": column},
    )
    row = r.first()
    return bool(row and str(row[0]).upper() == "YES")


async def _constraint_exists(conn: AsyncConnection, table: str, constraint: str) -> bool:
    r = await conn.execute(
        text(
            """
            SELECT 1
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public'
              AND t.relname = :t
              AND c.conname = :c
            """
        ),
        {"t": table, "c": constraint},
    )
    return r.first() is not None


async def _exec_ignore(conn: AsyncConnection, sql: str) -> None:
    try:
        async with conn.begin_nested():
            await conn.execute(text(sql))
    except Exception as e:
        logger.debug(f"[schema_upgrade] skip: {e}")


async def _add_constraint_if_missing(conn: AsyncConnection, table: str, name: str, sql: str) -> None:
    if await _constraint_exists(conn, table, name):
        return
    await _exec_ignore(conn, sql)


async def _drop_fkeys_to_users(conn: AsyncConnection) -> None:
    if not await _table_exists(conn, "users"):
        return
    r = await conn.execute(
        text(
            """
            SELECT con.conname, rel.relname AS src_table
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
            WHERE con.confrelid = 'users'::regclass
              AND con.contype = 'f'
              AND nsp.nspname = 'public'
            """
        )
    )
    for row in r.all():
        cname, src = row[0], row[1]
        await conn.execute(text(f'ALTER TABLE "{src}" DROP CONSTRAINT IF EXISTS "{cname}"'))


async def _drop_pk(conn: AsyncConnection, table: str) -> None:
    if not await _table_exists(conn, table):
        return
    r = await conn.execute(
        text(
            """
            SELECT tc.constraint_name
            FROM information_schema.table_constraints tc
            WHERE tc.table_schema = 'public'
              AND tc.table_name = :t
              AND tc.constraint_type = 'PRIMARY KEY'
            """
        ),
        {"t": table},
    )
    row = r.first()
    if row:
        await conn.execute(text(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{row[0]}"'))


async def _column_has_nulls(conn: AsyncConnection, table: str, column: str) -> bool:
    r = await conn.execute(text(f'SELECT 1 FROM "{table}" WHERE "{column}" IS NULL LIMIT 1'))
    return r.first() is not None


async def _safe_set_not_null(conn: AsyncConnection, table: str, column: str) -> bool:
    if await _column_has_nulls(conn, table, column):
        logger.warning(f"[schema_upgrade] {table}.{column} содержит NULL, пропуск SET NOT NULL")
        return False
    await conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET NOT NULL'))
    return True


async def _index_exists(conn: AsyncConnection, table: str, index: str) -> bool:
    r = await conn.execute(
        text(
            """
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = :t
              AND indexname = :i
            """
        ),
        {"t": table, "i": index},
    )
    return r.first() is not None


async def _ensure_users_id_referenceable(conn: AsyncConnection) -> None:
    if not await _column_exists(conn, "users", "id"):
        return
    if await _column_is_identity(conn, "users", "id"):
        await _exec_ignore(conn, "UPDATE users SET id = DEFAULT WHERE id IS NULL")
        await _exec_ignore(
            conn,
            """
            WITH d AS (
                SELECT ctid, row_number() OVER (PARTITION BY id ORDER BY ctid) AS rn
                FROM users
                WHERE id IS NOT NULL
            )
            UPDATE users u
            SET id = DEFAULT
            FROM d
            WHERE u.ctid = d.ctid AND d.rn > 1
            """,
        )
    else:
        await _exec_ignore(conn, "CREATE SEQUENCE IF NOT EXISTS users_id_seq")
        await _exec_ignore(conn, "ALTER TABLE users ALTER COLUMN id SET DEFAULT nextval('users_id_seq')")
        await _exec_ignore(conn, "ALTER SEQUENCE users_id_seq OWNED BY users.id")
        await _exec_ignore(conn, "UPDATE users SET id = nextval('users_id_seq') WHERE id IS NULL")
        await _exec_ignore(
            conn,
            """
            WITH d AS (
                SELECT ctid, row_number() OVER (PARTITION BY id ORDER BY ctid) AS rn
                FROM users
                WHERE id IS NOT NULL
            )
            UPDATE users u
            SET id = nextval('users_id_seq')
            FROM d
            WHERE u.ctid = d.ctid AND d.rn > 1
            """,
        )
    await _exec_ignore(conn, "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_id ON users (id)")


async def _migration_v1_add_users_id(conn: AsyncConnection) -> None:
    if not await _table_exists(conn, "users"):
        return

    pk = await _users_pk_columns(conn)
    if not pk:
        return
    if pk == ["id"]:
        return

    if pk != ["tg_id"]:
        logger.warning(f"[schema_upgrade] users PK неожиданен {pk}, пропуск v1")
        return

    logger.info("[schema_upgrade] v1: Добавление users.id")

    if not await _column_exists(conn, "users", "id"):
        await conn.execute(
            text(
                """
                ALTER TABLE users
                ADD COLUMN id BIGINT GENERATED BY DEFAULT AS IDENTITY NOT NULL
                """
            )
        )
    await _ensure_users_id_referenceable(conn)


async def _migration_v2_add_user_id_columns(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v2: Добавление user_id колонок в связанные таблицы")

    tables_columns = [
        ("keys", "user_id"),
        ("payments", "user_id"),
        ("referrals", "referred_user_id"),
        ("referrals", "referrer_user_id"),
        ("notifications", "user_id"),
        ("scheduled_broadcasts", "created_by_user_id"),
        ("gifts", "sender_user_id"),
        ("gifts", "recipient_user_id"),
        ("gift_usages", "user_id"),
        ("coupon_usages", "account_user_id"),
        ("temporary_data", "user_id"),
        ("manual_bans", "user_id"),
        ("blocked_users", "user_id"),
    ]

    for table, column in tables_columns:
        if await _table_exists(conn, table) and not await _column_exists(conn, table, column):
            await conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {column} BIGINT'))


async def _backfill_users_from_table(conn: AsyncConnection, table: str, tg_col: str = "tg_id") -> int:
    """Auto-создание users для orphan tg_id'ов из указанной таблицы.

    Legacy клиенты обновляются с TG-only схемы (где только tg_id), и в связанных
    таблицах могут быть строки, ссылающиеся на tg_id, которого нет в users. Вместо
    удаления таких строк — создаём минимальную users-запись, чтобы FK/NOT NULL
    проходили и данные сохранялись.
    """
    if not await _table_exists(conn, table):
        return 0
    if not await _column_exists(conn, table, tg_col):
        return 0
    if not await _table_exists(conn, "users"):
        return 0
    if not await _column_exists(conn, "users", "tg_id"):
        return 0

    has_created_at = await _column_exists(conn, "users", "created_at")
    has_updated_at = await _column_exists(conn, "users", "updated_at")

    cols = ["tg_id"]
    vals = [f't."{tg_col}"']
    if has_created_at:
        cols.append("created_at")
        vals.append("NOW()")
    if has_updated_at:
        cols.append("updated_at")
        vals.append("NOW()")

    cols_sql = ", ".join(cols)
    vals_sql = ", ".join(vals)

    result = await conn.execute(
        text(
            f"""
            INSERT INTO users ({cols_sql})
            SELECT DISTINCT {vals_sql}
            FROM "{table}" t
            WHERE t."{tg_col}" IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM users u WHERE u.tg_id = t."{tg_col}"
              )
            """
        )
    )
    created = result.rowcount or 0
    if created > 0:
        logger.info(f"[schema_upgrade] users backfill: создано {created} юзеров из orphan {table}.{tg_col}")
    return created


async def _migration_v3_populate_user_ids(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v3: Заполнение user_id из tg_id")

    if not await _table_exists(conn, "users") or not await _column_exists(conn, "users", "id"):
        return

    updates = [
        ("keys", "user_id", "tg_id"),
        ("payments", "user_id", "tg_id"),
        ("notifications", "user_id", "tg_id"),
        ("scheduled_broadcasts", "created_by_user_id", "created_by_tg_id"),
        ("gift_usages", "user_id", "tg_id"),
        ("temporary_data", "user_id", "tg_id"),
        ("manual_bans", "user_id", "tg_id"),
        ("blocked_users", "user_id", "tg_id"),
    ]

    for table, _user_col, tg_col in updates:
        await _backfill_users_from_table(conn, table, tg_col)

    for table, user_col, tg_col in updates:
        if await _table_exists(conn, table) and await _column_exists(conn, table, user_col):
            result = await conn.execute(
                text(
                    f"""
                    UPDATE "{table}" t
                    SET {user_col} = u.id
                    FROM users u
                    WHERE t.{user_col} IS NULL AND t.{tg_col} = u.tg_id
                    """
                )
            )
            updated = result.rowcount
            if updated > 0:
                logger.debug(f"[schema_upgrade] v3: заполнено {updated} записей {user_col} в {table}")

            null_count = await conn.execute(text(f'SELECT COUNT(*) FROM "{table}" WHERE {user_col} IS NULL'))
            nulls = null_count.scalar()
            if nulls > 0:
                logger.warning(f"[schema_upgrade] v3: в {table} осталось {nulls} записей с NULL {user_col}")

    if await _table_exists(conn, "referrals"):
        await _backfill_users_from_table(conn, "referrals", "referred_tg_id")
        await _backfill_users_from_table(conn, "referrals", "referrer_tg_id")
        if await _column_exists(conn, "referrals", "referred_user_id"):
            await conn.execute(
                text(
                    """
                    UPDATE referrals r
                    SET referred_user_id = u.id
                    FROM users u
                    WHERE r.referred_user_id IS NULL AND r.referred_tg_id = u.tg_id
                    """
                )
            )
        if await _column_exists(conn, "referrals", "referrer_user_id"):
            await conn.execute(
                text(
                    """
                    UPDATE referrals r
                    SET referrer_user_id = u.id
                    FROM users u
                    WHERE r.referrer_user_id IS NULL AND r.referrer_tg_id = u.tg_id
                    """
                )
            )

    if await _table_exists(conn, "gifts"):
        await _backfill_users_from_table(conn, "gifts", "sender_tg_id")
        await _backfill_users_from_table(conn, "gifts", "recipient_tg_id")
        if await _column_exists(conn, "gifts", "sender_user_id"):
            await conn.execute(
                text(
                    """
                    UPDATE gifts g
                    SET sender_user_id = u.id
                    FROM users u
                    WHERE g.sender_user_id IS NULL AND g.sender_tg_id = u.tg_id
                    """
                )
            )
        if await _column_exists(conn, "gifts", "recipient_user_id"):
            await conn.execute(
                text(
                    """
                    UPDATE gifts g
                    SET recipient_user_id = u.id
                    FROM users u
                    WHERE g.recipient_user_id IS NULL AND g.recipient_tg_id IS NOT NULL
                      AND g.recipient_tg_id = u.tg_id
                    """
                )
            )

    if await _table_exists(conn, "coupon_usages") and await _column_exists(conn, "coupon_usages", "account_user_id"):
        await _backfill_users_from_table(conn, "coupon_usages", "user_id")
        await conn.execute(
            text(
                """
                UPDATE coupon_usages c
                SET account_user_id = u.id
                FROM users u
                WHERE c.account_user_id IS NULL AND c.user_id = u.tg_id
                """
            )
        )


async def _migration_v4_add_tg_id_mirrors(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v4: Добавление tg_id mirror колонок")

    mirrors = [
        ("referrals", "referred_tg_id"),
        ("referrals", "referrer_tg_id"),
        ("notifications", "tg_id"),
        ("gift_usages", "tg_id"),
        ("keys", "tg_id"),
        ("payments", "tg_id"),
        ("gifts", "sender_tg_id"),
        ("gifts", "recipient_tg_id"),
        ("scheduled_broadcasts", "created_by_tg_id"),
        ("coupon_usages", "tg_id"),
        ("temporary_data", "tg_id"),
        ("manual_bans", "tg_id"),
        ("blocked_users", "tg_id"),
    ]

    for table, column in mirrors:
        if await _table_exists(conn, table) and not await _column_exists(conn, table, column):
            await conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {column} BIGINT'))


async def _migration_v5_switch_pks_to_user_id(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v5: Переключение PK на user_id где возможно")

    await _drop_fkeys_to_users(conn)
    await _ensure_users_id_referenceable(conn)

    if await _table_exists(conn, "referrals"):
        can_harden = not await _column_has_nulls(conn, "referrals", "referred_user_id")
        can_harden = can_harden and not await _column_has_nulls(conn, "referrals", "referrer_user_id")
        if can_harden:
            await _drop_pk(conn, "referrals")
            await conn.execute(text("ALTER TABLE referrals ALTER COLUMN referred_user_id SET NOT NULL"))
            await conn.execute(text("ALTER TABLE referrals ALTER COLUMN referrer_user_id SET NOT NULL"))
            await conn.execute(text("ALTER TABLE referrals ADD PRIMARY KEY (referred_user_id, referrer_user_id)"))
        else:
            logger.warning("[schema_upgrade] referrals содержит NULL user_id, пропуск перевода PK")

    if await _table_exists(conn, "notifications") and await _safe_set_not_null(conn, "notifications", "user_id"):
        await _drop_pk(conn, "notifications")
        await conn.execute(text("ALTER TABLE notifications ADD PRIMARY KEY (user_id, notification_type)"))

    if await _table_exists(conn, "gift_usages") and await _safe_set_not_null(conn, "gift_usages", "user_id"):
        await _drop_pk(conn, "gift_usages")
        await conn.execute(text("ALTER TABLE gift_usages ADD PRIMARY KEY (gift_id, user_id)"))

    if await _table_exists(conn, "coupon_usages"):
        await _drop_pk(conn, "coupon_usages")
        has_user_id = await _column_exists(conn, "coupon_usages", "user_id")
        has_account_user_id = await _column_exists(conn, "coupon_usages", "account_user_id")
        if has_account_user_id and not has_user_id:
            await conn.execute(text("ALTER TABLE coupon_usages RENAME COLUMN account_user_id TO user_id"))
        elif has_account_user_id and has_user_id:
            await conn.execute(
                text(
                    """
                    UPDATE coupon_usages
                    SET user_id = account_user_id
                    WHERE account_user_id IS NOT NULL
                    """
                )
            )
            await conn.execute(text("ALTER TABLE coupon_usages DROP COLUMN account_user_id"))
        elif not has_user_id:
            await conn.execute(text("ALTER TABLE coupon_usages ADD COLUMN user_id BIGINT"))
        if await _safe_set_not_null(conn, "coupon_usages", "user_id"):
            await conn.execute(text("ALTER TABLE coupon_usages ADD PRIMARY KEY (coupon_id, user_id)"))

    for tbl in ("temporary_data", "manual_bans", "blocked_users"):
        if not await _table_exists(conn, tbl):
            continue
        if not await _column_exists(conn, tbl, "user_id"):
            continue
        logger.info(f"[schema_upgrade] {tbl} оставлен на legacy PK по tg_id")

    if await _table_exists(conn, "users"):
        await _drop_pk(conn, "users")
        await conn.execute(text("ALTER TABLE users ADD PRIMARY KEY (id)"))
        await conn.execute(text("ALTER TABLE users ALTER COLUMN tg_id DROP NOT NULL"))
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_tg_id ON users (tg_id)"))


async def _migration_v6_add_foreign_keys(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v6: Добавление foreign key constraints")

    if await _table_exists(conn, "referrals"):
        await _add_constraint_if_missing(
            conn,
            "referrals",
            "fk_referrals_referred_user",
            """
            ALTER TABLE referrals
            ADD CONSTRAINT fk_referrals_referred_user
            FOREIGN KEY (referred_user_id) REFERENCES users (id) ON DELETE CASCADE
            """,
        )
        await _add_constraint_if_missing(
            conn,
            "referrals",
            "fk_referrals_referrer_user",
            """
            ALTER TABLE referrals
            ADD CONSTRAINT fk_referrals_referrer_user
            FOREIGN KEY (referrer_user_id) REFERENCES users (id) ON DELETE CASCADE
            """,
        )

    if await _table_exists(conn, "notifications"):
        await _add_constraint_if_missing(
            conn,
            "notifications",
            "fk_notifications_user",
            """
            ALTER TABLE notifications
            ADD CONSTRAINT fk_notifications_user
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            """,
        )

    if await _table_exists(conn, "gift_usages"):
        await _add_constraint_if_missing(
            conn,
            "gift_usages",
            "fk_gift_usages_user",
            """
            ALTER TABLE gift_usages
            ADD CONSTRAINT fk_gift_usages_user
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            """,
        )

    if await _table_exists(conn, "coupon_usages"):
        await _add_constraint_if_missing(
            conn,
            "coupon_usages",
            "fk_coupon_usages_user",
            """
            ALTER TABLE coupon_usages
            ADD CONSTRAINT fk_coupon_usages_user
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            """,
        )

    for tbl in ("temporary_data", "manual_bans", "blocked_users"):
        if await _table_exists(conn, tbl):
            safe = re.sub(r"[^a-z_]", "_", tbl)
            await _add_constraint_if_missing(
                conn,
                tbl,
                f"fk_{safe}_user",
                f"""
                ALTER TABLE "{tbl}"
                ADD CONSTRAINT fk_{safe}_user
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                """,
            )

    if await _table_exists(conn, "keys") and await _safe_set_not_null(conn, "keys", "user_id"):
        await _add_constraint_if_missing(
            conn,
            "keys",
            "fk_keys_user",
            """
            ALTER TABLE keys
            ADD CONSTRAINT fk_keys_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            """,
        )

    if await _table_exists(conn, "payments") and await _safe_set_not_null(conn, "payments", "user_id"):
        await _add_constraint_if_missing(
            conn,
            "payments",
            "fk_payments_user",
            """
            ALTER TABLE payments
            ADD CONSTRAINT fk_payments_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            """,
        )

    if await _table_exists(conn, "gifts"):
        if await _safe_set_not_null(conn, "gifts", "sender_user_id"):
            await _add_constraint_if_missing(
                conn,
                "gifts",
                "fk_gifts_sender_user",
                """
                ALTER TABLE gifts
                ADD CONSTRAINT fk_gifts_sender_user FOREIGN KEY (sender_user_id) REFERENCES users (id) ON DELETE CASCADE
                """,
            )
        await _add_constraint_if_missing(
            conn,
            "gifts",
            "fk_gifts_recipient_user",
            """
            ALTER TABLE gifts
            ADD CONSTRAINT fk_gifts_recipient_user FOREIGN KEY (recipient_user_id) REFERENCES users (id) ON DELETE SET NULL
            """,
        )

    if await _table_exists(conn, "scheduled_broadcasts"):
        await _add_constraint_if_missing(
            conn,
            "scheduled_broadcasts",
            "fk_scheduled_broadcasts_creator_user",
            """
            ALTER TABLE scheduled_broadcasts
            ADD CONSTRAINT fk_scheduled_broadcasts_creator_user
            FOREIGN KEY (created_by_user_id) REFERENCES users (id) ON DELETE SET NULL
            """,
        )


async def _migration_v7_backfill_tg_mirrors(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v7: Backfill tg_id mirrors")

    backfills = [
        "UPDATE keys k SET tg_id = u.tg_id FROM users u WHERE k.user_id = u.id",
        "UPDATE payments p SET tg_id = u.tg_id FROM users u WHERE p.user_id = u.id",
        "UPDATE referrals r SET referred_tg_id = ur.tg_id, referrer_tg_id = ux.tg_id FROM users ur, users ux WHERE r.referred_user_id = ur.id AND r.referrer_user_id = ux.id",
        "UPDATE notifications n SET tg_id = u.tg_id FROM users u WHERE n.user_id = u.id",
        "UPDATE gift_usages gu SET tg_id = u.tg_id FROM users u WHERE gu.user_id = u.id",
        "UPDATE manual_bans m SET tg_id = u.tg_id FROM users u WHERE m.user_id = u.id",
        "UPDATE temporary_data t SET tg_id = u.tg_id FROM users u WHERE t.user_id = u.id",
        "UPDATE blocked_users b SET tg_id = u.tg_id FROM users u WHERE b.user_id = u.id",
        "UPDATE scheduled_broadcasts s SET created_by_tg_id = u.tg_id FROM users u WHERE s.created_by_user_id = u.id",
        "UPDATE coupon_usages c SET tg_id = u.tg_id FROM users u WHERE c.user_id = u.id",
    ]

    for sql in backfills:
        try:
            async with conn.begin_nested():
                await conn.execute(text(sql))
        except Exception as e:
            logger.debug(f"[schema_upgrade] backfill skip: {e}")

    if await _table_exists(conn, "gifts"):
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        """
                        UPDATE gifts g
                        SET sender_tg_id = u.tg_id
                        FROM users u
                        WHERE g.sender_user_id = u.id
                        """
                    )
                )
        except Exception as e:
            logger.debug(f"[schema_upgrade] backfill gifts sender skip: {e}")

        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        """
                        UPDATE gifts g
                        SET recipient_tg_id = u.tg_id
                        FROM users u
                        WHERE g.recipient_user_id = u.id
                        """
                    )
                )
        except Exception as e:
            logger.debug(f"[schema_upgrade] backfill gifts recipient skip: {e}")


async def _migration_v8_fix_notification_timezone(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v8: Исправление timezone для last_notification_time")

    if not await _table_exists(conn, "notifications"):
        return

    r = await conn.execute(
        text(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'notifications'
              AND column_name = 'last_notification_time'
            """
        )
    )
    row = r.first()
    if not row:
        return

    current_type = row[0]
    if current_type == "timestamp with time zone":
        return

    try:
        await conn.execute(
            text(
                """
                ALTER TABLE notifications
                ALTER COLUMN last_notification_time
                TYPE TIMESTAMP WITH TIME ZONE
                USING last_notification_time AT TIME ZONE 'UTC'
                """
            )
        )
    except Exception as e:
        logger.warning(f"[schema_upgrade] v8: не удалось изменить тип колонки: {e}")


async def _migration_v9_cleanup_orphaned_records(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v5: Мягкий backfill user_id для legacy таблиц")

    tables_to_clean = [
        ("blocked_users", "user_id"),
        ("manual_bans", "user_id"),
        ("temporary_data", "user_id"),
    ]

    for table, user_col in tables_to_clean:
        if not await _table_exists(conn, table):
            continue

        if not await _column_exists(conn, table, user_col):
            continue

        result = await conn.execute(
            text(
                f"""
                UPDATE "{table}" AS t
                SET "{user_col}" = u.id
                FROM users AS u
                WHERE t."{user_col}" IS NULL
                  AND t.tg_id IS NOT NULL
                  AND t.tg_id = u.tg_id
                """
            )
        )
        updated = result.rowcount
        if updated > 0:
            logger.info(f"[schema_upgrade] v5: заполнено {updated} записей в {table}")


async def _migration_v10_finalize_user_id_not_null(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v10: legacy таблицы сохраняют nullable user_id и PK по tg_id")


async def _migration_v11_finalize_legacy_tables(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v11: финализация legacy таблиц на user_id")

    for table in ("blocked_users", "manual_bans", "temporary_data"):
        if not await _table_exists(conn, table):
            continue
        if not await _column_exists(conn, table, "user_id"):
            continue

        await _backfill_users_from_table(conn, table, "tg_id")

        await conn.execute(
            text(
                f"""
                UPDATE "{table}" AS t
                SET "user_id" = u.id
                FROM users AS u
                WHERE t."user_id" IS NULL
                  AND t."tg_id" IS NOT NULL
                  AND t."tg_id" = u.tg_id
                """
            )
        )

        deleted = await conn.execute(text(f'DELETE FROM "{table}" WHERE "user_id" IS NULL'))
        if deleted.rowcount > 0:
            logger.warning(
                f"[schema_upgrade] v11: удалено {deleted.rowcount} записей из {table} "
                "без tg_id и user_id (невосстановимы)"
            )

        await _drop_pk(conn, table)
        if await _column_exists(conn, table, "tg_id"):
            await _exec_ignore(conn, f'ALTER TABLE "{table}" ALTER COLUMN "tg_id" DROP NOT NULL')
        if not await _safe_set_not_null(conn, table, "user_id"):
            continue
        await _exec_ignore(conn, f'ALTER TABLE "{table}" ADD PRIMARY KEY ("user_id")')
        await _exec_ignore(conn, f'DROP INDEX IF EXISTS "ix_{table}_user_id"')

        tg_index_name = f"ix_{table}_tg_id"
        if await _column_exists(conn, table, "tg_id") and not await _index_exists(conn, table, tg_index_name):
            await conn.execute(text(f'CREATE INDEX "{tg_index_name}" ON "{table}" ("tg_id")'))


async def _migration_v12_relax_legacy_tg_id_nullability(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v12: приведение tg_id к nullable в legacy таблицах")

    for table in ("blocked_users", "manual_bans", "temporary_data"):
        if not await _table_exists(conn, table):
            continue
        if not await _column_exists(conn, table, "tg_id"):
            continue
        await _exec_ignore(conn, f'ALTER TABLE "{table}" ALTER COLUMN "tg_id" DROP NOT NULL')


async def _migration_v13_add_web_page_variants(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v13: добавление таблиц вариантов web-страниц")

    await _exec_ignore(
        conn,
        """
        CREATE TABLE IF NOT EXISTS web_page_variants (
            id VARCHAR(36) PRIMARY KEY,
            page_slug VARCHAR(64) NOT NULL REFERENCES web_pages(slug) ON DELETE CASCADE,
            variant_key VARCHAR(64) NOT NULL,
            name VARCHAR(255) NOT NULL DEFAULT 'Default',
            is_active BOOLEAN NOT NULL DEFAULT FALSE,
            theme_tokens JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
    )
    await _exec_ignore(
        conn,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_web_page_variants_page_slug_variant_key
        ON web_page_variants (page_slug, variant_key)
        """,
    )
    await _exec_ignore(
        conn,
        """
        CREATE INDEX IF NOT EXISTS ix_web_page_variants_page_slug_is_active
        ON web_page_variants (page_slug, is_active)
        """,
    )
    await _exec_ignore(
        conn,
        """
        CREATE TABLE IF NOT EXISTS web_page_variant_blocks (
            id VARCHAR(36) PRIMARY KEY,
            variant_id VARCHAR(36) NOT NULL REFERENCES web_page_variants(id) ON DELETE CASCADE,
            "order" INTEGER NOT NULL DEFAULT 0,
            type VARCHAR(64) NOT NULL,
            data JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """,
    )
    await _exec_ignore(
        conn,
        """
        CREATE INDEX IF NOT EXISTS ix_web_page_variant_blocks_variant_id_order
        ON web_page_variant_blocks (variant_id, "order")
        """,
    )


async def _migration_v14_web_flow_graph_model(conn: AsyncConnection) -> None:
    """Переход web_flows со steps[] на nodes[] + edges[] (граф-модель)."""
    if not await _table_exists(conn, "web_flows"):
        return

    if await _column_exists(conn, "web_flows", "steps"):
        if not await _column_exists(conn, "web_flows", "nodes"):
            await _exec_ignore(conn, "ALTER TABLE web_flows RENAME COLUMN steps TO nodes")
        else:
            await _exec_ignore(conn, "ALTER TABLE web_flows DROP COLUMN steps")

    if not await _column_exists(conn, "web_flows", "edges"):
        await _exec_ignore(conn, "ALTER TABLE web_flows ADD COLUMN edges JSONB NOT NULL DEFAULT '[]'::jsonb")

    if not await _column_exists(conn, "web_flows", "entry_node_id"):
        await _exec_ignore(conn, "ALTER TABLE web_flows ADD COLUMN entry_node_id VARCHAR(64)")

    rows = (await conn.execute(text("SELECT id, nodes FROM web_flows"))).all()
    for row in rows:
        flow_id = row[0]
        raw_nodes = row[1]
        if not isinstance(raw_nodes, list) or len(raw_nodes) == 0:
            continue
        first = raw_nodes[0] if raw_nodes else {}
        if isinstance(first, dict) and "position" not in first:
            new_nodes = []
            new_edges = []
            entry_id = None
            for i, step in enumerate(raw_nodes):
                if not isinstance(step, dict):
                    continue
                node_id = step.get("id", f"node-{i}")
                if i == 0:
                    entry_id = node_id
                new_nodes.append({
                    **step,
                    "position": {"x": 300, "y": i * 180},
                })
                if i > 0:
                    prev_id = raw_nodes[i - 1].get("id", f"node-{i - 1}")
                    new_edges.append({
                        "id": f"edge-migrated-{i}",
                        "source": prev_id,
                        "target": node_id,
                    })
            import json

            await conn.execute(
                text("UPDATE web_flows SET nodes = :nodes, edges = :edges, entry_node_id = :entry WHERE id = :fid"),
                {"nodes": json.dumps(new_nodes), "edges": json.dumps(new_edges), "entry": entry_id, "fid": flow_id},
            )


async def _migration_v15_recover_orphan_users(conn: AsyncConnection) -> None:
    """Safety net для клиентов, прошедших v3/v11 со старой логикой.

    Обходит все таблицы, где может быть orphan tg_id, создаёт недостающих юзеров
    и повторно заполняет user_id. Идемпотентно: если orphan'ов нет — no-op.
    """
    logger.info("[schema_upgrade] v15: Восстановление orphan tg_ids в users")

    if not await _table_exists(conn, "users") or not await _column_exists(conn, "users", "id"):
        return

    orphan_sources = [
        ("keys", "tg_id"),
        ("payments", "tg_id"),
        ("notifications", "tg_id"),
        ("scheduled_broadcasts", "created_by_tg_id"),
        ("gift_usages", "tg_id"),
        ("temporary_data", "tg_id"),
        ("manual_bans", "tg_id"),
        ("blocked_users", "tg_id"),
        ("referrals", "referred_tg_id"),
        ("referrals", "referrer_tg_id"),
        ("gifts", "sender_tg_id"),
        ("gifts", "recipient_tg_id"),
    ]

    total_created = 0
    for table, tg_col in orphan_sources:
        total_created += await _backfill_users_from_table(conn, table, tg_col)

    if total_created > 0:
        logger.info(f"[schema_upgrade] v15: всего создано {total_created} orphan-юзеров")

    repopulate = [
        ("keys", "user_id", "tg_id"),
        ("payments", "user_id", "tg_id"),
        ("notifications", "user_id", "tg_id"),
        ("scheduled_broadcasts", "created_by_user_id", "created_by_tg_id"),
        ("gift_usages", "user_id", "tg_id"),
        ("temporary_data", "user_id", "tg_id"),
        ("manual_bans", "user_id", "tg_id"),
        ("blocked_users", "user_id", "tg_id"),
        ("referrals", "referred_user_id", "referred_tg_id"),
        ("referrals", "referrer_user_id", "referrer_tg_id"),
        ("gifts", "sender_user_id", "sender_tg_id"),
        ("gifts", "recipient_user_id", "recipient_tg_id"),
    ]

    for table, user_col, tg_col in repopulate:
        if not await _table_exists(conn, table):
            continue
        if not await _column_exists(conn, table, user_col):
            continue
        if not await _column_exists(conn, table, tg_col):
            continue
        result = await conn.execute(
            text(
                f"""
                UPDATE "{table}" t
                SET "{user_col}" = u.id
                FROM users u
                WHERE t."{user_col}" IS NULL
                  AND t."{tg_col}" IS NOT NULL
                  AND t."{tg_col}" = u.tg_id
                """
            )
        )
        if result.rowcount and result.rowcount > 0:
            logger.info(f"[schema_upgrade] v15: повторно заполнено {result.rowcount} записей {table}.{user_col}")


async def _migration_v18_web_error_reports(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v18: таблица web_error_reports")
    await _exec_ignore(
        conn,
        """
        CREATE TABLE IF NOT EXISTS web_error_reports (
            id VARCHAR(36) PRIMARY KEY,
            signature VARCHAR(128) NOT NULL UNIQUE,
            error_name VARCHAR(255) NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            stack TEXT,
            url TEXT,
            user_agent TEXT,
            tag VARCHAR(64),
            last_identity_id VARCHAR(36),
            last_context JSONB,
            count INTEGER NOT NULL DEFAULT 1,
            resolved BOOLEAN NOT NULL DEFAULT FALSE,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
    )
    await _exec_ignore(
        conn,
        "CREATE INDEX IF NOT EXISTS ix_web_error_reports_signature ON web_error_reports (signature)",
    )
    await _exec_ignore(
        conn,
        "CREATE INDEX IF NOT EXISTS ix_web_error_reports_resolved_last ON web_error_reports (resolved, last_seen_at)",
    )


async def _migration_v16b_web_flow_events(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v17: таблица web_flow_events")

    await _exec_ignore(
        conn,
        """
        CREATE TABLE IF NOT EXISTS web_flow_events (
            id VARCHAR(36) PRIMARY KEY,
            flow_id VARCHAR(64) NOT NULL,
            node_id VARCHAR(64) NOT NULL,
            node_type VARCHAR(32) NOT NULL DEFAULT '',
            event_type VARCHAR(32) NOT NULL,
            ab_variant VARCHAR(16),
            device VARCHAR(16),
            locale VARCHAR(8),
            authenticated BOOLEAN,
            metadata JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
    )
    await _exec_ignore(
        conn,
        "CREATE INDEX IF NOT EXISTS ix_web_flow_events_flow_node ON web_flow_events (flow_id, node_id)",
    )
    await _exec_ignore(
        conn,
        "CREATE INDEX IF NOT EXISTS ix_web_flow_events_created ON web_flow_events (created_at)",
    )


async def _migration_v16_custom_element_builds(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v16: таблица web_custom_element_builds")

    await _exec_ignore(
        conn,
        """
        CREATE TABLE IF NOT EXISTS web_custom_element_builds (
            id VARCHAR(36) PRIMARY KEY,
            label VARCHAR(255) NOT NULL DEFAULT '',
            slug VARCHAR(128) NOT NULL DEFAULT '',
            runtime VARCHAR(32) NOT NULL DEFAULT 'react-component',
            source_kind VARCHAR(32) NOT NULL DEFAULT 'inline-code',
            source_value TEXT NOT NULL DEFAULT '',
            export_name VARCHAR(128) NOT NULL DEFAULT 'default',
            props_schema_text TEXT NOT NULL DEFAULT '',
            sample_props_text TEXT NOT NULL DEFAULT '',
            events_text TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            status VARCHAR(32) NOT NULL DEFAULT 'queued',
            summary TEXT NOT NULL DEFAULT '',
            next_steps JSONB NOT NULL DEFAULT '[]'::jsonb,
            artifact JSONB,
            upload_meta JSONB,
            worker_id VARCHAR(64),
            worker_claimed_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
    )
    await _exec_ignore(
        conn,
        "CREATE INDEX IF NOT EXISTS ix_web_custom_element_builds_status ON web_custom_element_builds (status)",
    )
    await _exec_ignore(
        conn,
        "CREATE INDEX IF NOT EXISTS ix_web_custom_element_builds_created ON web_custom_element_builds (created_at)",
    )


async def _migration_v19_keys_tg_id_nullable(conn: AsyncConnection) -> None:
    """Снимает NOT NULL с keys.tg_id и переносит PK на (user_id, client_id).

    Старый PK (tg_id, client_id) не позволяет создавать подписки для web-only
    пользователей, у которых tg_id=NULL. user_id у ключа есть всегда (FK на
    users.id), поэтому делаем его новым компонентом PK.
    """
    logger.info("[schema_upgrade] v19: keys.tg_id nullable, PK на (user_id, client_id)")

    if not await _table_exists(conn, "keys"):
        return

    if not await _column_exists(conn, "keys", "user_id"):
        logger.warning("[schema_upgrade] v19: keys.user_id не найден, пропуск")
        return

    await _exec_ignore(
        conn,
        """
        UPDATE keys k SET user_id = u.id
        FROM users u
        WHERE k.user_id IS NULL AND k.tg_id IS NOT NULL AND u.tg_id = k.tg_id
        """,
    )

    if await _column_has_nulls(conn, "keys", "user_id"):
        logger.warning("[schema_upgrade] v19: в keys остались строки с user_id=NULL, пропуск смены PK")
        return

    await _drop_pk(conn, "keys")
    await _exec_ignore(conn, 'ALTER TABLE "keys" ALTER COLUMN "user_id" SET NOT NULL')
    await _exec_ignore(conn, 'ALTER TABLE "keys" ALTER COLUMN "tg_id" DROP NOT NULL')
    await _exec_ignore(conn, 'ALTER TABLE "keys" ADD PRIMARY KEY (user_id, client_id)')
    await _exec_ignore(conn, 'CREATE INDEX IF NOT EXISTS ix_keys_tg_id ON "keys" (tg_id)')


async def _migration_v20_add_identity_google_sub(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v20: identities.google_sub")
    if not await _table_exists(conn, "identities"):
        return
    if not await _column_exists(conn, "identities", "google_sub"):
        await _exec_ignore(conn, "ALTER TABLE identities ADD COLUMN google_sub VARCHAR(64)")
    await _exec_ignore(
        conn,
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_identities_google_sub ON identities (google_sub) WHERE google_sub IS NOT NULL",
    )


async def _migration_v21_add_identity_yandex_sub(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v21: identities.yandex_sub")
    if not await _table_exists(conn, "identities"):
        return
    if not await _column_exists(conn, "identities", "yandex_sub"):
        await _exec_ignore(conn, "ALTER TABLE identities ADD COLUMN yandex_sub VARCHAR(64)")
    await _exec_ignore(
        conn,
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_identities_yandex_sub ON identities (yandex_sub) WHERE yandex_sub IS NOT NULL",
    )


async def _migration_v22_add_identity_onboarding_completed_at(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v22: identities.onboarding_completed_at")
    if not await _table_exists(conn, "identities"):
        return
    if not await _column_exists(conn, "identities", "onboarding_completed_at"):
        await _exec_ignore(conn, "ALTER TABLE identities ADD COLUMN onboarding_completed_at TIMESTAMP")


async def _migration_v23_add_identity_onboarding_stage(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v23: identities.onboarding_stage")
    if not await _table_exists(conn, "identities"):
        return
    if not await _column_exists(conn, "identities", "onboarding_stage"):
        await _exec_ignore(conn, "ALTER TABLE identities ADD COLUMN onboarding_stage VARCHAR(32)")


async def _migration_v26_add_keys_indexes(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v26: индексы keys(expiry_time/server_id/tariff_id)")
    if not await _table_exists(conn, "keys"):
        return
    if not await _index_exists(conn, "keys", "ix_keys_expiry_time"):
        await _exec_ignore(conn, "CREATE INDEX ix_keys_expiry_time ON keys(expiry_time)")
    if not await _index_exists(conn, "keys", "ix_keys_server_id"):
        await _exec_ignore(conn, "CREATE INDEX ix_keys_server_id ON keys(server_id)")
    if not await _index_exists(conn, "keys", "ix_keys_tariff_id"):
        await _exec_ignore(conn, "CREATE INDEX ix_keys_tariff_id ON keys(tariff_id)")


async def _migration_v25_add_partners_indexes(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v25: индексы на partners(partner_tg_id/joined_tg_id)")
    if not await _table_exists(conn, "partners"):
        return
    if not await _index_exists(conn, "partners", "ix_partners_partner_tg_id"):
        await _exec_ignore(conn, "CREATE INDEX ix_partners_partner_tg_id ON partners(partner_tg_id)")
    if not await _index_exists(conn, "partners", "ix_partners_joined_tg_id"):
        await _exec_ignore(conn, "CREATE INDEX ix_partners_joined_tg_id ON partners(joined_tg_id)")


async def _migration_v27_add_admins_permissions(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v27: admins.permissions (JSONB)")
    if not await _table_exists(conn, "admins"):
        return
    if not await _column_exists(conn, "admins", "permissions"):
        await _exec_ignore(
            conn,
            "ALTER TABLE admins ADD COLUMN permissions JSONB NOT NULL DEFAULT '[]'::jsonb",
        )
        await _exec_ignore(
            conn,
            """
            UPDATE admins
               SET permissions = '["users","keys","broadcasting","coupons","gifts"]'::jsonb
             WHERE role = 'moderator' AND permissions = '[]'::jsonb
            """,
        )


async def _migration_v28_add_identity_notif_prefs(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v28: таблица identity_notif_prefs (toggle каналов уведомлений)")
    if not await _table_exists(conn, "identities"):
        return
    if not await _table_exists(conn, "identity_notif_prefs"):
        await _exec_ignore(
            conn,
            """
            CREATE TABLE identity_notif_prefs (
                identity_id VARCHAR(36) NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                channel VARCHAR(32) NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (identity_id, channel)
            )
            """,
        )


async def _migration_v24_add_identity_sessions(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v24: таблица identity_sessions + перенос существующих токенов")
    if not await _table_exists(conn, "identities"):
        return
    if not await _table_exists(conn, "identity_sessions"):
        await _exec_ignore(
            conn,
            """
            CREATE TABLE identity_sessions (
                id VARCHAR(36) PRIMARY KEY,
                identity_id VARCHAR(36) NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
                token_hash VARCHAR(64) NOT NULL UNIQUE,
                device_label VARCHAR(128),
                user_agent TEXT,
                ip VARCHAR(64),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP
            )
            """,
        )
        await _exec_ignore(
            conn,
            "CREATE INDEX IF NOT EXISTS ix_identity_sessions_identity_id ON identity_sessions(identity_id)",
        )
        await _exec_ignore(
            conn,
            "CREATE INDEX IF NOT EXISTS ix_identity_sessions_identity_last_seen "
            "ON identity_sessions(identity_id, last_seen_at)",
        )
    if await _column_exists(conn, "identities", "api_token_hash"):
        await _exec_ignore(conn, "CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await _exec_ignore(
            conn,
            """
            INSERT INTO identity_sessions (
                id, identity_id, token_hash, device_label, created_at, last_seen_at
            )
            SELECT
                gen_random_uuid()::text,
                id,
                api_token_hash,
                'legacy',
                COALESCE(token_issued_at, CURRENT_TIMESTAMP),
                COALESCE(token_issued_at, CURRENT_TIMESTAMP)
            FROM identities
            WHERE api_token_hash IS NOT NULL
              AND api_token_hash NOT IN (SELECT token_hash FROM identity_sessions)
            """,
        )


async def _migration_v29_add_scheduled_broadcasts_channel(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v29: scheduled_broadcasts.channel (bot/site/both)")
    if not await _table_exists(conn, "scheduled_broadcasts"):
        return
    if not await _column_exists(conn, "scheduled_broadcasts", "channel"):
        await conn.execute(
            text("ALTER TABLE scheduled_broadcasts ADD COLUMN channel VARCHAR(8) NOT NULL DEFAULT 'both'")
        )


async def _migration_v30_add_users_created_at_index(conn: AsyncConnection) -> None:
    logger.info("[schema_upgrade] v30: индекс users(created_at)")
    if not await _table_exists(conn, "users"):
        return
    if not await _column_exists(conn, "users", "created_at"):
        return
    if not await _index_exists(conn, "users", "ix_users_created_at"):
        await _exec_ignore(conn, "CREATE INDEX IF NOT EXISTS ix_users_created_at ON users (created_at)")


_MIGRATIONS = [
    (1, "Добавление users.id", _migration_v1_add_users_id),
    (2, "Добавление user_id колонок", _migration_v2_add_user_id_columns),
    (3, "Заполнение user_id из tg_id", _migration_v3_populate_user_ids),
    (4, "Добавление tg_id mirrors", _migration_v4_add_tg_id_mirrors),
    (5, "Очистка записей с NULL user_id", _migration_v9_cleanup_orphaned_records),
    (6, "Переключение PK на user_id", _migration_v5_switch_pks_to_user_id),
    (7, "Добавление foreign keys", _migration_v6_add_foreign_keys),
    (8, "Backfill tg_id mirrors", _migration_v7_backfill_tg_mirrors),
    (9, "Исправление timezone для notifications", _migration_v8_fix_notification_timezone),
    (10, "Финальная установка NOT NULL на user_id", _migration_v10_finalize_user_id_not_null),
    (11, "Финализация legacy таблиц на user_id", _migration_v11_finalize_legacy_tables),
    (12, "Снятие NOT NULL с tg_id в legacy таблицах", _migration_v12_relax_legacy_tg_id_nullability),
    (13, "Таблицы вариантов web-страниц", _migration_v13_add_web_page_variants),
    (14, "WebFlow граф-модель (nodes + edges)", _migration_v14_web_flow_graph_model),
    (15, "Восстановление orphan tg_ids в users", _migration_v15_recover_orphan_users),
    (16, "Таблица custom element builds", _migration_v16_custom_element_builds),
    (17, "Таблица flow analytics events", _migration_v16b_web_flow_events),
    (18, "Таблица web_error_reports", _migration_v18_web_error_reports),
    (19, "keys.tg_id nullable, PK на (user_id, client_id)", _migration_v19_keys_tg_id_nullable),
    (20, "identities.google_sub", _migration_v20_add_identity_google_sub),
    (21, "identities.yandex_sub", _migration_v21_add_identity_yandex_sub),
    (22, "identities.onboarding_completed_at", _migration_v22_add_identity_onboarding_completed_at),
    (23, "identities.onboarding_stage", _migration_v23_add_identity_onboarding_stage),
    (24, "таблица identity_sessions (мультидевайс)", _migration_v24_add_identity_sessions),
    (25, "индексы на partners(partner_tg_id/joined_tg_id)", _migration_v25_add_partners_indexes),
    (26, "индексы keys(expiry_time/server_id/tariff_id)", _migration_v26_add_keys_indexes),
    (27, "admins.permissions (JSONB per-admin permissions)", _migration_v27_add_admins_permissions),
    (28, "таблица identity_notif_prefs (toggle каналов)", _migration_v28_add_identity_notif_prefs),
    (29, "scheduled_broadcasts.channel (bot/site/both)", _migration_v29_add_scheduled_broadcasts_channel),
    (30, "индекс users(created_at)", _migration_v30_add_users_created_at_index),
]


async def apply_all_migrations(conn: AsyncConnection) -> None:
    if not _is_postgresql():
        return

    await _ensure_migrations_table(conn)
    current_version = await _get_current_version(conn)

    for version, description, migration_func in _MIGRATIONS:
        if version <= current_version:
            continue

        logger.info(f"[schema_upgrade] Применение миграции v{version}: {description}")
        try:
            await migration_func(conn)
            await _mark_migration_applied(conn, version, description)
            logger.info(f"[schema_upgrade] Миграция v{version} применена успешно")
        except Exception as e:
            logger.error(f"[schema_upgrade] Ошибка при применении миграции v{version}: {e}")
            raise

    logger.info(f"[schema_upgrade] Все миграции применены, текущая версия: {await _get_current_version(conn)}")


async def apply_account_schema_if_needed(conn: AsyncConnection) -> None:
    await apply_all_migrations(conn)


_TG_MIRROR_TABLE_COLUMNS = (
    ("keys", "tg_id"),
    ("payments", "tg_id"),
    ("referrals", "referred_tg_id"),
    ("referrals", "referrer_tg_id"),
    ("notifications", "tg_id"),
    ("gift_usages", "tg_id"),
    ("gifts", "sender_tg_id"),
    ("gifts", "recipient_tg_id"),
    ("manual_bans", "tg_id"),
    ("temporary_data", "tg_id"),
    ("blocked_users", "tg_id"),
    ("scheduled_broadcasts", "created_by_tg_id"),
    ("coupon_usages", "tg_id"),
)


async def ensure_tg_mirror_columns_and_backfill(conn: AsyncConnection) -> None:
    if not _is_postgresql():
        return
    for table, col in _TG_MIRROR_TABLE_COLUMNS:
        if await _table_exists(conn, table) and not await _column_exists(conn, table, col):
            await conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {col} BIGINT'))
    await _migration_v7_backfill_tg_mirrors(conn)
