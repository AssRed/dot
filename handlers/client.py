"""Client-facing handlers: booking flow, cancellation, rescheduling, info."""
from __future__ import annotations

import logging
from datetime import date, datetime

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import keyboards as kb
from config import Settings
from database import Database, to_dt
from utils.slots import available_dates, available_slots_for_day

logger = logging.getLogger(__name__)
router = Router(name="client")


# -- FSM -------------------------------------------------------------------


class BookingFlow(StatesGroup):
    confirming_service = State()
    choosing_date = State()
    choosing_slot = State()
    entering_name = State()
    entering_phone = State()
    confirming = State()


class CancelFlow(StatesGroup):
    waiting_reason = State()


class RescheduleFlow(StatesGroup):
    choosing_date = State()
    choosing_slot = State()


# -- helpers ---------------------------------------------------------------


def _format_dt(dt: datetime) -> str:
    return f"{dt.strftime('%d.%m.%Y')} в {dt.strftime('%H:%M')}"


async def _notify_master(
    bot: Bot,
    settings: Settings,
    text: str,
) -> None:
    try:
        await bot.send_message(settings.master_tg_id, text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to notify master: %s", exc)


# -- services list / view --------------------------------------------------


@router.callback_query(F.data == "services:list")
async def services_list(call: CallbackQuery, db: Database) -> None:
    if call.message is None:
        return
    services = await db.list_services()
    if not services:
        await call.message.edit_text(
            "Услуги пока не добавлены. Загляните чуть позже 🌸",
            reply_markup=kb.main_menu_kb(),
        )
        await call.answer()
        return
    lines = ["💅 <b>Услуги и цены</b>\n"]
    for s in services:
        lines.append(
            f"• <b>{s['name']}</b> — {s['price']}₽ · {s['duration_minutes']} мин"
        )
    lines.append("\nНажмите на услугу, чтобы увидеть подробности.")
    await call.message.edit_text(
        "\n".join(lines), reply_markup=kb.services_list_kb(services)
    )
    await call.answer()


@router.callback_query(F.data.startswith("service:view:"))
async def service_view(call: CallbackQuery, db: Database) -> None:
    if call.message is None or call.data is None:
        return
    service_id = int(call.data.split(":")[2])
    service = await db.get_service(service_id)
    if service is None:
        await call.answer("Услуга не найдена", show_alert=True)
        return
    description = service["description"] or "Подробности появятся скоро."
    text = (
        f"<b>{service['name']}</b>\n"
        f"💰 Цена: {service['price']}₽\n"
        f"⏱ Длительность: {service['duration_minutes']} мин\n\n"
        f"{description}"
    )
    await call.message.edit_text(text, reply_markup=kb.service_view_kb(service_id))
    await call.answer()


# -- booking flow ----------------------------------------------------------


@router.callback_query(F.data == "book:start")
async def book_start(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    if call.message is None:
        return
    await state.clear()
    services = await db.list_services()
    if not services:
        await call.message.edit_text(
            "К сожалению, услуги пока не добавлены. Загляните позже 🌸",
            reply_markup=kb.main_menu_kb(),
        )
        await call.answer()
        return
    await call.message.edit_text(
        "Выберите услугу для записи:", reply_markup=kb.book_services_kb(services)
    )
    await call.answer()


@router.callback_query(F.data.startswith("book:service:"))
async def book_pick_service(
    call: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if call.message is None or call.data is None:
        return
    service_id = int(call.data.split(":")[2])
    service = await db.get_service(service_id)
    if service is None:
        await call.answer("Услуга не найдена", show_alert=True)
        return
    await state.set_state(BookingFlow.confirming_service)
    await state.update_data(service_id=service_id)
    description = service["description"] or "Все детали уточнит мастер при встрече."
    text = (
        f"<b>{service['name']}</b>\n"
        f"💰 {service['price']}₽ · ⏱ {service['duration_minutes']} мин\n\n"
        f"{description}\n\n"
        "Записаться на эту услугу?"
    )
    await call.message.edit_text(text, reply_markup=kb.confirm_service_kb(service_id))
    await call.answer()


@router.callback_query(BookingFlow.confirming_service, F.data.startswith("book:dates:"))
@router.callback_query(F.data.startswith("book:dates:"))
async def book_choose_date(
    call: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if call.message is None or call.data is None:
        return
    service_id = int(call.data.split(":")[2])
    service = await db.get_service(service_id)
    if service is None:
        await call.answer("Услуга не найдена", show_alert=True)
        return
    days = await available_dates(db, duration_minutes=int(service["duration_minutes"]))
    if not days:
        await call.message.edit_text(
            "К сожалению, на ближайшие 14 дней свободных слотов нет 😔\n"
            "Попробуйте позже или напишите мастеру напрямую.",
            reply_markup=kb.main_menu_kb(),
        )
        await state.clear()
        await call.answer()
        return
    await state.set_state(BookingFlow.choosing_date)
    await state.update_data(service_id=service_id)
    await call.message.edit_text(
        f"Выбрана услуга: <b>{service['name']}</b>\nКогда вам удобно?",
        reply_markup=kb.dates_kb(days, service_id=service_id, purpose="book"),
    )
    await call.answer()


@router.callback_query(BookingFlow.choosing_date, F.data.startswith("book:slots:"))
@router.callback_query(F.data.startswith("book:slots:"))
async def book_choose_slot(
    call: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if call.message is None or call.data is None:
        return
    parts = call.data.split(":")
    service_id = int(parts[2])
    day = date.fromisoformat(parts[3])
    service = await db.get_service(service_id)
    if service is None:
        await call.answer("Услуга не найдена", show_alert=True)
        return
    slots = await available_slots_for_day(
        db, day=day, duration_minutes=int(service["duration_minutes"])
    )
    if not slots:
        await call.message.edit_text(
            "На этот день уже не осталось свободных слотов 😔",
            reply_markup=kb.dates_kb(
                await available_dates(db, duration_minutes=int(service["duration_minutes"])),
                service_id=service_id,
                purpose="book",
            ),
        )
        await call.answer()
        return
    await state.set_state(BookingFlow.choosing_slot)
    await state.update_data(service_id=service_id, day=day.isoformat())
    await call.message.edit_text(
        f"<b>{service['name']}</b> — {kb.format_date_full(day)}\n"
        "Выберите время:",
        reply_markup=kb.slots_kb(slots, service_id=service_id, day=day, purpose="book"),
    )
    await call.answer()


@router.callback_query(BookingFlow.choosing_slot, F.data.startswith("book:pick:"))
@router.callback_query(F.data.startswith("book:pick:"))
async def book_pick_slot(
    call: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if call.message is None or call.data is None or call.from_user is None:
        return
    parts = call.data.split(":", 3)
    service_id = int(parts[2])
    slot_dt = datetime.fromisoformat(parts[3])
    await state.update_data(service_id=service_id, slot=slot_dt.isoformat())
    user = await db.get_user(call.from_user.id)
    saved_name = user["preferred_name"] if user else None
    await state.set_state(BookingFlow.entering_name)
    prompt = (
        "Как вас зовут? Напишите имя, чтобы мастер знал, как к вам обращаться 💖"
    )
    if saved_name:
        prompt += f"\nИли оставьте сохранённое имя: <b>{saved_name}</b>"
    await call.message.edit_text(prompt, reply_markup=kb.use_saved_name_kb(saved_name))
    await call.answer()


@router.callback_query(BookingFlow.entering_name, F.data == "book:name:keep")
async def book_keep_name(
    call: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if call.from_user is None or call.message is None:
        return
    user = await db.get_user(call.from_user.id)
    if user is None or not user["preferred_name"]:
        await call.answer("Сохранённого имени нет, напишите его сообщением.", show_alert=True)
        return
    await state.update_data(name=user["preferred_name"])
    await state.set_state(BookingFlow.entering_phone)
    await call.message.answer(
        "Спасибо! Теперь оставьте, пожалуйста, ваш номер телефона "
        "(можно нажать кнопку ниже).",
        reply_markup=kb.share_phone_kb(),
    )
    await call.answer()


@router.message(BookingFlow.entering_name)
async def book_enter_name(message: Message, state: FSMContext) -> None:
    if message.text is None:
        await message.answer("Пожалуйста, отправьте имя текстом 🌸")
        return
    name = message.text.strip()
    if not name or len(name) > 64:
        await message.answer("Имя должно быть от 1 до 64 символов. Попробуйте ещё раз.")
        return
    await state.update_data(name=name)
    await state.set_state(BookingFlow.entering_phone)
    await message.answer(
        "Спасибо! Теперь оставьте, пожалуйста, ваш номер телефона "
        "(можно нажать кнопку ниже).",
        reply_markup=kb.share_phone_kb(),
    )


@router.message(BookingFlow.entering_phone)
async def book_enter_phone(
    message: Message, state: FSMContext, db: Database
) -> None:
    phone: str | None = None
    if message.contact and message.contact.phone_number:
        phone = message.contact.phone_number
    elif message.text:
        phone = message.text.strip()
    if not phone:
        await message.answer(
            "Пожалуйста, отправьте номер телефона текстом или кнопкой ниже.",
            reply_markup=kb.share_phone_kb(),
        )
        return
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10:
        await message.answer("Похоже, номер неполный. Попробуйте ещё раз 🌸")
        return

    data = await state.get_data()
    service = await db.get_service(int(data["service_id"]))
    slot_dt = datetime.fromisoformat(data["slot"])
    if service is None:
        await message.answer("Услуга больше недоступна.", reply_markup=kb.remove_reply_kb())
        await state.clear()
        return

    await state.update_data(phone=phone)
    await state.set_state(BookingFlow.confirming)

    summary = (
        "Проверьте, всё ли верно:\n\n"
        f"💅 Услуга: <b>{service['name']}</b>\n"
        f"📅 Дата и время: <b>{_format_dt(slot_dt)}</b>\n"
        f"💰 Стоимость: {service['price']}₽\n"
        f"👤 Имя: {data['name']}\n"
        f"📞 Телефон: {phone}\n"
    )
    await message.answer(summary, reply_markup=kb.remove_reply_kb())
    await message.answer(
        "Подтверждаем запись?", reply_markup=kb.confirm_booking_kb()
    )


@router.callback_query(BookingFlow.confirming, F.data == "book:confirm")
async def book_confirm(
    call: CallbackQuery, state: FSMContext, db: Database, bot: Bot, settings: Settings
) -> None:
    if call.from_user is None or call.message is None:
        return
    data = await state.get_data()
    service = await db.get_service(int(data["service_id"]))
    slot_dt = datetime.fromisoformat(data["slot"])
    if service is None:
        await call.message.edit_text(
            "Услуга больше недоступна.", reply_markup=kb.main_menu_kb()
        )
        await state.clear()
        await call.answer()
        return

    # Re-check the slot is still free.
    free = await available_slots_for_day(
        db, day=slot_dt.date(), duration_minutes=int(service["duration_minutes"])
    )
    if slot_dt not in free:
        await call.message.edit_text(
            "К сожалению, это время только что заняли. Давайте выберем другое 🌸",
            reply_markup=kb.main_menu_kb(),
        )
        await state.clear()
        await call.answer()
        return

    appt_id = await db.create_appointment(
        client_tg_id=call.from_user.id,
        service_id=int(data["service_id"]),
        dt=slot_dt,
        client_name=data["name"],
        client_phone=data["phone"],
    )
    await db.update_user_profile(
        call.from_user.id,
        preferred_name=data["name"],
        phone=data["phone"],
        last_service_id=int(data["service_id"]),
        last_appointment_id=appt_id,
    )
    await state.clear()

    await call.message.edit_text(
        "Спасибо за доверие! Жду вас "
        f"<b>{_format_dt(slot_dt)}</b>. "
        "Если что-то изменится, всегда можно отменить или перенести запись через бота 💖",
        reply_markup=kb.my_appt_kb(appt_id),
    )
    username = call.from_user.username
    handle = f"@{username}" if username else data["name"]
    await _notify_master(
        bot,
        settings,
        f"📩 <b>Новая запись</b>\n"
        f"От: {handle}\n"
        f"Услуга: {service['name']}\n"
        f"Когда: {_format_dt(slot_dt)}\n"
        f"Имя: {data['name']}\n"
        f"Телефон: {data['phone']}",
    )
    await call.answer("Запись создана!")


@router.callback_query(BookingFlow.confirming, F.data == "book:abort")
async def book_abort(call: CallbackQuery, state: FSMContext) -> None:
    if call.message is None:
        return
    await state.clear()
    await call.message.edit_text(
        "Хорошо, отменили оформление. Возвращайтесь, как будете готовы 🌸",
        reply_markup=kb.main_menu_kb(),
    )
    await call.answer()


# -- my appointments -------------------------------------------------------


@router.callback_query(F.data == "my:list")
async def my_appointments(call: CallbackQuery, db: Database) -> None:
    if call.from_user is None or call.message is None:
        return
    rows = await db.list_active_for_client(call.from_user.id)
    if not rows:
        await call.message.edit_text(
            "Активных записей пока нет. Записаться можно из меню 🌸",
            reply_markup=kb.main_menu_kb(),
        )
        await call.answer()
        return
    await call.message.edit_text(
        "🗒 <b>Ваши предстоящие записи:</b>",
        reply_markup=kb.main_menu_kb(has_active=True),
    )
    for row in rows:
        appt_dt = to_dt(row["datetime"])
        if appt_dt is None:
            continue
        text = (
            f"💅 <b>{row['service_name']}</b>\n"
            f"📅 {_format_dt(appt_dt)}\n"
            f"💰 {row['service_price']}₽"
        )
        await call.message.answer(text, reply_markup=kb.my_appt_kb(int(row["id"])))
    await call.answer()


@router.callback_query(F.data == "my:manage")
async def my_manage(call: CallbackQuery, db: Database) -> None:
    # Same listing — every appointment row already has cancel / reschedule buttons.
    await my_appointments(call, db)


# -- cancellation ----------------------------------------------------------


@router.callback_query(F.data.startswith("appt:cancel:"))
async def cancel_start(
    call: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if call.from_user is None or call.message is None or call.data is None:
        return
    appt_id = int(call.data.split(":")[2])
    appt = await db.get_appointment(appt_id)
    if appt is None or appt["client_tg_id"] != call.from_user.id:
        await call.answer("Запись не найдена", show_alert=True)
        return
    if appt["status"] != "active":
        await call.answer("Эту запись уже нельзя отменить", show_alert=True)
        return
    await state.set_state(CancelFlow.waiting_reason)
    await state.update_data(appt_id=appt_id)
    await call.message.answer(
        "Подскажите, пожалуйста, причину отмены — это поможет мастеру стать лучше. "
        "Или нажмите «Без причины», если не хотите указывать.",
        reply_markup=kb.cancel_reason_skip_kb(),
    )
    await call.answer()


async def _do_cancel(
    *,
    bot: Bot,
    db: Database,
    settings: Settings,
    user_id: int,
    appt_id: int,
    reason: str | None,
    target_message: Message,
) -> None:
    appt = await db.get_appointment(appt_id)
    if appt is None or appt["client_tg_id"] != user_id:
        await target_message.answer("Запись не найдена.", reply_markup=kb.main_menu_kb())
        return
    ok = await db.cancel_appointment(appt_id, reason)
    if not ok:
        await target_message.answer(
            "Эту запись уже нельзя отменить.", reply_markup=kb.main_menu_kb()
        )
        return
    appt_dt = to_dt(appt["datetime"])
    when = _format_dt(appt_dt) if appt_dt else "—"
    await target_message.answer(
        "Спасибо, что сообщили! Будем рады видеть вас в другой раз 🌸",
        reply_markup=kb.main_menu_kb(),
    )
    reason_line = f"\nПричина: {reason}" if reason else ""
    await _notify_master(
        bot,
        settings,
        f"❌ <b>Клиент отменил запись</b>\n"
        f"Услуга: {appt['service_name']}\n"
        f"Когда: {when}\n"
        f"Имя: {appt['client_name']}\n"
        f"Телефон: {appt['client_phone']}"
        f"{reason_line}",
    )


@router.callback_query(CancelFlow.waiting_reason, F.data == "appt:reason:skip")
async def cancel_skip(
    call: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    settings: Settings,
) -> None:
    if call.from_user is None or call.message is None:
        return
    data = await state.get_data()
    await state.clear()
    await _do_cancel(
        bot=bot,
        db=db,
        settings=settings,
        user_id=call.from_user.id,
        appt_id=int(data["appt_id"]),
        reason=None,
        target_message=call.message,
    )
    await call.answer()


@router.message(CancelFlow.waiting_reason)
async def cancel_with_reason(
    message: Message,
    state: FSMContext,
    db: Database,
    bot: Bot,
    settings: Settings,
) -> None:
    if message.from_user is None:
        return
    reason = (message.text or "").strip()[:500] or None
    data = await state.get_data()
    await state.clear()
    await _do_cancel(
        bot=bot,
        db=db,
        settings=settings,
        user_id=message.from_user.id,
        appt_id=int(data["appt_id"]),
        reason=reason,
        target_message=message,
    )


# -- reschedule ------------------------------------------------------------


@router.callback_query(F.data.startswith("appt:reschedule:"))
async def reschedule_start(
    call: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if call.from_user is None or call.message is None or call.data is None:
        return
    appt_id = int(call.data.split(":")[2])
    appt = await db.get_appointment(appt_id)
    if appt is None or appt["client_tg_id"] != call.from_user.id:
        await call.answer("Запись не найдена", show_alert=True)
        return
    if appt["status"] != "active":
        await call.answer("Эту запись уже нельзя перенести", show_alert=True)
        return
    duration = int(appt["service_duration"])
    days = await available_dates(db, duration_minutes=duration)
    if not days:
        await call.message.answer(
            "Свободных дней пока нет 😔", reply_markup=kb.main_menu_kb()
        )
        await call.answer()
        return
    await state.set_state(RescheduleFlow.choosing_date)
    await state.update_data(
        appt_id=appt_id, service_id=int(appt["service_id"]), duration=duration
    )
    await call.message.answer(
        "На какую дату хотите перенести запись?",
        reply_markup=kb.dates_kb(
            days, service_id=int(appt["service_id"]), purpose="resched"
        ),
    )
    await call.answer()


@router.callback_query(RescheduleFlow.choosing_date, F.data.startswith("resched:slots:"))
async def reschedule_pick_date(
    call: CallbackQuery, state: FSMContext, db: Database
) -> None:
    if call.message is None or call.data is None:
        return
    parts = call.data.split(":")
    service_id = int(parts[2])
    day = date.fromisoformat(parts[3])
    data = await state.get_data()
    duration = int(data["duration"])
    slots = await available_slots_for_day(
        db,
        day=day,
        duration_minutes=duration,
        exclude_appointment_id=int(data["appt_id"]),
    )
    if not slots:
        await call.answer("На этот день нет свободных слотов", show_alert=True)
        return
    await state.set_state(RescheduleFlow.choosing_slot)
    await state.update_data(day=day.isoformat())
    await call.message.edit_text(
        f"{kb.format_date_full(day)} — выберите новое время:",
        reply_markup=kb.slots_kb(
            slots, service_id=service_id, day=day, purpose="resched"
        ),
    )
    await call.answer()


@router.callback_query(RescheduleFlow.choosing_slot, F.data.startswith("resched:pick:"))
async def reschedule_pick_slot(
    call: CallbackQuery,
    state: FSMContext,
    db: Database,
    bot: Bot,
    settings: Settings,
) -> None:
    if call.from_user is None or call.message is None or call.data is None:
        return
    parts = call.data.split(":", 3)
    new_dt = datetime.fromisoformat(parts[3])
    data = await state.get_data()
    appt_id = int(data["appt_id"])
    appt = await db.get_appointment(appt_id)
    if appt is None or appt["client_tg_id"] != call.from_user.id:
        await call.answer("Запись не найдена", show_alert=True)
        return

    free = await available_slots_for_day(
        db,
        day=new_dt.date(),
        duration_minutes=int(appt["service_duration"]),
        exclude_appointment_id=appt_id,
    )
    if new_dt not in free:
        await call.message.edit_text(
            "Это время уже заняли. Давайте выберем другое 🌸",
            reply_markup=kb.main_menu_kb(),
        )
        await state.clear()
        await call.answer()
        return

    cancelled = await db.cancel_appointment(appt_id, "rescheduled", mark_rescheduled=True)
    if not cancelled:
        await call.message.edit_text(
            "Не получилось перенести: запись уже изменена.",
            reply_markup=kb.main_menu_kb(),
        )
        await state.clear()
        await call.answer()
        return

    new_appt_id = await db.create_appointment(
        client_tg_id=call.from_user.id,
        service_id=int(appt["service_id"]),
        dt=new_dt,
        client_name=appt["client_name"] or "",
        client_phone=appt["client_phone"] or "",
    )
    await db.update_user_profile(call.from_user.id, last_appointment_id=new_appt_id)
    await state.clear()

    await call.message.edit_text(
        f"Хорошо, перенесли вашу запись на <b>{_format_dt(new_dt)}</b>. "
        "Если нужно ещё что-то — обращайтесь!",
        reply_markup=kb.my_appt_kb(new_appt_id),
    )

    old_dt = to_dt(appt["datetime"])
    old_when = _format_dt(old_dt) if old_dt else "—"
    await _notify_master(
        bot,
        settings,
        f"🔁 <b>Клиент перенёс запись</b>\n"
        f"Услуга: {appt['service_name']}\n"
        f"Было: {old_when}\n"
        f"Стало: {_format_dt(new_dt)}\n"
        f"Имя: {appt['client_name']}\n"
        f"Телефон: {appt['client_phone']}",
    )
    await call.answer("Перенесли!")
