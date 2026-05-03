"""FastAPI entrypoint for hosted deployment (Fly.io / any container host).

Uses Telegram **webhook** mode so that incoming updates are delivered as
HTTP POSTs to the `/webhook/{secret}` endpoint. This plays nicely with
Fly.io's auto-stop / auto-start: incoming updates wake the machine if
it is idle, and the worker doesn't have to keep a long-poll connection
open 24/7.

Run locally:
    uvicorn app:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import logging
import os
import secrets as _secrets
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request

from config import Settings
from database import Database
from handlers import client as client_handlers
from handlers import common as common_handlers
from handlers import master as master_handlers
from main import setup_logging
from utils.scheduler import start_scheduler

logger = logging.getLogger(__name__)


def _resolve_webhook_base() -> str | None:
    """Return public HTTPS base URL for the bot, or None if unknown."""
    explicit = os.getenv("WEBHOOK_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    fly_app = os.getenv("FLY_APP_NAME", "").strip()
    if fly_app:
        return f"https://{fly_app}.fly.dev"
    return None


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

    # Webhook setup
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip() or _secrets.token_urlsafe(32)
    webhook_base = _resolve_webhook_base()
    webhook_path = f"/webhook/{webhook_secret}"

    app.state.settings = settings
    app.state.bot = bot
    app.state.dispatcher = dp
    app.state.db = db
    app.state.scheduler = scheduler
    app.state.webhook_secret = webhook_secret

    if webhook_base:
        webhook_url = f"{webhook_base}{webhook_path}"
        try:
            await bot.set_webhook(
                url=webhook_url,
                drop_pending_updates=False,
                allowed_updates=["message", "callback_query"],
            )
            log.info("Webhook set to %s", webhook_url)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to set webhook to %s: %s", webhook_url, exc)
    else:
        log.warning(
            "No WEBHOOK_BASE_URL or FLY_APP_NAME — webhook NOT set. "
            "Bot will not receive updates until you set the webhook manually."
        )

    try:
        yield
    finally:
        log.info("Shutting down")
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception:  # noqa: BLE001
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


@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request) -> dict[str, bool]:
    expected = getattr(request.app.state, "webhook_secret", None)
    if not expected or secret != expected:
        raise HTTPException(status_code=404, detail="Not found")
    payload = await request.json()
    bot: Bot = request.app.state.bot
    dp: Dispatcher = request.app.state.dispatcher
    update = Update.model_validate(payload, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}
