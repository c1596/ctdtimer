import importlib.util
import subprocess
import sys

# Список необходимых внешних библиотек
REQUIRED_DEPENDENCIES = {
    "aiogram": "aiogram>=3.13.0",
    "pytz": "pytz>=2024.1",
}

def auto_install_dependencies() -> None:
    """
    Проверяет наличие требуемых библиотек и автоматически
    устанавливает их через pip, если они отсутствуют в системе.
    """
    missing_packages = []
    for module_name, pip_package in REQUIRED_DEPENDENCIES.items():
        if importlib.util.find_spec(module_name) is None:
            missing_packages.append(pip_package)

    if missing_packages:
        print(f"🔧 Обнаружены неустановленные зависимости: {', '.join(missing_packages)}")
        print("⏳ Запуск автоматической установки через pip...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", *missing_packages],
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            print("✅ Все библиотеки успешно установлены!\n")
        except subprocess.CalledProcessError as err:
            print(f"❌ Не удалось автоматически установить библиотеки: {err}", file=sys.stderr)
            print("Пожалуйста, выполните команду вручную: pip install " + " ".join(missing_packages), file=sys.stderr)
            sys.exit(1)

# Автоматическая инсталляция до импорта сторонних пакетов
auto_install_dependencies()

import asyncio
import html
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Set, Union

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
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# Токен и ID администратора прописаны напрямую без использования os.getenv(),
# что предотвращает сбой TokenValidationError при наличии пустых системных переменных
BOT_TOKEN: str = "8986545593:AAGsw08a8eY182N4LKIrAXKJeh3iVwmx5FA".strip().replace(" ", "").replace("\n", "").replace("\r", "").strip("\"'")
ADMIN_ID: int = 5341904332
DEFAULT_TIMEZONE_STR: str = "Europe/Moscow"
UPDATE_INTERVAL_SECONDS: int = 4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("CountdownBot")

# Множество для сохранения сильных ссылок на активные таски (защита от сборщика мусора GC)
active_tasks: Set[asyncio.Task] = set()

class CountdownFSM(StatesGroup):
    """Машина состояний для комфортного пошагового создания таймера."""
    waiting_title = State()          # Шаг 1: Название события
    waiting_datetime = State()       # Шаг 2: Дата и время
    waiting_destination = State()    # Шаг 3: Выбор: ЛС или Канал
    waiting_channel_target = State() # Шаг 3.1: Юзернейм/ID канала при выборе канала
    waiting_photo = State()          # Шаг 4: Изображение / обложка
    waiting_finish_text = State()    # Шаг 5: Финальный текст после 00:00:00

def generate_smooth_progress_bar(total_duration: float, remaining: float, length: int = 12) -> tuple[str, float]:
    """
    Формирует монолитный эстетичный прогресс-бар:
    Пример: [████████░░░░] 66.7%
    """
    if total_duration <= 0:
        return "█" * length, 100.0

    elapsed = max(0.0, total_duration - remaining)
    fraction = max(0.0, min(1.0, elapsed / total_duration))
    percent = fraction * 100.0

    filled_blocks = int(round(length * fraction))
    filled_blocks = max(0, min(length, filled_blocks))
    empty_blocks = length - filled_blocks

    bar_str = "█" * filled_blocks + "░" * empty_blocks
    return bar_str, percent

def format_countdown_badge(remaining_seconds: int) -> str:
    """Форматирует остаток времени в аккуратный бейдж с выравниванием."""
    if remaining_seconds <= 0:
        return "🏁  00:00:00  •  ФИНИШ!"

    days = remaining_seconds // 86400
    hours = (remaining_seconds % 86400) // 3600
    minutes = (remaining_seconds % 3600) // 60
    seconds = remaining_seconds % 60

    if days > 0:
        return f"⏳  {days:02d} дн.  {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"⏳  {hours:02d}:{minutes:02d}:{seconds:02d}"

