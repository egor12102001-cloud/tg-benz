"""
Telegram bot for gdebenz.ru — показывает где есть бензин по городам России.
Запуск: BOT_TOKEN=<token> python3 bot.py
"""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message, LinkPreviewOptions

from scraper import NearbyResult, Station, STATUS_EMOJI, STATUS_LABEL, fetch_city_fuel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
dp = Dispatcher()


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


async def _show_city(message: Message, city_name: str):
    msg = await message.answer(f"⏳ Ищу АЗС в городе <b>{city_name}</b>...")
    try:
        result = await fetch_city_fuel(city_name)
    except Exception as e:
        log.exception("fetch_city_fuel failed for %s", city_name)
        await msg.edit_text(f"❌ Ошибка при загрузке данных: {e}")
        return

    chunks = _fmt_result(result)
    await msg.edit_text(chunks[0], parse_mode="HTML")
    for chunk in chunks[1:]:
        await message.answer(chunk, parse_mode="HTML")


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Привет!</b> Я показываю наличие топлива на АЗС из <b>gdebenz.ru</b>.\n\n"
        "Просто напишите название города:\n"
        "<code>Александров</code>\n"
        "<code>Москва</code>\n"
        "<code>Краснодар</code>\n\n"
        "Команды:\n"
        "• /help — справка",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Справка</b>\n\n"
        "Напишите <b>название города</b> — бот покажет список АЗС с текущим статусом топлива.\n\n"
        "Статусы:\n"
        "✅ Есть — топливо в наличии\n"
        "🟡 Очередь — есть, но очередь\n"
        "🟠 Мало — заканчивается\n"
        "❌ Нет — закончилось\n\n"
        "Данные берутся с <b>gdebenz.ru</b> в реальном времени.",
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def msg_text(message: Message):
    city_name = (message.text or "").strip()
    if not city_name:
        return
    await _show_city(message, city_name)


async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    await bot.set_my_commands([
        BotCommand(command="start", description="Начало работы"),
        BotCommand(command="help", description="Справка"),
    ])
    log.info("Bot started")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
