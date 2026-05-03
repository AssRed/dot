"""SQLite database access layer for the beauty-master bot.

All functions are async and rely on :mod:`aiosqlite`. The schema is created
on the first connection. Default settings, welcome texts and an empty
master_info row are inserted on first run.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import aiosqlite

logger = logging.getLogger(__name__)

DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


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
        tg_id INTEGER PRIMARY KEY,
        role TEXT NOT NULL DEFAULT 'client',
        first_seen DATETIME NOT NULL,
        last_interaction DATETIME NOT NULL,
        total_visits INTEGER NOT NULL DEFAULT 0,
        preferred_name TEXT,
        phone TEXT,
        last_service_id INTEGER,
        last_appointment_id INTEGER,
        tg_username TEXT,
        tg_first_name TEXT,
        last_sleeping_ping DATETIME,
        broadcast_opt_out INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price INTEGER NOT NULL,
        duration_minutes INTEGER NOT NULL,
        description TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schedule (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day_of_week INTEGER NOT NULL UNIQUE,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        break_minutes INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_tg_id INTEGER NOT NULL,
        service_id INTEGER NOT NULL,
        datetime DATETIME NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        actual_amount INTEGER,
        created_at DATETIME NOT NULL,
        cancelled_at DATETIME,
        cancel_reason TEXT,
        client_phone TEXT,
        client_name TEXT,
        reminder_sent INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(service_id) REFERENCES services(id),
        FOREIGN KEY(client_tg_id) REFERENCES users(tg_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        amount INTEGER NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        comment TEXT NOT NULL DEFAULT '',
        created_at DATETIME NOT NULL,
        appointment_id INTEGER,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        tg_id INTEGER PRIMARY KEY,
        note TEXT NOT NULL DEFAULT '',
        updated_at DATETIME NOT NULL,
        FOREIGN KEY(tg_id) REFERENCES users(tg_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS client_tags (
        tg_id INTEGER NOT NULL,
        tag TEXT NOT NULL,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (tg_id, tag),
        FOREIGN KEY(tg_id) REFERENCES users(tg_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS waitlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_tg_id INTEGER NOT NULL,
        service_id INTEGER NOT NULL,
        target_date TEXT NOT NULL,
        target_time TEXT,
        client_name TEXT,
        client_phone TEXT,
        created_at DATETIME NOT NULL,
        notified_at DATETIME,
        status TEXT NOT NULL DEFAULT 'waiting',
        FOREIGN KEY(service_id) REFERENCES services(id),
        FOREIGN KEY(client_tg_id) REFERENCES users(tg_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS broadcasts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        audience TEXT NOT NULL DEFAULT 'all',
        created_at DATETIME NOT NULL,
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
    ("last_sleeping_ping", "DATETIME"),
    ("broadcast_opt_out", "INTEGER NOT NULL DEFAULT 0"),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now().strftime(DATETIME_FMT)


def to_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(value, DATETIME_FMT)
    except ValueError:
        # SQLite may store sub-second precision in some cases.
        return datetime.fromisoformat(value)


def to_str(dt: datetime) -> str:
    return dt.strftime(DATETIME_FMT)


# ---------------------------------------------------------------------------
# Database wrapper
# ---------------------------------------------------------------------------


class Database:
    """Thin async wrapper around aiosqlite with schema bootstrap."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)

    async def init(self) -> None:
        """Create tables, run column migrations, seed default rows."""
        async with self.connect() as conn:
            for stmt in SCHEMA_STATEMENTS:
                await conn.execute(stmt)
            await self._migrate_users(conn)
            await self._seed(conn)
            await conn.commit()
        logger.info("Database initialised at %s", self.path)

    async def _migrate_users(self, conn: aiosqlite.Connection) -> None:
        """Add missing columns from USERS_MIGRATIONS (idempotent)."""
        cur = await conn.execute("PRAGMA table_info(users)")
        existing = {row[1] for row in await cur.fetchall()}
        for col, ddl in USERS_MIGRATIONS:
            if col not in existing:
                await conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")

    async def _seed(self, conn: aiosqlite.Connection) -> None:
        # default settings
        for key, value in DEFAULT_SETTINGS.items():
            await conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        # default welcome texts
        await conn.execute(
            "INSERT OR IGNORE INTO welcome_texts(type, text) VALUES (?, ?)",
            ("new", DEFAULT_WELCOME_NEW),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO welcome_texts(type, text) VALUES (?, ?)",
            ("returning", DEFAULT_WELCOME_RETURNING),
        )
        # master_info row id=1 always present (text empty until configured)
        await conn.execute(
            "INSERT OR IGNORE INTO master_info(id, text, photo_file_id) VALUES (1, '', NULL)"
        )

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            await conn.close()

    # -- low level helpers --------------------------------------------------

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self.connect() as conn:
            cur = await conn.execute(sql, tuple(params))
            await conn.commit()
            return cur.lastrowid or 0

    async def fetchone(
        self, sql: str, params: Iterable[Any] = ()
    ) -> aiosqlite.Row | None:
        async with self.connect() as conn:
            cur = await conn.execute(sql, tuple(params))
            return await cur.fetchone()

    async def fetchall(
        self, sql: str, params: Iterable[Any] = ()
    ) -> list[aiosqlite.Row]:
        async with self.connect() as conn:
            cur = await conn.execute(sql, tuple(params))
            return list(await cur.fetchall())

    # -- users --------------------------------------------------------------

    async def get_user(self, tg_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))

    async def upsert_user_visit(
        self,
        tg_id: int,
        role: str = "client",
        *,
        username: str | None = None,
        first_name: str | None = None,
    ) -> tuple[bool, aiosqlite.Row]:
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
        async with self.connect() as conn:
            cur = await conn.execute(
                f"UPDATE services SET {', '.join(sets)} WHERE id = ?",
                tuple(vals),
            )
            await conn.commit()
            return cur.rowcount > 0

    async def delete_service(self, service_id: int) -> bool:
        async with self.connect() as conn:
            cur = await conn.execute("DELETE FROM services WHERE id = ?", (service_id,))
            await conn.commit()
            return cur.rowcount > 0

    async def get_service(self, service_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM services WHERE id = ?", (service_id,))

    async def list_services(self) -> list[aiosqlite.Row]:
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

    async def get_schedule(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM schedule ORDER BY day_of_week"
        )

    async def get_day_schedule(self, day_of_week: int) -> aiosqlite.Row | None:
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
            """,
            (
                client_tg_id,
                service_id,
                to_str(dt),
                _now(),
                client_name,
                client_phone,
            ),
        )

    async def get_appointment(self, appt_id: int) -> aiosqlite.Row | None:
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

    async def list_active_for_client(self, tg_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT a.*, s.name AS service_name, s.price AS service_price,
                   s.duration_minutes AS service_duration
              FROM appointments a
              JOIN services s ON s.id = a.service_id
             WHERE a.client_tg_id = ? AND a.status = 'active'
               AND datetime(a.datetime) >= datetime(?)
             ORDER BY a.datetime
            """,
            (tg_id, _now()),
        )

    async def list_upcoming(self) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT a.*, s.name AS service_name, s.price AS service_price,
                   s.duration_minutes AS service_duration
              FROM appointments a
              JOIN services s ON s.id = a.service_id
             WHERE a.status = 'active'
               AND datetime(a.datetime) >= datetime(?)
             ORDER BY a.datetime
            """,
            (_now(),),
        )

    async def list_active_for_day(self, day: datetime) -> list[aiosqlite.Row]:
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return await self.fetchall(
            """
            SELECT a.*, s.duration_minutes AS service_duration
              FROM appointments a
              JOIN services s ON s.id = a.service_id
             WHERE a.status = 'active'
               AND datetime(a.datetime) >= datetime(?)
               AND datetime(a.datetime) <  datetime(?)
             ORDER BY a.datetime
            """,
            (to_str(start), to_str(end)),
        )

    async def cancel_appointment(
        self, appt_id: int, reason: str | None, mark_rescheduled: bool = False
    ) -> bool:
        status = "rescheduled" if mark_rescheduled else "cancelled"
        async with self.connect() as conn:
            cur = await conn.execute(
                """
                UPDATE appointments
                   SET status = ?, cancelled_at = ?, cancel_reason = ?
                 WHERE id = ? AND status = 'active'
                """,
                (status, _now(), reason, appt_id),
            )
            await conn.commit()
            return cur.rowcount > 0

    async def complete_appointment(
        self, appt_id: int, actual_amount: int
    ) -> bool:
        async with self.connect() as conn:
            cur = await conn.execute(
                """
                UPDATE appointments
                   SET status = 'completed', actual_amount = ?
                 WHERE id = ? AND status = 'active'
                """,
                (actual_amount, appt_id),
            )
            await conn.commit()
            return cur.rowcount > 0

    async def mark_reminder_sent(self, appt_id: int) -> None:
        await self.execute(
            "UPDATE appointments SET reminder_sent = 1 WHERE id = ?",
            (appt_id,),
        )

    async def appointments_for_reminders(
        self, window_start: datetime, window_end: datetime
    ) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT a.*, s.name AS service_name, s.duration_minutes AS service_duration
              FROM appointments a
              JOIN services s ON s.id = a.service_id
             WHERE a.status = 'active'
               AND a.reminder_sent = 0
               AND datetime(a.datetime) >= datetime(?)
               AND datetime(a.datetime) <  datetime(?)
            """,
            (to_str(window_start), to_str(window_end)),
        )

    async def appointments_in_period(
        self,
        period_start: datetime,
        period_end: datetime,
        status: str | None = None,
    ) -> list[aiosqlite.Row]:
        sql = (
            """
            SELECT a.*, s.name AS service_name, s.price AS service_price
              FROM appointments a
              JOIN services s ON s.id = a.service_id
             WHERE datetime(a.datetime) >= datetime(?)
               AND datetime(a.datetime) <  datetime(?)
            """
        )
        params: list[Any] = [to_str(period_start), to_str(period_end)]
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
    ) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """
            SELECT * FROM transactions
             WHERE datetime(created_at) >= datetime(?)
               AND datetime(created_at) <  datetime(?)
             ORDER BY created_at
            """,
            (to_str(start), to_str(end)),
        )

    # -- master info --------------------------------------------------------

    async def get_master_info(self) -> aiosqlite.Row | None:
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
    ) -> list[aiosqlite.Row]:
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
                (SELECT GROUP_CONCAT(t.tag, ',')
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
                " HAVING (last_visit_dt IS NULL"
                "         OR datetime(last_visit_dt) <= datetime(?, ?))"
            )
            params.extend([_now(), f"-{int(sleeping_days)} days"])
        if top_spenders_only:
            sql += " ORDER BY total_spent DESC, completed_visits DESC"
        else:
            sql += " ORDER BY datetime(u.last_interaction) DESC"
        return await self.fetchall(sql, params)

    async def get_client_history(self, tg_id: int) -> list[aiosqlite.Row]:
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
        return dict(row) if row else {
            "total_spent": 0, "completed": 0, "cancelled": 0,
            "rescheduled": 0, "last_visit_dt": None,
        }

    # -- CRM: tags & notes --------------------------------------------------

    async def add_client_tag(self, tg_id: int, tag: str) -> bool:
        async with self.connect() as conn:
            cur = await conn.execute(
                """
                INSERT OR IGNORE INTO client_tags(tg_id, tag, created_at)
                VALUES (?, ?, ?)
                """,
                (tg_id, tag, _now()),
            )
            await conn.commit()
            return cur.rowcount > 0

    async def remove_client_tag(self, tg_id: int, tag: str) -> bool:
        async with self.connect() as conn:
            cur = await conn.execute(
                "DELETE FROM client_tags WHERE tg_id = ? AND tag = ?",
                (tg_id, tag),
            )
            await conn.commit()
            return cur.rowcount > 0

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
            """,
            (
                client_tg_id, service_id, target_date, target_time,
                client_name, client_phone, _now(),
            ),
        )

    async def list_waitlist_for_slot(
        self, *, service_id: int, target_date: str
    ) -> list[aiosqlite.Row]:
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
    ) -> list[aiosqlite.Row]:
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

    async def get_waitlist_entry(self, entry_id: int) -> aiosqlite.Row | None:
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

    async def list_all_waitlist(self) -> list[aiosqlite.Row]:
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
    ) -> list[aiosqlite.Row]:
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
                HAVING (last_visit_dt IS NULL
                        OR datetime(last_visit_dt) <= datetime(?, '-30 days'))
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
    ) -> list[aiosqlite.Row]:
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
            HAVING last_visit_dt IS NOT NULL
               AND datetime(last_visit_dt) <= datetime(?, ?)
               AND (u.last_sleeping_ping IS NULL
                    OR datetime(u.last_sleeping_ping) <= datetime(?, ?))
            """,
            (
                _now(), f"-{int(days)} days",
                _now(), f"-{int(cooldown_days)} days",
            ),
        )

    # -- Analytics ---------------------------------------------------------

    async def analytics_top_services(
        self, since: datetime | None = None, limit: int = 5
    ) -> list[aiosqlite.Row]:
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
            sql += " AND datetime(a.datetime) >= datetime(?)"
            params.append(to_str(since))
        sql += " GROUP BY s.id ORDER BY visits DESC, revenue DESC LIMIT ?"
        params.append(limit)
        return await self.fetchall(sql, params)

    async def analytics_busy_heatmap(
        self, since: datetime | None = None
    ) -> list[aiosqlite.Row]:
        """(weekday 0-6, hour 0-23, count) of completed/active appointments."""
        params: list[Any] = []
        sql = """
            SELECT CAST(strftime('%w', datetime) AS INT) AS weekday_sun0,
                   CAST(strftime('%H', datetime) AS INT) AS hour,
                   COUNT(*) AS cnt
              FROM appointments
             WHERE status IN ('active','completed')
        """
        if since is not None:
            sql += " AND datetime(datetime) >= datetime(?)"
            params.append(to_str(since))
        sql += " GROUP BY weekday_sun0, hour"
        return await self.fetchall(sql, params)

    async def analytics_funnel(
        self, since: datetime | None = None
    ) -> dict[str, int]:
        params: list[Any] = []
        where = ""
        if since is not None:
            where = " WHERE datetime(created_at) >= datetime(?)"
            params.append(to_str(since))
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
        return dict(row) if row else {
            "bookings": 0, "completed": 0, "cancelled": 0,
            "rescheduled": 0, "active": 0,
        }
