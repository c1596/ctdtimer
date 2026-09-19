import asyncio
import logging
import sys
from datetime import datetime
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
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ
# ---------------------------------------------------------------------------
BOT_TOKEN = "8986545593:AAGsw08a8eY182N4LKIrAXKJeh3iVwmx5FA"
ADMIN_ID = 5341904332
TIMEZONE = pytz.timezone("Europe/Moscow")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Множество для удержания сильных ссылок на фоновые таски (защита от Garbage Collector)
active_tasks: set[asyncio.Task] = set()

# ---------------------------------------------------------------------------
# FSM СОСТОЯНИЯ
# ---------------------------------------------------------------------------
class CountdownFSM(StatesGroup):
    name = State()
    target_time = State()
    destination = State()     # Выбор: ЛС или Канал
    channel_id = State()      # Ввод @юзернейма / ID канала
    photo = State()
    final_text = State()

router = Router()

# ---------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ФОРМАТИРОВАНИЯ
# ---------------------------------------------------------------------------
def generate_progress_bar(total_seconds: float, remaining_seconds: float, length: int = 10) -> str:
    """Генерирует визуальный индикатор прогресса: ▰▰▰▰▱▱▱▱ 45.0%"""
    if total_seconds <= 0:
        percent = 100.0
    else:
        progress = max(0.0, min(1.0, (total_seconds - remaining_seconds) / total_seconds))
        percent = progress * 100.0

    filled = int(round(length * (percent / 100.0)))
    bar = "▰" * filled + "▱" * (length - filled)
    return f"{bar} {percent:.1f}%"


def format_countdown(remaining_seconds: int) -> str:
    """Форматирует остаток времени в моноширинный вид."""
    if remaining_seconds <= 0:
        return "🏁 00:00:00"

    days = remaining_seconds // 86400
    hours = (remaining_seconds % 86400) // 3600
    minutes = (remaining_seconds % 3600) // 60
    seconds = remaining_seconds % 60

    if days > 0:
        return f"⏳ [ {days:02d} дн. {hours:02d}:{minutes:02d}:{seconds:02d} ]"
    return f"⏳ [ {hours:02d}:{minutes:02d}:{seconds:02d} ]"


def build_timer_card(name: str, target_dt: datetime, total_sec: float, remaining_sec: float) -> str:
    """Собирает тело сообщения таймера с моноширинными элементами."""
    time_str = format_countdown(int(remaining_sec))
    bar_str = generate_progress_bar(total_sec, remaining_sec)
    date_formatted = target_dt.strftime("%d.%m.%Y в %H:%M")

    return (
        f"🎯 <b>Событие:</b> {name}\n"
        f"📅 <b>Дедлайн:</b> <code>{date_formatted} (МСК)</code>\n\n"
        f"<code>{time_str}</code>\n"
        f"<code>{bar_str}</code>\n\n"
        f"<i>Обновление каждые 4 секунды...</i>"
    )

# ---------------------------------------------------------------------------
# ФОНОВЫЙ ЦИКЛ ОБНОВЛЕНИЯ (WORKER)
# ---------------------------------------------------------------------------
async def timer_worker(
    bot: Bot,
    target_chat_id: int | str,
    message_id: int,
    name: str,
    target_dt: datetime,
    total_seconds: float,
    final_text: str,
    has_photo: bool,
    creator_id: int,
):
    """Асинхронный воркер, обновляющий сообщение каждые 4 секунды."""
    logger.info(f"Запущен таймер для чата {target_chat_id}, сообщение {message_id}")

    try:
        while True:
            now = datetime.now(TIMEZONE)
            remaining = (target_dt - now).total_seconds()

            # Финальная стадия: дедлайн наступил
            if remaining <= 0:
                final_caption = (
                    f"🎉 <b>СОБЫТИЕ НАСТУПИЛО!</b>\n\n"
                    f"🎯 <b>Событие:</b> {name}\n"
                    f"<code>🏁 [ 00:00:00 ]</code>\n"
                    f"<code>▰▰▰▰▰▰▰▰▰▰ 100.0%</code>\n\n"
                    f"💬 <b>Итог:</b> {final_text}"
                )

                try:
                    if has_photo:
                        await bot.edit_message_caption(
                            chat_id=target_chat_id,
                            message_id=message_id,
                            caption=final_caption,
                        )
                    else:
                        await bot.edit_message_text(
                            chat_id=target_chat_id,
                            message_id=message_id,
                            text=final_caption,
                        )

                    # Звуковое победное уведомление
                    await bot.send_message(
                        chat_id=target_chat_id,
                        text=f"🔔 <b>Внимание! Время вышло!</b>\n\n🎯 <b>{name}</b>\n{final_text}",
                        disable_notification=False,
                    )

                    # Если таймер был в канале, уведомим создателя в ЛС
                    if str(target_chat_id) != str(creator_id):
                        await bot.send_message(
                            chat_id=creator_id,
                            text=f"✅ Таймер «<b>{name}</b>» в канале <code>{target_chat_id}</code> успешно завершен!",
                        )
                except Exception as e:
                    logger.error(f"Ошибка при публикации финала: {e}")
                break

            # Обычный кадр обновления
            text = build_timer_card(name, target_dt, total_seconds, remaining)

            try:
                if has_photo:
                    await bot.edit_message_caption(
                        chat_id=target_chat_id,
                        message_id=message_id,
                        caption=text,
                    )
                else:
                    await bot.edit_message_text(
                        chat_id=target_chat_id,
                        message_id=message_id,
                        text=text,
                    )
            except TelegramBadRequest as e:
                err_msg = str(e).lower()
                if "message is not modified" in err_msg:
                    pass
                elif "message to edit not found" in err_msg:
                    logger.warning(f"Сообщение {message_id} удалено. Остановка воркера.")
                    break
                else:
                    logger.warning(f"TelegramBadRequest: {e}")
            except TelegramRetryAfter as e:
                logger.warning(f"Flood control: ожидание {e.retry_after} сек.")
                await asyncio.sleep(e.retry_after)
                continue
            except (TelegramForbiddenError, TelegramNotFound):
                logger.warning(f"Бот исключен из чата {target_chat_id}. Остановка воркера.")
                break
            except Exception as e:
                logger.error(f"Непредвиденная ошибка в таймере: {e}")

            await asyncio.sleep(4)

    except asyncio.CancelledError:
        logger.info(f"Воркер для чата {target_chat_id} был отменен.")
    finally:
        logger.info(f"Воркер {message_id} завершил свою работу.")