def render_timer_card(
    title: str,
    target_dt: datetime,
    total_seconds: float,
    remaining_seconds: float,
    is_finished: bool = False,
    finish_message: Optional[str] = None,
) -> str:
    """
    Генерирует премиальное визуальное сообщение со стильными разделителями,
    моноширинным блоком и статус-баром.
    """
    safe_title = html.escape(title)
    date_str = target_dt.strftime("%d.%m.%Y в %H:%M")

    if is_finished:
        safe_finish = html.escape(finish_message or "Событие наступило!")
        return (
            f"🎉 ━━━━━━━━━━━━━━━━━━━━━━━━━ 🎉\n"
            f"🏆 <b>СОБЫТИЕ НАСТУПИЛО!</b>\n"
            f"🎉 ━━━━━━━━━━━━━━━━━━━━━━━━━ 🎉\n\n"
            f"📌 <b>Событие:</b> <b>{safe_title}</b>\n"
            f"🎯 <b>Финишная дата:</b> <code>{date_str} (МСК)</code>\n\n"
            f"<code>┌──────────────────────────────┐\n"
            f"│  🏁  00:00:00  •  ФИНИШ!     │\n"
            f"└──────────────────────────────┘</code>\n"
            f"<code>[████████████] 100.0%</code>\n\n"
            f"<blockquote>💬 <b>Финальное послание:</b>\n"
            f"<i>{safe_finish}</i></blockquote>"
        )

    bar, percent = generate_smooth_progress_bar(total_seconds, remaining_seconds, length=12)
    badge = format_countdown_badge(int(remaining_seconds))

    return (
        f"◈ ━━━━━━━━━━━━━━━━━━━━━━━━━ ◈\n"
        f"⏳ <b>ОБРАТНЫЙ ОТСЧЕТ В РЕАЛЬНОМ ВРЕМЕНИ</b>\n"
        f"◈ ━━━━━━━━━━━━━━━━━━━━━━━━━ ◈\n\n"
        f"📌 <b>Событие:</b> <b>{safe_title}</b>\n"
        f"🎯 <b>Цель:</b> <code>{date_str} (МСК)</code>\n\n"
        f"⏱ <b>До наступления момента:</b>\n"
        f"<code>┌──────────────────────────────┐\n"
        f"│  {badge:<28}│\n"
        f"└──────────────────────────────┘</code>\n\n"
        f"📊 <b>Шкала завершения:</b>\n"
        f"<code>[{bar}] {percent:>5.1f}%</code>\n\n"
        f"<i>⚡ Обновляется автоматически каждые {UPDATE_INTERVAL_SECONDS} сек.</i>"
    )

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Главная клавиатура приветствия."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏱ Создать новый таймер",
                    callback_data="start_fsm_wizard"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📢 Инструкция по каналу",
                    callback_data="help_channel_info"
                )
            ]
        ]
    )

def get_destination_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора места трансляции таймера."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 В этот диалог (ЛС)", callback_data="dest_private"),
                InlineKeyboardButton(text="📢 В Telegram-канал", callback_data="dest_channel"),
            ],
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_wizard")
            ]
        ]
    )

