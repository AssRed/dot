"""Common handlers: /start, main menu navigation, fallback messages."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import keyboards as kb
from config import Settings
from database import Database, to_dt

logger = logging.getLogger(__name__)
router = Router(name="common")


def _greeting_for_returning(active_rows: list) -> str:
    if not active_rows:
        return ""
    nearest = active_rows[0]
    appt_dt = to_dt(nearest["datetime"])
    if appt_dt is None:
        return ""
    return (
        f"\n\nБлижайшая запись: <b>{nearest['service_name']}</b> — "
        f"{appt_dt.strftime('%d.%m')} в {appt_dt.strftime('%H:%M')}."
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user_id = message.from_user.id

    is_master = user_id == settings.master_tg_id
    is_new, _ = await db.upsert_user_visit(user_id, role="master" if is_master else "client")

    welcome_type = "new" if is_new else "returning"
    welcome_text = await db.get_welcome_text(welcome_type)

    if is_master:
        await message.answer(
            "👑 С возвращением, мастер!\n\n"
            "Доступные команды:\n"
            "/appointments — предстоящие записи\n"
            "/list_services — услуги\n"
            "/add_service — добавить услугу\n"
            "/edit_service &lt;id&gt; — редактировать услугу\n"
            "/delete_service &lt;id&gt; — удалить услугу\n"
            "/set_schedule — расписание\n"
            "/complete &lt;id&gt; — завершить запись\n"
            "/finance — финансы и прогноз\n"
            "/set_master_info — раздел «О мастере»\n"
            "/set_tax_rate &lt;4|6&gt; — налог\n"
            "/set_welcome_text — приветствие клиентам"
        )
        return

    active_rows = await db.list_active_for_client(user_id)
    extra = _greeting_for_returning(active_rows) if not is_new else ""
    await message.answer(welcome_text + extra)
    await message.answer(
        "Чем могу помочь? Выберите действие:",
        reply_markup=kb.main_menu_kb(has_active=bool(active_rows)),
    )


@router.callback_query(F.data == "menu:main")
async def back_to_menu(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    if call.from_user is None or call.message is None:
        return
    active_rows = await db.list_active_for_client(call.from_user.id)
    try:
        await call.message.edit_text(
            "Главное меню. Выберите действие:",
            reply_markup=kb.main_menu_kb(has_active=bool(active_rows)),
        )
    except Exception:
        await call.message.answer(
            "Главное меню. Выберите действие:",
            reply_markup=kb.main_menu_kb(has_active=bool(active_rows)),
        )
    await call.answer()


@router.callback_query(F.data == "master:about")
async def about_master(call: CallbackQuery, db: Database) -> None:
    if call.message is None:
        return
    info = await db.get_master_info()
    text = (info["text"] if info else "") or (
        "Мастер пока не заполнил информацию о себе. Скоро здесь появится "
        "рассказ о квалификации и любимых процедурах ✨"
    )
    photo = info["photo_file_id"] if info else None
    back_kb = kb.InlineKeyboardMarkup(
        inline_keyboard=[
            [kb.InlineKeyboardButton(text="« Меню", callback_data="menu:main")]
        ]
    )
    if photo:
        await call.message.answer_photo(photo, caption=text, reply_markup=back_kb)
    else:
        await call.message.answer(text, reply_markup=back_kb)
    await call.answer()
