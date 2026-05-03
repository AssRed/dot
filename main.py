"""Entrypoint for the beauty-master Telegram bot."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update

from config import Settings
from database import Database
from handlers import client as client_handlers
from handlers import common as common_handlers
from handlers import master as master_handlers
from utils.scheduler import start_scheduler


def setup_logging(log_path: str) -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    formatter = logging.Formatter(fmt)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Avoid double handlers if reloaded.
    root.handlers = [file_handler, stream_handler]


async def main() -> None:
    settings = Settings.load()
    setup_logging(str(settings.log_path))
    log = logging.getLogger(__name__)

    db = Database(settings.database_url)
    await db.init()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Inject dependencies via the dispatcher's workflow data.
    dp["db"] = db
    dp["settings"] = settings
    dp["bot"] = bot

    dp.include_router(common_handlers.router)
    dp.include_router(master_handlers.router)
    dp.include_router(client_handlers.router)

    @dp.errors()
    async def on_error(event: Any, exception: Exception | None = None) -> bool:
        # In aiogram 3 the handler signature is ``on_error(event)`` where
        # ``event`` is an ErrorEvent with ``.update`` and ``.exception``.
        update: Update | None = getattr(event, "update", None)
        exc = getattr(event, "exception", exception)
        log.exception(
            "Unhandled error while processing update %s", update, exc_info=exc
        )
        return True

    scheduler = start_scheduler(bot, db, settings.master_tg_id, tz=settings.tz)
    log.info("Bot started")
    try:
        # Drop any previously-registered webhook so polling receives updates.
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
