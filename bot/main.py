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

dp = Dispatcher()

# ===== НАСТРОЙКИ =====
CHANNEL_USERNAME = "@resident_room"
CHANNEL_URL = "https://t.me/resident_room"

FREE_USES = 1
SUB_USES = 1
MAX_USES_PER_MONTH = 50

DB_PATH = "bot.db"
# =====================

db = DB(DB_PATH)


def free_limit() -> int:
    return FREE_USES + SUB_USES


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🪄 Убрать фон", callback_data="remove_bg")],
            [InlineKeyboardButton(text="💳 Тарифы", callback_data="tariffs")],
        ]
    )


def kb_subscribe() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Я подписался — проверить", callback_data="check_sub")],
            [InlineKeyboardButton(text="💳 Тарифы", callback_data="tariffs")],
        ]
    )


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Сегодня", callback_data="adm_today")],
            [InlineKeyboardButton(text="📈 7 дней", callback_data="adm_7d")],
            [InlineKeyboardButton(text="🔁 Конверсия", callback_data="adm_funnel_7d")],
            [InlineKeyboardButton(text="🧾 Таблица тарифов", callback_data="adm_plans")],
        ]
    )


async def process_photo(bot: Bot, chat_id: int, user_id: int, image_bytes: bytes):
    result = await remove_bg(image_bytes=image_bytes, api_key=PHOTOROOM_API_KEY)
    png = BufferedInputFile(result, filename="no_bg.png")

    await bot.send_document(
        chat_id,
        png,
        caption="✅ Готово! Фото без фона.",
        reply_markup=kb_main(),
    )

    await db.inc_used_this_month(user_id)
    await db.log_event("remove_bg_success", user_id=user_id)


@dp.message(CommandStart())
async def start(m: Message):
    await db.touch_user(m.from_user.id)
    await db.log_event("start", user_id=m.from_user.id)
    await m.answer("Отправь фото — я уберу фон и пришлю результат 👇", reply_markup=kb_main())


@dp.callback_query(F.data == "remove_bg")
async def cb_remove_bg(c: CallbackQuery):
    await db.touch_user(c.from_user.id)
    await db.log_event("click_remove_bg", user_id=c.from_user.id)
    await c.answer()
    await c.message.answer("📸 Отправь фото, где нужно убрать фон", reply_markup=kb_main())


@dp.callback_query(F.data == "tariffs")
async def cb_tariffs(c: CallbackQuery):
    await db.touch_user(c.from_user.id)
    await db.log_event("click_tariffs", user_id=c.from_user.id)

    plans = await db.list_plans()
    lines = ["💳 Тарифы (оплата позже):\n"]
    for p in plans:
        if p["is_subscription"]:
            lines.append(f"• {p['price_uah']} грн / месяц — {p['credits']} фото")
        else:
            lines.append(f"• {p['price_uah']} грн — {p['credits']} фото")
    text = "\n".join(lines)

    await db.log_event("tariffs_shown", user_id=c.from_user.id)
    await c.answer()
    await c.message.answer(text, reply_markup=kb_main())


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(c: CallbackQuery, bot: Bot):
    await db.touch_user(c.from_user.id)
    await db.log_event("check_sub", user_id=c.from_user.id)

    await c.answer()
    if await is_subscribed(bot, c.from_user.id):
        await db.log_event("check_sub_ok", user_id=c.from_user.id)
        await c.message.answer("✅ Подписка подтверждена. Теперь отправь фото 👇", reply_markup=kb_main())
    else:
        await db.log_event("check_sub_fail", user_id=c.from_user.id)
        await c.message.answer(
            "❌ Подписку пока не вижу. Подпишись и нажми «проверить» ещё раз.",
            reply_markup=kb_subscribe(),
        )