# ---------------------------------------------------------------------------
# ОБРАБОТЧИКИ FSM (СОЗДАНИЕ ТАЙМЕРА)
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Создать таймер", callback_data="create_timer")]
        ]
    )
    await message.answer(
        "👋 <b>Привет! Я бот обратного отсчета.</b>\n\n"
        "Я умею запускать живые таймеры с прогресс-баром в личных сообщениях или прямо в вашем Telegram-канале.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "create_timer")
async def start_fsm(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(CountdownFSM.name)
    await callback.message.answer("📝 <b>Шаг 1 из 5:</b> Введите название события:\n<i>(Например: До Нового Года, До Релиза игры)</i>")


@router.message(CountdownFSM.name)
async def process_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) > 64:
        await message.answer("⚠️ Название слишком длинное. Введите до 64 символов:")
        return

    await state.update_data(name=name)
    await state.set_state(CountdownFSM.target_time)
    await message.answer(
        "📅 <b>Шаг 2 из 5:</b> Введите дату и время окончания события.\n"
        "Формат: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
        "<i>Часовой пояс по умолчанию: Москва (Europe/Moscow)</i>\n\n"
        "Пример: <code>31.12.2026 23:59</code>"
    )


@router.message(CountdownFSM.target_time)
async def process_time(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        dt_naive = datetime.strptime(text, "%d.%m.%Y %H:%M")
        dt_aware = TIMEZONE.localize(dt_naive)
    except ValueError:
        await message.answer(
            "❌ <b>Неверный формат даты!</b>\n"
            "Используйте строгий шаблон <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> (например, <code>01.01.2027 00:00</code>):"
        )
        return

    now = datetime.now(TIMEZONE)
    if dt_aware <= now:
        await message.answer("⚠️ Указанное время уже прошло! Введите дату и время в будущем:")
        return

    await state.update_data(target_dt=dt_aware.isoformat())
    await state.set_state(CountdownFSM.destination)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 В этом чате (ЛС)", callback_data="dest_pm"),
                InlineKeyboardButton(text="📢 В Telegram-канале", callback_data="dest_channel"),
            ]
        ]
    )
    await message.answer("📍 <b>Шаг 3 из 5:</b> Где опубликовать и обновлять таймер?", reply_markup=kb)


@router.callback_query(CountdownFSM.destination, F.data == "dest_pm")
async def choose_dest_pm(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(target_chat_id=callback.message.chat.id)
    await state.set_state(CountdownFSM.photo)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить (Без фото)", callback_data="skip_photo")]]
    )
    await callback.message.answer("🖼 <b>Шаг 4 из 5:</b> Прикрепите изображение для фона или нажмите кнопку ниже:", reply_markup=kb)


@router.callback_query(CountdownFSM.destination, F.data == "dest_channel")
async def choose_dest_channel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(CountdownFSM.channel_id)
    await callback.message.answer(
        "📢 <b>Привязка канала:</b>\n\n"
        "1. Добавьте этого бота в ваш канал в качестве <b>Администратора</b>.\n"
        "2. Убедитесь, что боту выданы права: <b>Публикация сообщений</b> и <b>Редактирование сообщений</b>.\n"
        "3. Отправьте сюда <code>@username_канала</code> или его цифровой <code>ID</code> (например, <code>-1001234567890</code>):"
    )


