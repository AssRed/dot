"""Helpers for the client waitlist feature.

When an active appointment is cancelled or rescheduled, we look for waitlist
entries whose target_date and service match and ping each waiting client with
an inline button to grab the now-free slot.
"""
from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot

import keyboards as kb
from database import Database

logger = logging.getLogger(__name__)


async def notify_waitlist_for_freed_slot(
    *,
    bot: Bot,
    db: Database,
    service_id: int,
    freed_dt: datetime,
) -> int:
    """Notify all waiting clients for the same service+day. Return count notified."""
    target_date = freed_dt.strftime("%Y-%m-%d")
    entries = await db.list_waitlist_for_slot(
        service_id=service_id, target_date=target_date
    )
    if not entries:
        return 0
    when = freed_dt.strftime("%d.%m в %H:%M")
    notified = 0
    for entry in entries:
        text = (
            "🟢 Освободилось окно у мастера!\n\n"
            f"Услуга: <b>{entry['service_name']}</b>\n"
            f"Время: <b>{when}</b>\n\n"
            "Если хотите занять — нажмите «Записаться» (это запустит обычный "
            "процесс выбора времени)."
        )
        try:
            await bot.send_message(
                int(entry["client_tg_id"]),
                text,
                reply_markup=kb.waitlist_book_kb(int(entry["id"])),
            )
            await db.update_waitlist_status(
                int(entry["id"]), "notified", mark_notified=True
            )
            notified += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to notify waitlist entry %s: %s", entry["id"], exc
            )
    return notified