def get_skip_photo_keyboard() -> InlineKeyboardMarkup:
    """Кнопка пропуска прикрепления обложки."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏩ Без обложки (только текст)", callback_data="skip_photo_action")
            ],
            [
                InlineKeyboardButton(text="❌ Отменить создание", callback_data="cancel_wizard")
            ]
        ]
    )

def get_timer_widget_keyboard(can_stop: bool = True) -> InlineKeyboardMarkup:
    """Инлайн-кнопки под живым таймером."""
    buttons = []
    if can_stop:
        buttons.append([InlineKeyboardButton(text="🛑 Остановить отсчет", callback_data="stop_active_timer")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def run_live_timer_worker(
    bot: Bot,
    target_chat_id: Union[int, str],
    message_id: int,
    title: str,
    target_dt: datetime,
    total_seconds: float,
    finish_text: str,
    has_photo: bool,
    creator_user_id: int,
) -> None:
    """
    Высоконадежный асинхронный воркер живого обновления таймера:
    - Обновляет сообщение раз в 4 секунды
    - Игнорирует TelegramBadRequest "message is not modified"
    - Корректно ждет при TelegramRetryAfter (Flood Control)
    - Завершается при удалении поста или блокировке бота
    """
    logger.info(f"Запущен воркер для чата {target_chat_id} (msg_id: {message_id})")
    tz = pytz.timezone(DEFAULT_TIMEZONE_STR)
    last_rendered_badge = ""

    try:
        while True:
            now = datetime.now(tz)
            remaining_seconds = (target_dt - now).total_seconds()

            # Финальный рубеж: время истекло
            if remaining_seconds <= 0:
                final_card = render_timer_card(
                    title=title,
                    target_dt=target_dt,
                    total_seconds=total_seconds,
                    remaining_seconds=0,
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

                    # Звуковое уведомление
                    await bot.send_message(
                        chat_id=target_chat_id,
                        text=(
                            f"🔔 <b>ВНИМАНИЕ! СОБЫТИЕ НАСТУПИЛО!</b>\n\n"
                            f"🎯 <b>{html.escape(title)}</b>\n"
                            f"🎉 <i>{html.escape(finish_text)}</i>"
                        ),
                        disable_notification=False,
                    )

                    # Если таймер был в канале, сообщим создателю в ЛС
                    if str(target_chat_id) != str(creator_user_id):
                        await bot.send_message(
                            chat_id=creator_user_id,
                            text=(
                                f"✅ <b>Отсчет завершен!</b>\n\n"
                                f"Событие «<b>{html.escape(title)}</b>» в канале <code>{target_chat_id}</code> "
                                f"успешно подошло к концу!"
                            ),
                        )
                except Exception as exc:
                    logger.error(f"Не удалось обновить финал для {target_chat_id}: {exc}")

                break

            current_badge = format_countdown_badge(int(remaining_seconds))
            if current_badge != last_rendered_badge:
                rendered_card = render_timer_card(
                    title=title,
                    target_dt=target_dt,
                    total_seconds=total_seconds,
                    remaining_seconds=remaining_seconds,
                    is_finished=False,
                )

                try:
                    kb = get_timer_widget_keyboard(can_stop=(str(target_chat_id) == str(creator_user_id)))
                    if has_photo:
                        await bot.edit_message_caption(
                            chat_id=target_chat_id,
                            message_id=message_id,
                            caption=rendered_card,
                            reply_markup=kb,
                        )
                    else:
                        await bot.edit_message_text(
                            chat_id=target_chat_id,
                            message_id=message_id,
                            text=rendered_card,
                            reply_markup=kb,
                        )
                    last_rendered_badge = current_badge

                except TelegramRetryAfter as retry_err:
                    logger.warning(f"Flood control ({target_chat_id}): сон {retry_err.retry_after} сек.")
                    await asyncio.sleep(retry_err.retry_after + 1)
                    continue

                except TelegramBadRequest as bad_req:
                    err_msg = str(bad_req).lower()
                    if "message is not modified" in err_msg:
                        pass
                    elif "message to edit not found" in err_msg or "message can't be edited" in err_msg:
                        logger.info(f"Сообщение {message_id} удалено. Остановка таймера.")
                        break
                    else:
                        logger.warning(f"TelegramBadRequest ({target_chat_id}): {bad_req}")

                except (TelegramForbiddenError, TelegramNotFound):
                    logger.warning(f"Бот заблокирован или исключен из {target_chat_id}. Завершение воркера.")
                    break

                except Exception as unexpected_err:
                    logger.error(f"Ошибка цикла в {target_chat_id}: {unexpected_err}")

            await asyncio.sleep(UPDATE_INTERVAL_SECONDS)

    except asyncio.CancelledError:
        logger.info(f"Воркер {target_chat_id}:{message_id} принудительно остановлен.")
    finally:
        logger.info(f"Воркер {target_chat_id}:{message_id} финишировал.")

router = Router()

@router.message(CommandStart())
async def cmd_start_handler(message: Message, state: FSMContext) -> None:
    """Приветственное сообщение со стильным дизайном и быстрым доступом."""
    await state.clear()
    welcome_text = (
        f"✨ <b>ПРИВЕТСТВУЮ, {html.escape(message.from_user.first_name).upper()}!</b> ✨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Я — профессиональный бот <b>живого обратного отсчета</b>.\n\n"
        f"🚀 <b>Главные фичи:</b>\n"
        f"• <b>Живой таймер:</b> обновление каждые <code>{UPDATE_INTERVAL_SECONDS} секунды</code> без лагов\n"
        f"• <b>Каналы и ЛС:</b> публикация в личные сообщения или в ваш Telegram-канал\n"
        f"• <b>Стильный прогресс-бар:</b> аккуратная моноширинная шкала <code>[████░░░░]</code>\n"
        f"• <b>Медиа-баннеры:</b> поддержка фотографий и постеров событий\n"
        f"• <b>Финал:</b> победное сообщение и оповещение подписчиков в <code>00:00:00</code>\n\n"
        f"Нажмите кнопку ниже, чтобы сконфигурировать ваш таймер!"
    )
    await message.answer(welcome_text, reply_markup=get_main_menu_keyboard())

@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel_wizard")
async def cancel_wizard_handler(event: Union[Message, CallbackQuery], state: FSMContext) -> None:
    """Сброс мастера настройки."""
    current_state = await state.get_state()
    await state.clear()

    text = "🛑 <b>Мастер создания таймера отменен.</b>\nВы можете начать заново в любой момент через /start или /newtimer."
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.edit_text(text)
    else:
        await event.answer(text)

@router.callback_query(F.data == "help_channel_info")
async def channel_instructions_handler(callback: CallbackQuery) -> None:
    """Инструкция по публикации в Telegram-канал."""
    await callback.answer()
    help_text = (
        f"📢 <b>КАК ЗАПУСТИТЬ ТАЙМЕР В СВОЕМ КАНАЛЕ?</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"1️⃣ Откройте настройки вашего канала.\n"
        f"2️⃣ Добавьте этого бота в раздел <b>«Администраторы»</b>.\n"
        f"3️⃣ Предоставьте боту права:\n"
        f"   • <b>Публикация сообщений (Post Messages)</b>\n"
        f"   • <b>Редактирование сообщений (Edit Messages)</b>\n"
        f"4️⃣ Нажмите <b>«Создать новый таймер»</b> и выберите вариант <b>«В Telegram-канал»</b>.\n"
        f"5️⃣ Отправьте боту <code>@юзернейм_канала</code> или его ID.\n\n"
        f"<i>Бот проверит права, создаст живой виджет и будет обновлять его в канале!</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏱ Начать создание", callback_data="start_fsm_wizard")],
            [InlineKeyboardButton(text="◀️ В главное меню", callback_data="back_to_main_menu")]
        ]
    )
    await callback.message.edit_text(help_text, reply_markup=kb)

@router.callback_query(F.data == "back_to_main_menu")
async def back_to_menu_handler(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат на главный экран."""
    await callback.answer()
    await state.clear()
    welcome_text = (
        f"✨ <b>ГЛАВНОЕ МЕНЮ ОБРАТНОГО ОТСЧЕТА</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Выберите действие для продолжения:"
    )
    await callback.message.edit_text(welcome_text, reply_markup=get_main_menu_keyboard())

