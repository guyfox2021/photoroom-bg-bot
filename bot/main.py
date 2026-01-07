import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BufferedInputFile,
)
from aiogram.enums import ContentType

from bot.config import BOT_TOKEN, PHOTOROOM_API_KEY, ADMIN_ID
from bot.photoroom import remove_bg
from bot.db import DB

db = DB()


# ===== НАСТРОЙКИ =====
CHANNEL_ID = -1003173585559  # @resident_room
CHANNEL_URL = "https://t.me/resident_room"

FREE_USES = 1
SUB_USES = 1
MAX_USES_PER_MONTH = 50


def free_limit() -> int:
    return FREE_USES + SUB_USES


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator", "restricted")
    except Exception:
        return False


def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🪄 Убрать фон", callback_data="remove_bg")],
            [InlineKeyboardButton(text="💳 Тарифы", callback_data="tariffs")],
        ]
    )


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]
    )


def kb_subscribe() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
            [InlineKeyboardButton(text="📢 Канал", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
        ]
    )


def kb_tariffs() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Посмотреть тарифы", callback_data="tariffs")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
        ]
    )


async def send_tariffs(message: Message):
    plans = db.get_plans()
    if not plans:
        await message.answer(
            "💳 Тарифы пока в разработке.\n\n"
            "Скоро добавим оплату и пакеты обработок ✅",
            reply_markup=kb_back(),
        )
        return

    text = "💳 **Тарифы:**\n\n"
    for p in plans:
        text += (
            f"• **{p['title']}** — {p['price']} {p['currency']}\n"
            f"  Лимит: {p['limit']} / мес\n\n"
        )

    await message.answer(text, parse_mode="Markdown", reply_markup=kb_back())


async def ask_for_photo(message: Message):
    await message.answer(
        "📸 Отправь фото — я уберу фон.\n\n"
        f"✅ 1 фото бесплатно\n"
        f"🔒 2-е фото — за подписку на канал\n"
        f"💳 Дальше — тарифы",
        reply_markup=kb_main(),
    )


async def need_photo_for_remove_bg(callback: CallbackQuery):
    await callback.message.answer(
        "📸 Пришли новое фото, чтобы убрать фон.", reply_markup=kb_back()
    )
    await callback.answer()


async def handle_photo(message: Message, bot: Bot):
    user_id = message.from_user.id

    # событие воронки
    db.log_event(user_id, "photo_received")

    # создаём пользователя (если новый)
    db.ensure_user(user_id)

    # проверка месячного лимита (общий)
    used_month = db.get_month_usage(user_id)
    if used_month >= MAX_USES_PER_MONTH:
        db.log_event(user_id, "limit_month_reached")
        await message.answer(
            "🚫 Месячный лимит исчерпан.\n\n"
            "💳 Нужен тариф, чтобы продолжить.",
            reply_markup=kb_tariffs(),
        )
        return

    # сколько бесплатных попыток использовано
    used_free = db.get_free_usage(user_id)

    # 1-е бесплатно
    if used_free < FREE_USES:
        pass
    # 2-е — за подписку
    elif used_free < free_limit():
        if not await is_subscribed(bot, user_id):
            db.log_event(user_id, "sub_required")
            await message.answer(
                "🔒 Второе фото доступно после подписки на канал.\n\n"
                f"📢 Подпишись: {CHANNEL_URL}\n"
                "После подписки нажми ✅ «Я подписался».",
                reply_markup=kb_subscribe(),
            )
            return
    # дальше — тарифы
    else:
        db.log_event(user_id, "paid_required")
        await message.answer(
            "🚫 Бесплатный лимит исчерпан.\n\n"
            "💳 Ознакомься с тарифами ниже 👇",
            reply_markup=kb_tariffs(),
        )
        return

    # сохраняем file_id последнего фото (для кнопки “убрать фон”)
    largest = message.photo[-1]
    db.set_last_photo(user_id, largest.file_id)

    # обработаем сразу (по UX — “отправил фото → получил результат”)
    await process_remove_bg(message, bot, largest.file_id)