@router.message(CountdownFSM.channel_id)
async def process_channel_binding(message: Message, state: FSMContext, bot: Bot):
    channel_raw = message.text.strip()
    target_chat = int(channel_raw) if (channel_raw.startswith("-") and channel_raw[1:].isdigit()) else channel_raw

    # Валидация прав бота в канале
    try:
        chat = await bot.get_chat(target_chat)
        member = await bot.get_chat_member(chat.id, bot.id)

        if member.status not in ("administrator", "creator"):
            await message.answer(
                "❌ <b>Бот не является администратором в этом канале!</b>\n"
                "Сделайте бота админом канала с правом публикации и редактирования постов, затем попробуйте снова."
            )
            return

        if hasattr(member, "can_post_messages") and not member.can_post_messages:
            await message.answer("⚠️ У бота нет права <b>публиковать сообщения</b> в этом канале. Измените права и попробуйте снова.")
            return

        if hasattr(member, "can_edit_messages") and not member.can_edit_messages:
            await message.answer("⚠️ У бота нет права <b>редактировать сообщения</b> в этом канале. Измените права и попробуйте снова.")
            return

        target_chat_id = chat.id

    except Exception as err:
        await message.answer(
            f"❌ <b>Не удалось подключиться к каналу:</b>\n<code>{err}</code>\n\n"
            f"Проверьте, что имя/ID указано верно и бот уже добавлен в канал."
        )
        return

    await state.update_data(target_chat_id=target_chat_id)
    await state.set_state(CountdownFSM.photo)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить (Без фото)", callback_data="skip_photo")]]
    )
    await message.answer(
        f"✅ Канал <b>{chat.title}</b> успешно привязан!\n\n"
        "🖼 <b>Шаг 4 из 5:</b> Прикрепите изображение для таймера или нажмите кнопку ниже:",
        reply_markup=kb,
    )


@router.message(CountdownFSM.photo, F.photo)
async def process_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(photo_id=photo_id)
    await state.set_state(CountdownFSM.final_text)
    await message.answer("💬 <b>Шаг 5 из 5:</b> Введите финальный текст, который появится по завершении таймера:\n<i>(Например: Ура! Сервер открыт, залетайте!)</i>")


@router.callback_query(CountdownFSM.photo, F.data == "skip_photo")
async def skip_photo(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(photo_id=None)
    await state.set_state(CountdownFSM.final_text)
    await callback.message.answer("💬 <b>Шаг 5 из 5:</b> Введите финальный текст, который появится по завершении таймера:")


@router.message(CountdownFSM.final_text)
async def process_final_text(message: Message, state: FSMContext, bot: Bot):
    final_text = message.text.strip()
    data = await state.get_data()
    await state.clear()

    name = data["name"]
    target_dt = datetime.fromisoformat(data["target_dt"])
    target_chat_id = data["target_chat_id"]
    photo_id = data.get("photo_id")

    now = datetime.now(TIMEZONE)
    total_seconds = (target_dt - now).total_seconds()
    initial_text = build_timer_card(name, target_dt, total_seconds, total_seconds)

    # Публикация первого сообщения в целевой чат/канал
    try:
        if photo_id:
            sent_msg = await bot.send_photo(
                chat_id=target_chat_id,
                photo=photo_id,
                caption=initial_text,
            )
        else:
            sent_msg = await bot.send_message(
                chat_id=target_chat_id,
                text=initial_text,
            )
    except Exception as e:
        await message.answer(f"❌ <b>Ошибка при отправке сообщения в целевой чат:</b>\n<code>{e}</code>")
        return

    # Запуск асинхронного воркера
    task = asyncio.create_task(
        timer_worker(
            bot=bot,
            target_chat_id=target_chat_id,
            message_id=sent_msg.message_id,
            name=name,
            target_dt=target_dt,
            total_seconds=total_seconds,
            final_text=final_text,
            has_photo=bool(photo_id),
            creator_id=message.from_user.id,
        )
    )
    active_tasks.add(task)
    task.add_done_callback(active_tasks.discard)

    await message.answer(
        "🚀 <b>Таймер успешно запущен!</b>\n"
        f"Куда отправлен: <code>{target_chat_id}</code>\n"
        "Он будет обновляться каждые 4 секунды вплоть до наступления события."
    )

# ---------------------------------------------------------------------------
# ЗАПУСК БОТА
# ---------------------------------------------------------------------------
async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот запущен и готов к работе.")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        # Корректная остановка всех фоновых задач при завершении процесса
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
