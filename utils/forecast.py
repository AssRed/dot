"""Monthly earnings forecast for the master."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from keyboards import MONTHS_RU


@dataclass
class Forecast:
    month_label: str
    booked_amount: int
    potential_amount: int
    total_amount: int
    avg_check: int
    avg_daily_bookings: float
    remaining_workdays: int

    def render(self) -> str:
        return (
            f"📊 Прогноз дохода на <b>{self.month_label}</b>: "
            f"<b>{self.total_amount:,} ₽</b>\n"
            f"• Уже забронировано: {self.booked_amount:,} ₽\n"
            f"• Потенциально свободно: {self.potential_amount:,} ₽\n"
            f"  (исходя из средних показателей: "
            f"{self.avg_daily_bookings:.1f} запис./день, "
            f"{self.remaining_workdays} рабоч. дней до конца месяца, "
            f"средний чек {self.avg_check:,} ₽)\n\n"
            "💡 Чтобы увеличить прогноз, заполните свободные слоты — "
            "предложите клиентам акцию."
        ).replace(",", " ")


def _month_bounds(today: date) -> tuple[datetime, datetime]:
    start = datetime(today.year, today.month, 1)
    last_day = monthrange(today.year, today.month)[1]
    end = datetime(today.year, today.month, last_day, 23, 59, 59)
    return start, end


def _remaining_workdays(today: date, schedule_rows) -> int:
    """Count remaining working days in the current month based on schedule."""
    last_day = monthrange(today.year, today.month)[1]
    workdays = {row["day_of_week"] for row in schedule_rows}
    if not workdays:
        # Fallback: assume Mon-Fri
        workdays = {0, 1, 2, 3, 4}
    count = 0
    for day in range(today.day, last_day + 1):
        d = date(today.year, today.month, day)
        if d.weekday() in workdays:
            count += 1
    return count


async def build_forecast(db) -> Forecast:
    today = date.today()
    start, end = _month_bounds(today)

    # 1. Sum of active appointments in current month.
    active_rows = await db.appointments_in_period(start, end, status="active")
    booked_amount = sum(int(r["service_price"]) for r in active_rows)

    # 2. Remaining workdays.
    schedule_rows = await db.get_schedule()
    remaining = _remaining_workdays(today, schedule_rows)

    # 3. Average daily bookings — last 30 days, completed.
    period_end = datetime.now()
    period_start = period_end - timedelta(days=30)
    completed = await db.appointments_in_period(period_start, period_end, status="completed")
    if completed:
        avg_daily = len(completed) / 30
    else:
        raw = await db.get_setting("average_daily_bookings", "3")
        try:
            avg_daily = float(raw or "3")
        except ValueError:
            avg_daily = 3.0

    # 4. Average check.
    if completed:
        amounts = [
            int(r["actual_amount"]) if r["actual_amount"] is not None else int(r["service_price"])
            for r in completed
        ]
        avg_check = round(sum(amounts) / len(amounts)) if amounts else 0
    else:
        services = await db.list_services()
        if services:
            avg_check = round(sum(int(s["price"]) for s in services) / len(services))
        else:
            avg_check = 0

    potential = round(remaining * avg_daily * avg_check)
    total = booked_amount + potential

    return Forecast(
        month_label=f"{MONTHS_RU[today.month - 1]} {today.year}",
        booked_amount=booked_amount,
        potential_amount=potential,
        total_amount=total,
        avg_check=avg_check,
        avg_daily_bookings=avg_daily,
        remaining_workdays=remaining,
    )
