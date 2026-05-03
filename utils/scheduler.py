"""APScheduler integration: 2h reminders and daily auto-broadcast."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from database import Database, to_dt

logger = logging.getLogger(__name__)

# Default text used for the "we miss you" auto-broadcast. Master can override
# via the bot setting `sleeping_ping_text`.
DEFAULT_SLEEPING_PING = (
    "Соскучились! 🌸\n\n"
    "Заметила, что вы давно не были у меня — а ведь у меня появились новые "
    "услуги и свободные окошки. Загляните в бот и выберите удобное время — "
    "буду рада видеть вас снова!"
)


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


async def auto_ping_sleeping_clients(
    bot: Bot, db: Database, master_id: int
) -> None:
    """Daily job: ping clients with no completed visit in the last 30 days.

    Skips anyone we already pinged in the last 30 days. Records a row in
    `broadcasts` for traceability.
    """
    rows = await db.list_sleeping_for_auto_ping(days=30, cooldown_days=30)
    if not rows:
        logger.info("auto_ping: nothing to do")
        return
    text = (
        await db.get_setting("sleeping_ping_text")
        or DEFAULT_SLEEPING_PING
    )
    sent = 0
    failed = 0
    for row in rows:
        try:
            await bot.send_message(int(row["tg_id"]), text)
            await db.mark_sleeping_pinged(int(row["tg_id"]))
            sent += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning(
                "auto_ping failed for %s: %s", row["tg_id"], exc
            )
    await db.record_broadcast(
        text=text,
        audience="sleeping30",
        sent=sent,
        failed=failed,
        kind="auto_sleeping",
    )
    logger.info("auto_ping: sent=%d failed=%d", sent, failed)
    if sent or failed:
        try:
            await bot.send_message(
                master_id,
                f"📣 Авторассылка спящим клиентам:\n"
                f"Отправлено: {sent}\nНе доставлено: {failed}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto_ping master notification failed: %s", exc)


def start_scheduler(bot: Bot, db: Database, master_id: int, *, tz: str) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        check_reminders,
        trigger=IntervalTrigger(minutes=20),
        kwargs={"bot": bot, "db": db, "master_id": master_id},
        id="reminders",
        next_run_time=datetime.now() + timedelta(seconds=30),
    )
    # Daily 11:00 in the master's timezone.
    scheduler.add_job(
        auto_ping_sleeping_clients,
        trigger=CronTrigger(hour=11, minute=0),
        kwargs={"bot": bot, "db": db, "master_id": master_id},
        id="auto_ping_sleeping",
    )
    scheduler.start()
    logger.info("Scheduler started (tz=%s); jobs: reminders, auto_ping_sleeping", tz)
    return scheduler
