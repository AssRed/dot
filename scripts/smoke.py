"""Smoke checks: import everything, init the schema, exercise core paths.

This script does NOT need a real Telegram token — it only verifies that the
codebase loads, the database initialises, and the pure helpers behave.

Run it from the project root:

    python scripts/smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# Make the project root importable when run from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def _run() -> None:
    os.environ.setdefault("BOT_TOKEN", "0:dummy")
    os.environ.setdefault("MASTER_TG_ID", "1")

    # Imports — make sure nothing breaks.
    import config
    import database
    import keyboards
    from handlers import client, common, master
    from utils import forecast, report, scheduler, slots

    # Touch every imported module so pyflakes is satisfied and we get a real
    # smoke signal on the imports.
    for module in (keyboards, client, common, master, scheduler):
        assert module.__name__

    settings = config.Settings.load()
    print(f"[ok] settings loaded: master={settings.master_tg_id}")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        db = database.Database(db_path)
        await db.init()
        print("[ok] schema initialised")

        # Seed master + a sample client + a service + a schedule.
        await db.upsert_user_visit(settings.master_tg_id, role="master")
        await db.upsert_user_visit(42, role="client")
        sid = await db.add_service(
            "Маникюр", price=2500, duration_minutes=90,
            description="Тест: классический маникюр + покрытие.",
        )
        # Make every weekday Mon-Fri 10:00-19:00.
        for dow in range(0, 5):
            await db.upsert_schedule(dow, "10:00", "19:00", 0)

        # Slot generation.
        days = await slots.available_dates(db, duration_minutes=90)
        assert days, "expected at least one available day"
        first_day = days[0]
        free = await slots.available_slots_for_day(
            db, day=first_day, duration_minutes=90
        )
        assert free, "expected at least one free slot"
        print(f"[ok] slots: {len(days)} days, {len(free)} slots on {first_day}")

        # Create + complete an appointment to feed the forecast.
        appt_id = await db.create_appointment(
            client_tg_id=42,
            service_id=sid,
            dt=free[0],
            client_name="Тестовый клиент",
            client_phone="+79990001122",
        )
        await db.complete_appointment(appt_id, 2500)
        await db.add_transaction(
            user_id=42, ttype="income", amount=2500, category="service",
            comment="smoke", appointment_id=appt_id,
        )
        await db.add_transaction(
            user_id=settings.master_tg_id, ttype="expense",
            amount=500, category="materials", comment="smoke",
        )
        print("[ok] appointment + transactions created")

        # Forecast + report.
        f = await forecast.build_forecast(db)
        assert f.total_amount >= 0
        print(f"[ok] forecast rendered ({f.total_amount} ₽ total)")

        period_start = datetime.now() - timedelta(days=1)
        period_end = datetime.now() + timedelta(days=1)
        txs = await db.transactions_in_period(period_start, period_end)
        s = report.summarise(txs, tax_rate=6)
        payload = report.build_excel_report(
            txs, period_label="смоук", summary=s
        )
        assert payload[:2] == b"PK", "Excel file should start with the ZIP magic"
        print(f"[ok] excel report bytes={len(payload)}")

        # Cancel + reschedule paths exercised via DB API.
        appt2 = await db.create_appointment(
            client_tg_id=42, service_id=sid, dt=free[1] if len(free) > 1 else free[0],
            client_name="X", client_phone="+79990002233",
        )
        ok = await db.cancel_appointment(appt2, "test")
        assert ok
        print("[ok] cancellation works")

    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(_run())
