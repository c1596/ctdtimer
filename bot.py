import importlib
import importlib.util
import subprocess
import sys

# Список необходимых внешних библиотек
REQUIRED_DEPENDENCIES = {
    "aiogram": "aiogram>=3.13.0",
    "pytz": "pytz>=2024.1",
}

def auto_install_dependencies() -> None:
    """Проверяет и автоматически устанавливает зависимости при первом запуске."""
    missing_packages = []
    for module_name, pip_package in REQUIRED_DEPENDENCIES.items():
        if importlib.util.find_spec(module_name) is None:
            missing_packages.append(pip_package)

    if missing_packages:
        print(f"🔧 Установка недостающих пакетов: {', '.join(missing_packages)}")
        try:
            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-warn-script-location",
                    *missing_packages,
                ],
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            importlib.invalidate_caches()
            print("✅ Зависимости успешно установлены!\n")
        except subprocess.CalledProcessError as err:
            print(f"❌ Ошибка установки зависимостей: {err}", file=sys.stderr)
            sys.exit(1)

auto_install_dependencies()

import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple, Union

import pytz
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

FALLBACK_BOT_TOKEN: str = "8986545593:AAGsw08a8eY182N4LKIrAXKJeh3iVwmx5FA"
FALLBACK_ADMIN_ID: int = 5341904332

raw_env_token = os.getenv("BOT_TOKEN", "").strip().strip("\"'")
BOT_TOKEN: str = raw_env_token if raw_env_token else FALLBACK_BOT_TOKEN
BOT_TOKEN = BOT_TOKEN.replace(" ", "").replace("\n", "").replace("\r", "").strip("\"'")

raw_admin_env = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID: int = int(raw_admin_env) if (raw_admin_env.isdigit() or (raw_admin_env.startswith("-") and raw_admin_env[1:].isdigit())) else FALLBACK_ADMIN_ID

DEFAULT_TIMEZONE_STR: str = "Europe/Moscow"

# Непрерывный живой интервал отсчета (без адаптивных замедлений и искусственных задержек)
LIVE_UPDATE_INTERVAL_SECONDS: float = 3.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("CountdownBot")

# Глобальные хранилища задач и состояния
active_tasks: Dict[str, asyncio.Task] = {}
active_timers_registry: Dict[str, Dict[str, Any]] = {}

PROGRESS_BAR_STYLES = {
    "classic": ("█", "░", "Классика [████░░░░]"),
    "neon": ("▰", "▱", "Неон [▰▰▰▱▱▱]"),
    "emerald": ("🟩", "⬜", "Изумруд [🟩🟩⬜⬜]"),
    "squares": ("■", "□", "Квадраты [■■■□□□]"),
    "dots": ("●", "○", "Точки [●●●○○○]"),
}

class CountdownFSM(StatesGroup):
    waiting_title = State()          # 1. Название события
    waiting_datetime = State()       # 2. Дата / время
    waiting_style = State()          # 2.1 Выбор визуала шкалы
    waiting_destination = State()    # 3. Место публикации (ЛС или Канал)
    waiting_channel_target = State() # 3.1 Ввод канала
    waiting_photo = State()          # 4. Фото-обложка
    waiting_finish_text = State()    # 5. Финальное сообщение
    waiting_confirm = State()        # 6. Предпросмотр и запуск

def generate_progress_bar(
    total_duration: float,
    remaining: float,
    length: int = 10,
    style_key: str = "classic",
) -> Tuple[str, float]:
    """Формирует шкалу прогресса на основе выбранного стиля."""
    fill_char, empty_char, _ = PROGRESS_BAR_STYLES.get(style_key, PROGRESS_BAR_STYLES["classic"])

    if total_duration <= 0:
        return fill_char * length, 100.0

    elapsed = max(0.0, total_duration - remaining)
    fraction = max(0.0, min(1.0, elapsed / total_duration))
    percent = fraction * 100.0

    filled = int(round(length * fraction))
    filled = max(0, min(length, filled))
    bar_str = (fill_char * filled) + (empty_char * (length - filled))
    return bar_str, percent