@router.callback_query(F.data == "start_fsm_wizard")
@router.message(Command("newtimer"))
async def start_fsm_wizard_handler(event: Union[Message, CallbackQuery], state: FSMContext) -> None:
    """Старт FSM-мастера создания таймера."""
    await state.set_state(CountdownFSM.waiting_title)
    step1_text = (
        f"📝 <b>[▰▱▱▱▱] Шаг 1 из 5 • Название события</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Введите название вашего события.\n\n"
        f"<i>💡 Примеры:\n"
        f"• «Новый Год 2027 🍾»\n"
        f"• «Гранд-Открытие Сервера 🚀»\n"
        f"• «До Дня Рождения Кати 🎂»</i>\n\n"
        f"Отправьте текст в ответном сообщении или /cancel для отмены."
    )
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.edit_text(step1_text)
    else:
        await event.answer(step1_text)

@router.message(StateFilter(CountdownFSM.waiting_title), F.text)
async def process_title_step(message: Message, state: FSMContext) -> None:
    """Обработка названия события."""
    title = message.text.strip()
    if len(title) > 80:
        await message.answer("⚠️ Название слишком длинное. Пожалуйста, сократите его до 80 символов:")
        return

    await state.update_data(title=title)
    await state.set_state(CountdownFSM.waiting_datetime)

    tz = pytz.timezone(DEFAULT_TIMEZONE_STR)
    sample_dt = (datetime.now(tz) + timedelta(days=2, hours=4)).strftime("%d.%m.%Y %H:%M")

    step2_text = (
        f"📅 <b>[▰▰▱▱▱] Шаг 2 из 5 • Дата и время финиша</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Событие: <b>{html.escape(title)}</b>\n\n"
        f"Введите дату и время по московскому времени (МСК) строго в формате:\n"
        f"<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n\n"
        f"<i>💡 Пример ввода:</i> <code>{sample_dt}</code>"
    )
    await message.answer(step2_text)

