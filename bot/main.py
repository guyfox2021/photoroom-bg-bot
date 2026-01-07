import asyncio
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.enums import ContentType

from bot.config import BOT_TOKEN, PHOTOROOM_API_KEY, ADMIN_ID
from bot.photoroom import remove_bg
from bot.db import DB

# =======================
# CONFIG
# =======================
CHANNEL_ID = -1003173585559
CHANNEL_URL = "https://t.me/resident_room"

# Usage rules per month:
# 0 used -> free
# 1 used -> requires subscription
# 2+ used -> show tariffs

db = DB()
dp = Dispatcher()


# =======================
# Keyboards (bottom buttons)
# =======================
def rk_main(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text="🪄 Убрать фон"), KeyboardButton(text="💳 Тарифы")]]
    if is_admin:
        rows.append([KeyboardButton(text="📊 Статистика")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def rk_back(is_admin: bool) -> ReplyKeyboardMarkup:
    # keep admin button available for admin even on sub-screens
    rows = [[KeyboardButton(text="⬅️ Назад")]]
    if is_admin:
        rows.append([KeyboardButton(text="📊 Статистика")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def rk_subscribe(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="✅ Я подписался")],
        [KeyboardButton(text="⬅️ Назад")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="📊 Статистика")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def rk_admin() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Сегодня"), KeyboardButton(text="📈 7 дней")],
            [KeyboardButton(text="🎯 Конверсия"), KeyboardButton(text="💳 Тарифы (таблица)")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


# =======================
# Helpers
# =======================
def is_admin(user_id: int) -> bool:
    return int(user_id) == int(ADMIN_ID)


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator", "restricted")
    except Exception as e:
        # Distinguish "not subscribed" from errors
        try:
            await db.log_event(user_id=user_id, event="check_sub_error", meta=str(e)[:300])
        except Exception:
            pass
        return False


async def send_tariffs(message: Message):
    plans = await db.list_plans()
    if not plans:
        await message.answer(
            "💳 Тарифы пока не настроены.\n\nСкоро добавим оплату и пакеты обработок ✅",
            reply_markup=rk_back(is_admin(message.from_user.id)),
        )
        return

    lines = ["💳 Тарифы:\n"]
    for p in plans:
        # Schema from db.py: code,title,price_uah,credits,is_subscription,is_active,created_at
        title = p.get("title", "—")
        price = p.get("price_uah", "—")
        credits = p.get("credits", "—")
        lines.append(f"• {title} — {price} грн — {credits} фото")

    await message.answer("\n".join(lines), reply_markup=rk_back(is_admin(message.from_user.id)))


async def ask_for_photo(message: Message):
    a = is_admin(message.from_user.id)
    await message.answer(
        "📸 Отправь фото — я уберу фон.\n\n"
        "✅ 1 фото бесплатно\n"
        "🔒 2-е фото — за подписку на канал\n"
        "💳 Дальше — тарифы",
        reply_markup=rk_main(a),
    )


async def process_image(message: Message, bot: Bot, file_id: str, mime_type: str | None = None):
    user_id = message.from_user.id
    a = is_admin(user_id)

    await db.touch_user(user_id)
    await db.log_event(user_id=user_id, event="image_received", meta=mime_type or "")

    used = await db.get_used_this_month(user_id)

    # 0 -> free
    if used == 0:
        pass
    # 1 -> requires subscription
    elif used == 1:
        if not await is_subscribed(bot, user_id):
            await db.log_event(user_id=user_id, event="sub_required", meta=f"used={used}")
            await message.answer(
                "🔒 Второе фото доступно после подписки на канал.\n\n"
                f"📢 Подпишись: {CHANNEL_URL}\n"
                "После подписки нажми «✅ Я подписался».",
                reply_markup=rk_subscribe(a),
            )
            return
    # 2+ -> tariffs
    else:
        await db.log_event(user_id=user_id, event="paid_required", meta=f"used={used}")
        await message.answer(
            "🚫 Бесплатный лимит исчерпан.\n\n💳 Ознакомься с тарифами.",
            reply_markup=rk_main(a),
        )
        return

    await message.answer("⏳ Обрабатываю…", reply_markup=ReplyKeyboardRemove())
    await db.log_event(user_id=user_id, event="remove_bg_start")

    try:
        tg_file = await bot.get_file(file_id)
        stream = await bot.download_file(tg_file.file_path)
        image_bytes = stream.read()

        # PhotoRoom
        result_bytes = await remove_bg(image_bytes=image_bytes, api_key=PHOTOROOM_API_KEY)

        await db.inc_used_this_month(user_id)
        await db.log_event(user_id=user_id, event="remove_bg_success")

        await message.answer_photo(
            photo=result_bytes,
            caption="✅ Готово! Фон убран.\n\nЧтобы обработать ещё — отправь следующее фото.",
            reply_markup=rk_main(a),
        )
    except Exception as e:
        await db.log_event(user_id=user_id, event="remove_bg_error", meta=str(e)[:300])
        await message.answer(
            "⚠️ Не получилось обработать фото. Попробуй другое изображение.",
            reply_markup=rk_main(a),
        )


# =======================
# Admin stats (queries over events table)
# =======================
async def _count_events(day_from: str, day_to: str | None = None) -> dict:
    """
    Returns counts for key funnel events in [day_from, day_to] inclusive.
    day_* are 'YYYY-MM-DD' (UTC).
    """
    # Common funnel events we log / might exist in older code
    keys = [
        "start",
        "image_received",
        "photo_received",
        "remove_bg_start",
        "remove_bg_success",
        "remove_bg_error",
        "sub_required",
        "sub_ok",
        "sub_fail",
        "paid_required",
        "check_sub_error",
    ]

    # Use db._conn directly (aiosqlite connection created in db.connect()).
    conn = getattr(db, "_conn", None)
    if conn is None:
        return {k: 0 for k in keys}

    if day_to is None:
        day_to = day_from

    counts = {k: 0 for k in keys}
    placeholders = ",".join("?" for _ in keys)
    sql = f"""
        SELECT event, COUNT(*) as c
        FROM events
        WHERE day >= ? AND day <= ?
          AND event IN ({placeholders})
        GROUP BY event
    """
    params = [day_from, day_to, *keys]
    cur = await conn.execute(sql, params)
    rows = await cur.fetchall()
    for event, c in rows:
        counts[event] = c
    return counts


async def admin_show_today(message: Message):
    # UTC day (same as db stores)
    today = datetime.now(timezone.utc).date().isoformat()
    s = await _count_events(today, today)

    # Prefer image_received, but keep fallback photo_received for older versions
    received = s.get("image_received", 0) + s.get("photo_received", 0)
    ok = s.get("remove_bg_success", 0)
    err = s.get("remove_bg_error", 0)
    subreq = s.get("sub_required", 0)
    paid = s.get("paid_required", 0)

    text = (
        f"📊 Сегодня (UTC {today})\n\n"
        f"👤 /start: {s.get('start',0)}\n"
        f"📩 Фото получено: {received}\n"
        f"✅ Успешно: {ok}\n"
        f"⚠️ Ошибки: {err}\n"
        f"🔒 Треб. подписку: {subreq}\n"
        f"💳 Уперлись в тарифы: {paid}\n"
    )
    await message.answer(text, reply_markup=rk_admin())


async def admin_show_7d(message: Message):
    today = datetime.now(timezone.utc).date()
    day_to = today.isoformat()
    day_from = (today - timedelta(days=6)).isoformat()
    s = await _count_events(day_from, day_to)

    received = s.get("image_received", 0) + s.get("photo_received", 0)
    ok = s.get("remove_bg_success", 0)
    err = s.get("remove_bg_error", 0)
    subreq = s.get("sub_required", 0)
    paid = s.get("paid_required", 0)

    text = (
        f"📈 7 дней (UTC {day_from} … {day_to})\n\n"
        f"👤 /start: {s.get('start',0)}\n"
        f"📩 Фото получено: {received}\n"
        f"✅ Успешно: {ok}\n"
        f"⚠️ Ошибки: {err}\n"
        f"🔒 Треб. подписку: {subreq}\n"
        f"💳 Уперлись в тарифы: {paid}\n"
    )
    await message.answer(text, reply_markup=rk_admin())


async def admin_show_conversion(message: Message):
    today = datetime.now(timezone.utc).date()
    day_to = today.isoformat()
    day_from = (today - timedelta(days=6)).isoformat()
    s = await _count_events(day_from, day_to)

    starts = s.get("start", 0)
    received = s.get("image_received", 0) + s.get("photo_received", 0)
    ok = s.get("remove_bg_success", 0)
    subreq = s.get("sub_required", 0)
    paid = s.get("paid_required", 0)

    def pct(a: int, b: int) -> str:
        if b <= 0:
            return "—"
        return f"{(a / b) * 100:.1f}%"

    text = (
        f"🎯 Конверсия (UTC {day_from} … {day_to})\n\n"
        f"/start: {starts}\n"
        f"Фото получено: {received} (от /start: {pct(received, starts)})\n"
        f"Успешно убрали фон: {ok} (от фото: {pct(ok, received)})\n"
        f"Запрос подписки (2-я попытка): {subreq}\n"
        f"Уперлись в тарифы (3+): {paid}\n"
    )
    await message.answer(text, reply_markup=rk_admin())


# =======================
# Commands / Buttons
# =======================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await db.touch_user(message.from_user.id)
    await db.log_event(user_id=message.from_user.id, event="start", meta=message.text or "")
    await ask_for_photo(message)


@dp.message(F.text == "🪄 Убрать фон")
async def btn_remove_bg(message: Message):
    await message.answer("📸 Пришли новое фото, чтобы убрать фон.", reply_markup=rk_back(is_admin(message.from_user.id)))


@dp.message(F.text == "💳 Тарифы")
async def btn_tariffs(message: Message):
    await send_tariffs(message)


@dp.message(F.text == "⬅️ Назад")
async def btn_back(message: Message):
    await ask_for_photo(message)


@dp.message(F.text.in_({"/admin", "/stats", "📊 Статистика"}))
async def btn_admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("📊 Админ-статистика:", reply_markup=rk_admin())


@dp.message(F.text == "📊 Сегодня")
async def btn_admin_today(message: Message):
    if not is_admin(message.from_user.id):
        return
    await admin_show_today(message)


@dp.message(F.text == "📈 7 дней")
async def btn_admin_7d(message: Message):
    if not is_admin(message.from_user.id):
        return
    await admin_show_7d(message)


@dp.message(F.text == "🎯 Конверсия")
async def btn_admin_conv(message: Message):
    if not is_admin(message.from_user.id):
        return
    await admin_show_conversion(message)


@dp.message(F.text == "💳 Тарифы (таблица)")
async def btn_admin_plans(message: Message):
    if not is_admin(message.from_user.id):
        return
    await send_tariffs(message)


@dp.message(F.text == "✅ Я подписался")
async def btn_check_sub(message: Message, bot: Bot):
    user_id = message.from_user.id
    a = is_admin(user_id)

    ok = await is_subscribed(bot, user_id)
    if ok:
        await db.log_event(user_id=user_id, event="sub_ok")
        await message.answer("✅ Подписка подтверждена! Теперь пришли фото.", reply_markup=rk_main(a))
    else:
        await db.log_event(user_id=user_id, event="sub_fail")
        await message.answer(
            f"❌ Подписка не найдена.\n\nПодпишись: {CHANNEL_URL}\nИ нажми «✅ Я подписался» снова.",
            reply_markup=rk_subscribe(a),
        )


# PHOTO
@dp.message(F.photo)
async def on_photo(message: Message, bot: Bot):
    file_id = message.photo[-1].file_id
    await process_image(message, bot, file_id, mime_type="photo")


# DOCUMENT image/*
@dp.message(F.document)
async def on_document(message: Message, bot: Bot):
    doc = message.document
    if not doc:
        return
    if not (doc.mime_type or "").startswith("image/"):
        return
    await process_image(message, bot, doc.file_id, mime_type=doc.mime_type)


async def main():
    await db.connect()
    bot = Bot(token=BOT_TOKEN)
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
