"""Common handlers: /start, main menu navigation, fallback messages."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import keyboards as kb
from config import Settings
from database import Database, to_dt

logger = logging.getLogger(__name__)
router = Router(name="common")

# Deep-link payload (after `/start <payload>`) that triggers the master
# mini-landing experience. Anything starting with this prefix is treated
# as a referral hit.
LANDING_PREFIXES = ("master", "landing", "portfolio")


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


async def _send_landing(message: Message, db: Database) -> None:
    """Mini-landing: master info + services + booking CTA."""
    info = await db.get_master_info()
    services = await db.list_services()
    intro = (info["text"] if info else "") or (
        "Добро пожаловать! Здесь вы можете записаться на процедуры и узнать "
        "подробности у мастера ✨"
    )
    photo = info["photo_file_id"] if info else None

    parts = [intro.strip()]
    if services:
        parts.append("")
        parts.append("<b>Услуги и цены:</b>")
        for s in services:
            parts.append(
                f"• <b>{s['name']}</b> — {s['price']}₽ · "
                f"{s['duration_minutes']} мин"
            )
    body = "\n".join(parts)
    cta_kb = kb.landing_cta_kb(has_services=bool(services))

    if photo:
        # Telegram captions are limited to ~1024 characters.
        if len(body) <= 1000:
            await message.answer_photo(photo, caption=body, reply_markup=cta_kb)
        else:
            await message.answer_photo(photo, caption=intro)
            await message.answer(body, reply_markup=cta_kb)
    else:
        await message.answer(body, reply_markup=cta_kb)


@router.message(CommandStart(deep_link=True))
async def cmd_start_deep(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    """Handle deep-link /start payload (mini-landing)."""
    await state.clear()
    if message.from_user is None:
        return
    user = message.from_user
    is_master = user.id == settings.master_tg_id
    await db.upsert_user_visit(
        user.id,
        role="master" if is_master else "client",
        username=user.username,
        first_name=user.first_name,
    )

    payload = (command.args or "").strip().lower()
    is_landing = any(payload.startswith(p) for p in LANDING_PREFIXES) or payload == ""

    if is_master:
        # Master clicking own deep-link → still show admin menu.
        await _send_master_menu(message)
        return

    if is_landing:
        await _send_landing(message, db)
        return

    # Unknown payload → fall back to normal greeting.
    await _send_normal_start(message, db, is_new=False)


@router.message(CommandStart())
async def cmd_start(
    message: Message, state: FSMContext, db: Database, settings: Settings
) -> None:
    await state.clear()
    if message.from_user is None:
        return
    user = message.from_user
    is_master = user.id == settings.master_tg_id
    is_new, _ = await db.upsert_user_visit(
        user.id,
        role="master" if is_master else "client",
        username=user.username,
        first_name=user.first_name,
    )

    if is_master:
        await _send_master_menu(message)
        return
    await _send_normal_start(message, db, is_new=is_new)


async def _send_master_menu(message: Message) -> None:
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
        "/clients — база клиентов (CRM)\n"
        "/client &lt;tg_id&gt; — карточка клиента\n"
        "/broadcast — рассылка по клиентам\n"
        "/waitlist — список ожидания\n"
        "/analytics — аналитика и метрики\n"
        "/landing — ссылка-визитка для соцсетей\n"
        "/set_master_info — раздел «О мастере»\n"
        "/set_tax_rate &lt;4|6&gt; — налог\n"
        "/set_welcome_text — приветствие клиентам"
    )


async def _send_normal_start(
    message: Message, db: Database, *, is_new: bool
) -> None:
    welcome_type = "new" if is_new else "returning"
    welcome_text = await db.get_welcome_text(welcome_type)
    if message.from_user is None:
        return
    active_rows = await db.list_active_for_client(message.from_user.id)
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
