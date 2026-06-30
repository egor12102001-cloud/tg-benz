"""
Telegram bot for gdebenz.ru — показывает где есть бензин по городам России.
Работает в личных сообщениях и в групповых чатах.
Запуск: BOT_TOKEN=<token> ADMIN_IDS=123456,789012 python3 bot.py
"""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
    Message, LinkPreviewOptions,
)

from db import get_stats, init_db, log_query, upsert_user
from scraper import NearbyResult, Station, STATUS_EMOJI, STATUS_LABEL, fetch_city_fuel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {int(x) for x in _raw_admins.split(",") if x.strip().isdigit()}

dp = Dispatcher()

IS_PRIVATE = F.chat.type == ChatType.PRIVATE
IS_GROUP   = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})


# ─── formatting ──────────────────────────────────────────────────────────────

def _fmt_station(s: Station, idx: int) -> str:
    emoji = STATUS_EMOJI.get(s.status, "❓")
    label = STATUS_LABEL.get(s.status, "Неизвестно")
    lines = [f"{emoji} <b>{idx}. {s.name or s.brand or 'АЗС'}</b> — {label}"]
    if s.addr:
        lines.append(f"   📍 {s.addr}")
    lines.append(f"   📏 {s.distance_km:.1f} км")
    if s.detail:
        lines.append(f"   ⛽ {s.detail}")
    if s.last_at:
        lines.append(f"   🕐 {s.last_at}")
    return "\n".join(lines)


def _fmt_summary(summary: dict) -> str:
    parts = []
    for key, emoji in STATUS_EMOJI.items():
        n = summary.get(key, 0)
        if n:
            parts.append(f"{emoji}{n}")
    return " ".join(parts) if parts else ""


def _fmt_result(result: NearbyResult) -> list[str]:
    if result.error:
        return [f"❌ {result.error}"]

    summary_str = _fmt_summary(result.summary)
    header = (
        f"🏙 <b>{result.city}</b> — АЗС в радиусе {result.radius_km} км\n"
        + (f"Итого: {summary_str}\n" if summary_str else "")
    )

    if not result.stations:
        return [header + "\nДанных о заправках нет — попробуйте позже."]

    chunks: list[str] = []
    current = header
    for i, st in enumerate(result.stations, 1):
        block = "\n\n" + _fmt_station(st, i)
        if len(current) + len(block) > 3800:
            chunks.append(current)
            current = f"<b>{result.city}</b> (продолжение)\n"
        current += block
    chunks.append(current)
    return chunks


def _fmt_stats(s: dict) -> str:
    lines = ["📊 <b>Статистика бота</b>\n"]

    lines.append(
        f"👥 Всего пользователей: <b>{s['total_users']}</b>\n"
        f"🔍 Всего запросов: <b>{s['total_queries']}</b> "
        f"(✅{s['success_count']} / ❌{s['error_count']})\n"
        f"📅 Сегодня: запросов <b>{s['today_queries']}</b>, "
        f"уникальных пользователей <b>{s['today_users']}</b>"
    )

    if s["top_cities"]:
        lines.append("\n🏙 <b>Топ городов:</b>")
        for i, c in enumerate(s["top_cities"], 1):
            lines.append(f"  {i}. {c['city']} — {c['cnt']} раз")

    if s["top_users"]:
        lines.append("\n🙋 <b>Топ пользователей:</b>")
        for i, u in enumerate(s["top_users"], 1):
            name = u["first_name"] or ""
            uname = f" (@{u['username']})" if u["username"] else ""
            lines.append(f"  {i}. {name}{uname} — {u['cnt']} запросов")

    if s["recent"]:
        lines.append("\n🕐 <b>Последние запросы:</b>")
        for r in s["recent"]:
            name = r["first_name"] or "?"
            status = "✅" if r["success"] else "❌"
            ts = r["created_at"][:16].replace("T", " ")
            lines.append(f"  {status} {name} → {r['city']} [{r['chat_type']}] {ts}")

    return "\n".join(lines)


# ─── helpers ─────────────────────────────────────────────────────────────────

def _track(message: Message) -> None:
    if message.from_user:
        u = message.from_user
        upsert_user(u.id, u.username, u.first_name)


async def _show_city(message: Message, city_name: str) -> None:
    _track(message)
    msg = await message.answer(f"⏳ Ищу АЗС в городе <b>{city_name}</b>...")

    uid = message.from_user.id if message.from_user else 0
    cid = message.chat.id
    ctype = message.chat.type

    try:
        result = await fetch_city_fuel(city_name)
    except Exception as e:
        log.exception("fetch_city_fuel failed for %s", city_name)
        log_query(uid, cid, ctype, city_name, success=False, error=str(e))
        await msg.edit_text(f"❌ Ошибка при загрузке данных: {e}")
        return

    if result.error:
        log_query(uid, cid, ctype, city_name, success=False, error=result.error)
    else:
        log_query(uid, cid, ctype, city_name, success=True, stations=len(result.stations))
        log.info("user=%s city=%s stations=%d", uid, city_name, len(result.stations))

    chunks = _fmt_result(result)
    await msg.edit_text(chunks[0], parse_mode="HTML")
    for chunk in chunks[1:]:
        await message.answer(chunk, parse_mode="HTML")


# ─── handlers ────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    _track(message)
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer(
            "👋 <b>Привет!</b> Я показываю наличие топлива на АЗС.\n\n"
            "Используйте команду:\n"
            "<code>/fuel Александров</code>\n"
            "<code>/fuel Москва</code>",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    else:
        await message.answer(
            "👋 <b>Привет!</b> Я показываю наличие топлива на АЗС из <b>gdebenz.ru</b>.\n\n"
            "Напишите название города:\n"
            "<code>Александров</code>\n"
            "<code>Москва</code>\n\n"
            "Или используйте команду:\n"
            "<code>/fuel Краснодар</code>\n\n"
            "• /help — справка",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    _track(message)
    await message.answer(
        "📖 <b>Справка</b>\n\n"
        "В группе: <code>/fuel Александров</code>\n"
        "В личке: просто напишите название города.\n\n"
        "Статусы:\n"
        "✅ Есть — топливо в наличии\n"
        "🟡 Очередь — есть, но очередь\n"
        "🟠 Мало — заканчивается\n"
        "❌ Нет — закончилось\n\n"
        "Данные берутся с <b>gdebenz.ru</b> в реальном времени.",
    )


@dp.message(Command("fuel"))
async def cmd_fuel(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Укажите город после команды:\n"
            "<code>/fuel Александров</code>"
        )
        return
    await _show_city(message, parts[1].strip())


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    _track(message)
    uid = message.from_user.id if message.from_user else 0
    if ADMIN_IDS and uid not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещён.")
        return
    s = get_stats()
    await message.answer(_fmt_stats(s), parse_mode="HTML")


# Plain text в личке
@dp.message(IS_PRIVATE & F.text & ~F.text.startswith("/"))
async def msg_text_private(message: Message):
    city_name = (message.text or "").strip()
    if not city_name:
        return
    await _show_city(message, city_name)


# ─── main ─────────────────────────────────────────────────────────────────────

async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set")

    init_db()
    log.info("Database initialised. Admins: %s", ADMIN_IDS or "anyone")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начало работы"),
            BotCommand(command="fuel", description="АЗС по городу: /fuel Город"),
            BotCommand(command="help", description="Справка"),
            BotCommand(command="stats", description="Статистика (только для админов)"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [
            BotCommand(command="fuel", description="АЗС по городу: /fuel Город"),
            BotCommand(command="help", description="Справка"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )

    log.info("Bot started")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
