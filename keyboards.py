"""Inline / reply keyboards used across the bot."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

WEEKDAYS_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
WEEKDAYS_FULL = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)
MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _row(*buttons: InlineKeyboardButton) -> list[InlineKeyboardButton]:
    return list(buttons)


# ---------- main client menu ------------------------------------------------


def main_menu_kb(*, has_active: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        _row(InlineKeyboardButton(text="📅 Записаться", callback_data="book:start")),
        _row(InlineKeyboardButton(text="🗒 Мои записи", callback_data="my:list")),
    ]
    if has_active:
        rows.append(
            _row(
                InlineKeyboardButton(
                    text="❌ Отменить/перенести запись",
                    callback_data="my:manage",
                )
            )
        )
    rows.extend(
        [
            _row(InlineKeyboardButton(text="💅 Услуги и цены", callback_data="services:list")),
            _row(InlineKeyboardButton(text="✨ О мастере", callback_data="master:about")),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- services --------------------------------------------------------


def services_list_kb(services: Iterable) -> InlineKeyboardMarkup:
    rows = [
        _row(
            InlineKeyboardButton(
                text=f"{s['name']} — {s['price']}₽",
                callback_data=f"service:view:{s['id']}",
            )
        )
        for s in services
    ]
    rows.append(_row(InlineKeyboardButton(text="« Назад", callback_data="menu:main")))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_view_kb(service_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(
                InlineKeyboardButton(
                    text="📅 Записаться на эту услугу",
                    callback_data=f"book:service:{service_id}",
                )
            ),
            _row(
                InlineKeyboardButton(text="« К списку услуг", callback_data="services:list"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"),
            ),
        ]
    )


# ---------- booking flow ----------------------------------------------------


def book_services_kb(services: Iterable) -> InlineKeyboardMarkup:
    rows = [
        _row(
            InlineKeyboardButton(
                text=f"{s['name']} — {s['price']}₽ · {s['duration_minutes']} мин",
                callback_data=f"book:service:{s['id']}",
            )
        )
        for s in services
    ]
    rows.append(_row(InlineKeyboardButton(text="« Меню", callback_data="menu:main")))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_service_kb(service_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(
                InlineKeyboardButton(
                    text="✅ Да, выбрать дату",
                    callback_data=f"book:dates:{service_id}",
                ),
                InlineKeyboardButton(text="🚫 Нет", callback_data="menu:main"),
            )
        ]
    )


def format_date_short(d: date) -> str:
    return f"{WEEKDAYS_SHORT[d.weekday()]} {d.day} {MONTHS_RU[d.month - 1]}"


def format_date_full(d: date) -> str:
    return f"{WEEKDAYS_FULL[d.weekday()]}, {d.day} {MONTHS_RU[d.month - 1]} {d.year}"


def dates_kb(
    available_days: list[date],
    *,
    service_id: int,
    purpose: str = "book",
    extra_back_cb: str = "menu:main",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for d in available_days:
        pair.append(
            InlineKeyboardButton(
                text=format_date_short(d),
                callback_data=f"{purpose}:slots:{service_id}:{d.isoformat()}",
            )
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append(_row(InlineKeyboardButton(text="« Назад", callback_data=extra_back_cb)))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slots_kb(
    slots: list[datetime],
    *,
    service_id: int,
    day: date,
    purpose: str = "book",
    back_cb: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    triplet: list[InlineKeyboardButton] = []
    for slot in slots:
        triplet.append(
            InlineKeyboardButton(
                text=slot.strftime("%H:%M"),
                callback_data=f"{purpose}:pick:{service_id}:{slot.strftime('%Y-%m-%dT%H:%M')}",
            )
        )
        if len(triplet) == 3:
            rows.append(triplet)
            triplet = []
    if triplet:
        rows.append(triplet)
    rows.append(
        _row(
            InlineKeyboardButton(
                text="« К датам",
                callback_data=back_cb or f"{purpose}:dates:{service_id}",
            )
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_booking_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="book:confirm"),
                InlineKeyboardButton(text="🚫 Отменить", callback_data="book:abort"),
            )
        ]
    )


def use_saved_name_kb(saved_name: str | None) -> InlineKeyboardMarkup | None:
    if not saved_name:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(
                InlineKeyboardButton(
                    text=f"Оставить «{saved_name}»",
                    callback_data="book:name:keep",
                )
            )
        ]
    )


def share_phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def remove_reply_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


# ---------- my appointments -------------------------------------------------


def my_appt_kb(appt_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(
                InlineKeyboardButton(
                    text="❌ Отменить", callback_data=f"appt:cancel:{appt_id}"
                ),
                InlineKeyboardButton(
                    text="🔁 Перенести", callback_data=f"appt:reschedule:{appt_id}"
                ),
            ),
            _row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")),
        ]
    )


def cancel_reason_skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(InlineKeyboardButton(text="Без причины", callback_data="appt:reason:skip"))
        ]
    )


# ---------- master finance --------------------------------------------------


def finance_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(InlineKeyboardButton(text="➕ Доход", callback_data="fin:income")),
            _row(InlineKeyboardButton(text="➖ Расход", callback_data="fin:expense")),
            _row(InlineKeyboardButton(text="📊 Отчёт за период", callback_data="fin:report")),
            _row(
                InlineKeyboardButton(
                    text="🔮 Прогноз на месяц", callback_data="fin:forecast"
                )
            ),
        ]
    )


def report_period_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(
                InlineKeyboardButton(text="День", callback_data="fin:rep:day"),
                InlineKeyboardButton(text="Неделя", callback_data="fin:rep:week"),
            ),
            _row(
                InlineKeyboardButton(text="Месяц", callback_data="fin:rep:month"),
                InlineKeyboardButton(text="Квартал", callback_data="fin:rep:quarter"),
            ),
            _row(
                InlineKeyboardButton(text="Произвольный", callback_data="fin:rep:custom"),
            ),
        ]
    )


def report_export_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(InlineKeyboardButton(text="📥 Экспорт в Excel", callback_data="fin:export"))
        ]
    )


def expense_photo_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(InlineKeyboardButton(text="Без фото", callback_data="fin:exp:nophoto"))
        ]
    )


# ---------- master schedule -------------------------------------------------


def weekday_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for i, name in enumerate(WEEKDAYS_FULL):
        pair.append(
            InlineKeyboardButton(text=name, callback_data=f"sched:day:{i}")
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append(
        _row(
            InlineKeyboardButton(text="✅ Готово", callback_data="sched:done"),
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_action_kb(day_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(
                InlineKeyboardButton(
                    text="Задать время", callback_data=f"sched:set:{day_index}"
                ),
                InlineKeyboardButton(
                    text="Выходной", callback_data=f"sched:off:{day_index}"
                ),
            ),
            _row(InlineKeyboardButton(text="« Назад", callback_data="sched:back")),
        ]
    )


# ---------- master appointments --------------------------------------------


def master_appt_kb(appt_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(
                InlineKeyboardButton(
                    text="✅ Завершить", callback_data=f"mappt:complete:{appt_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отменить", callback_data=f"mappt:cancel:{appt_id}"
                ),
            )
        ]
    )


# ---------- welcome editor --------------------------------------------------


def welcome_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _row(
                InlineKeyboardButton(text="Новые клиенты", callback_data="welcome:new"),
                InlineKeyboardButton(
                    text="Постоянные клиенты", callback_data="welcome:returning"
                ),
            )
        ]
    )


# ---------- helpers ---------------------------------------------------------


def upcoming_dates(days: int = 14, *, start_offset: int = 1) -> list[date]:
    today = date.today()
    return [today + timedelta(days=start_offset + i) for i in range(days)]
