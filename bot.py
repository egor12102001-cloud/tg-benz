"""
Telegram bot for gdebenz.ru — показывает где есть бензин, цены и наличие по городам.

Запуск: BOT_TOKEN=<token> python3 bot.py
"""
import asyncio
import logging
import os
import re
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from scraper import CityFuelInfo, FuelStation, get_cities, get_fuel_info, search_city

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

dp = Dispatcher(storage=MemoryStorage())


class SearchState(StatesGroup):
    waiting_for_city = State()


# ─── helpers ────────────────────────────────────────────────────────────────

def _format_station(s: FuelStation) -> str:
    lines = [f"<b>{s.name}</b>"]
    if s.address:
        lines.append(f"📍 {s.address}")
    if s.status:
        lines.append(f"ℹ️ {s.status}")
    if s.fuel_types:
        lines.append("⛽ Топливо:")
        for fuel, price in s.fuel_types.items():
            lines.append(f"  • {fuel}: {price} ₽/л")
    return "\n".join(lines)


def _format_city_info(info: CityFuelInfo) -> str:
    if info.error:
        return f"❌ Ошибка при получении данных: {info.error}"

    header = f"🏙 <b>{info.city}</b>\n"
    if not info.stations:
        return header + "\nНет данных о заправках. Возможно, сайт временно недоступен."

    parts = [header, f"Найдено заправок: <b>{len(info.stations)}</b>\n"]
    for i, station in enumerate(info.stations[:20], 1):
        parts.append(f"━━━━━━━━━━\n{_format_station(station)}")
    if len(info.stations) > 20:
        parts.append(f"\n…и ещё {len(info.stations) - 20} заправок")
    return "\n".join(parts)


def _cities_keyboard(cities: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    page_size = 8
    start = page * page_size
    chunk = cities[start : start + page_size]

    rows = []
    for c in chunk:
        rows.append([InlineKeyboardButton(text=c["name"], callback_data=f"city:{c['slug']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"page:{page-1}"))
    if start + page_size < len(cities):
        nav.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_loading(message: Message, text: str = "⏳ Загружаю данные...") -> Message:
    return await message.answer(text)


# ─── handlers ───────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = (
        "👋 <b>Привет!</b> Я помогу найти информацию о наличии топлива и ценах на АЗС "
        "из сайта <a href=\"https://gdebenz.ru\">gdebenz.ru</a>.\n\n"
        "Команды:\n"
        "• /city — выбрать город из списка\n"
        "• /search &lt;название&gt; — найти город\n"
        "• /help — справка"
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📖 <b>Справка</b>\n\n"
        "/city — показать список городов\n"
        "/search &lt;город&gt; — поиск города по названию\n"
        "  Пример: <code>/search Москва</code>\n\n"
        "После выбора города я покажу:\n"
        "• Список АЗС\n"
        "• Доступные виды топлива\n"
        "• Цены за литр\n"
        "• Адреса заправок"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("city"))
async def cmd_city(message: Message, state: FSMContext):
    await state.clear()
    loading = await _send_loading(message, "⏳ Загружаю список городов...")
    try:
        cities = await get_cities()
    except Exception as e:
        log.error("get_cities failed: %s", e)
        await loading.edit_text(f"❌ Не удалось загрузить список городов: {e}")
        return

    if not cities:
        await loading.edit_text(
            "❌ Не удалось получить список городов с сайта.\n"
            "Попробуйте /search &lt;название города&gt;",
            parse_mode="HTML",
        )
        return

    await state.update_data(cities=cities)
    await loading.edit_text(
        f"🏙 Выберите город ({len(cities)} доступно):",
        reply_markup=_cities_keyboard(cities, 0),
    )


@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    await state.clear()
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer(
            "Укажите название города. Пример:\n<code>/search Москва</code>",
            parse_mode="HTML",
        )
        return

    query = args[1].strip()
    loading = await _send_loading(message, f"🔍 Ищу <b>{query}</b>...")
    try:
        results = await search_city(query)
    except Exception as e:
        log.error("search_city failed: %s", e)
        await loading.edit_text(f"❌ Ошибка поиска: {e}")
        return

    if not results:
        await loading.edit_text(
            f"❌ Город <b>{query}</b> не найден.\n\nПопробуйте /city для полного списка.",
            parse_mode="HTML",
        )
        return

    if len(results) == 1:
        await loading.edit_text(
            f"✅ Нашёл: <b>{results[0]['name']}</b>. Загружаю данные...",
            parse_mode="HTML",
        )
        info = await get_fuel_info(results[0]["slug"])
        await loading.edit_text(_format_city_info(info), parse_mode="HTML")
        return

    await state.update_data(cities=results)
    await loading.edit_text(
        f"Найдено городов: {len(results)}. Выберите нужный:",
        reply_markup=_cities_keyboard(results, 0),
    )


# Allow plain text input as city search
@dp.message(F.text & ~F.text.startswith("/"))
async def msg_text(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    if not query:
        return

    loading = await message.answer(f"🔍 Ищу <b>{query}</b>...", parse_mode="HTML")
    try:
        results = await search_city(query)
    except Exception as e:
        await loading.edit_text(f"❌ Ошибка: {e}")
        return

    if not results:
        await loading.edit_text(
            f"❌ Город <b>{query}</b> не найден.\nПопробуйте /city для полного списка.",
            parse_mode="HTML",
        )
        return

    if len(results) == 1:
        await loading.edit_text("⏳ Загружаю данные о топливе...")
        info = await get_fuel_info(results[0]["slug"])
        await loading.edit_text(_format_city_info(info), parse_mode="HTML")
        return

    await state.update_data(cities=results)
    await loading.edit_text(
        f"Найдено городов: {len(results)}. Выберите нужный:",
        reply_markup=_cities_keyboard(results, 0),
    )


# ─── callback handlers ───────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("city:"))
async def cb_city(call: CallbackQuery, state: FSMContext):
    slug = call.data.split(":", 1)[1]
    await call.answer()
    await call.message.edit_text(f"⏳ Загружаю данные для <b>{slug}</b>...", parse_mode="HTML")
    try:
        info = await get_fuel_info(slug)
    except Exception as e:
        log.error("get_fuel_info(%s) failed: %s", slug, e)
        await call.message.edit_text(f"❌ Ошибка: {e}")
        return
    await call.message.edit_text(_format_city_info(info), parse_mode="HTML")


@dp.callback_query(F.data.startswith("page:"))
async def cb_page(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    cities = data.get("cities", [])
    await call.answer()
    if not cities:
        await call.message.edit_text("❌ Список городов устарел. Запустите /city снова.")
        return
    await call.message.edit_reply_markup(reply_markup=_cities_keyboard(cities, page))


# ─── main ────────────────────────────────────────────────────────────────────

async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set")

    bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
    await bot.set_my_commands([
        BotCommand(command="start", description="Начало работы"),
        BotCommand(command="city", description="Выбрать город из списка"),
        BotCommand(command="search", description="Найти город по названию"),
        BotCommand(command="help", description="Справка"),
    ])
    log.info("Bot started")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
