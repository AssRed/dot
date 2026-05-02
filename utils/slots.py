"""Helpers for computing free time slots based on the master's schedule."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable, Sequence

SLOT_STEP_MINUTES = 30


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def schedule_for_day(
    schedule_rows: Iterable, day: date
) -> tuple[time, time, int] | None:
    """Return (start, end, break_minutes) for ``day`` or ``None`` if day off."""
    dow = day.weekday()
    for row in schedule_rows:
        if row["day_of_week"] == dow:
            return (
                parse_hhmm(row["start_time"]),
                parse_hhmm(row["end_time"]),
                int(row["break_minutes"]),
            )
    return None


def generate_slots(
    *,
    day: date,
    start: time,
    end: time,
    break_minutes: int,
    duration_minutes: int,
    busy: Sequence[tuple[datetime, int]],
    now: datetime | None = None,
    step_minutes: int = SLOT_STEP_MINUTES,
) -> list[datetime]:
    """Compute free start times for a service of ``duration_minutes``.

    Args:
        day: target calendar day.
        start, end: working hours boundaries.
        break_minutes: padding to keep between appointments.
        duration_minutes: length of the requested service.
        busy: existing active appointments as ``(datetime, duration_minutes)``.
        now: reference "current" datetime for filtering past slots.
        step_minutes: granularity at which slots are offered to clients.
    """
    now = now or datetime.now()
    day_start = datetime.combine(day, start)
    day_end = datetime.combine(day, end)
    slot_delta = timedelta(minutes=step_minutes)
    duration = timedelta(minutes=duration_minutes)
    pad = timedelta(minutes=break_minutes)

    busy_intervals = [
        (b_start - pad, b_start + timedelta(minutes=b_dur) + pad)
        for b_start, b_dur in busy
    ]

    slots: list[datetime] = []
    cursor = day_start
    while cursor + duration <= day_end:
        if cursor < now + timedelta(minutes=15):
            cursor += slot_delta
            continue
        slot_end = cursor + duration
        overlaps = any(
            cursor < b_end and slot_end > b_start
            for b_start, b_end in busy_intervals
        )
        if not overlaps:
            slots.append(cursor)
        cursor += slot_delta
    return slots


async def available_dates(
    db,
    *,
    duration_minutes: int,
    start_offset: int = 1,
    horizon_days: int = 14,
    now: datetime | None = None,
) -> list[date]:
    """Return calendar days in the next ``horizon_days`` that have at least one slot."""
    schedule_rows = await db.get_schedule()
    if not schedule_rows:
        return []
    now = now or datetime.now()
    today = now.date()
    days: list[date] = []
    for offset in range(start_offset, start_offset + horizon_days):
        day = today + timedelta(days=offset)
        info = schedule_for_day(schedule_rows, day)
        if info is None:
            continue
        start, end, break_minutes = info
        busy_rows = await db.list_active_for_day(datetime.combine(day, time.min))
        busy = [
            (datetime.fromisoformat(r["datetime"].replace(" ", "T")), int(r["service_duration"]))
            for r in busy_rows
        ]
        slots = generate_slots(
            day=day,
            start=start,
            end=end,
            break_minutes=break_minutes,
            duration_minutes=duration_minutes,
            busy=busy,
            now=now,
        )
        if slots:
            days.append(day)
    return days


async def available_slots_for_day(
    db,
    *,
    day: date,
    duration_minutes: int,
    now: datetime | None = None,
    exclude_appointment_id: int | None = None,
) -> list[datetime]:
    schedule_rows = await db.get_schedule()
    info = schedule_for_day(schedule_rows, day)
    if info is None:
        return []
    start, end, break_minutes = info
    busy_rows = await db.list_active_for_day(datetime.combine(day, time.min))
    busy: list[tuple[datetime, int]] = []
    for r in busy_rows:
        if exclude_appointment_id is not None and r["id"] == exclude_appointment_id:
            continue
        busy.append(
            (
                datetime.fromisoformat(r["datetime"].replace(" ", "T")),
                int(r["service_duration"]),
            )
        )
    return generate_slots(
        day=day,
        start=start,
        end=end,
        break_minutes=break_minutes,
        duration_minutes=duration_minutes,
        busy=busy,
        now=now or datetime.now(),
    )