@router.message(StateFilter(CountdownFSM.waiting_datetime), F.text)
async def process_datetime_step(message: Message, state: FSMContext) -> None:
    """Валидация введенной даты и времени."""
    text = message.text.strip()
    tz = pytz.timezone(DEFAULT_TIMEZONE_STR)

    try:
        naive_dt = datetime.strptime(text, "%d.%m.%Y %H:%M")
        target_dt = tz.localize(naive_dt)
    except ValueError:
        await message.answer(
            f"❌ <b>Неверный формат даты!</b>\n\n"
            f"Пожалуйста, соблюдайте шаблон: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
            f"<i>Например:</i> <code>31.12.2026 23:59</code>"
        )
        return

    now = datetime.now(tz)
    diff_sec = (target_dt - now).total_seconds()
    if diff_sec <= 20:
        await message.answer(
            f"⚠️ <b>Время события должно быть в будущем!</b>\n"
            f"Укажите время хотя бы на 20 секунд позже текущего момента."
        )
        return

    await state.update_data(
        target_dt_iso=target_dt.isoformat(),
        total_seconds=diff_sec,
    )
    await state.set_state(CountdownFSM.waiting_destination)

    step3_text = (
        f"📍 <b>[▰▰▰▱▱] Шаг 3 из 5 • Место публикации</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Где именно запустить живой таймер?"
    )
    await message.answer(step3_text, reply_markup=get_destination_keyboard())

