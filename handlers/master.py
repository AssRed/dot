"""Master-only handlers (admin commands)."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Message,
)

import keyboards as kb
from config import Settings
from database import Database, to_dt
from utils.forecast import build_forecast
from utils.report import build_excel_report, summarise

logger = logging.getLogger(__name__)
router = Router(name="master")


# Master-only filter: applied per handler via a custom check.
def _is_master(message_or_call: Message | CallbackQuery, settings: Settings) -> bool:
    user = message_or_call.from_user
    return bool(user and user.id == settings.master_tg_id)


# -- FSM groups -----------------------------------------------------------


class AddServiceFlow(StatesGroup):
    name = State()
    price = State()
    duration = State()
    description = State()


class EditServiceFlow(StatesGroup):
    field = State()
    value = State()


class ScheduleFlow(StatesGroup):
    main = State()
    waiting_time = State()


class CompleteFlow(StatesGroup):
    waiting_amount = State()


class IncomeFlow(StatesGroup):
    amount = State()
    category = State()
    comment = State()


class ExpenseFlow(StatesGroup):
    amount = State()
    category = State()
    comment = State()
    photo = State()


class ReportFlow(StatesGroup):
    custom_start = State()
    custom_end = State()


class MasterInfoFlow(StatesGroup):
    text = State()
    photo = State()


class WelcomeFlow(StatesGroup):
    waiting_text = State()


# -- helpers --------------------------------------------------------------


async def _ensure_master(
    obj: Message | CallbackQuery, settings: Settings
) -> bool:
    if _is_master(obj, settings):
        return True
    if isinstance(obj, CallbackQuery):
        await obj.answer("Эта функция доступна только мастеру.", show_alert=True)
    else:
        await obj.answer("Эта команда доступна только мастеру 🌸")
    return False


def _format_dt(dt: datetime) -> str:
    return f"{dt.strftime('%d.%m.%Y')} в {dt.strftime('%H:%M')}"


# -- /list_services & /add_service & /edit_service & /delete_service ------


@router.message(Command("list_services"))
async def list_services_cmd(
    message: Message, db: Database, settings: Settings
) -> None:
    if not await _ensure_master(message, settings):
        return
    services = await db.list_services()
    if not services:
        await message.answer("Услуг пока нет. Добавьте через /add_service.")
        return
    lines = ["💅 <b>Услуги:</b>\n"]
    for s in services:
        lines.append(
            f"#{s['id']}. <b>{s['name']}</b> — {s['price']}₽ · "
            f"{s['duration_minutes']} мин"
        )
    lines.append(
        "\nКоманды: /add_service · /edit_service &lt;id&gt; · /delete_service &lt;id&gt;"
    )
    await message.answer("\n".join(lines))


@router.message(Command("add_service"))
async def add_service_start(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    if not await _ensure_master(message, settings):
        return
    await state.set_state(AddServiceFlow.name)
    await message.answer(
        "Добавим новую услугу. Как она называется?\n"
        "Можно отменить в любой момент: /cancel"
    )


@router.message(Command("cancel"))
async def cancel_any(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _ensure_master(message, settings):
        return
    if await state.get_state() is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("Отменил текущее действие.")


@router.message(AddServiceFlow.name)
async def add_service_name(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Пришлите название услуги текстом.")
        return
    await state.update_data(name=message.text.strip()[:100])
    await state.set_state(AddServiceFlow.price)
    await message.answer("Сколько стоит услуга? Введите число в рублях (только цифры).")


@router.message(AddServiceFlow.price)
async def add_service_price(message: Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Цена должна быть целым числом, например 2500.")
        return
    await state.update_data(price=int(message.text.strip()))
    await state.set_state(AddServiceFlow.duration)
    await message.answer("Сколько минут занимает процедура?")


@router.message(AddServiceFlow.duration)
async def add_service_duration(message: Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Длительность — целое число минут, например 60.")
        return
    duration = int(message.text.strip())
    if duration <= 0 or duration > 600:
        await message.answer("Длительность должна быть от 1 до 600 минут.")
        return
    await state.update_data(duration=duration)
    await state.set_state(AddServiceFlow.description)
    await message.answer(
        "Опишите услугу: что входит, как готовиться, рекомендации после. "
        "Этот текст увидят клиенты."
    )


@router.message(AddServiceFlow.description)
async def add_service_description(
    message: Message, state: FSMContext, db: Database
) -> None:
    if not message.text:
        await message.answer("Пришлите описание текстом.")
        return
    data = await state.get_data()
    sid = await db.add_service(
        name=data["name"],
        price=int(data["price"]),
        duration_minutes=int(data["duration"]),
        description=message.text.strip(),
    )
    await state.clear()
    await message.answer(f"Услуга добавлена под номером #{sid}.")
    services = await db.list_services()
    lines = ["Текущий список:"]
    for s in services:
        lines.append(f"#{s['id']}. {s['name']} — {s['price']}₽")
    await message.answer("\n".join(lines))


@router.message(Command("delete_service"))
async def delete_service(
    message: Message, db: Database, settings: Settings
) -> None:
    if not await _ensure_master(message, settings):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Использование: /delete_service &lt;id&gt;")
        return
    sid = int(parts[1].strip())
    ok = await db.delete_service(sid)
    if ok:
        await message.answer(f"Услуга #{sid} удалена.")
    else:
        await message.answer("Услуга с таким ID не найдена.")


@router.message(Command("edit_service"))
async def edit_service_start(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    if not await _ensure_master(message, settings):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Использование: /edit_service &lt;id&gt;")
        return
    sid = int(parts[1].strip())
    service = await db.get_service(sid)
    if service is None:
        await message.answer("Услуга с таким ID не найдена.")
        return
    await state.set_state(EditServiceFlow.field)
    await state.update_data(service_id=sid)
    await message.answer(
        f"Что изменить в услуге <b>{service['name']}</b>?\n"
        "Введите одно из: name, price, duration, description.\n"
        "Отмена: /cancel"
    )


@router.message(EditServiceFlow.field)
async def edit_service_field(message: Message, state: FSMContext) -> None:
    field = (message.text or "").strip().lower()
    if field not in {"name", "price", "duration", "description"}:
        await message.answer("Можно менять только: name, price, duration, description.")
        return
    await state.update_data(field=field)
    await state.set_state(EditServiceFlow.value)
    prompts = {
        "name": "Введите новое название.",
        "price": "Введите новую цену числом.",
        "duration": "Введите новую длительность в минутах.",
        "description": "Введите новое описание.",
    }
    await message.answer(prompts[field])


@router.message(EditServiceFlow.value)
async def edit_service_value(
    message: Message, state: FSMContext, db: Database
) -> None:
    data = await state.get_data()
    field = data["field"]
    sid = int(data["service_id"])
    if not message.text:
        await message.answer("Пришлите значение текстом.")
        return
    raw = message.text.strip()
    if field == "name":
        await db.update_service(sid, name=raw[:100])
    elif field == "price":
        if not raw.isdigit():
            await message.answer("Цена должна быть числом.")
            return
        await db.update_service(sid, price=int(raw))
    elif field == "duration":
        if not raw.isdigit():
            await message.answer("Длительность должна быть числом минут.")
            return
        await db.update_service(sid, duration_minutes=int(raw))
    else:
        await db.update_service(sid, description=raw)
    await state.clear()
    await message.answer("Готово ✅")


# -- /set_schedule --------------------------------------------------------


@router.message(Command("set_schedule"))
async def schedule_start(
    message: Message, state: FSMContext, db: Database, settings: Settings
) -> None:
    if not await _ensure_master(message, settings):
        return
    await state.set_state(ScheduleFlow.main)
    rows = await db.get_schedule()
    summary = _render_schedule(rows)
    await message.answer(
        f"<b>Текущее расписание:</b>\n{summary}\n\n"
        "Выберите день, чтобы изменить его, или нажмите «Готово».",
        reply_markup=kb.weekday_kb(),
    )


def _render_schedule(rows: list) -> str:
    if not rows:
        return "—"
    by_day = {row["day_of_week"]: row for row in rows}
    parts: list[str] = []
    for i, name in enumerate(kb.WEEKDAYS_FULL):
        if i in by_day:
            r = by_day[i]
            extra = f" (перерыв {r['break_minutes']} мин)" if r["break_minutes"] else ""
            parts.append(f"{name}: {r['start_time']}–{r['end_time']}{extra}")
        else:
            parts.append(f"{name}: выходной")
    return "\n".join(parts)


@router.callback_query(F.data == "sched:done")
async def schedule_done(call: CallbackQuery, state: FSMContext) -> None:
    if call.message is None:
        return
    await state.clear()
    await call.message.edit_text("Расписание сохранено ✅")
    await call.answer()


@router.callback_query(F.data == "sched:back")
async def schedule_back(call: CallbackQuery, db: Database) -> None:
    if call.message is None:
        return
    rows = await db.get_schedule()
    summary = _render_schedule(rows)
    await call.message.edit_text(
        f"<b>Текущее расписание:</b>\n{summary}\n\nВыберите день:",
        reply_markup=kb.weekday_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("sched:day:"))
async def schedule_pick_day(call: CallbackQuery, state: FSMContext) -> None:
    if call.message is None or call.data is None:
        return
    day_index = int(call.data.split(":")[2])
    await state.update_data(day_index=day_index)
    await call.message.edit_text(
        f"<b>{kb.WEEKDAYS_FULL[day_index]}</b>\nЧто сделать?",
        reply_markup=kb.schedule_action_kb(day_index),
    )
    await call.answer()


@router.callback_query(F.data.startswith("sched:off:"))
async def schedule_set_off(
    call: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if call.message is None or call.data is None:
        return
    day_index = int(call.data.split(":")[2])
    await db.remove_day_schedule(day_index)
    rows = await db.get_schedule()
    summary = _render_schedule(rows)
    await call.message.edit_text(
        f"Сохранили: {kb.WEEKDAYS_FULL[day_index]} — выходной.\n\n"
        f"<b>Расписание:</b>\n{summary}",
        reply_markup=kb.weekday_kb(),
    )
    await state.set_state(ScheduleFlow.main)
    await call.answer()


@router.callback_query(F.data.startswith("sched:set:"))
async def schedule_set_time(
    call: CallbackQuery, state: FSMContext
) -> None:
    if call.message is None or call.data is None:
        return
    day_index = int(call.data.split(":")[2])
    await state.set_state(ScheduleFlow.waiting_time)
    await state.update_data(day_index=day_index)
    await call.message.answer(
        f"Введите время для дня <b>{kb.WEEKDAYS_FULL[day_index]}</b> в формате\n"
        "<code>HH:MM-HH:MM перерыв_мин</code>\n"
        "Например: <code>10:00-19:00 15</code> или <code>09:00-18:00</code>"
    )
    await call.answer()


@router.message(ScheduleFlow.waiting_time)
async def schedule_save_time(
    message: Message, state: FSMContext, db: Database
) -> None:
    raw = (message.text or "").strip()
    parts = raw.split()
    if not parts:
        await message.answer("Не понял формат. Пример: 10:00-19:00 15")
        return
    range_part = parts[0]
    break_minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if "-" not in range_part:
        await message.answer("Время должно быть в формате HH:MM-HH:MM.")
        return
    start_s, end_s = range_part.split("-", 1)
    if not _valid_hhmm(start_s) or not _valid_hhmm(end_s):
        await message.answer("Неверное время. Пример: 10:00-19:00")
        return
    if start_s >= end_s:
        await message.answer("Время начала должно быть раньше окончания.")
        return
    data = await state.get_data()
    day_index = int(data["day_index"])
    await db.upsert_schedule(day_index, start_s, end_s, break_minutes)
    rows = await db.get_schedule()
    summary = _render_schedule(rows)
    await state.set_state(ScheduleFlow.main)
    await message.answer(
        f"Сохранили {kb.WEEKDAYS_FULL[day_index]}: {start_s}–{end_s}.\n\n"
        f"<b>Расписание:</b>\n{summary}",
        reply_markup=kb.weekday_kb(),
    )


def _valid_hhmm(value: str) -> bool:
    if len(value) != 5 or value[2] != ":":
        return False
    try:
        h = int(value[:2])
        m = int(value[3:])
    except ValueError:
        return False
    return 0 <= h < 24 and 0 <= m < 60


# -- /appointments --------------------------------------------------------


@router.message(Command("appointments"))
async def appointments_cmd(
    message: Message, db: Database, settings: Settings
) -> None:
    if not await _ensure_master(message, settings):
        return
    rows = await db.list_upcoming()
    if not rows:
        await message.answer("Активных записей нет.")
        return
    await message.answer(f"📅 Предстоящие записи: {len(rows)}")
    for row in rows:
        appt_dt = to_dt(row["datetime"])
        if appt_dt is None:
            continue
        text = (
            f"#{row['id']} · <b>{row['service_name']}</b>\n"
            f"📅 {_format_dt(appt_dt)}\n"
            f"👤 {row['client_name']} · {row['client_phone']}\n"
            f"💰 {row['service_price']}₽"
        )
        await message.answer(text, reply_markup=kb.master_appt_kb(int(row["id"])))


@router.callback_query(F.data.startswith("mappt:cancel:"))
async def master_cancel(
    call: CallbackQuery, db: Database, bot: Bot, settings: Settings
) -> None:
    if not await _ensure_master(call, settings):
        return
    if call.message is None or call.data is None:
        return
    appt_id = int(call.data.split(":")[2])
    appt = await db.get_appointment(appt_id)
    if appt is None:
        await call.answer("Запись не найдена", show_alert=True)
        return
    ok = await db.cancel_appointment(appt_id, "cancelled by master")
    if not ok:
        await call.answer("Не удалось отменить", show_alert=True)
        return
    appt_dt = to_dt(appt["datetime"])
    when = _format_dt(appt_dt) if appt_dt else "—"
    try:
        await bot.send_message(
            int(appt["client_tg_id"]),
            "Мастер был вынужден отменить вашу запись на "
            f"<b>{appt['service_name']}</b> ({when}). "
            "Простите, пожалуйста, за неудобства 🌸 "
            "Если хотите, можно сразу выбрать другое время через бота.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to notify client about cancel: %s", exc)
    await call.message.edit_text(
        f"Запись #{appt_id} отменена. Клиенту отправлено уведомление."
    )
    await call.answer()


@router.callback_query(F.data.startswith("mappt:complete:"))
async def master_complete_inline(
    call: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    if not await _ensure_master(call, settings):
        return
    if call.message is None or call.data is None:
        return
    appt_id = int(call.data.split(":")[2])
    await _start_complete(call.message, state, db, appt_id)
    await call.answer()


@router.message(Command("complete"))
async def complete_cmd(
    message: Message, state: FSMContext, db: Database, settings: Settings
) -> None:
    if not await _ensure_master(message, settings):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Использование: /complete &lt;appointment_id&gt;")
        return
    appt_id = int(parts[1].strip())
    await _start_complete(message, state, db, appt_id)


async def _start_complete(
    message: Message, state: FSMContext, db: Database, appt_id: int
) -> None:
    appt = await db.get_appointment(appt_id)
    if appt is None:
        await message.answer("Запись не найдена.")
        return
    if appt["status"] != "active":
        await message.answer("Эту запись уже нельзя завершить (статус не active).")
        return
    await state.set_state(CompleteFlow.waiting_amount)
    await state.update_data(appt_id=appt_id, default_amount=int(appt["service_price"]))
    await message.answer(
        f"Запись #{appt_id}: <b>{appt['service_name']}</b>\n"
        f"Цена по прайсу: {appt['service_price']}₽\n\n"
        "Отправьте фактическую сумму числом или «-», чтобы взять цену из прайса."
    )


@router.message(CompleteFlow.waiting_amount)
async def complete_amount(
    message: Message, state: FSMContext, db: Database
) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip()
    if raw == "-" or raw == "":
        amount = int(data["default_amount"])
    else:
        if not raw.isdigit():
            await message.answer("Введите сумму числом или «-».")
            return
        amount = int(raw)
    appt_id = int(data["appt_id"])
    appt = await db.get_appointment(appt_id)
    if appt is None:
        await state.clear()
        await message.answer("Запись пропала.")
        return
    ok = await db.complete_appointment(appt_id, amount)
    if not ok:
        await state.clear()
        await message.answer("Не удалось пометить запись как завершённую.")
        return
    await db.add_transaction(
        user_id=int(appt["client_tg_id"]),
        ttype="income",
        amount=amount,
        category="service",
        comment=f"appointment #{appt_id} ({appt['service_name']})",
        appointment_id=appt_id,
    )
    await state.clear()
    await message.answer(
        f"Готово ✅ Запись #{appt_id} завершена, доход {amount}₽ записан."
    )


# -- /finance -------------------------------------------------------------


@router.message(Command("finance"))
async def finance_menu(
    message: Message, settings: Settings
) -> None:
    if not await _ensure_master(message, settings):
        return
    await message.answer(
        "💼 <b>Финансы</b>\nВыберите действие:",
        reply_markup=kb.finance_menu_kb(),
    )


@router.callback_query(F.data == "fin:income")
async def finance_income(
    call: CallbackQuery, state: FSMContext, settings: Settings
) -> None:
    if not await _ensure_master(call, settings):
        return
    if call.message is None:
        return
    await state.set_state(IncomeFlow.amount)
    await call.message.answer("➕ Доход. Введите сумму в рублях.")
    await call.answer()


@router.message(IncomeFlow.amount)
async def income_amount(message: Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Сумма должна быть целым числом.")
        return
    await state.update_data(amount=int(message.text.strip()))
    await state.set_state(IncomeFlow.category)
    await message.answer("Категория? (например: услуги, чаевые)")


@router.message(IncomeFlow.category)
async def income_category(message: Message, state: FSMContext) -> None:
    await state.update_data(category=(message.text or "").strip()[:64] or "income")
    await state.set_state(IncomeFlow.comment)
    await message.answer("Комментарий (или «-» чтобы пропустить).")


@router.message(IncomeFlow.comment)
async def income_comment(
    message: Message, state: FSMContext, db: Database, settings: Settings
) -> None:
    raw = (message.text or "").strip()
    comment = "" if raw in {"-", ""} else raw
    data = await state.get_data()
    await db.add_transaction(
        user_id=settings.master_tg_id,
        ttype="income",
        amount=int(data["amount"]),
        category=data["category"],
        comment=comment,
    )
    await state.clear()
    await message.answer("Доход добавлен ✅")


@router.callback_query(F.data == "fin:expense")
async def finance_expense(
    call: CallbackQuery, state: FSMContext, settings: Settings
) -> None:
    if not await _ensure_master(call, settings):
        return
    if call.message is None:
        return
    await state.set_state(ExpenseFlow.amount)
    await call.message.answer("➖ Расход. Введите сумму в рублях.")
    await call.answer()


@router.message(ExpenseFlow.amount)
async def expense_amount(message: Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Сумма должна быть целым числом.")
        return
    await state.update_data(amount=int(message.text.strip()))
    await state.set_state(ExpenseFlow.category)
    await message.answer("Категория? (например: материалы, аренда)")


@router.message(ExpenseFlow.category)
async def expense_category(message: Message, state: FSMContext) -> None:
    await state.update_data(category=(message.text or "").strip()[:64] or "expense")
    await state.set_state(ExpenseFlow.comment)
    await message.answer("Комментарий (или «-» чтобы пропустить).")


@router.message(ExpenseFlow.comment)
async def expense_comment(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    comment = "" if raw in {"-", ""} else raw
    await state.update_data(comment=comment)
    await state.set_state(ExpenseFlow.photo)
    await message.answer(
        "Можно приложить фото чека (одно фото) или нажмите «Без фото».",
        reply_markup=kb.expense_photo_kb(),
    )


@router.callback_query(ExpenseFlow.photo, F.data == "fin:exp:nophoto")
async def expense_no_photo(
    call: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    if call.message is None:
        return
    data = await state.get_data()
    await db.add_transaction(
        user_id=settings.master_tg_id,
        ttype="expense",
        amount=int(data["amount"]),
        category=data["category"],
        comment=data["comment"],
    )
    await state.clear()
    await call.message.answer("Расход добавлен ✅")
    await call.answer()


@router.message(ExpenseFlow.photo, F.photo)
async def expense_with_photo(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    photo = message.photo[-1] if message.photo else None
    if photo is None:
        await message.answer("Пришлите изображение или нажмите «Без фото».")
        return
    data = await state.get_data()
    await db.add_transaction(
        user_id=settings.master_tg_id,
        ttype="expense",
        amount=int(data["amount"]),
        category=data["category"],
        comment=data["comment"],
        photo_file_id=photo.file_id,
    )
    await state.clear()
    await message.answer("Расход добавлен с фото ✅")


@router.message(ExpenseFlow.photo)
async def expense_photo_other(message: Message) -> None:
    await message.answer(
        "Жду фото или кнопку «Без фото».", reply_markup=kb.expense_photo_kb()
    )


# -- finance reports ------------------------------------------------------


def _period_for_label(label: str, *, today: date | None = None) -> tuple[datetime, datetime, str]:
    today = today or date.today()
    if label == "day":
        start = datetime(today.year, today.month, today.day)
        end = start + timedelta(days=1)
        return start, end, "сегодня"
    if label == "week":
        start_d = today - timedelta(days=today.weekday())
        start = datetime(start_d.year, start_d.month, start_d.day)
        end = start + timedelta(days=7)
        return start, end, "текущую неделю"
    if label == "month":
        start = datetime(today.year, today.month, 1)
        if today.month == 12:
            end = datetime(today.year + 1, 1, 1)
        else:
            end = datetime(today.year, today.month + 1, 1)
        return start, end, "текущий месяц"
    if label == "quarter":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        start = datetime(today.year, q_start_month, 1)
        end_month = q_start_month + 3
        end_year = today.year + (1 if end_month > 12 else 0)
        end_month = end_month if end_month <= 12 else end_month - 12
        end = datetime(end_year, end_month, 1)
        return start, end, "текущий квартал"
    raise ValueError(f"Unknown period label {label!r}")


async def _send_report(
    *,
    message: Message,
    db: Database,
    state: FSMContext,
    period_start: datetime,
    period_end: datetime,
    label: str,
) -> None:
    transactions = await db.transactions_in_period(period_start, period_end)
    tax_rate_raw = await db.get_setting("tax_rate", "6")
    try:
        tax_rate = int(tax_rate_raw or "6")
    except ValueError:
        tax_rate = 6
    summary = summarise(transactions, tax_rate=tax_rate)
    await state.update_data(
        report_start=period_start.isoformat(),
        report_end=period_end.isoformat(),
        report_label=label,
    )
    await message.answer(
        summary.render(label) + "\n\nХотите выгрузить в Excel?",
        reply_markup=kb.report_export_kb(),
    )


@router.callback_query(F.data == "fin:report")
async def finance_report(
    call: CallbackQuery, settings: Settings
) -> None:
    if not await _ensure_master(call, settings):
        return
    if call.message is None:
        return
    await call.message.answer(
        "За какой период?", reply_markup=kb.report_period_kb()
    )
    await call.answer()


@router.callback_query(F.data.startswith("fin:rep:"))
async def finance_report_period(
    call: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    if not await _ensure_master(call, settings):
        return
    if call.message is None or call.data is None:
        return
    label = call.data.split(":")[2]
    if label == "custom":
        await state.set_state(ReportFlow.custom_start)
        await call.message.answer("Введите дату начала в формате YYYY-MM-DD.")
        await call.answer()
        return
    start, end, label_text = _period_for_label(label)
    await _send_report(
        message=call.message, db=db, state=state,
        period_start=start, period_end=end, label=label_text,
    )
    await call.answer()


@router.message(ReportFlow.custom_start)
async def report_custom_start(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        start = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        await message.answer("Формат: YYYY-MM-DD. Попробуйте ещё раз.")
        return
    await state.update_data(custom_start=start.isoformat())
    await state.set_state(ReportFlow.custom_end)
    await message.answer("Введите дату окончания в формате YYYY-MM-DD.")


@router.message(ReportFlow.custom_end)
async def report_custom_end(
    message: Message, state: FSMContext, db: Database
) -> None:
    raw = (message.text or "").strip()
    try:
        end = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        await message.answer("Формат: YYYY-MM-DD. Попробуйте ещё раз.")
        return
    end = end + timedelta(days=1)  # include the entire end day
    data = await state.get_data()
    start = datetime.fromisoformat(data["custom_start"])
    if start >= end:
        await message.answer("Дата начала должна быть раньше даты окончания.")
        return
    label = f"{start.strftime('%d.%m.%Y')} – {(end - timedelta(days=1)).strftime('%d.%m.%Y')}"
    await _send_report(
        message=message, db=db, state=state,
        period_start=start, period_end=end, label=label,
    )


@router.callback_query(F.data == "fin:export")
async def finance_export(
    call: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    if not await _ensure_master(call, settings):
        return
    if call.message is None:
        return
    data = await state.get_data()
    if "report_start" not in data:
        await call.answer("Сначала выберите период отчёта.", show_alert=True)
        return
    start = datetime.fromisoformat(data["report_start"])
    end = datetime.fromisoformat(data["report_end"])
    label = data["report_label"]
    transactions = await db.transactions_in_period(start, end)
    tax_rate_raw = await db.get_setting("tax_rate", "6")
    try:
        tax_rate = int(tax_rate_raw or "6")
    except ValueError:
        tax_rate = 6
    summary = summarise(transactions, tax_rate=tax_rate)
    payload = build_excel_report(transactions, period_label=label, summary=summary)
    filename = f"report_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.xlsx"
    await call.message.answer_document(
        BufferedInputFile(payload, filename=filename),
        caption=f"Отчёт за {label}",
    )
    await call.answer()


@router.callback_query(F.data == "fin:forecast")
async def finance_forecast(
    call: CallbackQuery, db: Database, settings: Settings
) -> None:
    if not await _ensure_master(call, settings):
        return
    if call.message is None:
        return
    forecast = await build_forecast(db)
    await call.message.answer(forecast.render())
    await call.answer()


# -- /set_master_info -----------------------------------------------------


@router.message(Command("set_master_info"))
async def set_master_info_start(
    message: Message, state: FSMContext, settings: Settings
) -> None:
    if not await _ensure_master(message, settings):
        return
    await state.set_state(MasterInfoFlow.text)
    await message.answer(
        "Пришлите текст для раздела «О мастере». "
        "Опишите квалификацию, любимые процедуры, философию работы. "
        "Отмена: /cancel"
    )


@router.message(MasterInfoFlow.text)
async def set_master_info_text(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Пришлите текст сообщением.")
        return
    await state.update_data(text=message.text)
    await state.set_state(MasterInfoFlow.photo)
    await message.answer(
        "Теперь можно прислать фото (одно). "
        "Если фото не нужно — отправьте «-»."
    )


@router.message(MasterInfoFlow.photo, F.photo)
async def set_master_info_photo(
    message: Message, state: FSMContext, db: Database
) -> None:
    data = await state.get_data()
    photo = message.photo[-1] if message.photo else None
    await db.set_master_info(data["text"], photo.file_id if photo else None)
    await state.clear()
    await message.answer("Информация о мастере обновлена ✅")


@router.message(MasterInfoFlow.photo)
async def set_master_info_no_photo(
    message: Message, state: FSMContext, db: Database
) -> None:
    raw = (message.text or "").strip()
    if raw != "-":
        await message.answer("Пришлите фото или «-», чтобы пропустить.")
        return
    data = await state.get_data()
    await db.set_master_info(data["text"], None)
    await state.clear()
    await message.answer("Информация о мастере обновлена ✅")


# -- /set_tax_rate --------------------------------------------------------


@router.message(Command("set_tax_rate"))
async def set_tax_rate(
    message: Message, db: Database, settings: Settings
) -> None:
    if not await _ensure_master(message, settings):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip() not in {"4", "6"}:
        await message.answer("Использование: /set_tax_rate 4 или /set_tax_rate 6")
        return
    await db.set_setting("tax_rate", parts[1].strip())
    await message.answer(f"Ставка налога сохранена: {parts[1].strip()}%")


# -- /set_welcome_text ----------------------------------------------------


@router.message(Command("set_welcome_text"))
async def set_welcome_text(
    message: Message, settings: Settings
) -> None:
    if not await _ensure_master(message, settings):
        return
    await message.answer(
        "Какое приветствие меняем?", reply_markup=kb.welcome_type_kb()
    )


@router.callback_query(F.data.startswith("welcome:"))
async def welcome_pick(
    call: CallbackQuery, state: FSMContext, settings: Settings
) -> None:
    if not await _ensure_master(call, settings):
        return
    if call.message is None or call.data is None:
        return
    type_ = call.data.split(":")[1]
    if type_ not in {"new", "returning"}:
        await call.answer()
        return
    await state.set_state(WelcomeFlow.waiting_text)
    await state.update_data(welcome_type=type_)
    label = "новых" if type_ == "new" else "постоянных"
    await call.message.answer(
        f"Пришлите новый текст приветствия для <b>{label}</b> клиентов. /cancel чтобы отменить."
    )
    await call.answer()


@router.message(WelcomeFlow.waiting_text)
async def welcome_save(
    message: Message, state: FSMContext, db: Database
) -> None:
    if not message.text:
        await message.answer("Пришлите текст сообщением.")
        return
    data = await state.get_data()
    await db.set_welcome_text(data["welcome_type"], message.text)
    await state.clear()
    await message.answer("Приветствие обновлено ✅")
