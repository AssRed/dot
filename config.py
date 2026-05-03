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
    db_path: Path
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

        db_path = Path(os.getenv("DB_PATH", "bot.db"))
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path

        tz = os.getenv("TZ", "Europe/Moscow")
        log_path = BASE_DIR / "bot.log"

        return cls(
            bot_token=token,
            master_tg_id=master_id,
            db_path=db_path,
            tz=tz,
            log_path=log_path,
        )