@router.callback_query(CountdownFSM.waiting_destination, F.data == "dest_private")
async def choose_dest_private_handler(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор публикации в ЛС."""
    await callback.answer()
    await state.update_data(target_chat_id=callback.message.chat.id)
    await state.set_state(CountdownFSM.waiting_photo)

    step4_text = (
        f"🖼 <b>[▰▰▰▰▱] Шаг 4 из 5 • Обложка / Баннер</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Отправьте фотографию для создания красивой карточки события.\n\n"
        f"<i>Если фото не требуется, нажмите кнопку ниже:</i>"
    )
    await callback.message.edit_text(step4_text, reply_markup=get_skip_photo_keyboard())

@router.callback_query(CountdownFSM.waiting_destination, F.data == "dest_channel")
async def choose_dest_channel_handler(callback: CallbackQuery, state: FSMContext) -> None:
    """Выбор публикации в Telegram-канал."""
    await callback.answer()
    await state.set_state(CountdownFSM.waiting_channel_target)

    prompt_channel = (
        f"📢 <b>[▰▰▰▱▱] Привязка Telegram-канала</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"1. Добавьте бота в канал как <b>Администратора</b>.\n"
        f"2. Предоставьте права: <b>Публикация</b> и <b>Редактирование</b>.\n"
        f"3. Отправьте сюда <code>@юзернейм_канала</code> или его цифровой <code>ID</code> "
        f"(например, <code>-1001234567890</code>):"
    )
    await callback.message.edit_text(prompt_channel)

@router.message(StateFilter(CountdownFSM.waiting_channel_target), F.text)
async def process_channel_input_step(message: Message, state: FSMContext, bot: Bot) -> None:
    """Проверка прав бота в указанном канале."""
    raw_channel = message.text.strip()
    target_chat_spec: Union[int, str] = int(raw_channel) if (raw_channel.startswith("-") and raw_channel[1:].isdigit()) else raw_channel

    try:
        chat = await bot.get_chat(target_chat_spec)
        member = await bot.get_chat_member(chat.id, bot.id)

        if member.status not in ("administrator", "creator"):
            await message.answer(
                f"❌ <b>Бот не является администратором в канале «{chat.title}»!</b>\n\n"
                f"Назначьте бота администратором и повторите отправку юзернейма/ID."
            )
            return

        if hasattr(member, "can_post_messages") and not member.can_post_messages:
            await message.answer(
                f"⚠️ У бота нет права <b>публиковать посты</b> в канале «{chat.title}».\n"
                f"Выдайте соответствующее право в настройках прав администратора."
            )
            return

        if hasattr(member, "can_edit_messages") and not member.can_edit_messages:
            await message.answer(
                f"⚠️ У бота нет права <b>редактировать чужие посты</b> в канале «{chat.title}».\n"
                f"Включите право «Редактирование сообщений»."
            )
            return

        target_chat_id = chat.id
        channel_title = chat.title

    except Exception as exc:
        await message.answer(
            f"❌ <b>Не удалось подключиться к каналу:</b>\n"
            f"<code>{html.escape(str(exc))}</code>\n\n"
            f"Убедитесь, что бот уже добавлен в канал и юзернейм указан верно."
        )
        return

    await state.update_data(target_chat_id=target_chat_id)
    await state.set_state(CountdownFSM.waiting_photo)

    step4_text = (
        f"✅ <b>Канал «{html.escape(channel_title)}» успешно подключен!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🖼 <b>[▰▰▰▰▱] Шаг 4 из 5 • Обложка / Баннер</b>\n\n"
        f"Отправьте фотографию для поста в канал или нажмите кнопку <b>«⏩ Без обложки»</b>:"
    )
    await message.answer(step4_text, reply_markup=get_skip_photo_keyboard())

@router.message(StateFilter(CountdownFSM.waiting_photo), F.photo)
async def process_photo_received(message: Message, state: FSMContext) -> None:
    """Пользователь загрузил изображение."""
    photo_file_id = message.photo[-1].file_id
    await state.update_data(photo_id=photo_file_id)
    await state.set_state(CountdownFSM.waiting_finish_text)

    step5_text = (
        f"🎉 <b>[▰▰▰▰▰] Шаг 5 из 5 • Финальное послание</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Введите текст, который появится в момент <code>00:00:00</code>:\n\n"
        f"<i>💡 Пример: «Ура! С Новым Годом! 🍾 Настало время открывать шампанское!»</i>"
    )
    await message.answer(step5_text)

@router.callback_query(CountdownFSM.waiting_photo, F.data == "skip_photo_action")
async def process_photo_skipped(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь пропустил фото."""
    await callback.answer()
    await state.update_data(photo_id=None)
    await state.set_state(CountdownFSM.waiting_finish_text)

    step5_text = (
        f"🎉 <b>[▰▰▰▰▰] Шаг 5 из 5 • Финальное послание</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Введите текст, который появится в момент <code>00:00:00</code>:\n\n"
        f"<i>💡 Пример: «Время пришло! Релиз успешно состоялся!»</i>"
    )
    await callback.message.edit_text(step5_text)

@router.message(StateFilter(CountdownFSM.waiting_finish_text), F.text)
async def process_finish_text_step(message: Message, state: FSMContext, bot: Bot) -> None:
    """Получение финального сообщения и запуск живого виджета."""
    finish_text = message.text.strip()
    data = await state.get_data()
    await state.clear()

    title: str = data["title"]
    target_dt = datetime.fromisoformat(data["target_dt_iso"])
    target_chat_id: Union[int, str] = data["target_chat_id"]
    photo_id: Optional[str] = data.get("photo_id")

    tz = pytz.timezone(DEFAULT_TIMEZONE_STR)
    now = datetime.now(tz)
    total_seconds = max(1.0, (target_dt - now).total_seconds())

    initial_card = render_timer_card(
        title=title,
        target_dt=target_dt,
        total_seconds=total_seconds,
        remaining_seconds=total_seconds,
        is_finished=False,
    )

    is_private_chat = (str(target_chat_id) == str(message.chat.id))
    kb = get_timer_widget_keyboard(can_stop=is_private_chat)

    # Публикация первого кадра
    try:
        if photo_id:
            sent_msg = await bot.send_photo(
                chat_id=target_chat_id,
                photo=photo_id,
                caption=initial_card,
                reply_markup=kb,
            )
        else:
            sent_msg = await bot.send_message(
                chat_id=target_chat_id,
                text=initial_card,
                reply_markup=kb,
            )
    except Exception as send_err:
        await message.answer(
            f"❌ <b>Ошибка при отправке в чат/канал:</b>\n"
            f"<code>{html.escape(str(send_err))}</code>\n\n"
            f"Убедитесь, что бот имеет достаточные права в указанном чате."
        )
        return

    # Запуск фонового асинхронного таска
    task = asyncio.create_task(
        run_live_timer_worker(
            bot=bot,
            target_chat_id=target_chat_id,
            message_id=sent_msg.message_id,
            title=title,
            target_dt=target_dt,
            total_seconds=total_seconds,
            finish_text=finish_text,
            has_photo=bool(photo_id),
            creator_user_id=message.from_user.id,
        ),
        name=f"timer_{target_chat_id}_{sent_msg.message_id}",
    )
    active_tasks.add(task)
    task.add_done_callback(active_tasks.discard)

    # Подтверждение пользователю
    dest_label = "в этот диалог" if is_private_chat else f"в канал (<code>{target_chat_id}</code>)"
    await message.answer(
        f"🚀 <b>ТАЙМЕР УСПЕШНО ЗАПУЩЕН!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Событие:</b> {html.escape(title)}\n"
        f"🎯 <b>Финиш:</b> <code>{target_dt.strftime('%d.%m.%Y в %H:%M')} (МСК)</code>\n"
        f"📍 <b>Место:</b> {dest_label}\n"
        f"🔄 <b>Обновление:</b> каждые {UPDATE_INTERVAL_SECONDS} секунды.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⏱ Создать еще один", callback_data="start_fsm_wizard")]]
        )
    )

