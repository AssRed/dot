"""APScheduler integration for hourly reminder checks."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import Database, to_dt

logger = logging.getLogger(__name__)


def format_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m в %H:%M")


async def check_reminders(bot: Bot, db: Database, master_id: int) -> None:
    """Send reminders for appointments starting in ~2 hours."""
    now = datetime.now()
    # Look for appointments that fall in the (110-130 minutes) window so that
    # the hourly poll catches each appointment exactly once.
    window_start = now + timedelta(minutes=110)
    window_end = now + timedelta(minutes=130)

    rows = await db.appointments_for_reminders(window_start, window_end)
    if not rows:
        return

    for row in rows:
        appt_dt = to_dt(row["datetime"])
        if appt_dt is None:
            continue
        when = format_dt(appt_dt)
        client_text = (
            f"⏰ Напоминание: у вас запись на <b>{row['service_name']}</b> "
            f"{when}. Будем рады видеть вас 🌸"
        )
        master_text = (
            f"⏰ Напоминание: запись клиента {row['client_name']} "
            f"({row['client_phone']}) на <b>{row['service_name']}</b> {when}."
        )
        try:
            await bot.send_message(row["client_tg_id"], client_text)
        except Exception as exc:  # noqa: BLE001 - we want to log all failures
            logger.warning("Failed to remind client %s: %s", row["client_tg_id"], exc)
        try:
            await bot.send_message(master_id, master_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to remind master: %s", exc)
        await db.mark_reminder_sent(row["id"])


def start_scheduler(bot: Bot, db: Database, master_id: int, *, tz: str) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        check_reminders,
        trigger=IntervalTrigger(minutes=20),
        kwargs={"bot": bot, "db": db, "master_id": master_id},
        id="reminders",
        next_run_time=datetime.now() + timedelta(seconds=30),
    )
    scheduler.start()
    logger.info("Reminder scheduler started (tz=%s)", tz)
    return scheduler