@dp.message(F.content_type == ContentType.PHOTO)
async def handle_photo(m: Message, bot: Bot):
    user_id = m.from_user.id
    await db.touch_user(user_id)
    await db.log_event("photo_received", user_id=user_id)

    used = await db.get_used_this_month(user_id)

    # защитный месячный лимит
    if used >= MAX_USES_PER_MONTH:
        await db.log_event("month_limit_reached", user_id=user_id)
        await m.answer("🚫 Месячный лимит исчерпан. Попробуйте в следующем месяце 🙂", reply_markup=kb_main())
        return

    # лимит бесплатных генераций
    if used >= free_limit():
        await db.log_event("free_limit_reached", user_id=user_id)
        await m.answer(
            "🚫 Вы исчерпали лимит бесплатных генераций.\n\n"
            "💳 Выберите подходящий вам тариф для продолжения.",
            reply_markup=kb_main(),
        )
        # фиксируем, что тарифы “показаны как следующий шаг” (это важно для конверсии)
        await db.log_event("tariffs_shown", user_id=user_id, meta="from_free_limit_message")
        return

    # 2-я генерация: проверка подписки
    if used >= FREE_USES:
        if not await is_subscribed(bot, user_id):
            await db.log_event("need_subscribe_block", user_id=user_id)
            await m.answer(
                "🔒 Для второй генерации нужна подписка на канал.\n"
                "Подпишитесь и нажмите «Я подписался — проверить».",
                reply_markup=kb_subscribe(),
            )
            return

    # скачиваем фото из Telegram
    photo = m.photo[-1]
    tg_file = await bot.get_file(photo.file_id)
    stream = await bot.download_file(tg_file.file_path)
    image_bytes = stream.read()

    await m.answer("⏳ Убираю фон…", reply_markup=kb_main())
    try:
        await process_photo(bot, m.chat.id, user_id, image_bytes)
    except Exception as e:
        await db.log_event("remove_bg_error", user_id=user_id, meta=str(e)[:800])
        await m.answer(f"❌ Ошибка обработки:\n{e}", reply_markup=kb_main())


# --------- ADMIN ---------

@dp.message(F.text.in_({"/stats", "/admin"}))
async def admin_entry(m: Message):
    await db.touch_user(m.from_user.id)
    await db.log_event("admin_cmd", user_id=m.from_user.id, meta=m.text)

    if not is_admin(m.from_user.id):
        return  # молча игнорируем

    await m.answer("🛠 Админ-панель:", reply_markup=kb_admin())


@dp.callback_query(F.data.in_({"adm_today", "adm_7d"}))
async def cb_admin_stats(c: CallbackQuery):
    await c.answer()
    if not is_admin(c.from_user.id):
        return

    days = 1 if c.data == "adm_today" else 7
    s = await db.get_stats_range(days)

    text = (
        f"📊 Статистика ({s['day_from']} → {s['day_to']}):\n\n"
        f"👤 Пользователей всего: {s['users_total']}\n"
        f"👥 Уникальных за период: {s['unique_users']}\n\n"
        f"📸 Фото получено: {s['photo_received']}\n"
        f"✅ Успешных удалений: {s['remove_ok']}\n"
        f"🔒 Блок подписки (2-я попытка): {s['need_subscribe_block']}\n"
        f"🚫 Уперлись в лимит: {s['free_limit_reached']}\n"
        f"💳 Клик «Тарифы»: {s['tariffs_click']}\n"
        f"📄 «Тарифы показаны»: {s['tariffs_shown']}\n\n"
        f"🗓 Генераций в месяце ({s['month']}): {s['month_used_total']}\n"
        f"🖼 Генераций всего: {s['all_used_total']}\n"
    )
    await c.message.answer(text, reply_markup=kb_admin())


@dp.callback_query(F.data == "adm_funnel_7d")
async def cb_admin_funnel(c: CallbackQuery):
    await c.answer()
    if not is_admin(c.from_user.id):
        return

    f = await db.get_funnel_range(7)

    text = (
        f"🔁 Воронка (7 дней: {f['day_from']} → {f['day_to']}):\n\n"
        f"📸 Фото получено: {f['photo_received']}\n"
        f"🚫 Уперлись в лимит: {f['free_limit_reached']} "
        f"({f['rate_limit_from_photo']:.1f}%)\n"
        f"📄 Тарифы показаны: {f['tariffs_shown']} "
        f"({f['rate_tariffs_shown_from_limit']:.1f}%)\n"
        f"💳 Клик «Тарифы»: {f['tariffs_click']} "
        f"({f['rate_tariffs_click_from_shown']:.1f}%)\n\n"
        f"Идея: повышаем кликабельность «Тарифы» и понятность текста лимита."
    )
    await c.message.answer(text, reply_markup=kb_admin())


@dp.callback_query(F.data == "adm_plans")
async def cb_admin_plans(c: CallbackQuery):
    await c.answer()
    if not is_admin(c.from_user.id):
        return

    plans = await db.list_plans()
    lines = ["🧾 Таблица тарифов (без оплаты):\n"]
    for p in plans:
        if p["is_subscription"]:
            lines.append(f"• {p['code']}: {p['title']} — {p['price_uah']} грн/мес, лимит {p['credits']} фото")
        else:
            lines.append(f"• {p['code']}: {p['title']} — {p['price_uah']} грн, {p['credits']} фото")
    await c.message.answer("\n".join(lines), reply_markup=kb_admin())


# --------- lifecycle ---------

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not found. Проверь .env (BOT_TOKEN=...)")
    await db.connect()
    bot = Bot(token=BOT_TOKEN)
    try:
        await dp.start_polling(bot, polling_timeout=30)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
