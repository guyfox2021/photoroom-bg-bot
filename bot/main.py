import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.enums import ContentType

from bot.config import BOT_TOKEN, PHOTOROOM_API_KEY
from bot.photoroom import remove_bg


dp = Dispatcher()

# ====== НАСТРОЙКИ ======
CHANNEL_USERNAME = "@resident_room"
CHANNEL_URL = "https://t.me/resident_room"

FREE_USES = 1          # 1 фото бесплатно
SUB_USES = 1           # +1 фото за подписку
MAX_USES_PER_MONTH = 50  # защита от злоупотребления (платную часть добавим позже)
# =======================

# USER_USAGE[user_id] = {"month": "YYYY-MM", "used": int}
USER_USAGE: dict[int, dict] = {}
# LAST_PHOTO[user_id] = file_id
LAST_PHOTO: dict[int, str] = {}


def month_key() -> str:
    return datetime.utcnow().strftime("%Y-%m")


def get_usage(user_id: int) -> dict:
    mk = month_key()
    data = USER_USAGE.get(user_id)
    if not data or data.get("month") != mk:
        data = {"month": mk, "used": 0}
        USER_USAGE[user_id] = data
    return data


def allowed_free_count() -> int:
    return FREE_USES + SUB_USES


def monthly_limit_reached(user_id: int) -> bool:
    return get_usage(user_id)["used"] >= MAX_USES_PER_MONTH


def need_subscription_for_next_use(user_id: int) -> bool:
    used = get_usage(user_id)["used"]
    # 0 -> 1-е фото бесплатно
    # 1 -> 2-е фото только при подписке
    return used >= FREE_USES and used < allowed_free_count()


def is_paid_required(user_id: int) -> bool:
    # после 2 использований (free + за подписку) — платная версия позже
    return get_usage(user_id)["used"] >= allowed_free_count()


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    """
    Для проверки подписки в канале чаще всего нужно,
    чтобы бот был добавлен админом в канал.
    """
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


def kb_subscribe() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Я подписался — проверить", callback_data="check_sub")],
        ]
    )


def kb_after_result() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Повторить (последнее фото)", callback_data="repeat_last")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="status")],
            [InlineKeyboardButton(text="📢 Канал", url=CHANNEL_URL)],
        ]
    )


def kb_paid_soon() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Канал", url=CHANNEL_URL)],
        ]
    )


async def process_photo(bot: Bot, chat_id: int, user_id: int, file_id: str):
    if not PHOTOROOM_API_KEY:
        await bot.send_message(chat_id, "❌ Не найден PHOTOROOM_API_KEY в .env.")
        return

    tg_file = await bot.get_file(file_id)
    file_stream = await bot.download_file(tg_file.file_path)
    image_bytes = file_stream.read()

    result_png = await remove_bg(image_bytes=image_bytes, api_key=PHOTOROOM_API_KEY)
    png = BufferedInputFile(result_png, filename="no_bg.png")

    await bot.send_document(
        chat_id,
        png,
        caption="✅ Готово! PNG с прозрачным фоном.",
        reply_markup=kb_after_result(),
    )

    # Засчитываем использование только после успеха
    usage = get_usage(user_id)
    usage["used"] += 1


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "👋 Привет! Я «Фон OFF».\n\n"
        "Правила:\n"
        "• 1 фото — бесплатно\n"
        "• ещё 1 фото — за подписку на канал\n"
        "• дальше — платная версия (добавим позже)\n\n"
        "Отправь фото 👇",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📢 Канал", url=CHANNEL_URL)]]
        ),
    )


@dp.message(F.content_type == ContentType.PHOTO)
async def handle_photo(m: Message, bot: Bot):
    user_id = m.from_user.id

    if monthly_limit_reached(user_id):
        await m.answer("🚫 Ты достиг месячного лимита. Попробуй в следующем месяце 🙂")
        return

    photo = m.photo[-1]
    LAST_PHOTO[user_id] = photo.file_id

    if need_subscription_for_next_use(user_id):
        subscribed = await is_subscribed(bot, user_id)
        if not subscribed:
            await m.answer(
                "🔒 Бесплатный лимит исчерпан.\n"
                "Подпишись на канал, чтобы получить ещё 1 обработку 👇",
                reply_markup=kb_subscribe(),
            )
            return

    if is_paid_required(user_id):
        await m.answer(
            "💎 Ты использовал бесплатные попытки.\nПлатная версия скоро появится 🙂",
            reply_markup=kb_paid_soon(),
        )
        return

    await m.answer("⏳ Убираю фон…")
    try:
        await process_photo(bot, m.chat.id, user_id, photo.file_id)
    except Exception as e:
        await m.answer(f"❌ Ошибка обработки:\n{e}")


@dp.callback_query(F.data == "repeat_last")
async def cb_repeat_last(c: CallbackQuery, bot: Bot):
    await c.answer()
    user_id = c.from_user.id

    file_id = LAST_PHOTO.get(user_id)
    if not file_id:
        await c.message.answer("🤷‍♂️ Нет последнего фото. Отправь фото сначала.")
        return

    if monthly_limit_reached(user_id):
        await c.message.answer("🚫 Месячный лимит исчерпан.")
        return

    if need_subscription_for_next_use(user_id):
        subscribed = await is_subscribed(bot, user_id)
        if not subscribed:
            await c.message.answer(
                "🔒 Для следующей обработки нужна подписка на канал 👇",
                reply_markup=kb_subscribe(),
            )
            return

    if is_paid_required(user_id):
        await c.message.answer(
            "💎 Бесплатные попытки закончились.\nПлатная версия скоро 🙂",
            reply_markup=kb_paid_soon(),
        )
        return

    await c.message.answer("⏳ Повторяю обработку…")
    try:
        await process_photo(bot, c.message.chat.id, user_id, file_id)
    except Exception as e:
        await c.message.answer(f"❌ Ошибка обработки:\n{e}")


@dp.callback_query(F.data == "status")
async def cb_status(c: CallbackQuery, bot: Bot):
    await c.answer()
    user_id = c.from_user.id
    used = get_usage(user_id)["used"]
    subscribed = await is_subscribed(bot, user_id)

    await c.message.answer(
        "📊 Статус:\n"
        f"• Использовано в этом месяце: {used}/{MAX_USES_PER_MONTH}\n"
        f"• Подписка на канал: {'✅ есть' if subscribed else '❌ нет'}\n\n"
        "Правила:\n"
        "• 1 фото — бесплатно\n"
        "• +1 фото — за подписку\n"
        "• дальше — платная версия позже"
    )


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(c: CallbackQuery, bot: Bot):
    await c.answer()
    subscribed = await is_subscribed(bot, c.from_user.id)
    if subscribed:
        await c.message.answer("✅ Подписка подтверждена! Теперь доступна ещё 1 обработка.")
    else:
        await c.message.answer(
            "❌ Подписку пока не вижу.\n"
            "Убедись, что подписался, и нажми ещё раз.",
            reply_markup=kb_subscribe(),
        )


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not found. Check your .env file.")
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot, polling_timeout=30)


if __name__ == "__main__":
    asyncio.run(main())
