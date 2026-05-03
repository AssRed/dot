"""PostgreSQL access layer for the beauty-master bot.

Uses asyncpg + a thin compatibility layer so the rest of the codebase
keeps using ``?``-style placeholders. The schema is created on first
connection; column migrations are idempotent.
"""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Iterable

import asyncpg

logger = logging.getLogger(__name__)


# -- default seed data -------------------------------------------------------
DEFAULT_WELCOME_NEW = (
    "✨ Приветствую! Я — виртуальный ассистент мастера. "
    "Здесь вы можете записаться на процедуры, узнать подробности и даже "
    "отменить визит. Мастер заботится о вашем комфорте, поэтому отвечу "
    "на все вопросы нежно и без спешки. 🌸\n"
    "Посмотрите, какие услуги есть, или сразу выберите время — буду рада помочь!"
)

DEFAULT_WELCOME_RETURNING = "❤️ С возвращением! Чем могу помочь сегодня?"

DEFAULT_SETTINGS: dict[str, str] = {
    "tax_rate": "6",
    "average_daily_bookings": "3",
}


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        tg_id BIGINT PRIMARY KEY,
        role TEXT NOT NULL DEFAULT 'client',
        first_seen TIMESTAMP NOT NULL,
        last_interaction TIMESTAMP NOT NULL,
        total_visits INTEGER NOT NULL DEFAULT 0,
        preferred_name TEXT,
        phone TEXT,
        last_service_id INTEGER,
        last_appointment_id INTEGER,
        tg_username TEXT,
        tg_first_name TEXT,
        last_sleeping_ping TIMESTAMP,
        broadcast_opt_out INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS services (
        id BIGSERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        price INTEGER NOT NULL,
        duration_minutes INTEGER NOT NULL,
        description TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schedule (
        id BIGSERIAL PRIMARY KEY,
        day_of_week INTEGER NOT NULL UNIQUE,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        break_minutes INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS appointments (
        id BIGSERIAL PRIMARY KEY,
        client_tg_id BIGINT NOT NULL,
        service_id BIGINT NOT NULL,
        datetime TIMESTAMP NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        actual_amount INTEGER,
        created_at TIMESTAMP NOT NULL,
        cancelled_at TIMESTAMP,
        cancel_reason TEXT,
        client_phone TEXT,
        client_name TEXT,
        reminder_sent INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transactions (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        type TEXT NOT NULL,
        amount INTEGER NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        comment TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMP NOT NULL,
        appointment_id BIGINT,
        photo_file_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS master_info (
        id INTEGER PRIMARY KEY,
        text TEXT NOT NULL DEFAULT '',
        photo_file_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS welcome_texts (
        id BIGSERIAL PRIMARY KEY,
        type TEXT NOT NULL UNIQUE,
        text TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS client_notes (
        tg_id BIGINT PRIMARY KEY,
        note TEXT NOT NULL DEFAULT '',
        updated_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS client_tags (
        tg_id BIGINT NOT NULL,
        tag TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL,
        PRIMARY KEY (tg_id, tag)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS waitlist (
        id BIGSERIAL PRIMARY KEY,
        client_tg_id BIGINT NOT NULL,
        service_id BIGINT NOT NULL,
        target_date TEXT NOT NULL,
        target_time TEXT,
        client_name TEXT,
        client_phone TEXT,
        created_at TIMESTAMP NOT NULL,
        notified_at TIMESTAMP,
        status TEXT NOT NULL DEFAULT 'waiting'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broadcasts (
        id BIGSERIAL PRIMARY KEY,
        text TEXT NOT NULL,
        audience TEXT NOT NULL DEFAULT 'all',
        created_at TIMESTAMP NOT NULL,
        sent_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        kind TEXT NOT NULL DEFAULT 'manual'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_appt_client ON appointments(client_tg_id)",
    "CREATE INDEX IF NOT EXISTS idx_appt_dt ON appointments(datetime)",
    "CREATE INDEX IF NOT EXISTS idx_appt_status ON appointments(status)",
    "CREATE INDEX IF NOT EXISTS idx_tx_created ON transactions(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_waitlist_status ON waitlist(status)",
    "CREATE INDEX IF NOT EXISTS idx_waitlist_target ON waitlist(target_date, service_id)",
    "CREATE INDEX IF NOT EXISTS idx_tags_tag ON client_tags(tag)",
)


# Columns added after the initial schema; keep in sync with schema above.
USERS_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("tg_username", "TEXT"),
    ("tg_first_name", "TEXT"),
    ("last_sleeping_ping", "TIMESTAMP"),
    ("broadcast_opt_out", "INTEGER NOT NULL DEFAULT 0"),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now()


def to_dt(value: str | datetime | None) -> datetime | None:
    """Normalise a possibly-string datetime to a ``datetime`` object."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def to_str(dt: datetime) -> str:
    return dt.isoformat(sep=" ", timespec="seconds")


# ``?`` → ``$N`` translation (single-pass, ignores placeholders inside literals)
_PARAM_RE = re.compile(r"\?")


def _translate(sql: str) -> str:
    """Replace ``?`` placeholders with ``$1, $2, ...`` (skipping string literals)."""
    out: list[str] = []
    n = 1
    in_squote = False
    in_dquote = False
    for ch in sql:
        if ch == "'" and not in_dquote:
            in_squote = not in_squote
            out.append(ch)
            continue
        if ch == '"' and not in_squote:
            in_dquote = not in_dquote
            out.append(ch)
            continue
        if ch == "?" and not in_squote and not in_dquote:
            out.append(f"${n}")
            n += 1
        else:
            out.append(ch)
    return "".join(out)


_STATUS_RE = re.compile(r"\b(\d+)\s*$")


def _parse_rowcount(status: str) -> int:
    m = _STATUS_RE.search(status or "")
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# Database wrapper
# ---------------------------------------------------------------------------


class Database:
    """Async wrapper around an asyncpg pool with schema bootstrap."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def init(self) -> None:
        self._pool = await asyncpg.create_pool(
            self.dsn,
            min_size=1,
            max_size=4,
            command_timeout=30,
        )
        async with self._pool.acquire() as conn:
            for stmt in SCHEMA_STATEMENTS:
                await conn.execute(stmt)
            await self._migrate_users(conn)
            await self._seed(conn)
        logger.info("Database initialised (Postgres)")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _migrate_users(self, conn: asyncpg.Connection) -> None:
        """Add missing columns from USERS_MIGRATIONS (idempotent)."""
        rows = await conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
             WHERE table_name = 'users'
            """
        )
        existing = {row["column_name"] for row in rows}
        for col, ddl in USERS_MIGRATIONS:
            if col not in existing:
                await conn.execute(
                    f"ALTER TABLE users ADD COLUMN {col} {ddl}"
                )

    async def _seed(self, conn: asyncpg.Connection) -> None:
        for key, value in DEFAULT_SETTINGS.items():
            await conn.execute(
                """
                INSERT INTO settings(key, value) VALUES ($1, $2)
                ON CONFLICT(key) DO NOTHING
                """,
                key, value,
            )
        await conn.execute(
            """
            INSERT INTO welcome_texts(type, text) VALUES ($1, $2)
            ON CONFLICT(type) DO NOTHING
            """,
            "new", DEFAULT_WELCOME_NEW,
        )
        await conn.execute(
            """
            INSERT INTO welcome_texts(type, text) VALUES ($1, $2)
            ON CONFLICT(type) DO NOTHING
            """,
            "returning", DEFAULT_WELCOME_RETURNING,
        )
        await conn.execute(
            """
            INSERT INTO master_info(id, text, photo_file_id)
            VALUES (1, '', NULL)
            ON CONFLICT(id) DO NOTHING
            """
        )

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[asyncpg.Connection]:
        assert self._pool is not None, "Database.init() must be awaited first"
        async with self._pool.acquire() as conn:
            yield conn

    # -- low level helpers --------------------------------------------------

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run an INSERT/UPDATE/DELETE.

        - If the SQL contains ``RETURNING ``, returns the first column of the
          first row (typically an id).
        - Otherwise returns 0.
        """
        sql = _translate(sql)
        params = tuple(params)
        async with self.connect() as conn:
            if " returning " in sql.lower():
                val = await conn.fetchval(sql, *params)
                return int(val) if val is not None else 0
            await conn.execute(sql, *params)
            return 0

    async def execute_count(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run an UPDATE/DELETE and return the affected rowcount."""
        sql = _translate(sql)
        params = tuple(params)
        async with self.connect() as conn:
            status = await conn.execute(sql, *params)
            return _parse_rowcount(status)

    async def fetchone(
        self, sql: str, params: Iterable[Any] = ()
    ) -> asyncpg.Record | None:
        sql = _translate(sql)
        params = tuple(params)
        async with self.connect() as conn:
            return await conn.fetchrow(sql, *params)

    async def fetchall(
        self, sql: str, params: Iterable[Any] = ()
    ) -> list[asyncpg.Record]:
        sql = _translate(sql)
        params = tuple(params)
        async with self.connect() as conn:
            return list(await conn.fetch(sql, *params))

    # -- users --------------------------------------------------------------

    async def get_user(self, tg_id: int) -> asyncpg.Record | None:
        return await self.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))

    async def upsert_user_visit(
        self,
        tg_id: int,
        role: str = "client",
        *,
        username: str | None = None,
        first_name: str | None = None,
    ) -> tuple[bool, asyncpg.Record]:
        """Insert or update a user; return (is_new, row)."""
        existing = await self.get_user(tg_id)
        now = _now()
        if existing is None:
            await self.execute(
                """
                INSERT INTO users(
                    tg_id, role, first_seen, last_interaction, total_visits,
                    tg_username, tg_first_name
                )
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (tg_id, role, now, now, username, first_name),
            )
            row = await self.get_user(tg_id)
            assert row is not None
            return True, row
        await self.execute(
            """
            UPDATE users
               SET last_interaction = ?,
                   total_visits = total_visits + 1,
                   tg_username = COALESCE(?, tg_username),
                   tg_first_name = COALESCE(?, tg_first_name)
             WHERE tg_id = ?
            """,
            (now, username, first_name, tg_id),
        )
        row = await self.get_user(tg_id)
        assert row is not None
        return False, row

    async def update_user_profile(
        self,
        tg_id: int,
        *,
        preferred_name: str | None = None,
        phone: str | None = None,
        last_service_id: int | None = None,
        last_appointment_id: int | None = None,
    ) -> None:
        sets: list[str] = []
        vals: list[Any] = []
        if preferred_name is not None:
            sets.append("preferred_name = ?")
            vals.append(preferred_name)
        if phone is not None:
            sets.append("phone = ?")
            vals.append(phone)
        if last_service_id is not None:
            sets.append("last_service_id = ?")
            vals.append(last_service_id)
        if last_appointment_id is not None:
            sets.append("last_appointment_id = ?")
            vals.append(last_appointment_id)
        if not sets:
            return
        vals.append(tg_id)
        await self.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE tg_id = ?",
            vals,
        )

    # -- services -----------------------------------------------------------

    async def add_service(
        self, name: str, price: int, duration_minutes: int, description: str
    ) -> int:
        return await self.execute(
            """
            INSERT INTO services(name, price, duration_minutes, description)
            VALUES (?, ?, ?, ?)
            RETURNING id
            """,
            (name, price, duration_minutes, description),
        )

    async def update_service(
        self,
        service_id: int,
        *,
        name: str | None = None,
        price: int | None = None,
        duration_minutes: int | None = None,
        description: str | None = None,
    ) -> bool:
        sets: list[str] = []
        vals: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            vals.append(name)
        if price is not None:
            sets.append("price = ?")
            vals.append(price)
        if duration_minutes is not None:
            sets.append("duration_minutes = ?")
            vals.append(duration_minutes)
        if description is not None:
            sets.append("description = ?")
            vals.append(description)
        if not sets:
            return False
        vals.append(service_id)
        return (
            await self.execute_count(
                f"UPDATE services SET {', '.join(sets)} WHERE id = ?",
                vals,
            )
            > 0
        )

    async def delete_service(self, service_id: int) -> bool:
        return (
            await self.execute_count(
                "DELETE FROM services WHERE id = ?", (service_id,)
            )
            > 0
        )

    async def get_service(self, service_id: int) -> asyncpg.Record | None:
        return await self.fetchone("SELECT * FROM services WHERE id = ?", (service_id,))

    async def list_services(self) -> list[asyncpg.Record]:
        return await self.fetchall("SELECT * FROM services ORDER BY id")

    # -- schedule -----------------------------------------------------------

    async def upsert_schedule(
        self,
        day_of_week: int,
        start_time: str,
        end_time: str,
        break_minutes: int,
    ) -> None:
        await self.execute(
            """
            INSERT INTO schedule(day_of_week, start_time, end_time, break_minutes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(day_of_week) DO UPDATE SET
                start_time = excluded.start_time,
                end_time = excluded.end_time,
                break_minutes = excluded.break_minutes
            """,
            (day_of_week, start_time, end_time, break_minutes),
        )

    async def get_schedule(self) -> list[asyncpg.Record]:
        return await self.fetchall(
            "SELECT * FROM schedule ORDER BY day_of_week"
        )

    async def get_day_schedule(self, day_of_week: int) -> asyncpg.Record | None:
        return await self.fetchone(
            "SELECT * FROM schedule WHERE day_of_week = ?", (day_of_week,)
        )

    async def remove_day_schedule(self, day_of_week: int) -> None:
        await self.execute(
            "DELETE FROM schedule WHERE day_of_week = ?", (day_of_week,)
        )

    # -- appointments -------------------------------------------------------

    async def create_appointment(
        self,
        client_tg_id: int,
        service_id: int,
        dt: datetime,
        client_name: str,
        client_phone: str,
    ) -> int:
        return await self.execute(
            """
            INSERT INTO appointments(
                client_tg_id, service_id, datetime, status, created_at,
                client_name, client_phone
            ) VALUES (?, ?, ?, 'active', ?, ?, ?)
            RETURNING id
            """,
            (
                client_tg_id,
                service_id,
                dt,
                _now(),
                client_name,
                client_phone,
            ),
        )

    async def get_appointment(self, appt_id: int) -> asyncpg.Record | None:
        return await self.fetchone(
            """
            SELECT a.*, s.name AS service_name, s.price AS service_price,
                   s.duration_minutes AS service_duration
              FROM appointments a
              JOIN services s ON s.id = a.service_id
             WHERE a.id = ?
            """,
            (appt_id,),
        )

    async def list_active_for_client(self, tg_id: int) -> list[asyncpg.Record]:
        return await self.fetchall(
            """
            SELECT a.*, s.name AS service_name, s.price AS service_price,
                   s.duration_minutes AS service_duration
              FROM appointments a
              JOIN services s ON s.id = a.service_id
             WHERE a.client_tg_id = ? AND a.status = 'active'
               AND a.datetime >= ?
             ORDER BY a.datetime
            """,
            (tg_id, _now()),
        )

    async def list_upcoming(self) -> list[asyncpg.Record]:
        return await self.fetchall(
            """
            SELECT a.*, s.name AS service_name, s.price AS service_price,
                   s.duration_minutes AS service_duration
              FROM appointments a
              JOIN services s ON s.id = a.service_id
             WHERE a.status = 'active'
               AND a.datetime >= ?
             ORDER BY a.datetime
            """,
            (_now(),),
        )

    async def list_active_for_day(self, day: datetime) -> list[asyncpg.Record]:
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return await self.fetchall(
            """
            SELECT a.*, s.duration_minutes AS service_duration
              FROM appointments a
              JOIN services s ON s.id = a.service_id
             WHERE a.status = 'active'
               AND a.datetime >= ?
               AND a.datetime <  ?
             ORDER BY a.datetime
            """,
            (start, end),
        )

    async def cancel_appointment(
        self, appt_id: int, reason: str | None, mark_rescheduled: bool = False
    ) -> bool:
        status = "rescheduled" if mark_rescheduled else "cancelled"
        return (
            await self.execute_count(
                """
                UPDATE appointments
                   SET status = ?, cancelled_at = ?, cancel_reason = ?
                 WHERE id = ? AND status = 'active'
                """,
                (status, _now(), reason, appt_id),
            )
            > 0
        )

    async def complete_appointment(
        self, appt_id: int, actual_amount: int
    ) -> bool:
        return (
            await self.execute_count(
                """
                UPDATE appointments
                   SET status = 'completed', actual_amount = ?
                 WHERE id = ? AND status = 'active'
                """,
                (actual_amount, appt_id),
            )
            > 0
        )

    async def mark_reminder_sent(self, appt_id: int) -> None:
        await self.execute(
            "UPDATE appointments SET reminder_sent = 1 WHERE id = ?",
            (appt_id,),
        )

    async def appointments_for_reminders(
        self, window_start: datetime, window_end: datetime
    ) -> list[asyncpg.Record]:
        return await self.fetchall(
            """
            SELECT a.*, s.name AS service_name, s.duration_minutes AS service_duration
              FROM appointments a
              JOIN services s ON s.id = a.service_id
             WHERE a.status = 'active'
               AND a.reminder_sent = 0
               AND a.datetime >= ?
               AND a.datetime <  ?
            """,
            (window_start, window_end),
        )

    async def appointments_in_period(
        self,
        period_start: datetime,
        period_end: datetime,
        status: str | None = None,
    ) -> list[asyncpg.Record]:
        sql = (
            """
            SELECT a.*, s.name AS service_name, s.price AS service_price
              FROM appointments a
              JOIN services s ON s.id = a.service_id
             WHERE a.datetime >= ?
               AND a.datetime <  ?
            """
        )
        params: list[Any] = [period_start, period_end]
        if status is not None:
            sql += " AND a.status = ?"
            params.append(status)
        sql += " ORDER BY a.datetime"
        return await self.fetchall(sql, params)

    # -- transactions -------------------------------------------------------

    async def add_transaction(
        self,
        user_id: int,
        ttype: str,
        amount: int,
        category: str,
        comment: str,
        appointment_id: int | None = None,
        photo_file_id: str | None = None,
    ) -> int:
        return await self.execute(
            """
            INSERT INTO transactions(
                user_id, type, amount, category, comment, created_at,
                appointment_id, photo_file_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                user_id,
                ttype,
                amount,
                category,
                comment,
                _now(),
                appointment_id,
                photo_file_id,
            ),
        )

    async def transactions_in_period(
        self, start: datetime, end: datetime
    ) -> list[asyncpg.Record]:
        return await self.fetchall(
            """
            SELECT * FROM transactions
             WHERE created_at >= ?
               AND created_at <  ?
             ORDER BY created_at
            """,
            (start, end),
        )

    # -- master info --------------------------------------------------------

    async def get_master_info(self) -> asyncpg.Record | None:
        return await self.fetchone("SELECT * FROM master_info WHERE id = 1")

    async def set_master_info(
        self, text: str, photo_file_id: str | None
    ) -> None:
        await self.execute(
            """
            INSERT INTO master_info(id, text, photo_file_id)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                text = excluded.text,
                photo_file_id = excluded.photo_file_id
            """,
            (text, photo_file_id),
        )

    # -- welcome texts ------------------------------------------------------

    async def get_welcome_text(self, type_: str) -> str:
        row = await self.fetchone(
            "SELECT text FROM welcome_texts WHERE type = ?", (type_,)
        )
        if row is None:
            return (
                DEFAULT_WELCOME_NEW
                if type_ == "new"
                else DEFAULT_WELCOME_RETURNING
            )
        return row["text"]

    async def set_welcome_text(self, type_: str, text: str) -> None:
        await self.execute(
            """
            INSERT INTO welcome_texts(type, text) VALUES (?, ?)
            ON CONFLICT(type) DO UPDATE SET text = excluded.text
            """,
            (type_, text),
        )

    # -- settings -----------------------------------------------------------

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = await self.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return default
        return row["value"]

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    # -- CRM: clients listing ----------------------------------------------

    async def list_clients(
        self,
        *,
        only_role: str = "client",
        sleeping_days: int | None = None,
        top_spenders_only: bool = False,
        with_tag: str | None = None,
    ) -> list[asyncpg.Record]:
        """Return clients with aggregated visit/spending data and tag list."""
        params: list[Any] = [only_role]
        sql = """
            SELECT
                u.tg_id,
                u.preferred_name,
                u.phone,
                u.tg_username,
                u.tg_first_name,
                u.first_seen,
                u.last_interaction,
                COALESCE(SUM(CASE WHEN a.status = 'completed'
                                  THEN COALESCE(a.actual_amount, 0)
                                  ELSE 0 END), 0) AS total_spent,
                COALESCE(SUM(CASE WHEN a.status = 'completed' THEN 1 ELSE 0 END), 0)
                    AS completed_visits,
                COALESCE(SUM(CASE WHEN a.status = 'cancelled' THEN 1 ELSE 0 END), 0)
                    AS cancelled_visits,
                MAX(CASE WHEN a.status = 'completed' THEN a.datetime END)
                    AS last_visit_dt,
                (SELECT STRING_AGG(t.tag, ',')
                   FROM client_tags t WHERE t.tg_id = u.tg_id) AS tags_csv
              FROM users u
              LEFT JOIN appointments a ON a.client_tg_id = u.tg_id
             WHERE u.role = ?
        """
        if with_tag is not None:
            sql += (
                " AND EXISTS (SELECT 1 FROM client_tags t "
                "WHERE t.tg_id = u.tg_id AND t.tag = ?)"
            )
            params.append(with_tag)
        sql += " GROUP BY u.tg_id"
        if sleeping_days is not None:
            sql += (
                " HAVING (MAX(CASE WHEN a.status = 'completed' THEN a.datetime END) IS NULL"
                "         OR MAX(CASE WHEN a.status = 'completed' THEN a.datetime END)"
                "            <= ? - INTERVAL '1 day' * ?)"
            )
            params.extend([_now(), int(sleeping_days)])
        if top_spenders_only:
            sql += " ORDER BY total_spent DESC, completed_visits DESC"
        else:
            sql += " ORDER BY u.last_interaction DESC"
        return await self.fetchall(sql, params)

    async def get_client_history(self, tg_id: int) -> list[asyncpg.Record]:
        return await self.fetchall(
            """
            SELECT a.*, s.name AS service_name, s.price AS service_price
              FROM appointments a
              JOIN services s ON s.id = a.service_id
             WHERE a.client_tg_id = ?
             ORDER BY a.datetime DESC
            """,
            (tg_id,),
        )

    async def client_summary(self, tg_id: int) -> dict[str, Any]:
        row = await self.fetchone(
            """
            SELECT
                COALESCE(SUM(CASE WHEN status = 'completed'
                                  THEN COALESCE(actual_amount, 0) ELSE 0 END), 0)
                    AS total_spent,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                    AS completed,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END)
                    AS cancelled,
                SUM(CASE WHEN status = 'rescheduled' THEN 1 ELSE 0 END)
                    AS rescheduled,
                MAX(CASE WHEN status = 'completed' THEN datetime END)
                    AS last_visit_dt
              FROM appointments
             WHERE client_tg_id = ?
            """,
            (tg_id,),
        )
        if row is None:
            return {
                "total_spent": 0, "completed": 0, "cancelled": 0,
                "rescheduled": 0, "last_visit_dt": None,
            }
        return dict(row)

    # -- CRM: tags & notes --------------------------------------------------

    async def add_client_tag(self, tg_id: int, tag: str) -> bool:
        return (
            await self.execute_count(
                """
                INSERT INTO client_tags(tg_id, tag, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(tg_id, tag) DO NOTHING
                """,
                (tg_id, tag, _now()),
            )
            > 0
        )

    async def remove_client_tag(self, tg_id: int, tag: str) -> bool:
        return (
            await self.execute_count(
                "DELETE FROM client_tags WHERE tg_id = ? AND tag = ?",
                (tg_id, tag),
            )
            > 0
        )

    async def get_client_tags(self, tg_id: int) -> list[str]:
        rows = await self.fetchall(
            "SELECT tag FROM client_tags WHERE tg_id = ? ORDER BY tag",
            (tg_id,),
        )
        return [r["tag"] for r in rows]

    async def set_client_note(self, tg_id: int, note: str) -> None:
        await self.execute(
            """
            INSERT INTO client_notes(tg_id, note, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (tg_id, note, _now()),
        )

    async def get_client_note(self, tg_id: int) -> str:
        row = await self.fetchone(
            "SELECT note FROM client_notes WHERE tg_id = ?", (tg_id,)
        )
        return row["note"] if row else ""

    # -- Waitlist -----------------------------------------------------------

    async def add_to_waitlist(
        self,
        *,
        client_tg_id: int,
        service_id: int,
        target_date: str,
        target_time: str | None,
        client_name: str | None,
        client_phone: str | None,
    ) -> int:
        return await self.execute(
            """
            INSERT INTO waitlist(
                client_tg_id, service_id, target_date, target_time,
                client_name, client_phone, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                client_tg_id, service_id, target_date, target_time,
                client_name, client_phone, _now(),
            ),
        )

    async def list_waitlist_for_slot(
        self, *, service_id: int, target_date: str
    ) -> list[asyncpg.Record]:
        return await self.fetchall(
            """
            SELECT w.*, s.name AS service_name, s.duration_minutes AS service_duration
              FROM waitlist w
              JOIN services s ON s.id = w.service_id
             WHERE w.status = 'waiting'
               AND w.service_id = ?
               AND w.target_date = ?
             ORDER BY w.created_at
            """,
            (service_id, target_date),
        )

    async def list_active_waitlist_for_client(
        self, client_tg_id: int
    ) -> list[asyncpg.Record]:
        return await self.fetchall(
            """
            SELECT w.*, s.name AS service_name
              FROM waitlist w
              JOIN services s ON s.id = w.service_id
             WHERE w.client_tg_id = ? AND w.status = 'waiting'
             ORDER BY w.created_at DESC
            """,
            (client_tg_id,),
        )

    async def get_waitlist_entry(self, entry_id: int) -> asyncpg.Record | None:
        return await self.fetchone(
            """
            SELECT w.*, s.name AS service_name, s.price AS service_price,
                   s.duration_minutes AS service_duration
              FROM waitlist w
              JOIN services s ON s.id = w.service_id
             WHERE w.id = ?
            """,
            (entry_id,),
        )

    async def update_waitlist_status(
        self, entry_id: int, status: str, *, mark_notified: bool = False
    ) -> None:
        if mark_notified:
            await self.execute(
                "UPDATE waitlist SET status = ?, notified_at = ? WHERE id = ?",
                (status, _now(), entry_id),
            )
        else:
            await self.execute(
                "UPDATE waitlist SET status = ? WHERE id = ?",
                (status, entry_id),
            )

    async def list_all_waitlist(self) -> list[asyncpg.Record]:
        return await self.fetchall(
            """
            SELECT w.*, s.name AS service_name
              FROM waitlist w
              JOIN services s ON s.id = w.service_id
             WHERE w.status = 'waiting'
             ORDER BY w.target_date, w.created_at
            """
        )

    # -- Broadcasts ---------------------------------------------------------

    async def list_broadcast_audience(
        self, audience: str
    ) -> list[asyncpg.Record]:
        """Return user rows for a given audience selection."""
        if audience == "sleeping30":
            return await self.fetchall(
                """
                SELECT u.*, MAX(CASE WHEN a.status = 'completed' THEN a.datetime END)
                            AS last_visit_dt
                  FROM users u
                  LEFT JOIN appointments a ON a.client_tg_id = u.tg_id
                 WHERE u.role = 'client' AND COALESCE(u.broadcast_opt_out, 0) = 0
                 GROUP BY u.tg_id
                HAVING (MAX(CASE WHEN a.status = 'completed' THEN a.datetime END) IS NULL
                        OR MAX(CASE WHEN a.status = 'completed' THEN a.datetime END)
                           <= ? - INTERVAL '30 days')
                """,
                (_now(),),
            )
        if audience == "vip":
            return await self.fetchall(
                """
                SELECT DISTINCT u.*
                  FROM users u
                  JOIN client_tags t ON t.tg_id = u.tg_id
                 WHERE u.role = 'client' AND COALESCE(u.broadcast_opt_out, 0) = 0
                   AND t.tag = 'VIP'
                """
            )
        # default: all clients
        return await self.fetchall(
            """
            SELECT * FROM users
             WHERE role = 'client' AND COALESCE(broadcast_opt_out, 0) = 0
            """
        )

    async def record_broadcast(
        self,
        *,
        text: str,
        audience: str,
        sent: int,
        failed: int,
        kind: str = "manual",
    ) -> int:
        return await self.execute(
            """
            INSERT INTO broadcasts(text, audience, created_at, sent_count, failed_count, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (text, audience, _now(), sent, failed, kind),
        )

    async def mark_sleeping_pinged(self, tg_id: int) -> None:
        await self.execute(
            "UPDATE users SET last_sleeping_ping = ? WHERE tg_id = ?",
            (_now(), tg_id),
        )

    async def list_sleeping_for_auto_ping(
        self, *, days: int = 30, cooldown_days: int = 30
    ) -> list[asyncpg.Record]:
        """Sleeping clients we haven't pinged in the last cooldown_days."""
        return await self.fetchall(
            """
            SELECT u.*, MAX(CASE WHEN a.status = 'completed' THEN a.datetime END)
                        AS last_visit_dt
              FROM users u
              LEFT JOIN appointments a ON a.client_tg_id = u.tg_id
             WHERE u.role = 'client'
               AND COALESCE(u.broadcast_opt_out, 0) = 0
             GROUP BY u.tg_id
            HAVING MAX(CASE WHEN a.status = 'completed' THEN a.datetime END) IS NOT NULL
               AND MAX(CASE WHEN a.status = 'completed' THEN a.datetime END)
                   <= ? - INTERVAL '1 day' * ?
               AND (u.last_sleeping_ping IS NULL
                    OR u.last_sleeping_ping <= ? - INTERVAL '1 day' * ?)
            """,
            (
                _now(), int(days),
                _now(), int(cooldown_days),
            ),
        )

    # -- Analytics ---------------------------------------------------------

    async def analytics_top_services(
        self, since: datetime | None = None, limit: int = 5
    ) -> list[asyncpg.Record]:
        params: list[Any] = []
        sql = """
            SELECT s.id, s.name,
                   SUM(CASE WHEN a.status = 'completed' THEN 1 ELSE 0 END) AS visits,
                   SUM(CASE WHEN a.status = 'completed'
                            THEN COALESCE(a.actual_amount, 0) ELSE 0 END) AS revenue,
                   SUM(CASE WHEN a.status IN ('active','completed','cancelled','rescheduled')
                            THEN 1 ELSE 0 END) AS bookings
              FROM services s
              LEFT JOIN appointments a ON a.service_id = s.id
        """
        if since is not None:
            sql += " AND a.datetime >= ?"
            params.append(since)
        sql += " GROUP BY s.id, s.name ORDER BY visits DESC, revenue DESC LIMIT ?"
        params.append(limit)
        return await self.fetchall(sql, params)

    async def analytics_busy_heatmap(
        self, since: datetime | None = None
    ) -> list[asyncpg.Record]:
        """(weekday 0-6 Sun-based, hour 0-23, count) of completed/active appointments."""
        params: list[Any] = []
        sql = """
            SELECT CAST(EXTRACT(DOW FROM datetime) AS INT) AS weekday_sun0,
                   CAST(EXTRACT(HOUR FROM datetime) AS INT) AS hour,
                   COUNT(*) AS cnt
              FROM appointments
             WHERE status IN ('active','completed')
        """
        if since is not None:
            sql += " AND datetime >= ?"
            params.append(since)
        sql += " GROUP BY weekday_sun0, hour"
        return await self.fetchall(sql, params)

    async def analytics_funnel(
        self, since: datetime | None = None
    ) -> dict[str, int]:
        params: list[Any] = []
        where = ""
        if since is not None:
            where = " WHERE created_at >= ?"
            params.append(since)
        row = await self.fetchone(
            f"""
            SELECT
                COUNT(*) AS bookings,
                SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
                SUM(CASE WHEN status='rescheduled' THEN 1 ELSE 0 END) AS rescheduled,
                SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active
              FROM appointments
              {where}
            """,
            params,
        )
        if row is None:
            return {
                "bookings": 0, "completed": 0, "cancelled": 0,
                "rescheduled": 0, "active": 0,
            }
        return dict(row)
