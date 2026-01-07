import asyncio
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.enums import ContentType

from bot.config import BOT_TOKEN, PHOTOROOM_API_KEY, ADMIN_ID
from bot.photoroom import remove_bg
from bot.db import DB

# ========= CONFIG =========
CHANNEL_ID = -1003173585559
CHANNEL_URL = "https://t.me/resident_room"

# free rules:
# 0 used this month -> free
# 1 used this month -> requires subscription
# >=2 -> show tariffs

db = DB()
dp = Dispatcher()


# ========= KEYBOARDS =========
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
            [InlineKeyboardButton(text="📢 Канал", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
        ]
    )


# ========= HELPERS =========
async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator", "restricted")
    except Exception as e:
        # важно: отличаем "не подписан" от ошибок доступа/сети
        await db.log_event(user_id=user_id, event="check_sub_error", meta=str(e)[:300])
        return False


async def send_tariffs(message: Message):
    plans = await db.list_plans()
    if not plans:
        await message.answer(
            "💳 Тарифы пока не настроены.\n\n"
            "Скоро добавим оплату и пакеты обработок ✅",
            reply_markup=kb_back(),
        )
        return

    text = "💳 Тарифы:\n\n"
    for p in plans:
        # columns in db.py: code, title, price_uah, credits, is_subscription, is_active
        text += f"• {p['title']} — {p['price_uah']} грн — {p['credits']} фото\n"

    await message.answer(text, reply_markup=kb_back())


async def ask_for_photo(message: Message):
    await message.answer(
        "📸 Отправь фото — я уберу фон.\n\n"
        "✅ 1 фото бесплатно\n"
        "🔒 2-е фото — за подписку на канал\n"
        "💳 Дальше — тарифы",
        reply_markup=kb_main(),
    )


async def process_image(message: Message, bot: Bot, file_id: str):
    user_id = message.from_user.id

    await db.touch_user(user_id)
    used = await db.get_used_this_month(user_id)

    # 1) free
    if used == 0:
        pass
    # 2) subscription required
    elif used == 1:
        if not await is_subscribed(bot, user_id):
            await db.log_event(user_id=user_id, event="sub_required", meta="used==1")
            await message.answer(
                "🔒 Второе фото доступно после подписки на канал.\n\n"
                f"📢 Подпишись: {CHANNEL_URL}\n"
                "После подписки нажми ✅ «Я подписался».",
                reply_markup=kb_subscribe(),
            )
            return
    # 3) tariffs
    else:
        await db.log_event(user_id=user_id, event="paid_required", meta=f"used={used}")
        await message.answer(
            "🚫 Бесплатный лимит исчерпан.\n\n"
            "💳 Ознакомься с тарифами 👇",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Тарифы", callback_data="tariffs")],
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
                ]
            ),
        )
        return

    await db.log_event(user_id=user_id, event="remove_bg_start")
    await message.answer("⏳ Обрабатываю…")

    try:
        tg_file = await bot.get_file(file_id)
        file_bytes = await bot.download_file(tg_file.file_path)
        input_bytes = file_bytes.read()

        output_bytes = await remove_bg(
            api_key=PHOTOROOM_API_KEY,
            image_bytes=input_bytes,
        )

        await db.inc_used_this_month(user_id, 1)
        await db.log_event(user_id=user_id, event="remove_bg_success")

        await message.answer_photo(
            photo=BufferedInputFile(output_bytes, filename="result.png"),
            caption="✅ Готово! Фон убран.\n\n"
            "Чтобы обработать ещё — отправь следующее фото.",
            reply_markup=kb_main(),
        )
    except Exception as e:
        await db.log_event(user_id=user_id, event="remove_bg_error", meta=str(e)[:300])
        await message.answer(
            "⚠️ Не получилось обработать фото. Попробуй другое изображение.",
            reply_markup=kb_main(),
        )


# ========= HANDLERS =========
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await db.touch_user(message.from_user.id)
    await db.log_event(user_id=message.from_user.id, event="start")
    await ask_for_photo(message)


@dp.callback_query(F.data == "back")
async def cb_back(callback: CallbackQuery):
    await ask_for_photo(callback.message)
    await callback.answer()


@dp.callback_query(F.data == "tariffs")
async def cb_tariffs(callback: CallbackQuery):
    await send_tariffs(callback.message)
    await callback.answer()


@dp.callback_query(F.data == "remove_bg")
async def cb_remove_bg(callback: CallbackQuery):
    await callback.message.answer("📸 Пришли новое фото, чтобы убрать фон.", reply_markup=kb_back())
    await callback.answer()


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    if await is_subscribed(bot, user_id):
        await db.log_event(user_id=user_id, event="sub_ok")
        await callback.message.answer(
            "✅ Подписка подтверждена!\n\n📸 Теперь пришли фото — уберу фон.",
            reply_markup=kb_main(),
        )
    else:
        await db.log_event(user_id=user_id, event="sub_fail")
        await callback.message.answer(
            "❌ Подписка не найдена.\n\n"
            f"Подпишись: {CHANNEL_URL}\n"
            "И нажми ✅ «Я подписался» снова.",
            reply_markup=kb_subscribe(),
        )
    await callback.answer()


# Фото как PHOTO
@dp.message(F.photo)
async def on_photo(message: Message, bot: Bot):
    await db.log_event(user_id=message.from_user.id, event="photo_received")
    file_id = message.photo[-1].file_id
    await process_image(message, bot, file_id)


# Фото как DOCUMENT (файл)
@dp.message(F.document)
async def on_document(message: Message, bot: Bot):
    doc = message.document
    if not doc:
        return
    if not (doc.mime_type or "").startswith("image/"):
        return

    await db.log_event(user_id=message.from_user.id, event="image_document_received", meta=doc.mime_type or "")
    await process_image(message, bot, doc.file_id)


# Админ: быстро глянуть, сколько использовано в этом месяце
@dp.message(F.text.in_({"/admin", "/stats"}))
async def admin_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    used = await db.get_used_this_month(message.from_user.id)
    await message.answer(f"📊 Used this month (for you): {used}\n\n(Глобальная админ-статистика будет дальше)")


async def main():
    await db.connect()
    bot = Bot(token=BOT_TOKEN)
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
