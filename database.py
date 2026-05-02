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
        last_appointment_id INTEGER
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
    "CREATE INDEX IF NOT EXISTS idx_appt_client ON appointments(client_tg_id)",
    "CREATE INDEX IF NOT EXISTS idx_appt_dt ON appointments(datetime)",
    "CREATE INDEX IF NOT EXISTS idx_appt_status ON appointments(status)",
    "CREATE INDEX IF NOT EXISTS idx_tx_created ON transactions(created_at)",
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
        """Create tables and seed default rows."""
        async with self.connect() as conn:
            for stmt in SCHEMA_STATEMENTS:
                await conn.execute(stmt)
            await self._seed(conn)
            await conn.commit()
        logger.info("Database initialised at %s", self.path)

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
        self, tg_id: int, role: str = "client"
    ) -> tuple[bool, aiosqlite.Row]:
        """Insert or update a user; return (is_new, row)."""
        existing = await self.get_user(tg_id)
        now = _now()
        if existing is None:
            await self.execute(
                """
                INSERT INTO users(tg_id, role, first_seen, last_interaction, total_visits)
                VALUES (?, ?, ?, ?, 1)
                """,
                (tg_id, role, now, now),
            )
            row = await self.get_user(tg_id)
            assert row is not None
            return True, row
        await self.execute(
            """
            UPDATE users
               SET last_interaction = ?,
                   total_visits = total_visits + 1
             WHERE tg_id = ?
            """,
            (now, tg_id),
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