async def process_remove_bg(message: Message, bot: Bot, file_id: str):
    user_id = message.from_user.id
    db.log_event(user_id, "remove_bg_start")

    try:
        file = await bot.get_file(file_id)
        file_bytes = await bot.download_file(file.file_path)
        input_bytes = file_bytes.read()

        output_bytes = await remove_bg(
            api_key=PHOTOROOM_API_KEY,
            image_bytes=input_bytes,
        )

        # учитываем использование
        db.inc_month_usage(user_id)
        db.inc_free_usage(user_id)

        db.log_event(user_id, "remove_bg_success")

        await message.answer_photo(
            photo=BufferedInputFile(output_bytes, filename="result.png"),
            caption="✅ Готово! Фон убран.\n\n"
            "Хочешь ещё? Жми 🪄 «Убрать фон» и пришли новое фото.",
            reply_markup=kb_main(),
        )

    except Exception as e:
        db.log_event(user_id, "remove_bg_error", meta=str(e)[:300])
        await message.answer(
            "⚠️ Не получилось обработать фото. Попробуй другое изображение.",
            reply_markup=kb_main(),
        )


# ===== Админка =====

def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сегодня", callback_data="stats_today")],
            [InlineKeyboardButton(text="7 дней", callback_data="stats_7d")],
            [InlineKeyboardButton(text="Конверсия", callback_data="stats_conv")],
            [InlineKeyboardButton(text="Таблица тарифов", callback_data="stats_plans")],
        ]
    )


async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("📊 Админка:", reply_markup=kb_admin())


async def send_stats_today(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    text = db.stats_today()
    await callback.message.answer(text)
    await callback.answer()


async def send_stats_7d(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    text = db.stats_7d()
    await callback.message.answer(text)
    await callback.answer()


async def send_stats_conv(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    text = db.stats_conversion()
    await callback.message.answer(text)
    await callback.answer()


async def send_stats_plans(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    plans = db.get_plans()
    if not plans:
        await callback.message.answer("Тарифов пока нет.")
        await callback.answer()
        return

    text = "💳 Тарифы (plans):\n\n"
    for p in plans:
        text += f"• {p['title']}: {p['price']} {p['currency']} — {p['limit']}/мес\n"

    await callback.message.answer(text)
    await callback.answer()


# ===== Handlers =====

dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    db.log_event(message.from_user.id, "start")
    db.ensure_user(message.from_user.id)
    await ask_for_photo(message)


@dp.message(F.content_type == ContentType.PHOTO)
async def on_photo(message: Message, bot: Bot):
    await handle_photo(message, bot)


@dp.callback_query(F.data == "remove_bg")
async def cb_remove_bg(callback: CallbackQuery):
    await need_photo_for_remove_bg(callback)


@dp.callback_query(F.data == "tariffs")
async def cb_tariffs(callback: CallbackQuery):
    await send_tariffs(callback.message)
    await callback.answer()


@dp.callback_query(F.data == "back")
async def cb_back(callback: CallbackQuery):
    await ask_for_photo(callback.message)
    await callback.answer()


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    if await is_subscribed(bot, user_id):
        db.log_event(user_id, "sub_ok")
        await callback.message.answer(
            "✅ Подписка подтверждена!\n\n"
            "📸 Теперь пришли фото — уберу фон.",
            reply_markup=kb_main(),
        )
    else:
        db.log_event(user_id, "sub_fail")
        await callback.message.answer(
            "❌ Подписка не найдена.\n\n"
            "Подпишись на канал и нажми ✅ «Я подписался» снова.",
            reply_markup=kb_subscribe(),
        )
    await callback.answer()


@dp.message(F.text.in_({"/admin", "/stats"}))
async def cmd_admin(message: Message):
    await admin_stats(message)


@dp.callback_query(F.data == "stats_today")
async def cb_stats_today(callback: CallbackQuery):
    await send_stats_today(callback)


@dp.callback_query(F.data == "stats_7d")
async def cb_stats_7d(callback: CallbackQuery):
    await send_stats_7d(callback)


@dp.callback_query(F.data == "stats_conv")
async def cb_stats_conv(callback: CallbackQuery):
    await send_stats_conv(callback)


@dp.callback_query(F.data == "stats_plans")
async def cb_stats_plans(callback: CallbackQuery):
    await send_stats_plans(callback)


async def main():
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