def format_countdown_badge(remaining_seconds: int) -> str:
    """Форматирует оставшееся время в компактный моноширинный вид."""
    if remaining_seconds <= 0:
        return "00:00:00"

    days = remaining_seconds // 86400
    hours = (remaining_seconds % 86400) // 3600
    minutes = (remaining_seconds % 3600) // 60
    seconds = remaining_seconds % 60

    if days > 0:
        return f"{days}д {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def render_timer_card(
    title: str,
    target_dt: datetime,
    total_seconds: float,
    remaining_seconds: float,
    style_key: str = "classic",
    is_finished: bool = False,
    finish_message: Optional[str] = None,
) -> str:
    """
    Генерирует строго компактный пост таймера:
    Содержит исключительно:
    1. Событие
    2. Цель
    3. Шкала завершения
    Никакого лишнего текста про автоматическое обновление!
    """
    safe_title = html.escape(title)
    date_str = target_dt.strftime("%d.%m.%Y в %H:%M")

    if is_finished:
        safe_finish = html.escape(finish_message or "Событие наступило!")
        full_bar, _ = generate_progress_bar(1, 0, length=10, style_key=style_key)
        return (
            f"🎉 <b>Событие:</b> <b>{safe_title}</b>\n"
            f"🎯 <b>Цель:</b> <code>{date_str} МСК</code>\n"
            f"📊 <b>Шкала завершения:</b> <code>[{full_bar}] 100%</code> (🏁 <code>00:00:00</code>)\n\n"
            f"<blockquote>💬 <i>{safe_finish}</i></blockquote>"
        )

    bar, percent = generate_progress_bar(
        total_duration=total_seconds,
        remaining=remaining_seconds,
        length=10,
        style_key=style_key,
    )
    badge = format_countdown_badge(int(remaining_seconds))

    return (
        f"📌 <b>Событие:</b> <b>{safe_title}</b>\n"
        f"🎯 <b>Цель:</b> <code>{date_str} МСК</code>\n"
        f"📊 <b>Шкала завершения:</b> <code>[{bar}] {percent:>4.1f}%</code> (⏳ <code>{badge}</code>)"
    )

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Создать живой таймер", callback_data="wizard_start")],
            [InlineKeyboardButton(text="📋 Мои активные таймеры", callback_data="my_active_timers")],
            [InlineKeyboardButton(text="📢 Инструкция для каналов", callback_data="help_channel_info")],
        ]
    )

