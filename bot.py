"""
Telegram bot for gdebenz.ru — показывает где есть бензин, цены и наличие по городам.
Запуск: BOT_TOKEN=<token> python3 bot.py
"""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from scraper import CityFuelInfo, FuelStation, city_to_slug, get_cities, get_fuel_info

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
dp = Dispatcher(storage=MemoryStorage())


# ─── formatting ──────────────────────────────────────────────────────────────

def _fmt_station(s: FuelStation, idx: int) -> str:
    lines = [f"<b>{idx}. {s.name}</b>" if s.name else f"<b>{idx}. АЗС</b>"]
    if s.address:
        lines.append(f"📍 {s.address}")
    if s.fuel_types:
        for fuel, info in s.fuel_types.items():
            lines.append(f"  ⛽ {fuel}: {info}")
    if s.updated:
        lines.append(f"  🕐 {s.updated}")
    return "\n".join(lines)


def _fmt_city(info: CityFuelInfo) -> list[str]:
    """Split city result into chunks ≤4000 chars (Telegram limit)."""
    if info.error and not info.stations:
        return [f"❌ {info.error}"]

    header = (
        f"🏙 <b>{info.city}</b>\n"
        f"Источник: gdebenz.ru\n"
        f"Найдено АЗС: <b>{info.total or len(info.stations)}</b>"
    )

    if not info.stations:
        return [header + "\n\nДанных о заправках пока нет — попробуйте позже."]

    chunks: list[str] = []
    current = header + "\n"
    for i, st in enumerate(info.stations, 1):
        block = "\n━━━━━━━━━━\n" + _fmt_station(st, i)
        if len(current) + len(block) > 3800:
            chunks.append(current)
            current = f"<b>{info.city}</b> (продолжение)\n"
        current += block
    chunks.append(current)
    return chunks


def _cities_kb(cities: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    page_size = 8
    start = page * page_size
    chunk = cities[start: start + page_size]
    rows = [[InlineKeyboardButton(text=c["name"], callback_data=f"city:{c['slug']}")] for c in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"page:{page - 1}"))
    if start + page_size < len(cities):
        nav.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"page:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_city(target: Message, city_name: str, slug: str | None = None):
    """Load and display fuel info for a city."""
    msg = await target.answer(f"⏳ Загружаю данные для <b>{city_name}</b>...\n(это может занять ~15 сек)")
    try:
        info = await get_fuel_info(slug or city_name)
    except Exception as e:
        log.exception("get_fuel_info failed")
        await msg.edit_text(f"❌ Ошибка при загрузке: {e}")
        return

    chunks = _fmt_city(info)
    await msg.edit_text(chunks[0])
    for chunk in chunks[1:]:
        await target.answer(chunk)


# ─── handlers ────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 <b>Привет!</b> Я показываю наличие топлива и цены на АЗС из <b>gdebenz.ru</b>.\n\n"
        "Просто напишите название города — например:\n"
        "<code>Москва</code>\n"
        "<code>Краснодар</code>\n"
        "<code>Ростов-на-Дону</code>\n\n"
        "Или используйте команды:\n"
        "• /city — список городов\n"
        "• /help — справка",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Справка</b>\n\n"
        "Просто напишите <b>название города</b> — бот сразу покажет данные.\n\n"
        "Команды:\n"
        "/city — список всех городов с кнопками\n"
        "/help — эта справка\n\n"
        "Данные берутся с <b>gdebenz.ru</b> в реальном времени.\n"
        "Загрузка занимает ~10–20 секунд (открывается браузер).",
    )


@dp.message(Command("city"))
async def cmd_city(message: Message, state: FSMContext):
    await state.clear()
    msg = await message.answer("⏳ Загружаю список городов...")
    try:
        cities = await get_cities()
    except Exception as e:
        log.error("get_cities failed: %s", e)
        await msg.edit_text(f"❌ Не удалось загрузить список городов: {e}")
        return

    if not cities:
        await msg.edit_text(
            "ℹ️ Сайт не отдаёт список городов напрямую.\n\n"
            "Напишите название города прямо в чат — например:\n"
            "<code>Москва</code>"
        )
        return

    await state.update_data(cities=cities)
    await msg.edit_text(
        f"🏙 Выберите город ({len(cities)} доступно):",
        reply_markup=_cities_kb(cities, 0),
    )


# Plain text → city search
@dp.message(F.text & ~F.text.startswith("/"))
async def msg_text(message: Message, state: FSMContext):
    city_name = (message.text or "").strip()
    if not city_name:
        return
    await _show_city(message, city_name)


# ─── callbacks ────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("city:"))
async def cb_city(call: CallbackQuery):
    slug = call.data.split(":", 1)[1]
    await call.answer()
    await call.message.edit_text(f"⏳ Загружаю данные...\n(это может занять ~15 сек)")
    try:
        info = await get_fuel_info(slug)
    except Exception as e:
        log.exception("get_fuel_info failed for slug=%s", slug)
        await call.message.edit_text(f"❌ Ошибка: {e}")
        return

    chunks = _fmt_city(info)
    await call.message.edit_text(chunks[0])
    for chunk in chunks[1:]:
        await call.message.answer(chunk)


@dp.callback_query(F.data.startswith("page:"))
async def cb_page(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    cities = data.get("cities", [])
    await call.answer()
    if not cities:
        await call.message.edit_text("❌ Список городов устарел. Запустите /city снова.")
        return
    await call.message.edit_reply_markup(reply_markup=_cities_kb(cities, page))


# ─── main ─────────────────────────────────────────────────────────────────────

async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    await bot.set_my_commands([
        BotCommand(command="start", description="Начало работы"),
        BotCommand(command="city", description="Список городов"),
        BotCommand(command="help", description="Справка"),
    ])
    log.info("Bot started")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
