"""
Telegram bot for gdebenz.ru — показывает где есть бензин по городам России.
Работает в личных сообщениях и в групповых чатах.

Переменные окружения:
  BOT_TOKEN   — токен бота (обязательно)
  ADMIN_IDS   — начальные администраторы через запятую: 123456,789012
"""
import asyncio
import csv
import io
import json
import logging
import os
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
    BotCommandScopeChat, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, Message, LinkPreviewOptions,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

from db import (
    add_subscription, all_subscriptions, get_stats, get_user, get_user_by_username,
    init_db, is_admin, list_admins, list_all_users, list_subscriptions, log_query,
    remove_subscription, set_last_city, set_role, update_subscription_status, upsert_user,
)
from scraper import (
    NearbyResult, Station, STATUS_EMOJI, STATUS_LABEL,
    fetch_city_fuel, geocode_city, get_nearby_stations, normalize_city, reverse_geocode,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))


def _to_msk(iso_str: str, fmt: str = "%d.%m %H:%M") -> str:
    """Convert a UTC ISO timestamp (from DB or API) to a Moscow-time string."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MSK).strftime(fmt)
    except ValueError:
        return iso_str


BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
_raw_admin = os.getenv("ADMIN_IDS", "")
SEED_ADMINS: set[int] = {int(x) for x in _raw_admin.split(",") if x.strip().isdigit()}

SUBSCRIPTION_CHECK_INTERVAL = 600  # 10 минут

dp = Dispatcher()
IS_PRIVATE = F.chat.type == ChatType.PRIVATE

USER_COMMANDS = [
    BotCommand(command="start",     description="Начало работы"),
    BotCommand(command="subscribe", description="Подписаться на город"),
    BotCommand(command="unsubscribe", description="Отписаться от города"),
    BotCommand(command="mysubs",    description="Мои подписки"),
    BotCommand(command="help",      description="Справка"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="stats",       description="Статистика"),
    BotCommand(command="admins",      description="Список администраторов"),
    BotCommand(command="addadmin",    description="Назначить админа"),
    BotCommand(command="removeadmin", description="Снять права админа"),
    BotCommand(command="broadcast",   description="Рассылка всем пользователям"),
    BotCommand(command="export",      description="Экспорт статистики в CSV"),
]
GROUP_COMMANDS = [
    BotCommand(command="fuel",    description="Все АЗС: /fuel Город"),
    BotCommand(command="fuelnow", description="Только с топливом: /fuelnow Город"),
    BotCommand(command="help",    description="Справка"),
]

LOCATION_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📍 Отправить геопозицию", request_location=True)]],
    resize_keyboard=True, one_time_keyboard=True,
)


# ─── keyboards ───────────────────────────────────────────────────────────────

def _mode_kb(city: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Все АЗС",          callback_data=f"show:all:{city}"),
        InlineKeyboardButton(text="⛽ Только с топливом", callback_data=f"show:now:{city}"),
    ]])


def _refresh_kb(city: str, mode: str) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh:{mode}:{city}"),
        InlineKeyboardButton(
            text="⛽ Только с топливом" if mode == "all" else "📋 Все АЗС",
            callback_data=f"show:{'now' if mode == 'all' else 'all'}:{city}",
        ),
    ], [
        InlineKeyboardButton(text="🔔 Подписаться на обновления", callback_data=f"sub:{city}"),
    ]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─── formatting ──────────────────────────────────────────────────────────────

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
        lines.append(f"   🕐 {_to_msk(s.last_at.replace(' ', 'T'))} МСК")
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
    updated_at = datetime.now(MSK).strftime("%H:%M МСК")
    header = (
        f"🏙 <b>{result.city}</b> — АЗС в радиусе {result.radius_km} км{mode_label}\n"
        + (f"Итого: {summary_str}\n" if summary_str else "")
        + f"<i>Обновлено: {updated_at}</i>"
    )

    if not stations:
        msg = "\n\nНет АЗС с топливом в наличии." if only_available else "\n\nДанных о заправках нет."
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
            ts  = _to_msk(r["created_at"])
            st  = f" ({r['stations']} АЗС)" if r["stations"] is not None else ""
            lines.append(f"  {ok} {tag} → <b>{r['city']}</b>{st} [{r['chat_type']}] {ts}")

    chunks = ["\n".join(lines)]
    if s["all_users"]:
        ulines = ["👤 <b>Все пользователи:</b>\n"]
        for u in s["all_users"]:
            tag   = _user_tag(u["first_name"], u["last_name"], u["username"])
            role  = "👑" if u["role"] == "admin" else "🙍"
            uid   = u["user_id"]
            total = u["total_queries"] or 0
            ok    = u["ok"] or 0
            err   = u["err"] or 0
            seen  = _to_msk(u["last_seen"], "%d.%m.%Y")
            ulines.append(
                f"{role} <b>{tag}</b>\n"
                f"   ID: <code>{uid}</code> | запросов: {total} (✅{ok}/❌{err}) | был: {seen}"
            )
        chunks.append("\n".join(ulines))
    return chunks


# ─── core fetch ──────────────────────────────────────────────────────────────

def _track(message: Message) -> None:
    if message.from_user:
        u = message.from_user
        upsert_user(u.id, u.username, u.first_name, u.last_name)


async def _fetch_and_render(
    city: str, mode: str, uid: int, cid: int, ctype: str,
) -> tuple[list[str], InlineKeyboardMarkup]:
    """Fetch stations and return (text_chunks, keyboard)."""
    only_available = mode == "now"
    result = await fetch_city_fuel(city)
    city_norm = normalize_city(city)

    if result.error:
        log_query(uid, cid, ctype, city, city_norm, success=False, error=result.error)
    else:
        log_query(uid, cid, ctype, city, city_norm, success=True, stations=len(result.stations))
        if uid:
            set_last_city(uid, result.city)

    chunks = _fmt_result(result, only_available=only_available)
    kb = _refresh_kb(city, mode)
    return chunks, kb


# ─── /start, /help ───────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    _track(message)
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer(
            "👋 <b>Привет!</b> Я показываю наличие топлива на АЗС.\n\n"
            "Напишите: <code>/fuel Александров</code>",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return

    user = get_user(message.from_user.id) if message.from_user else None
    last_city = user.get("last_city") if user else None

    text = (
        "👋 <b>Привет!</b> Я показываю наличие топлива на АЗС из <b>gdebenz.ru</b>.\n\n"
        "Напишите название города или отправьте геопозицию — я предложу выбрать режим показа.\n\n"
        "/help — справка"
    )
    kb = LOCATION_KB
    if last_city:
        text += f"\n\nПоследний запрошенный город: <b>{last_city}</b>"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"🔁 Снова: {last_city}", callback_data=f"show:all:{last_city[:40]}")
        ]])
        await message.answer(text, link_preview_options=LinkPreviewOptions(is_disabled=True))
        await message.answer("Или отправьте геопозицию:", reply_markup=LOCATION_KB)
        await message.answer("Либо повторите прошлый город:", reply_markup=kb)
        return

    await message.answer(text, reply_markup=kb, link_preview_options=LinkPreviewOptions(is_disabled=True))


@dp.message(Command("help"))
async def cmd_help(message: Message):
    _track(message)
    await message.answer(
        "📖 <b>Справка</b>\n\n"
        "Напишите название города или отправьте 📍 геопозицию — выберите режим:\n"
        "📋 <b>Все АЗС</b> — полный список\n"
        "⛽ <b>Только с топливом</b> — отфильтрованный список\n\n"
        "<code>/subscribe Город</code> — получать уведомление, когда в городе появится топливо\n"
        "<code>/unsubscribe Город</code> — отписаться\n"
        "<code>/mysubs</code> — список подписок\n\n"
        "В группе: <code>/fuel Город</code> или <code>/fuelnow Город</code>\n\n"
        "Статусы:\n"
        "✅ Есть  🟡 Очередь  🟠 Мало  ❌ Нет\n\n"
        "Данные берутся с <b>gdebenz.ru</b> в реальном времени.",
    )


# ─── /fuel, /fuelnow (группы и прямые команды) ───────────────────────────────

async def _run_fuel_command(message: Message, mode: str):
    parts = (message.text or "").split(maxsplit=1)
    cmd_name = "fuel" if mode == "all" else "fuelnow"
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(f"Укажите город: <code>/{cmd_name} Александров</code>")
        return
    city = parts[1].strip()
    _track(message)
    uid = message.from_user.id if message.from_user else 0
    msg = await message.answer(f"⏳ Загружаю АЗС для <b>{city}</b>...")
    try:
        chunks, kb = await _fetch_and_render(city, mode, uid, message.chat.id, message.chat.type)
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")
        return
    no_preview = LinkPreviewOptions(is_disabled=True)
    await msg.edit_text(chunks[0], reply_markup=kb, link_preview_options=no_preview)
    for chunk in chunks[1:]:
        await message.answer(chunk, link_preview_options=no_preview)


@dp.message(Command("fuel"))
async def cmd_fuel(message: Message):
    await _run_fuel_command(message, "all")


@dp.message(Command("fuelnow"))
async def cmd_fuelnow(message: Message):
    await _run_fuel_command(message, "now")


# ─── /subscribe, /unsubscribe, /mysubs ───────────────────────────────────────

@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    _track(message)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Укажите город: <code>/subscribe Александров</code>")
        return
    city = parts[1].strip()
    uid = message.from_user.id if message.from_user else 0
    city_norm = normalize_city(city)
    ok = add_subscription(uid, message.chat.id, city, city_norm)
    if ok:
        await message.answer(
            f"🔔 Подписка оформлена на <b>{city}</b>.\n"
            f"Напишу, когда статус топлива изменится (проверка каждые {SUBSCRIPTION_CHECK_INTERVAL // 60} мин)."
        )
    else:
        await message.answer(f"ℹ️ Вы уже подписаны на <b>{city}</b>.")


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    _track(message)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Укажите город: <code>/unsubscribe Александров</code>")
        return
    city = parts[1].strip()
    uid = message.from_user.id if message.from_user else 0
    ok = remove_subscription(uid, normalize_city(city))
    if ok:
        await message.answer(f"🔕 Подписка на <b>{city}</b> отменена.")
    else:
        await message.answer(f"ℹ️ Подписки на <b>{city}</b> не найдено.")


@dp.message(Command("mysubs"))
async def cmd_mysubs(message: Message):
    _track(message)
    uid = message.from_user.id if message.from_user else 0
    subs = list_subscriptions(uid)
    if not subs:
        await message.answer("У вас нет активных подписок. Оформить: <code>/subscribe Город</code>")
        return
    lines = ["🔔 <b>Ваши подписки:</b>\n"]
    for s in subs:
        lines.append(f"• {s['city']}")
    await message.answer("\n".join(lines))


# ─── geolocation ──────────────────────────────────────────────────────────────

@dp.message(IS_PRIVATE & F.location)
async def msg_location(message: Message):
    _track(message)
    loc = message.location
    msg = await message.answer(
        "⏳ Определяю город...", reply_markup=ReplyKeyboardRemove()
    )
    geo = await reverse_geocode(loc.latitude, loc.longitude)
    if not geo or not geo[2]:
        await msg.edit_text("❌ Не удалось определить город по координатам. Напишите название вручную.")
        return
    _, _, city = geo
    city_safe = city[:40]
    await msg.edit_text(
        f"🏙 <b>{city}</b>\n\nКакой список показать?",
        reply_markup=_mode_kb(city_safe),
    )


# ─── plain text в личке → меню выбора режима ────────────────────────────────

@dp.message(IS_PRIVATE & F.text & ~F.text.startswith("/"))
async def msg_text_private(message: Message):
    city = (message.text or "").strip()
    if not city:
        return
    _track(message)
    city_safe = city[:40]
    await message.answer(
        f"🏙 <b>{city}</b>\n\nКакой список показать?",
        reply_markup=_mode_kb(city_safe),
    )


# ─── callbacks: show, refresh, sub ───────────────────────────────────────────

@dp.callback_query(F.data.startswith("show:") | F.data.startswith("refresh:"))
async def cb_show_or_refresh(call: CallbackQuery):
    await call.answer()
    action, mode, city = call.data.split(":", 2)
    uid = call.from_user.id if call.from_user else 0

    if call.from_user:
        upsert_user(call.from_user.id, call.from_user.username,
                    call.from_user.first_name, call.from_user.last_name)

    await call.message.edit_text(f"⏳ Загружаю АЗС для <b>{city}</b>...")

    try:
        chunks, kb = await _fetch_and_render(
            city, mode, uid, call.message.chat.id, call.message.chat.type
        )
    except Exception as e:
        log.exception("fetch failed city=%s", city)
        await call.message.edit_text(f"❌ Ошибка при загрузке: {e}")
        return

    no_preview = LinkPreviewOptions(is_disabled=True)
    await call.message.edit_text(
        chunks[0], reply_markup=kb,
        parse_mode="HTML", link_preview_options=no_preview,
    )
    for chunk in chunks[1:]:
        await call.message.answer(chunk, parse_mode="HTML", link_preview_options=no_preview)


@dp.callback_query(F.data.startswith("sub:"))
async def cb_subscribe(call: CallbackQuery):
    city = call.data.split(":", 1)[1]
    uid = call.from_user.id if call.from_user else 0
    city_norm = normalize_city(city)
    ok = add_subscription(uid, call.message.chat.id, city, city_norm)
    await call.answer("🔔 Подписка оформлена!" if ok else "ℹ️ Вы уже подписаны.", show_alert=True)


# ─── admin ────────────────────────────────────────────────────────────────────

def _require_admin(message: Message) -> bool:
    return is_admin(message.from_user.id if message.from_user else 0)


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    _track(message)
    if not _require_admin(message):
        await message.answer("⛔ Доступ только для администраторов.")
        return
    for chunk in _fmt_stats(get_stats()):
        await message.answer(chunk, parse_mode="HTML")


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
        since = _to_msk(a["first_seen"], "%d.%m.%Y")
        lines.append(f"• {tag}\n  ID: <code>{a['user_id']}</code> | с {since}")
    await message.answer("\n".join(lines), parse_mode="HTML")


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
        await message.answer("❌ Пользователь не найден. Он должен написать боту хотя бы раз.")
        return
    if user["role"] == "admin":
        await message.answer(f"ℹ️ {_user_tag(user['first_name'], user['last_name'], user['username'])} уже администратор.")
        return
    set_role(user_id, "admin")
    tag = _user_tag(user["first_name"], user["last_name"], user["username"])
    log.info("Admin added: %s id=%s by %s", tag, user_id, message.from_user.id)
    try:
        await message.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=user_id))
    except Exception:
        pass
    await message.answer(f"✅ {tag} теперь администратор.")


@dp.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message):
    _track(message)
    if not _require_admin(message):
        await message.answer("⛔ Доступ только для администраторов.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Укажите ID или @username:\n<code>/removeadmin 123456789</code>")
        return
    arg = parts[1].strip()
    if arg.lstrip("@").isdigit():
        user_id = int(arg.lstrip("@"))
        user = get_user(user_id)
    else:
        user = get_user_by_username(arg)
        user_id = user["user_id"] if user else None
    if not user:
        await message.answer("❌ Пользователь не найден.")
        return
    uid_self = message.from_user.id if message.from_user else 0
    if user_id == uid_self:
        await message.answer("❌ Нельзя снять права у самого себя.")
        return
    if user["role"] != "admin":
        await message.answer(f"ℹ️ {_user_tag(user['first_name'], user['last_name'], user['username'])} и так не администратор.")
        return
    set_role(user_id, "user")
    tag = _user_tag(user["first_name"], user["last_name"], user["username"])
    log.info("Admin removed: %s id=%s by %s", tag, user_id, message.from_user.id)
    try:
        await message.bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeChat(chat_id=user_id))
    except Exception:
        pass
    await message.answer(f"✅ Права администратора у {tag} сняты.")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    _track(message)
    if not _require_admin(message):
        await message.answer("⛔ Доступ только для администраторов.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Текст рассылки: <code>/broadcast Привет всем!</code>")
        return
    text = parts[1].strip()
    users = list_all_users()
    sent, failed = 0, 0
    status = await message.answer(f"⏳ Рассылка для {len(users)} пользователей...")
    for u in users:
        try:
            await message.bot.send_message(u["user_id"], f"📢 {text}")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # не упереться в rate limit
    await status.edit_text(f"✅ Рассылка завершена.\nОтправлено: {sent}\nНе удалось: {failed}")


@dp.message(Command("export"))
async def cmd_export(message: Message):
    _track(message)
    if not _require_admin(message):
        await message.answer("⛔ Доступ только для администраторов.")
        return
    s = get_stats()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "name", "username", "role", "total_queries", "ok", "err", "last_seen_msk"])
    for u in s["all_users"]:
        writer.writerow([
            u["user_id"],
            _user_tag(u["first_name"], u["last_name"], None),
            u["username"] or "",
            u["role"],
            u["total_queries"] or 0,
            u["ok"] or 0,
            u["err"] or 0,
            _to_msk(u["last_seen"], "%Y-%m-%d %H:%M"),
        ])
    data = buf.getvalue().encode("utf-8-sig")
    fname = f"stats_{datetime.now(MSK).strftime('%Y%m%d_%H%M')}.csv"
    await message.answer_document(BufferedInputFile(data, filename=fname))


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
        text = json.dumps(raw[0], ensure_ascii=False, indent=2)
        for i in range(0, len(text), 3800):
            chunk = f"<pre>{text[i:i+3800]}</pre>"
            if i == 0:
                await msg.edit_text(chunk, parse_mode="HTML")
            else:
                await message.answer(chunk, parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


# ─── background: subscription polling ────────────────────────────────────────

async def _check_subscriptions(bot: Bot):
    while True:
        await asyncio.sleep(SUBSCRIPTION_CHECK_INTERVAL)
        subs = all_subscriptions()
        if not subs:
            continue
        log.info("Checking %d subscriptions", len(subs))
        seen_cities: dict[str, NearbyResult] = {}
        for sub in subs:
            try:
                if sub["city_norm"] not in seen_cities:
                    seen_cities[sub["city_norm"]] = await fetch_city_fuel(sub["city"])
                result = seen_cities[sub["city_norm"]]
                if result.error:
                    continue

                available = [s for s in result.stations if s.status in ("yes", "queue", "low")]
                new_status = "yes" if available else "no"

                if sub["last_status"] is not None and sub["last_status"] != new_status:
                    if new_status == "yes":
                        text = (
                            f"⛽ <b>{result.city}</b>: появилось топливо!\n"
                            f"Доступно станций: {len(available)}\n"
                            f"Проверьте: /fuelnow {result.city}"
                        )
                    else:
                        text = f"⚠️ <b>{result.city}</b>: топливо закончилось на всех АЗС."
                    try:
                        await bot.send_message(sub["chat_id"], text)
                    except Exception as e:
                        log.warning("Could not notify sub %s: %s", sub["id"], e)

                update_subscription_status(sub["id"], new_status)
            except Exception:
                log.exception("Subscription check failed for sub_id=%s", sub["id"])


# ─── main ─────────────────────────────────────────────────────────────────────

async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set")

    init_db()

    for uid in SEED_ADMINS:
        user = get_user(uid)
        if user and user["role"] != "admin":
            set_role(uid, "admin")
            log.info("Seed admin promoted: %s", uid)
        elif not user:
            log.warning("Seed admin %s not in DB yet — they need to /start first", uid)

    log.info("Seed admins: %s", SEED_ADMINS or "none")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))

    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())

    for admin in list_admins():
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin["user_id"]))
        except Exception as e:
            log.warning("Could not set commands for admin %s: %s", admin["user_id"], e)

    asyncio.create_task(_check_subscriptions(bot))

    log.info("Bot started")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