def get_datetime_presets_keyboard() -> InlineKeyboardMarkup:
    """Быстрые пресеты времени для моментальной настройки."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+5 мин", callback_data="dt_preset:5m"),
                InlineKeyboardButton(text="+15 мин", callback_data="dt_preset:15m"),
                InlineKeyboardButton(text="+30 мин", callback_data="dt_preset:30m"),
            ],
            [
                InlineKeyboardButton(text="+1 час", callback_data="dt_preset:1h"),
                InlineKeyboardButton(text="+3 часа", callback_data="dt_preset:3h"),
                InlineKeyboardButton(text="+12 часов", callback_data="dt_preset:12h"),
            ],
            [
                InlineKeyboardButton(text="+1 день", callback_data="dt_preset:1d"),
                InlineKeyboardButton(text="+3 дня", callback_data="dt_preset:3d"),
                InlineKeyboardButton(text="+7 дней", callback_data="dt_preset:7d"),
            ],
            [
                InlineKeyboardButton(text="« Назад", callback_data="wizard_back_to_title"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard"),
            ],
        ]
    )

def get_style_selection_keyboard(current_style: str = "classic") -> InlineKeyboardMarkup:
    """Клавиатура выбора визуального стиля шкалы."""
    buttons = []
    for key, (_, _, label) in PROGRESS_BAR_STYLES.items():
        mark = "✓ " if key == current_style else ""
        buttons.append([InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_style:{key}")])

    buttons.append([
        InlineKeyboardButton(text="« Назад", callback_data="wizard_back_to_datetime"),
        InlineKeyboardButton(text="Далее »", callback_data="wizard_continue_from_style"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_destination_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 В этот диалог (ЛС)", callback_data="dest_private"),
                InlineKeyboardButton(text="📢 В Telegram-канал", callback_data="dest_channel"),
            ],
            [
                InlineKeyboardButton(text="« Назад", callback_data="wizard_back_to_style"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard"),
            ],
        ]
    )

def get_photo_step_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏩ Без обложки (только текст)", callback_data="skip_photo_action")],
            [
                InlineKeyboardButton(text="« Назад", callback_data="wizard_back_to_dest"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard"),
            ],
        ]
    )

def get_finish_text_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏩ Стандартное («Событие наступило!»)", callback_data="default_finish_text")],
            [
                InlineKeyboardButton(text="« Назад", callback_data="wizard_back_to_photo"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard"),
            ],
        ]
    )

def get_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Опубликовать и запустить", callback_data="confirm_launch_timer")],
            [
                InlineKeyboardButton(text="✏️ Начать заново", callback_data="wizard_start"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard"),
            ],
        ]
    )

def get_timer_widget_keyboard(timer_key: str, can_stop: bool = True) -> Optional[InlineKeyboardMarkup]:
    if not can_stop:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛑 Остановить отсчет", callback_data=f"stop_timer:{timer_key}")]
        ]
    )

def parse_flexible_datetime(user_input: str, tz: pytz.BaseTzInfo) -> Optional[datetime]:
    """
    Умный парсер времени:
    - ДД.ММ.ГГГГ ЧЧ:ММ (31.12.2026 23:59)
    - ДД.ММ ЧЧ:ММ (31.12 23:59)
    - ЧЧ:ММ (автоматически сегодня или завтра)
    - Относительный ввод: +45m, +2h, +3d
    """
    text = user_input.strip()
    now = datetime.now(tz)

    rel_match = re.fullmatch(r"\+(\d+)\s*([mмhчdд])", text, re.IGNORECASE)
    if rel_match:
        val = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        if unit in ("m", "м"):
            return now + timedelta(minutes=val)
        elif unit in ("h", "ч"):
            return now + timedelta(hours=val)
        elif unit in ("d", "д"):
            return now + timedelta(days=val)

    time_match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if time_match:
        hour, minute = int(time_match.group(1)), int(time_match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target

    short_dt_match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})", text)
    if short_dt_match:
        day = int(short_dt_match.group(1))
        month = int(short_dt_match.group(2))
        hour = int(short_dt_match.group(3))
        minute = int(short_dt_match.group(4))
        try:
            target = tz.localize(datetime(now.year, month, day, hour, minute))
            if target <= now:
                target = tz.localize(datetime(now.year + 1, month, day, hour, minute))
            return target
        except ValueError:
            return None

    try:
        naive = datetime.strptime(text, "%d.%m.%Y %H:%M")
        return tz.localize(naive)
    except ValueError:
        return None

async def run_live_timer_worker(
    bot: Bot,
    timer_key: str,
    target_chat_id: Union[int, str],
    message_id: int,
    title: str,
    target_dt: datetime,
    total_seconds: float,
    style_key: str,
    finish_text: str,
    has_photo: bool,
    creator_user_id: int,
) -> None:
    """
    Непрерывный живой воркер:
    - Обновляется строго с постоянным интервалом LIVE_UPDATE_INTERVAL_SECONDS.
    - Никакого адаптивного замедления частоты.
    """
    tz = pytz.timezone(DEFAULT_TIMEZONE_STR)
    last_rendered_payload = ""

    try:
        while True:
            now = datetime.now(tz)
            remaining_seconds = (target_dt - now).total_seconds()

            if remaining_seconds <= 0:
                final_card = render_timer_card(
                    title=title,
                    target_dt=target_dt,
                    total_seconds=total_seconds,
                    remaining_seconds=0,
                    style_key=style_key,
                    is_finished=True,
                    finish_message=finish_text,
                )

                try:
                    if has_photo:
                        await bot.edit_message_caption(
                            chat_id=target_chat_id,
                            message_id=message_id,
                            caption=final_card,
                            reply_markup=None,
                        )
                    else:
                        await bot.edit_message_text(
                            chat_id=target_chat_id,
                            message_id=message_id,
                            text=final_card,
                            reply_markup=None,
                        )

                    await bot.send_message(
                        chat_id=target_chat_id,
                        text=(
                            f"🔔 <b>СОБЫТИЕ НАСТУПИЛО!</b>\n"
                            f"🎯 <b>{html.escape(title)}</b>\n\n"
                            f"<i>{html.escape(finish_text)}</i>"
                        ),
                        disable_notification=False,
                    )

                    if str(target_chat_id) != str(creator_user_id):
                        await bot.send_message(
                            chat_id=creator_user_id,
                            text=(
                                f"✅ <b>Таймер завершен!</b>\n"
                                f"Отсчет для события «<b>{html.escape(title)}</b>» в канале <code>{target_chat_id}</code> подошел к концу!"
                            ),
                        )
                except Exception as finish_err:
                    logger.error(f"Ошибка финализации таймера ({target_chat_id}): {finish_err}")
                break

            current_card = render_timer_card(
                title=title,
                target_dt=target_dt,
                total_seconds=total_seconds,
                remaining_seconds=remaining_seconds,
                style_key=style_key,
                is_finished=False,
            )

            if current_card != last_rendered_payload:
                try:
                    can_stop = (str(target_chat_id) == str(creator_user_id))
                    kb = get_timer_widget_keyboard(timer_key=timer_key, can_stop=can_stop)

                    if has_photo:
                        await bot.edit_message_caption(
                            chat_id=target_chat_id,
                            message_id=message_id,
                            caption=current_card,
                            reply_markup=kb,
                        )
                    else:
                        await bot.edit_message_text(
                            chat_id=target_chat_id,
                            message_id=message_id,
                            text=current_card,
                            reply_markup=kb,
                        )
                    last_rendered_payload = current_card

                except TelegramRetryAfter as retry_err:
                    await asyncio.sleep(retry_err.retry_after + 0.5)
                    continue

                except TelegramBadRequest as bad_req:
                    err_text = str(bad_req).lower()
                    if "message is not modified" in err_text:
                        pass
                    elif "message to edit not found" in err_text or "message can't be edited" in err_text:
                        logger.info(f"Сообщение {message_id} удалено. Остановка воркера.")
                        break
                    else:
                        logger.warning(f"TelegramBadRequest ({target_chat_id}): {bad_req}")

                except (TelegramForbiddenError, TelegramNotFound):
                    logger.warning(f"Бот заблокирован или исключен из {target_chat_id}.")
                    break

                except Exception as unexpected:
                    logger.error(f"Ошибка воркера: {unexpected}")

            await asyncio.sleep(LIVE_UPDATE_INTERVAL_SECONDS)

    except asyncio.CancelledError:
        logger.info(f"Воркер {timer_key} остановлен.")
    finally:
        active_tasks.pop(timer_key, None)
        active_timers_registry.pop(timer_key, None)

router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    first_name = html.escape(message.from_user.first_name)
    text = (
        f"👋 <b>Привет, {first_name}!</b>\n\n"
        f"Я создаю <b>живые динамические таймеры</b> для личных сообщений и каналов.\n\n"
        f"✨ <b>Что внутри:</b>\n"
        f"• <b>Компактный пост:</b> только Событие, Цель и Шкала завершения\n"
        f"• <b>Живой непрерывный отсчет:</b> обновление каждые <code>{int(LIVE_UPDATE_INTERVAL_SECONDS)} сек</code>\n"
        f"• <b>Стили прогресс-бара:</b> Классика, Неон, Изумруд, Квадраты, Точки\n"
        f"• <b>Поддержка фото:</b> посты с баннерами или лаконичные текстовые карточки\n"
        f"• <b>Удобное управление:</b> моментальная отмена и просмотр таймеров\n\n"
        f"Нажмите кнопку ниже, чтобы запустить мастер создания!"
    )
    await message.answer(text, reply_markup=get_main_menu_keyboard())

@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel_wizard")
async def cancel_wizard(event: Union[Message, CallbackQuery], state: FSMContext) -> None:
    await state.clear()
    msg_text = "❌ <b>Создание таймера отменено.</b>"
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.edit_text(msg_text, reply_markup=get_main_menu_keyboard())
    else:
        await event.answer(msg_text, reply_markup=get_main_menu_keyboard())

@router.callback_query(F.data == "help_channel_info")
async def help_channel(callback: CallbackQuery) -> None:
    await callback.answer()
    text = (
        f"📢 <b>КАК ПОДКЛЮЧИТЬ ТАЙМЕР К ВАШЕМУ КАНАЛУ:</b>\n\n"
        f"1️⃣ Откройте настройки канала в Telegram.\n"
        f"2️⃣ Добавьте этого бота в список <b>Администраторов</b>.\n"
        f"3️⃣ Включите права: <b>Публикация</b> и <b>Редактирование сообщений</b>.\n"
        f"4️⃣ В боте выберите <b>«В Telegram-канал»</b> и просто <b>перешлите любой пост</b> из канала либо отправьте <code>@username</code> канала."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Создать таймер", callback_data="wizard_start")],
            [InlineKeyboardButton(text="◀️ В главное меню", callback_data="back_to_main_menu")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=kb)

@router.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Главное меню управления таймерами:", reply_markup=get_main_menu_keyboard())

@router.callback_query(F.data == "my_active_timers")
@router.message(Command("timers"))
async def show_active_timers(event: Union[Message, CallbackQuery]) -> None:
    user_id = event.from_user.id
    user_timers = [
        (k, v) for k, v in active_timers_registry.items()
        if v.get("creator_id") == user_id
    ]

    if not user_timers:
        text = "📭 У вас нет активных запущенных таймеров."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⚡ Создать таймер", callback_data="wizard_start")],
                [InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_main_menu")],
            ]
        )
    else:
        text = f"📋 <b>Ваши активные таймеры ({len(user_timers)}):</b>\n\n"
        buttons = []
        tz = pytz.timezone(DEFAULT_TIMEZONE_STR)
        now = datetime.now(tz)

        for key, info in user_timers:
            target_dt: datetime = info["target_dt"]
            title: str = info["title"]
            rem_sec = max(0, int((target_dt - now).total_seconds()))
            badge = format_countdown_badge(rem_sec)

            text += (
                f"• <b>{html.escape(title)}</b>\n"
                f"  Цель: <code>{target_dt.strftime('%d.%m %H:%M')} МСК</code> | ⏳ <code>{badge}</code>\n"
            )
            buttons.append([
                InlineKeyboardButton(
                    text=f"🛑 Остановить: {title[:20]}",
                    callback_data=f"stop_timer:{key}"
                )
            ])
        buttons.append([InlineKeyboardButton(text="◀️ В главное меню", callback_data="back_to_main_menu")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.edit_text(text, reply_markup=kb)
    else:
        await event.answer(text, reply_markup=kb)

@router.callback_query(F.data == "wizard_start")
@router.message(Command("newtimer"))
async def wizard_step_1_title(event: Union[Message, CallbackQuery], state: FSMContext) -> None:
    await state.set_state(CountdownFSM.waiting_title)
    text = (
        f"📝 <b>[●○○○○○] Шаг 1 из 6 • Название события</b>\n\n"
        f"Введите название для карточки таймера (до 70 символов):\n"
        f"<i>Например: «Новый Год 2027 🍾» или «Запуск Проекта 🚀»</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard")]]
    )
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.edit_text(text, reply_markup=kb)
    else:
        await event.answer(text, reply_markup=kb)

@router.callback_query(F.data == "wizard_back_to_title")
async def back_to_title(callback: CallbackQuery, state: FSMContext) -> None:
    await wizard_step_1_title(callback, state)

@router.message(StateFilter(CountdownFSM.waiting_title), F.text)
async def process_title(message: Message, state: FSMContext) -> None:
    title = message.text.strip()
    if len(title) > 70:
        await message.answer("⚠️ Название слишком длинное. Введите текст до 70 символов:")
        return

    await state.update_data(title=title)
    await state.set_state(CountdownFSM.waiting_datetime)

    tz = pytz.timezone(DEFAULT_TIMEZONE_STR)
    sample_time = (datetime.now(tz) + timedelta(days=1, hours=3)).strftime("%d.%m.%Y %H:%M")

    text = (
        f"📅 <b>[●●○○○○] Шаг 2 из 6 • Время финиша</b>\n\n"
        f"Событие: <b>{html.escape(title)}</b>\n\n"
        f"Выберите быстрый интервал кнопкой или введите время вручную:\n"
        f"• <code>{sample_time}</code> (ДД.ММ.ГГГГ ЧЧ:ММ)\n"
        f"• <code>20:30</code> (сегодня/завтра)\n"
        f"• <code>+45m</code>, <code>+2h</code> или <code>+3d</code>"
    )
    await message.answer(text, reply_markup=get_datetime_presets_keyboard())

@router.callback_query(StateFilter(CountdownFSM.waiting_datetime), F.data.startswith("dt_preset:"))
async def process_datetime_preset(callback: CallbackQuery, state: FSMContext) -> None:
    preset = callback.data.split(":")[1]
    tz = pytz.timezone(DEFAULT_TIMEZONE_STR)
    now = datetime.now(tz)

    delta_map = {
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "3h": timedelta(hours=3),
        "12h": timedelta(hours=12),
        "1d": timedelta(days=1),
        "3d": timedelta(days=3),
        "7d": timedelta(days=7),
    }

    target_dt = now + delta_map.get(preset, timedelta(hours=1))
    diff_sec = (target_dt - now).total_seconds()

    await state.update_data(target_dt_iso=target_dt.isoformat(), total_seconds=diff_sec, style_key="classic")
    await state.set_state(CountdownFSM.waiting_style)
    await callback.answer("Время выбрано!")

    await callback.message.edit_text(
        f"🎨 <b>[●●●○○○] Шаг 3 из 6 • Стиль шкалы прогресса</b>\n\n"
        f"Финиш: <code>{target_dt.strftime('%d.%m.%Y в %H:%M')} МСК</code>\n\n"
        f"Выберите понравившийся вид полосы прогресса:",
        reply_markup=get_style_selection_keyboard("classic")
    )

@router.message(StateFilter(CountdownFSM.waiting_datetime), F.text)
async def process_datetime_text(message: Message, state: FSMContext) -> None:
    tz = pytz.timezone(DEFAULT_TIMEZONE_STR)
    target_dt = parse_flexible_datetime(message.text, tz)

    if not target_dt:
        await message.answer(
            "❌ <b>Не распознано время!</b>\nПопробуйте формат <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> или выберите кнопку:",
            reply_markup=get_datetime_presets_keyboard()
        )
        return

    now = datetime.now(tz)
    diff_sec = (target_dt - now).total_seconds()
    if diff_sec <= 10:
        await message.answer("⚠️ Дата события должна быть в будущем (хотя бы на 10 секунд вперед)!")
        return

    await state.update_data(target_dt_iso=target_dt.isoformat(), total_seconds=diff_sec, style_key="classic")
    await state.set_state(CountdownFSM.waiting_style)

    await message.answer(
        f"🎨 <b>[●●●○○○] Шаг 3 из 6 • Стиль шкалы прогресса</b>\n\n"
        f"Финиш: <code>{target_dt.strftime('%d.%m.%Y в %H:%M')} МСК</code>\n\n"
        f"Выберите стиль шкалы прогресса:",
        reply_markup=get_style_selection_keyboard("classic")
    )

@router.callback_query(F.data == "wizard_back_to_datetime")
async def back_to_datetime(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    title = data.get("title", "Событие")
    await state.set_state(CountdownFSM.waiting_datetime)
    await callback.answer()
    await callback.message.edit_text(
        f"📅 <b>[●●○○○○] Шаг 2 из 6 • Время финиша</b>\n\n"
        f"Событие: <b>{html.escape(title)}</b>\n"
        f"Выберите кнопку или отправьте дату:",
        reply_markup=get_datetime_presets_keyboard()
    )

@router.callback_query(StateFilter(CountdownFSM.waiting_style), F.data.startswith("set_style:"))
async def choose_style_handler(callback: CallbackQuery, state: FSMContext) -> None:
    style_key = callback.data.split(":")[1]
    await state.update_data(style_key=style_key)
    await callback.answer("Стиль обновлен!")
    await callback.message.edit_reply_markup(reply_markup=get_style_selection_keyboard(style_key))

@router.callback_query(StateFilter(CountdownFSM.waiting_style), F.data == "wizard_continue_from_style")
async def continue_from_style_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(CountdownFSM.waiting_destination)
    await callback.message.edit_text(
        f"📍 <b>[●●●●○○] Шаг 4 из 6 • Место публикации</b>\n\n"
        f"Куда вы хотите отправить живой виджет таймера?",
        reply_markup=get_destination_keyboard()
    )

@router.callback_query(F.data == "wizard_back_to_style")
async def back_to_style(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current_style = data.get("style_key", "classic")
    await state.set_state(CountdownFSM.waiting_style)
    await callback.answer()
    await callback.message.edit_text(
        f"🎨 <b>[●●●○○○] Шаг 3 из 6 • Стиль шкалы прогресса</b>\n\n"
        f"Выберите стиль шкалы прогресса:",
        reply_markup=get_style_selection_keyboard(current_style)
    )

@router.callback_query(CountdownFSM.waiting_destination, F.data == "dest_private")
async def choose_dest_private(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(target_chat_id=callback.message.chat.id, target_chat_name="Этот диалог (ЛС)")
    await state.set_state(CountdownFSM.waiting_photo)
    await callback.message.edit_text(
        f"🖼 <b>[●●●●●○] Шаг 5 из 6 • Обложка / Баннер</b>\n\n"
        f"Отправьте фотографию для карточки события или нажмите «Без обложки»:",
        reply_markup=get_photo_step_keyboard()
    )

@router.callback_query(CountdownFSM.waiting_destination, F.data == "dest_channel")
async def choose_dest_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(CountdownFSM.waiting_channel_target)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="wizard_back_to_style")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard")],
        ]
    )
    await callback.message.edit_text(
        f"📢 <b>Привязка канала:</b>\n\n"
        f"1. Добавьте бота в канал администратором (права на публикацию и редактирование).\n"
        f"2. <b>Перешлите сюда любой пост из канала</b> или введите <code>@username</code> / цифровой ID:",
        reply_markup=kb
    )

@router.callback_query(F.data == "wizard_back_to_dest")
async def back_to_dest(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CountdownFSM.waiting_destination)
    await callback.answer()
    await callback.message.edit_text(
        "📍 <b>[●●●●○○] Шаг 4 из 6 • Место публикации</b>\nКуда отправить живой таймер?",
        reply_markup=get_destination_keyboard()
    )

@router.message(StateFilter(CountdownFSM.waiting_channel_target))
async def process_channel_target(message: Message, state: FSMContext, bot: Bot) -> None:
    chat_identifier: Union[int, str]

    if message.forward_from_chat:
        chat_identifier = message.forward_from_chat.id
    else:
        raw_text = (message.text or "").strip()
        if not raw_text:
            await message.answer("Пожалуйста, отправьте @юзернейм, ID или перешлите пост из канала.")
            return
        chat_identifier = int(raw_text) if (raw_text.startswith("-") and raw_text[1:].isdigit()) else raw_text

    try:
        chat = await bot.get_chat(chat_identifier)
        member = await bot.get_chat_member(chat.id, bot.id)

        if member.status not in ("administrator", "creator"):
            await message.answer(f"❌ Бот не является администратором в канале «{chat.title}». Назначьте права и попробуйте снова:")
            return

        if hasattr(member, "can_post_messages") and not member.can_post_messages:
            await message.answer(f"⚠️ У бота нет права <b>публиковать посты</b> в «{chat.title}».")
            return

        target_chat_id = chat.id
        target_title = chat.title

    except Exception as exc:
        await message.answer(f"❌ Ошибка проверки канала: <code>{html.escape(str(exc))}</code>\nУбедитесь, что бот уже добавлен.")
        return

    await state.update_data(target_chat_id=target_chat_id, target_chat_name=f"Канал «{target_title}»")
    await state.set_state(CountdownFSM.waiting_photo)

    await message.answer(
        f"✅ Канал <b>«{html.escape(target_title)}»</b> успешно подключен!\n\n"
        f"🖼 <b>[●●●●●○] Шаг 5 из 6 • Обложка / Баннер</b>\n"
        f"Отправьте фотографию или пропустите шаг:",
        reply_markup=get_photo_step_keyboard()
    )

@router.message(StateFilter(CountdownFSM.waiting_photo), F.photo)
async def process_photo_upload(message: Message, state: FSMContext) -> None:
    photo_file_id = message.photo[-1].file_id
    await state.update_data(photo_id=photo_file_id)
    await state.set_state(CountdownFSM.waiting_finish_text)
    await message.answer(
        f"🖼 Обложка сохранена!\n\n"
        f"🎉 <b>[●●●●●●] Шаг 6 из 6 • Финальное послание</b>\n"
        f"Введите текст, который появится в карточке и уведомлении при 00:00:00:",
        reply_markup=get_finish_text_keyboard()
    )

@router.callback_query(CountdownFSM.waiting_photo, F.data == "skip_photo_action")
async def process_photo_skipped(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(photo_id=None)
    await state.set_state(CountdownFSM.waiting_finish_text)
    await callback.message.edit_text(
        f"🎉 <b>[●●●●●●] Шаг 6 из 6 • Финальное послание</b>\n\n"
        f"Введите текст поздравления/завершения или нажмите кнопку по умолчанию:",
        reply_markup=get_finish_text_keyboard()
    )

@router.callback_query(F.data == "wizard_back_to_photo")
async def back_to_photo(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CountdownFSM.waiting_photo)
    await callback.answer()
    await callback.message.edit_text(
        f"🖼 <b>[●●●●●○] Шаг 5 из 6 • Обложка / Баннер</b>\n"
        f"Отправьте фото или продолжите без него:",
        reply_markup=get_photo_step_keyboard()
    )

@router.callback_query(CountdownFSM.waiting_finish_text, F.data == "default_finish_text")
async def finish_text_default(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(finish_text="Событие наступило! Поздравляем! 🎉")
    await show_confirmation_preview(callback, state)

@router.message(StateFilter(CountdownFSM.waiting_finish_text), F.text)
async def finish_text_custom(message: Message, state: FSMContext) -> None:
    await state.update_data(finish_text=message.text.strip())
    await show_confirmation_preview(message, state)

async def show_confirmation_preview(event: Union[Message, CallbackQuery], state: FSMContext) -> None:
    await state.set_state(CountdownFSM.waiting_confirm)
    data = await state.get_data()

    title = data["title"]
    target_dt = datetime.fromisoformat(data["target_dt_iso"])
    style_key = data.get("style_key", "classic")
    dest_name = data.get("target_chat_name", "Диалог")
    has_photo = bool(data.get("photo_id"))
    finish_text = data.get("finish_text", "Событие наступило!")

    sample_bar, _ = generate_progress_bar(100, 75, length=10, style_key=style_key)

    preview_text = (
        f"🔍 <b>ПРЕДПРОСМОТР ТАЙМЕРА ПЕРЕД СТАРТОМ</b>\n"
        f"─────────────────────────────\n"
        f"📌 <b>Событие:</b> {html.escape(title)}\n"
        f"🎯 <b>Цель:</b> <code>{target_dt.strftime('%d.%m.%Y в %H:%M')} МСК</code>\n"
        f"📊 <b>Вид шкалы:</b> <code>[{sample_bar}] 25%</code>\n"
        f"📍 <b>Куда:</b> {html.escape(dest_name)}\n"
        f"🖼 <b>Обложка:</b> {'Прикреплена' if has_photo else 'Без фото'}\n"
        f"💬 <b>Финал:</b> <i>«{html.escape(finish_text)}»</i>\n"
        f"─────────────────────────────\n"
        f"⚡ <i>Отсчет будет идти непрерывно каждые {int(LIVE_UPDATE_INTERVAL_SECONDS)} секунды.</i>"
    )

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(preview_text, reply_markup=get_confirmation_keyboard())
    else:
        await event.answer(preview_text, reply_markup=get_confirmation_keyboard())

@router.callback_query(CountdownFSM.waiting_confirm, F.data == "confirm_launch_timer")
async def confirm_launch_timer(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    await callback.answer("Запускаем живой отсчет...")

    title: str = data["title"]
    target_dt = datetime.fromisoformat(data["target_dt_iso"])
    target_chat_id: Union[int, str] = data["target_chat_id"]
    style_key: str = data.get("style_key", "classic")
    photo_id: Optional[str] = data.get("photo_id")
    finish_text: str = data.get("finish_text", "Событие наступило!")

    tz = pytz.timezone(DEFAULT_TIMEZONE_STR)
    now = datetime.now(tz)
    total_seconds = max(1.0, (target_dt - now).total_seconds())

    initial_card = render_timer_card(
        title=title,
        target_dt=target_dt,
        total_seconds=total_seconds,
        remaining_seconds=total_seconds,
        style_key=style_key,
        is_finished=False,
    )

    is_private_chat = (str(target_chat_id) == str(callback.message.chat.id))

    try:
        if photo_id:
            sent_msg = await bot.send_photo(
                chat_id=target_chat_id,
                photo=photo_id,
                caption=initial_card,
            )
        else:
            sent_msg = await bot.send_message(
                chat_id=target_chat_id,
                text=initial_card,
            )
    except Exception as err:
        await callback.message.edit_text(
            f"❌ <b>Ошибка отправки сообщения:</b>\n<code>{html.escape(str(err))}</code>\n\n"
            f"Убедитесь, что у бота есть права на публикацию в чате/канале.",
            reply_markup=get_main_menu_keyboard()
        )
        return

    timer_key = f"{target_chat_id}_{sent_msg.message_id}"

    if is_private_chat:
        try:
            kb = get_timer_widget_keyboard(timer_key=timer_key, can_stop=True)
            if photo_id:
                await bot.edit_message_caption(
                    chat_id=target_chat_id,
                    message_id=sent_msg.message_id,
                    caption=initial_card,
                    reply_markup=kb,
                )
            else:
                await bot.edit_message_text(
                    chat_id=target_chat_id,
                    message_id=sent_msg.message_id,
                    text=initial_card,
                    reply_markup=kb,
                )
        except Exception:
            pass

    active_timers_registry[timer_key] = {
        "title": title,
        "target_dt": target_dt,
        "chat_id": target_chat_id,
        "msg_id": sent_msg.message_id,
        "creator_id": callback.from_user.id,
    }

    worker_task = asyncio.create_task(
        run_live_timer_worker(
            bot=bot,
            timer_key=timer_key,
            target_chat_id=target_chat_id,
            message_id=sent_msg.message_id,
            title=title,
            target_dt=target_dt,
            total_seconds=total_seconds,
            style_key=style_key,
            finish_text=finish_text,
            has_photo=bool(photo_id),
            creator_user_id=callback.from_user.id,
        ),
        name=f"timer_task_{timer_key}",
    )
    active_tasks[timer_key] = worker_task

    await callback.message.edit_text(
        f"🚀 <b>Таймер успешно запущен!</b>\n\n"
        f"📌 <b>Событие:</b> {html.escape(title)}\n"
        f"🎯 <b>Цель:</b> <code>{target_dt.strftime('%d.%m.%Y в %H:%M')} МСК</code>\n\n"
        f"Виджет уже непрерывно обновляется в реальном времени.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⚡ Создать еще один", callback_data="wizard_start")],
                [InlineKeyboardButton(text="📋 Мои активные таймеры", callback_data="my_active_timers")],
            ]
        )
    )

@router.callback_query(F.data.startswith("stop_timer:"))
async def stop_timer_handler(callback: CallbackQuery, bot: Bot) -> None:
    timer_key = callback.data.split(":", 1)[1]
    timer_info = active_timers_registry.get(timer_key)

    task = active_tasks.get(timer_key)
    if task:
        task.cancel()

    if timer_info:
        chat_id = timer_info["chat_id"]
        msg_id = timer_info["msg_id"]
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=None)
        except Exception:
            pass

    active_tasks.pop(timer_key, None)
    active_timers_registry.pop(timer_key, None)

    await callback.answer("🛑 Отсчет остановлен.", show_alert=True)
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)

async def on_startup(bot: Bot) -> None:
    logger.info("Бот обратного отсчета запущен!")
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="newtimer", description="Создать таймер"),
        BotCommand(command="timers", description="Активные таймеры"),
        BotCommand(command="cancel", description="Отменить создание"),
    ])

async def on_shutdown(bot: Bot) -> None:
    logger.info("Остановка бота... Отмена активных задач.")
    for task in active_tasks.values():
        task.cancel()
    if active_tasks:
        await asyncio.gather(*active_tasks.values(), return_exceptions=True)
    await bot.session.close()

async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот выключен.")
