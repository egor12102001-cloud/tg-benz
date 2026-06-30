"""
Telegram bot for gdebenz.ru — показывает где есть бензин по городам России.
Работает в личных сообщениях и в групповых чатах.

Переменные окружения:
  BOT_TOKEN   — токен бота (обязательно)
  ADMIN_IDS   — начальные администраторы через запятую: 123456,789012
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
    BotCommandScopeChat, Message, LinkPreviewOptions,
)

from db import (
    get_stats, get_user, get_user_by_username, init_db,
    is_admin, list_admins, log_query, set_role, upsert_user,
)
from scraper import (
    NearbyResult, Station, STATUS_EMOJI, STATUS_LABEL,
    fetch_city_fuel, geocode_city, get_nearby_stations,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
_raw_admin = os.getenv("ADMIN_IDS", "")
SEED_ADMINS: set[int] = {int(x) for x in _raw_admin.split(",") if x.strip().isdigit()}

dp = Dispatcher()
IS_PRIVATE = F.chat.type == ChatType.PRIVATE
IS_GROUP   = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})

USER_COMMANDS = [
    BotCommand(command="start",   description="Начало работы"),
    BotCommand(command="fuel",    description="Все АЗС: /fuel Город"),
    BotCommand(command="fuelnow", description="Только с топливом: /fuelnow Город"),
    BotCommand(command="help",    description="Справка"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="stats",       description="Статистика"),
    BotCommand(command="admins",      description="Список администраторов"),
    BotCommand(command="addadmin",    description="Назначить админа"),
    BotCommand(command="removeadmin", description="Снять права админа"),
]
GROUP_COMMANDS = [
    BotCommand(command="fuel",    description="Все АЗС: /fuel Город"),
    BotCommand(command="fuelnow", description="Только с топливом: /fuelnow Город"),
    BotCommand(command="help",    description="Справка"),
]


# ─── formatting helpers ───────────────────────────────────────────────────────

def _user_tag(first: str | None, last: str | None, username: str | None) -> str:
    name = " ".join(p for p in [first, last] if p) or "Без имени"
    return f"{name} (@{username})" if username else name


def _fmt_station(s: Station, idx: int) -> str:
    emoji = STATUS_EMOJI.get(s.status, "❓")
    label = STATUS_LABEL.get(s.status, "Неизвестно")
    lines = [f"{emoji} <b>{idx}. {s.name or s.brand or 'АЗС'}</b> — {label}"]
    nav_url = f"https://yandex.ru/maps/?rtext=~{s.lat},{s.lon}&rtt=auto"
    addr_text = s.addr if s.addr else "Открыть на карте"
    lines.append(f"   📍 <a href=\"{nav_url}\">{addr_text}</a> · {s.distance_km:.1f} км")
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


def _fmt_result(result: NearbyResult, only_available: bool = False) -> list[str]:
    if result.error:
        return [f"❌ {result.error}"]

    stations = result.stations
    if only_available:
        stations = [s for s in stations if s.status in ("yes", "queue", "low")]

    summary_str = _fmt_summary(result.summary)
    mode_label = " (только с топливом)" if only_available else ""
    header = (
        f"🏙 <b>{result.city}</b> — АЗС в радиусе {result.radius_km} км{mode_label}\n"
        + (f"Итого: {summary_str}\n" if summary_str else "")
    )

    if not stations:
        msg = "\nНет АЗС с топливом в наличии." if only_available else "\nДанных о заправках нет — попробуйте позже."
        return [header + msg]

    chunks: list[str] = []
    current = header
    for i, st in enumerate(stations, 1):
        block = "\n\n" + _fmt_station(st, i)
        if len(current) + len(block) > 3800:
            chunks.append(current)
            current = f"<b>{result.city}</b> (продолжение)\n"
        current += block
    chunks.append(current)
    return chunks


def _fmt_stats(s: dict) -> list[str]:
    lines = ["📊 <b>Статистика бота</b>\n"]
    lines.append(
        f"👥 Пользователей: <b>{s['total_users']}</b>\n"
        f"🔍 Запросов всего: <b>{s['total_queries']}</b> "
        f"(✅{s['success_count']} / ❌{s['error_count']})\n"
        f"📅 Сегодня: <b>{s['today_queries']}</b> запросов, "
        f"<b>{s['today_users']}</b> уникальных"
    )

    if s["top_cities"]:
        lines.append("\n🏙 <b>Топ городов:</b>")
        for i, c in enumerate(s["top_cities"], 1):
            lines.append(f"  {i}. {c['city']} — {c['cnt']} раз")

    if s["recent"]:
        lines.append("\n🕐 <b>Последние запросы:</b>")
        for r in s["recent"]:
            tag = _user_tag(r["first_name"], r["last_name"], r["username"])
            ok  = "✅" if r["success"] else "❌"
            ts  = r["created_at"][:16].replace("T", " ")
            st  = f" ({r['stations']} АЗС)" if r["stations"] is not None else ""
            lines.append(f"  {ok} {tag} → <b>{r['city']}</b>{st} [{r['chat_type']}] {ts}")

    # Users section — may be long, split into separate chunk
    chunks = ["\n".join(lines)]

    if s["all_users"]:
        ulines = ["👤 <b>Все пользователи:</b>\n"]
        for u in s["all_users"]:
            tag    = _user_tag(u["first_name"], u["last_name"], u["username"])
            role   = "👑" if u["role"] == "admin" else "🙍"
            uid    = u["user_id"]
            total  = u["total_queries"] or 0
            ok     = u["ok"] or 0
            err    = u["err"] or 0
            seen   = (u["last_seen"] or "")[:10]
            ulines.append(
                f"{role} <b>{tag}</b>\n"
                f"   ID: <code>{uid}</code> | запросов: {total} (✅{ok}/❌{err}) | был: {seen}"
            )
        chunks.append("\n".join(ulines))

    return chunks


# ─── helpers ─────────────────────────────────────────────────────────────────

def _track(message: Message) -> None:
    if message.from_user:
        u = message.from_user
        upsert_user(u.id, u.username, u.first_name, u.last_name)


async def _show_city(message: Message, city_name: str, only_available: bool = False) -> None:
    _track(message)
    msg   = await message.answer(f"⏳ Ищу АЗС в городе <b>{city_name}</b>...")
    uid   = message.from_user.id if message.from_user else 0
    cid   = message.chat.id
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
        log.info("user=%s city=%s stations=%d only_available=%s", uid, city_name, len(result.stations), only_available)

    no_preview = LinkPreviewOptions(is_disabled=True)
    chunks = _fmt_result(result, only_available=only_available)
    await msg.edit_text(chunks[0], parse_mode="HTML", link_preview_options=no_preview)
    for chunk in chunks[1:]:
        await message.answer(chunk, parse_mode="HTML", link_preview_options=no_preview)


def _require_admin(message: Message) -> bool:
    uid = message.from_user.id if message.from_user else 0
    if not is_admin(uid):
        return False
    return True


# ─── /start, /help ───────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    _track(message)
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer(
            "👋 <b>Привет!</b> Я показываю наличие топлива на АЗС.\n\n"
            "<code>/fuel Александров</code> — все АЗС\n"
            "<code>/fuelnow Александров</code> — только где есть топливо",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    else:
        await message.answer(
            "👋 <b>Привет!</b> Я показываю наличие топлива на АЗС из <b>gdebenz.ru</b>.\n\n"
            "Напишите название города — покажу все АЗС.\n\n"
            "Или используйте команды:\n"
            "<code>/fuel Александров</code> — все АЗС\n"
            "<code>/fuelnow Александров</code> — только где есть топливо\n\n"
            "/help — справка",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    _track(message)
    await message.answer(
        "📖 <b>Справка</b>\n\n"
        "<code>/fuel Город</code> — все АЗС в радиусе 20 км\n"
        "<code>/fuelnow Город</code> — только где есть топливо\n\n"
        "В личке можно просто написать название города.\n\n"
        "Статусы:\n"
        "✅ Есть  🟡 Очередь  🟠 Мало  ❌ Нет\n\n"
        "Данные берутся с <b>gdebenz.ru</b> в реальном времени.",
    )


# ─── /fuel ───────────────────────────────────────────────────────────────────

@dp.message(Command("fuel"))
async def cmd_fuel(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Укажите город:\n"
            "<code>/fuel Александров</code> — все АЗС\n"
            "<code>/fuelnow Александров</code> — только где есть топливо"
        )
        return
    await _show_city(message, parts[1].strip(), only_available=False)


@dp.message(Command("fuelnow"))
async def cmd_fuelnow(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Укажите город: <code>/fuelnow Александров</code>")
        return
    await _show_city(message, parts[1].strip(), only_available=True)


# ─── /stats ──────────────────────────────────────────────────────────────────

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    _track(message)
    if not _require_admin(message):
        await message.answer("⛔ Доступ только для администраторов.")
        return
    chunks = _fmt_stats(get_stats())
    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML")


# ─── /admins ─────────────────────────────────────────────────────────────────

@dp.message(Command("admins"))
async def cmd_admins(message: Message):
    _track(message)
    if not _require_admin(message):
        await message.answer("⛔ Доступ только для администраторов.")
        return
    admins = list_admins()
    if not admins:
        await message.answer("Администраторов нет.")
        return
    lines = ["👑 <b>Администраторы:</b>\n"]
    for a in admins:
        tag   = _user_tag(a["first_name"], a["last_name"], a["username"])
        since = (a["first_seen"] or "")[:10]
        lines.append(f"• {tag}\n  ID: <code>{a['user_id']}</code> | с {since}")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ─── /addadmin ───────────────────────────────────────────────────────────────

@dp.message(Command("addadmin"))
async def cmd_addadmin(message: Message):
    _track(message)
    if not _require_admin(message):
        await message.answer("⛔ Доступ только для администраторов.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Укажите ID или @username:\n"
            "<code>/addadmin 123456789</code>\n"
            "<code>/addadmin @username</code>"
        )
        return

    arg = parts[1].strip()
    if arg.lstrip("@").isdigit():
        user_id = int(arg.lstrip("@"))
        user = get_user(user_id)
    else:
        user = get_user_by_username(arg)
        user_id = user["user_id"] if user else None

    if not user:
        await message.answer(
            "❌ Пользователь не найден в базе.\n"
            "Он должен сначала написать боту хотя бы один раз."
        )
        return

    if user["role"] == "admin":
        tag = _user_tag(user["first_name"], user["last_name"], user["username"])
        await message.answer(f"ℹ️ {tag} уже администратор.")
        return

    set_role(user_id, "admin")
    tag = _user_tag(user["first_name"], user["last_name"], user["username"])
    log.info("Admin added: %s (id=%s) by %s", tag, user_id, message.from_user.id)
    try:
        await message.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=user_id))
    except Exception:
        pass
    await message.answer(f"✅ {tag} теперь администратор.")


# ─── /removeadmin ────────────────────────────────────────────────────────────

@dp.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message):
    _track(message)
    if not _require_admin(message):
        await message.answer("⛔ Доступ только для администраторов.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Укажите ID или @username:\n"
            "<code>/removeadmin 123456789</code>"
        )
        return

    arg = parts[1].strip()
    if arg.lstrip("@").isdigit():
        user_id = int(arg.lstrip("@"))
        user = get_user(user_id)
    else:
        user = get_user_by_username(arg)
        user_id = user["user_id"] if user else None

    if not user:
        await message.answer("❌ Пользователь не найден в базе.")
        return

    uid_self = message.from_user.id if message.from_user else 0
    if user_id == uid_self:
        await message.answer("❌ Нельзя снять права у самого себя.")
        return

    if user["role"] != "admin":
        tag = _user_tag(user["first_name"], user["last_name"], user["username"])
        await message.answer(f"ℹ️ {tag} и так не администратор.")
        return

    set_role(user_id, "user")
    tag = _user_tag(user["first_name"], user["last_name"], user["username"])
    log.info("Admin removed: %s (id=%s) by %s", tag, user_id, message.from_user.id)
    try:
        await message.bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeChat(chat_id=user_id))
    except Exception:
        pass
    await message.answer(f"✅ Права администратора у {tag} сняты.")


# ─── /rawstation (admin debug) ───────────────────────────────────────────────

@dp.message(Command("rawstation"))
async def cmd_rawstation(message: Message):
    _track(message)
    if not _require_admin(message):
        await message.answer("⛔ Доступ только для администраторов.")
        return
    parts = (message.text or "").split(maxsplit=1)
    city = parts[1].strip() if len(parts) > 1 else "Александров"
    msg = await message.answer(f"⏳ Получаю сырые данные для {city}...")
    try:
        geo = await geocode_city(city)
        if not geo:
            await msg.edit_text("❌ Город не найден.")
            return
        lat, lon, _ = geo
        _, raw = await get_nearby_stations(lat, lon, 20)
        if not raw:
            await msg.edit_text("Станций не найдено.")
            return
        import json
        text = json.dumps(raw[0], ensure_ascii=False, indent=2)
        for i in range(0, len(text), 3800):
            chunk = f"<pre>{text[i:i+3800]}</pre>"
            if i == 0:
                await msg.edit_text(chunk, parse_mode="HTML")
            else:
                await message.answer(chunk, parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


# ─── plain text (личка) ───────────────────────────────────────────────────────

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

    # Promote seed admins from env on every startup
    for uid in SEED_ADMINS:
        user = get_user(uid)
        if user and user["role"] != "admin":
            set_role(uid, "admin")
            log.info("Seed admin promoted: %s", uid)
        elif not user:
            log.warning("Seed admin %s not in DB yet — they need to /start first", uid)

    log.info("Seed admins from env: %s", SEED_ADMINS or "none")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))

    # Базовые команды для всех личных чатов (без админских)
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())

    # Установить расширенное меню каждому текущему админу персонально
    for admin in list_admins():
        try:
            await bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin["user_id"])
            )
        except Exception as e:
            log.warning("Could not set commands for admin %s: %s", admin["user_id"], e)

    log.info("Bot started")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