@router.callback_query(F.data == "stop_active_timer")
async def stop_active_timer_handler(callback: CallbackQuery) -> None:
    """Ручная остановка таймера в ЛС."""
    chat_id = callback.message.chat.id
    msg_id = callback.message.message_id

    stopped = False
    for task in list(active_tasks):
        if task.get_name() == f"timer_{chat_id}_{msg_id}":
            task.cancel()
            stopped = True
            break

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if stopped:
        await callback.answer("🛑 Отсчет остановлен.", show_alert=True)
        await callback.message.reply("⏹ Этот таймер был остановлен создателем.")
    else:
        await callback.answer("Этот таймер уже завершен или неактивен.", show_alert=False)

async def on_startup(bot: Bot) -> None:
    """Оповещение администратора при запуске."""
    logger.info("Бот обратного отсчета успешно стартовал!")
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🚀 <b>БОТ ОБРАТНОГО ОТСЧЕТА ЗАПУЩЕН</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ Автоустановка зависимостей отработала штатно.\n"
                f"⚡ Готов к работе в ЛС и Telegram-каналах!"
            ),
        )
    except Exception as exc:
        logger.warning(f"Не удалось отправить уведомление админу {ADMIN_ID}: {exc}")

async def on_shutdown(bot: Bot) -> None:
    """Корректная остановка всех тасков."""
    logger.info("Остановка бота... Отмена активных воркеров.")
    for task in list(active_tasks):
        task.cancel()
    if active_tasks:
        await asyncio.gather(*active_tasks, return_exceptions=True)
    await bot.session.close()
    logger.info("Бот полностью выключен.")

async def main() -> None:
    """Главная точка входа приложения."""
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
        logger.info("Бот выключен пользователем.")
