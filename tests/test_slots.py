"""Unit tests for slot generation and forecast helpers."""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "0:dummy")
os.environ.setdefault("MASTER_TG_ID", "1")

from database import Database  # noqa: E402
from utils import forecast, slots  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def test_generate_slots_basic():
    day = date.today() + timedelta(days=2)
    free = slots.generate_slots(
        day=day,
        start=time(10, 0),
        end=time(12, 0),
        break_minutes=0,
        duration_minutes=60,
        busy=[],
        now=datetime.combine(day, time(0, 0)),
    )
    assert free
    assert all(s.date() == day for s in free)
    assert free[0].time() == time(10, 0)
    # last possible 60-minute slot starts at 11:00 (ends at 12:00)
    assert free[-1].time() == time(11, 0)


def test_generate_slots_filters_busy():
    day = date.today() + timedelta(days=2)
    busy_start = datetime.combine(day, time(13, 0))
    free = slots.generate_slots(
        day=day,
        start=time(10, 0),
        end=time(18, 0),
        break_minutes=0,
        duration_minutes=60,
        busy=[(busy_start, 60)],
        now=datetime.combine(day, time(0, 0)),
    )
    times = {s.time() for s in free}
    # 13:00-14:00 is busy; the 12:30-13:30 and 13:30-14:30 starts conflict.
    assert time(10, 0) in times
    assert time(12, 0) in times
    assert time(13, 0) not in times
    assert time(13, 30) not in times
    assert time(14, 0) in times


def test_generate_slots_respects_break():
    day = date.today() + timedelta(days=2)
    busy_start = datetime.combine(day, time(13, 0))
    free = slots.generate_slots(
        day=day,
        start=time(10, 0),
        end=time(18, 0),
        break_minutes=15,
        duration_minutes=60,
        busy=[(busy_start, 60)],
        now=datetime.combine(day, time(0, 0)),
    )
    times = {s.time() for s in free}
    # With 15-min break, 12:00 (ends 13:00) violates the 15-min pre-busy buffer.
    assert time(12, 0) not in times
    # 11:30 (ends 12:30) is fine: 30-min gap before 13:00 busy.
    assert time(11, 30) in times
    # 14:00 (starts at busy end) violates 15-min post-busy buffer.
    assert time(14, 0) not in times
    # 14:30 is the first valid slot after the busy interval.
    assert time(14, 30) in times


def test_forecast_no_data(tmp_path):
    async def _go():
        db = Database(tmp_path / "t.db")
        await db.init()
        f = await forecast.build_forecast(db)
        # No services / appointments -> only structural sanity.
        assert f.booked_amount == 0
        assert f.total_amount == 0
        assert "Прогноз" in f.render()

    _run(_go())


def test_database_roundtrip():
    async def _go():
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "rt.db")
            await db.init()
            await db.upsert_user_visit(123, role="client")
            sid = await db.add_service("test", 100, 30, "desc")
            assert sid > 0
            await db.upsert_schedule(0, "10:00", "18:00", 0)
            sched = await db.get_schedule()
            assert len(sched) == 1
            await db.set_setting("tax_rate", "4")
            assert await db.get_setting("tax_rate") == "4"

    _run(_go())
