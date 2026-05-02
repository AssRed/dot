"""FastAPI entrypoint for hosted deployment (Fly.io / any container host).

Exposes a tiny HTTP surface for healthchecks while running the bot's
long-polling loop as an asyncio background task on the same event loop.

Why FastAPI?
    Most managed container platforms (Fly.io, Railway with web service,
    Cloud Run) expect an HTTP service that listens on ``$PORT``. A worker-
    only container is trickier to configure and to keep alive. By bundling
    a small FastAPI app we:
      * give the platform an obvious "is alive" endpoint,
      * keep a single process,
      * still use the native aiogram polling (no webhook plumbing),
      * trivially fall back to ``python main.py`` for VPS / Railway worker
        deployments.

Run locally:
    uvicorn app:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from fastapi import FastAPI

from config import Settings
from database import Database
from handlers import client as client_handlers
from handlers import common as common_handlers
from handlers import master as master_handlers
from main import setup_logging
from utils.scheduler import start_scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.load()
    setup_logging(str(settings.log_path))
    log = logging.getLogger("beauty-bot")

    db = Database(settings.db_path)
    await db.init()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db
    dp["settings"] = settings
    dp["bot"] = bot

    dp.include_router(common_handlers.router)
    dp.include_router(master_handlers.router)
    dp.include_router(client_handlers.router)

    @dp.errors()
    async def on_error(event: Any) -> bool:
        update: Update | None = getattr(event, "update", None)
        exc = getattr(event, "exception", None)
        log.exception(
            "Unhandled error while processing update %s", update, exc_info=exc
        )
        return True

    scheduler = start_scheduler(bot, db, settings.master_tg_id, tz=settings.tz)

    # Drop any pending webhook so polling can take over cleanly.
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_webhook failed (likely no webhook set): %s", exc)

    polling_task = asyncio.create_task(dp.start_polling(bot))
    log.info("Bot polling started")

    app.state.settings = settings
    app.state.bot = bot
    app.state.dispatcher = dp
    app.state.db = db
    app.state.scheduler = scheduler
    app.state.polling_task = polling_task

    try:
        yield
    finally:
        log.info("Shutting down")
        await dp.stop_polling()
        polling_task.cancel()
        try:
            await polling_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        scheduler.shutdown(wait=False)
        await bot.session.close()


app = FastAPI(
    title="Beauty Master Bot",
    description="Telegram bot for a single beauty master.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "beauty-master-bot"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
