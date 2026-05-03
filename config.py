"""Application configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    bot_token: str
    master_tg_id: int
    database_url: str
    tz: str
    log_path: Path

    @classmethod
    def load(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
            )

        raw_master_id = os.getenv("MASTER_TG_ID", "").strip()
        if not raw_master_id:
            raise RuntimeError(
                "MASTER_TG_ID is not set. Put the master's Telegram user ID into .env."
            )
        try:
            master_id = int(raw_master_id)
        except ValueError as exc:
            raise RuntimeError("MASTER_TG_ID must be an integer") from exc

        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. Use a Postgres connection string "
                "(postgres://user:pass@host/db?sslmode=require)."
            )
        # asyncpg expects a libpq-style URL but does not support `?sslmode=...`
        # query parameter directly; we keep the URL as-is (asyncpg honours
        # `sslmode=require` for managed providers like Neon when forwarded).
        if database_url.startswith("postgresql+"):
            # SQLAlchemy-style → strip dialect suffix.
            database_url = "postgresql://" + database_url.split("://", 1)[1]

        tz = os.getenv("TZ", "Europe/Moscow")
        log_path = BASE_DIR / "bot.log"

        return cls(
            bot_token=token,
            master_tg_id=master_id,
            database_url=database_url,
            tz=tz,
            log_path=log_path,
        )
