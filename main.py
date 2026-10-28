"""Telegram-бот: файл по ссылке после проверки подписки.

Локально:
    pip install -r requirements.txt
    python main.py

Render (Web Service):
    Build: pip install -r requirements.txt
    Start: python -u main.py
    Env: BOT_TOKEN, ADMIN_IDS, MANDATORY_CHANNELS
    Бесплатный тариф засыпает ~15 минут без HTTP — пингуйте URL или Background Worker.

Бот должен быть администратором обязательного канала и каждого канала из админки,
иначе get_chat_member недоступен.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8573398640:AAELCe9o54NX2Tm0Dsewcxln2ytDhHpuA_I").strip()
_DEFAULT_ADMINS = "7479229021,823427049"
ADMIN_IDS = frozenset(
    int(part.strip())
    for part in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", _DEFAULT_ADMINS)).split(",")
    if part.strip().lstrip("-").isdigit()
)
_DEFAULT_MANDATORY = "@amstercheats,@amstercloud"
_REMOVED_CHANNELS = frozenset({"@dfgames2"})


def _parse_channel_list(raw: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        if not value.startswith("@") and not value.lstrip("-").isdigit():
            value = f"@{value}"
        key = value.lower()
        if key in seen or key in _REMOVED_CHANNELS:
            continue
        seen.add(key)
        result.append(value)
    return result


MANDATORY_CHANNELS = _parse_channel_list(
    os.getenv("MANDATORY_CHANNELS", os.getenv("MANDATORY_CHANNEL", _DEFAULT_MANDATORY))
)
if not MANDATORY_CHANNELS:
    MANDATORY_CHANNELS = _parse_channel_list(_DEFAULT_MANDATORY)

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"
ALLOWED_MEMBER_STATUSES = frozenset({"member", "creator", "administrator"})
HASH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
CHANNEL_ID_RE = re.compile(r"^-?\d{5,20}$")
INVITE_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)/?$",
    re.IGNORECASE,
)
NO_LINK = LinkPreviewOptions(is_disabled=True)
MANDATORY_LOOKUP = {item.lower() for item in MANDATORY_CHANNELS}


def normalize_invite_url(raw: str) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    match = INVITE_RE.fullmatch(value)
    if not match:
        return None
    code = match.group(1)
    return f"https://t.me/+{code}"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("file_bot")

router = Router(name="users")
admin_router = Router(name="admin")


class AdminFilter(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return user is not None and user.id in ADMIN_IDS


admin_router.message.filter(AdminFilter(), F.chat.type == ChatType.PRIVATE)
admin_router.callback_query.filter(AdminFilter())


class AdminStates(StatesGroup):
    add_channel = State()
    delete_channel = State()
    set_link_channel = State()
    set_link_url = State()


def chat_target(channel: str) -> int | str:
    if channel.lstrip("-").isdigit():
        return int(channel)
    return channel


def normalize_channel(raw: str) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None

    # Если в строке есть и канал, и invite — берём первый токен как канал.
    if " " in value or "\n" in value:
        first = re.split(r"\s+", value, maxsplit=1)[0]
        return normalize_channel(first)

    lowered = value.lower()
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "t.me/"):
        if lowered.startswith(prefix):
            value = value[len(prefix):].split("?", 1)[0].strip("/")
            value = value.split("/", 1)[0]
            if not value or value.startswith("+") or value.lower().startswith("joinchat"):
                return None
            value = value if value.startswith("@") else f"@{value}"
            break

    if value.startswith("@"):
        name = value[1:]
        if USERNAME_RE.fullmatch(name):
            return f"@{name}"
        return None
    if CHANNEL_ID_RE.fullmatch(value):
        return str(int(value))
    if USERNAME_RE.fullmatch(value):
        return f"@{value}"
    return None


def split_channel_and_invite(raw: str) -> tuple[str | None, str | None]:
    """Разбор: '@channel' или '@channel https://t.me/+xxx'."""
    text = (raw or "").strip()
    if not text:
        return None, None
    parts = re.split(r"\s+", text, maxsplit=1)
    channel = normalize_channel(parts[0])
    invite = normalize_invite_url(parts[1]) if len(parts) > 1 else None
    return channel, invite


def _parse_button_links(raw: str) -> dict[str, str]:
    """Формат: @channel=https://t.me/+xxx,@other=https://t.me/+yyy"""
    result: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        left, right = part.split("=", 1)
        channel = normalize_channel(left.strip())
        invite = normalize_invite_url(right.strip())
        if channel and invite:
            result[channel.lower()] = invite
    return result


# Ссылки на кнопках подписки (invite/трекинг). Проверка подписки идёт по @каналу.
_DEFAULT_BUTTON_LINKS = "@amstercheats=https://t.me/+9OI-NIb-CPozNDg6"
BUTTON_LINKS: dict[str, str] = _parse_button_links(
    os.getenv("CHANNEL_BUTTON_LINKS", _DEFAULT_BUTTON_LINKS)
)


def member_status(member: object) -> str:
    status = getattr(member, "status", "")
    if hasattr(status, "value"):
        return str(status.value)
    return str(status)


async def db_conn() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH, timeout=30)
    db.row_factory = aiosqlite.Row
    return db


def _invite_meta_key(channel: str) -> str:
    return f"invite:{channel.lower()}"


async def init_db() -> None:
    db = await db_conn()
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY,
                channel_id TEXT,
                invite_url TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                file_id TEXT,
                file_hash TEXT UNIQUE,
                file_type TEXT,
                caption TEXT
            )
            """
        )
        await db.commit()
        try:
            await db.execute("ALTER TABLE files ADD COLUMN caption TEXT")
            await db.commit()
        except sqlite3.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE channels ADD COLUMN invite_url TEXT")
            await db.commit()
        except sqlite3.OperationalError:
            pass
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await db.commit()
        # Одноразовая очистка тестовых загрузок админов (wipe_files_v1).
        cursor = await db.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("wipe_files_v1",),
        )
        wiped = await cursor.fetchone()
        if wiped is None:
            await db.execute("DELETE FROM files")
            await db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("wipe_files_v1", "done"),
            )
            await db.commit()
            logger.info("Очищены старые записи files (wipe_files_v1)")
        # Удаляем старые обязательные и дубликаты текущих обязательных из таблицы.
        cursor = await db.execute("SELECT id, channel_id FROM channels")
        rows = await cursor.fetchall()
        for row in rows:
            channel_id = (row["channel_id"] or "").strip().lower()
            if channel_id in _REMOVED_CHANNELS or channel_id in MANDATORY_LOOKUP:
                await db.execute("DELETE FROM channels WHERE id = ?", (row["id"],))
        await db.commit()
        # Дефолтные invite-ссылки для обязательных каналов (если ещё не заданы).
        for channel, invite in BUTTON_LINKS.items():
            key = _invite_meta_key(channel)
            cursor = await db.execute("SELECT value FROM meta WHERE key = ?", (key,))
            if await cursor.fetchone() is None:
                await db.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    (key, invite),
                )
        await db.commit()
    finally:
        await db.close()


async def get_invite_url(channel: str) -> str | None:
    key = channel.lower()
    if key in BUTTON_LINKS:
        cached = BUTTON_LINKS[key]
    else:
        cached = None
    db = await db_conn()
    try:
        # Сначала meta (обязательные и /setlink).
        cursor = await db.execute(
            "SELECT value FROM meta WHERE key = ?",
            (_invite_meta_key(channel),),
        )
        row = await cursor.fetchone()
        if row and row["value"]:
            return str(row["value"])
        # Затем колонка channels.invite_url.
        cursor = await db.execute(
            "SELECT invite_url FROM channels WHERE lower(channel_id) = ?",
            (key,),
        )
        row = await cursor.fetchone()
        if row and row["invite_url"]:
            return str(row["invite_url"])
    finally:
        await db.close()
    return cached


async def set_invite_url(channel: str, invite: str | None) -> tuple[bool, str]:
    channel_norm = normalize_channel(channel)
    if channel_norm is None:
        return False, "Отправьте юзернейм канала (с @) или числовой ID."
    invite_norm = normalize_invite_url(invite) if invite else None
    if invite and invite_norm is None:
        return False, "Нужна invite-ссылка вида https://t.me/+XXXX"

    key = channel_norm.lower()
    db = await db_conn()
    try:
        if key in MANDATORY_LOOKUP:
            meta_key = _invite_meta_key(channel_norm)
            if invite_norm:
                await db.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (meta_key, invite_norm),
                )
            else:
                await db.execute("DELETE FROM meta WHERE key = ?", (meta_key,))
            await db.commit()
            if invite_norm:
                BUTTON_LINKS[key] = invite_norm
            else:
                BUTTON_LINKS.pop(key, None)
            text = (
                f"Для {channel_norm} кнопка: {invite_norm}"
                if invite_norm
                else f"Invite-ссылка для {channel_norm} снята."
            )
            return True, text

        cursor = await db.execute(
            "SELECT id FROM channels WHERE lower(channel_id) = ?",
            (key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return False, "Канал не найден. Сначала добавьте его через /addchannel."
        await db.execute(
            "UPDATE channels SET invite_url = ? WHERE id = ?",
            (invite_norm, row["id"]),
        )
        await db.commit()
    finally:
        await db.close()

    if invite_norm:
        return True, f"Для {channel_norm} кнопка: {invite_norm}"
    return True, f"Invite-ссылка для {channel_norm} снята."


async def required_channels() -> list[str]:
    channels = list(MANDATORY_CHANNELS)
    seen = set(MANDATORY_LOOKUP)
    db = await db_conn()
    try:
        cursor = await db.execute("SELECT channel_id FROM channels ORDER BY id")
        rows = await cursor.fetchall()
    finally:
        await db.close()
    for row in rows:
        channel_id = (row["channel_id"] or "").strip()
        key = channel_id.lower()
        if channel_id and key not in seen and key not in _REMOVED_CHANNELS:
            seen.add(key)
            channels.append(channel_id)
    return channels


async def list_channels() -> list[sqlite3.Row]:
    db = await db_conn()
    try:
        cursor = await db.execute(
            "SELECT id, channel_id, invite_url FROM channels ORDER BY id"
        )
        return await cursor.fetchall()
    finally:
        await db.close()


async def add_channel(raw: str, bot: Bot | None = None) -> tuple[bool, str]:
    channel, invite = split_channel_and_invite(raw)
    if channel is None:
        # Чистая invite без @ — не подходит для проверки подписки.
        if normalize_invite_url(raw.strip().split()[0] if raw.strip() else ""):
            return (
                False,
                "Invite-ссылка одна не подходит.\n"
                "Добавляйте так:\n"
                "@channel https://t.me/+XXXX\n"
                "Проверка — по @каналу, кнопка — по invite.",
            )
        return (
            False,
            "Отправьте юзернейм канала (с @) или числовой ID.\n"
            "Можно сразу с invite: @channel https://t.me/+XXXX",
        )
    if len(raw.strip().split()) > 1 and invite is None:
        return False, "Вторая часть должна быть invite-ссылкой https://t.me/+XXXX"

    current = await required_channels()
    if any(channel.lower() == item.lower() for item in current):
        if invite:
            ok, text = await set_invite_url(channel, invite)
            return ok, ("Канал уже есть. " + text) if ok else text
        return False, "Этот канал уже есть в списке проверки."

    db = await db_conn()
    try:
        await db.execute(
            "INSERT INTO channels (channel_id, invite_url) VALUES (?, ?)",
            (channel, invite),
        )
        await db.commit()
    finally:
        await db.close()

    text = f"Канал {channel} добавлен."
    if invite:
        text += f"\nКнопка: {invite}"
    else:
        text += (
            "\nПодсказка: для трекинга задайте invite-ссылку:\n"
            f"/setlink {channel} https://t.me/+XXXX"
        )
    probe_user = next(iter(ADMIN_IDS), 0)
    if bot is not None and probe_user and await is_subscribed(bot, probe_user, channel) is None:
        text += (
            "\nПредупреждение: бот не может проверить этот канал. "
            "Добавьте его администратором."
        )
    return True, text


async def delete_channel_by_ref(raw: str) -> tuple[bool, str]:
    channel = normalize_channel(raw)
    if channel is None:
        return False, "Отправьте юзернейм канала (с @) или числовой ID."
    if channel.lower() in MANDATORY_LOOKUP:
        return False, "Обязательный канал удалить нельзя."

    db = await db_conn()
    try:
        cursor = await db.execute("SELECT id, channel_id FROM channels")
        rows = await cursor.fetchall()
        target_id = None
        for row in rows:
            if str(row["channel_id"]).lower() == channel.lower():
                target_id = row["id"]
                break
        if target_id is None:
            return False, "Канал не найден."
        await db.execute("DELETE FROM channels WHERE id = ?", (target_id,))
        await db.commit()
    finally:
        await db.close()
    return True, f"Канал {channel} удалён."


async def delete_channel_by_id(channel_pk: int) -> str | None:
    db = await db_conn()
    try:
        cursor = await db.execute(
            "SELECT channel_id FROM channels WHERE id = ?",
            (channel_pk,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        await db.execute("DELETE FROM channels WHERE id = ?", (channel_pk,))
        await db.commit()
        return str(row["channel_id"])
    finally:
        await db.close()

async def save_content(
    file_type: str,
    *,
    file_id: str | None = None,
    caption: str | None = None,
) -> str:
    caption_value = (caption or "").strip() or None
    file_value = (file_id or "").strip() or None
    if file_type == "text":
        if not caption_value:
            raise ValueError("Пустой текст")
        file_value = None
    elif not file_value:
        raise ValueError("Нет file_id")

    db = await db_conn()
    try:
        for _ in range(8):
            file_hash = secrets.token_urlsafe(6)
            try:
                await db.execute(
                    """
                    INSERT INTO files (file_id, file_hash, file_type, caption)
                    VALUES (?, ?, ?, ?)
                    """,
                    (file_value, file_hash, file_type, caption_value),
                )
                await db.commit()
                return file_hash
            except sqlite3.IntegrityError:
                continue
    finally:
        await db.close()
    raise RuntimeError("Не удалось создать уникальный хэш")


async def get_file(file_hash: str) -> sqlite3.Row | None:
    db = await db_conn()
    try:
        cursor = await db.execute(
            "SELECT file_id, file_type, caption FROM files WHERE file_hash = ?",
            (file_hash,),
        )
        return await cursor.fetchone()
    finally:
        await db.close()


async def is_subscribed(bot: Bot, user_id: int, channel: str) -> bool | None:
    try:
        member = await bot.get_chat_member(chat_id=chat_target(channel), user_id=user_id)
    except Exception as exc:
        logger.warning(
            "Предупреждение: не удалось проверить подписку user_id=%s на %s (%s). "
            "Добавьте бота администратором этого канала.",
            user_id,
            channel,
            exc,
        )
        return None
    return member_status(member) in ALLOWED_MEMBER_STATUSES


async def check_subscriptions(bot: Bot, user_id: int) -> tuple[list[str], list[str]]:
    channels = await required_channels()
    results = await asyncio.gather(
        *(is_subscribed(bot, user_id, channel) for channel in channels)
    )
    missing: list[str] = []
    failed: list[str] = []
    for channel, result in zip(channels, results):
        if result is None:
            failed.append(channel)
        elif not result:
            missing.append(channel)
    return missing, failed


def gate_text(failed: list[str]) -> str:
    text = "Подпишитесь на каналы, чтобы получить файл"
    if not failed:
        return text
    names = ", ".join(failed)
    return (
        f"{text}\n\n"
        f"Предупреждение: не удалось проверить {names}. "
        "Бот должен быть администратором этих каналов."
    )


async def channel_list_text(rows: list[sqlite3.Row]) -> str:
    lines = ["Обязательные каналы:"]
    for channel in MANDATORY_CHANNELS:
        invite = await get_invite_url(channel)
        if invite:
            lines.append(f"• {channel}\n  кнопка: {invite}")
        else:
            lines.append(f"• {channel}")
    if not rows:
        lines.append("Дополнительные каналы не добавлены.")
        return "\n".join(lines)
    lines.append("Дополнительные каналы:")
    for row in rows:
        channel_id = row["channel_id"]
        try:
            invite = row["invite_url"]
        except (KeyError, IndexError):
            invite = None
        invite = invite or await get_invite_url(str(channel_id))
        if invite:
            lines.append(f"• {channel_id}\n  кнопка: {invite}")
        else:
            lines.append(f"• {channel_id}")
    return "\n".join(lines)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Добавить канал", callback_data="admin:add")],
            [InlineKeyboardButton(text="Ссылка кнопки", callback_data="admin:link")],
            [InlineKeyboardButton(text="Удалить канал", callback_data="admin:del")],
            [InlineKeyboardButton(text="Список каналов", callback_data="admin:list")],
        ]
    )


def delete_keyboard(rows: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        label = f"Удалить {row['channel_id']}"
        if len(label) > 64:
            label = label[:61] + "..."
        buttons.append(
            [InlineKeyboardButton(text=label, callback_data=f"delch:{row['id']}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def channel_button(bot: Bot, channel: str) -> InlineKeyboardButton:
    title = channel
    # Приоритет: invite/трекинг-ссылка (как хочет заказчик).
    url: str | None = await get_invite_url(channel)
    try:
        chat = await bot.get_chat(chat_target(channel))
    except Exception as exc:
        logger.warning(
            "Предупреждение: не удалось получить данные канала %s (%s).",
            channel,
            exc,
        )
        chat = None
    if chat is not None:
        title = chat.title or channel
        if url is None:
            if chat.username:
                url = f"https://t.me/{chat.username}"
            elif chat.invite_link:
                url = chat.invite_link
    if url is None and channel.startswith("@"):
        url = f"https://t.me/{channel[1:]}"
    title = (title or channel).strip()
    if len(title) > 60:
        title = title[:57] + "..."
    if url:
        return InlineKeyboardButton(text=title, url=url)
    return InlineKeyboardButton(text=title, callback_data="channel:nolink")


async def build_subscribe_keyboard(bot: Bot, file_hash: str) -> InlineKeyboardMarkup:
    rows = [[await channel_button(bot, channel)] for channel in await required_channels()]
    rows.append(
        [InlineKeyboardButton(text="Проверить подписку", callback_data=f"check_sub:{file_hash}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def extract_file(message: Message) -> tuple[str, str] | None:
    if message.document:
        return message.document.file_id, "document"
    if message.video:
        return message.video.file_id, "video"
    if message.photo:
        return message.photo[-1].file_id, "photo"
    return None


def row_caption(row: sqlite3.Row) -> str | None:
    try:
        value = row["caption"]
    except (KeyError, IndexError):
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def send_stored_content(bot: Bot, chat_id: int, row: sqlite3.Row) -> None:
    file_type = (row["file_type"] or "document").strip()
    caption = row_caption(row)
    file_id = (row["file_id"] or "").strip()

    if file_type == "text":
        if not caption:
            raise RuntimeError("Пустой текст в записи")
        await bot.send_message(chat_id, caption)
        return

    if not file_id:
        raise RuntimeError("Пустой file_id")

    # Подпись у медиа в Telegram до 1024 символов; длинный текст шлём отдельно.
    media_caption = caption if caption and len(caption) <= 1024 else None
    overflow = caption if caption and len(caption) > 1024 else None

    if file_type == "photo":
        await bot.send_photo(chat_id, file_id, caption=media_caption)
    elif file_type == "video":
        await bot.send_video(chat_id, file_id, caption=media_caption)
    else:
        await bot.send_document(chat_id, file_id, caption=media_caption)

    if overflow:
        await bot.send_message(chat_id, overflow)
    elif caption and media_caption is None:
        await bot.send_message(chat_id, caption)


async def reply_with_share_link(message: Message, bot: Bot, file_hash: str, kind: str) -> None:
    username = await ensure_username(bot)
    shown = f"t.me/{username}?start={file_hash}"
    url = f"https://t.me/{username}?start={file_hash}"
    label = "Ссылка на текст" if kind == "text" else "Ссылка на файл"
    await message.answer(
        f'{label}:\n<a href="{url}">{shown}</a>',
        parse_mode=ParseMode.HTML,
        link_preview_options=NO_LINK,
    )


async def ensure_username(bot: Bot) -> str:
    me = await bot.get_me()
    if not me.username:
        raise RuntimeError("У бота нет username")
    return me.username


async def notify(event: Message | CallbackQuery, text: str, **kwargs: object) -> None:
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await event.message.answer(text, **kwargs)
            return
        await event.bot.send_message(event.from_user.id, text, **kwargs)
        return
    await event.answer(text, **kwargs)


async def deliver_or_gate(bot: Bot, chat_id: int, user_id: int, file_hash: str, row: sqlite3.Row) -> tuple[str, InlineKeyboardMarkup] | None:
    missing, failed = await check_subscriptions(bot, user_id)
    if not missing and not failed:
        await send_stored_content(bot, chat_id, row)
        return None
    keyboard = await build_subscribe_keyboard(bot, file_hash)
    return gate_text(failed), keyboard


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    payload = ""
    if command.args:
        payload = command.args.strip().split(maxsplit=1)[0]

    if message.from_user is None:
        return

    if not payload:
        if message.from_user.id in ADMIN_IDS:
            await message.answer(
                "Админ-панель.\n"
                "• Файл (документ/фото/видео) — ссылка на файл\n"
                "• Файл с подписью — файл + текст пользователю\n"
                "• Просто текст — ссылка только на текст",
                reply_markup=admin_keyboard(),
            )
            return
        await message.answer("Перейдите по ссылке на файл или текст, чтобы получить его.")
        return

    if not HASH_RE.fullmatch(payload):
        await message.answer("Файл не найден")
        return

    row = await get_file(payload)
    if row is None:
        await message.answer("Файл не найден")
        return

    try:
        gate = await deliver_or_gate(bot, message.chat.id, message.from_user.id, payload, row)
    except Exception as exc:
        logger.warning("Предупреждение: не удалось отправить контент %s (%s).", payload, exc)
        await message.answer("Не удалось отправить. Попробуйте позже.")
        return
    if gate is not None:
        text, keyboard = gate
        await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("check_sub:"))
async def cb_check_sub(callback: CallbackQuery, bot: Bot) -> None:
    file_hash = callback.data.split(":", 1)[1]
    if not HASH_RE.fullmatch(file_hash):
        await callback.answer("Файл не найден", show_alert=True)
        return

    row = await get_file(file_hash)
    if row is None:
        await callback.answer("Файл не найден", show_alert=True)
        return

    missing, failed = await check_subscriptions(bot, callback.from_user.id)
    if missing or failed:
        keyboard = await build_subscribe_keyboard(bot, file_hash)
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(gate_text(failed), reply_markup=keyboard)
            except TelegramBadRequest:
                pass
        if missing:
            await callback.answer("Вы подписались не на все каналы", show_alert=True)
        else:
            await callback.answer(
                "Не удалось проверить подписку. Бот должен быть администратором канала.",
                show_alert=True,
            )
        return

    await callback.answer()
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    try:
        await send_stored_content(bot, callback.from_user.id, row)
    except Exception as exc:
        logger.warning("Предупреждение: не удалось отправить контент %s (%s).", file_hash, exc)
        await bot.send_message(callback.from_user.id, "Не удалось отправить. Попробуйте позже.")


@router.callback_query(F.data == "channel:nolink")
async def cb_channel_without_link(callback: CallbackQuery) -> None:
    await callback.answer("Ссылка на этот канал недоступна.", show_alert=True)


@router.callback_query(F.data.startswith("admin:"))
@router.callback_query(F.data.startswith("delch:"))
async def reject_admin_callback(callback: CallbackQuery) -> None:
    await callback.answer("Недоступно", show_alert=True)


@admin_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=admin_keyboard())


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Админ-панель.\n"
        "• Файл / текст — ссылка для раздачи\n"
        "• Каналы: проверка по @, кнопка может быть invite (t.me/+…)\n"
        "• /setlink @channel https://t.me/+XXXX — ссылка в кнопке",
        reply_markup=admin_keyboard(),
    )


@admin_router.message(Command("channels"))
async def cmd_channels(message: Message) -> None:
    rows = await list_channels()
    await message.answer(await channel_list_text(rows), reply_markup=admin_keyboard())


def command_argument(command: CommandObject) -> str:
    if not command.args:
        return ""
    return command.args.strip().split(maxsplit=1)[0].strip(",;")


def command_full_args(command: CommandObject) -> str:
    return (command.args or "").strip()


@admin_router.message(Command("addchannel"))
async def cmd_addchannel(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    argument = command_full_args(command)
    if argument:
        await state.clear()
        ok, text = await add_channel(argument, bot)
        await message.answer(text, reply_markup=admin_keyboard() if ok else None)
        return
    await state.set_state(AdminStates.add_channel)
    await message.answer(
        "Отправьте юзернейм канала (с @) или ID.\n"
        "С invite для кнопки:\n"
        "@channel https://t.me/+XXXX\n"
        "Отмена — /cancel"
    )


@admin_router.message(Command("setlink"))
async def cmd_setlink(message: Message, command: CommandObject, state: FSMContext) -> None:
    argument = command_full_args(command)
    if argument:
        channel, invite = split_channel_and_invite(argument)
        if channel and invite:
            await state.clear()
            ok, text = await set_invite_url(channel, invite)
            await message.answer(text, reply_markup=admin_keyboard() if ok else None)
            return
        if channel and not invite and len(argument.split()) == 1:
            await state.clear()
            await state.update_data(set_link_channel=channel)
            await state.set_state(AdminStates.set_link_url)
            await message.answer(
                f"Канал {channel}.\n"
                "Отправьте invite-ссылку https://t.me/+XXXX\n"
                "Отмена — /cancel"
            )
            return
        await message.answer(
            "Формат:\n/setlink @channel https://t.me/+XXXX\n"
            "Или только /setlink @channel — потом пришлёте ссылку."
        )
        return
    await state.set_state(AdminStates.set_link_channel)
    await message.answer(
        "Отправьте @канал (или ID), для которого задать ссылку кнопки.\nОтмена — /cancel"
    )

@admin_router.message(Command("delchannel"))
async def cmd_delchannel(message: Message, command: CommandObject, state: FSMContext) -> None:
    argument = command_argument(command)
    if argument:
        await state.clear()
        ok, text = await delete_channel_by_ref(argument)
        await message.answer(text, reply_markup=admin_keyboard() if ok else None)
        return
    rows = await list_channels()
    if not rows:
        await state.clear()
        await message.answer(
            "Дополнительных каналов нет. Обязательный канал удалить нельзя.",
            reply_markup=admin_keyboard(),
        )
        return
    await state.set_state(AdminStates.delete_channel)
    await message.answer(
        "Выберите канал для удаления или отправьте его @юзернейм / ID.\nОтмена — /cancel",
        reply_markup=delete_keyboard(rows),
    )


@admin_router.message(AdminStates.add_channel, F.text, ~F.text.startswith("/"))
async def on_add_channel_text(message: Message, state: FSMContext, bot: Bot) -> None:
    ok, text = await add_channel(message.text or "", bot)
    if ok:
        await state.clear()
        await message.answer(text, reply_markup=admin_keyboard())
        return
    await message.answer(text)


@admin_router.message(AdminStates.set_link_channel, F.text, ~F.text.startswith("/"))
async def on_set_link_channel(message: Message, state: FSMContext) -> None:
    channel, invite = split_channel_and_invite(message.text or "")
    if channel is None:
        await message.answer("Нужен @юзернейм или числовой ID канала.")
        return
    if invite:
        await state.clear()
        ok, text = await set_invite_url(channel, invite)
        await message.answer(text, reply_markup=admin_keyboard() if ok else None)
        return
    await state.update_data(set_link_channel=channel)
    await state.set_state(AdminStates.set_link_url)
    await message.answer(
        f"Канал {channel}.\nОтправьте invite-ссылку https://t.me/+XXXX\nОтмена — /cancel"
    )


@admin_router.message(AdminStates.set_link_url, F.text, ~F.text.startswith("/"))
async def on_set_link_url(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    channel = data.get("set_link_channel")
    if not channel:
        await state.clear()
        await message.answer("Начните заново: /setlink")
        return
    ok, text = await set_invite_url(str(channel), message.text or "")
    if ok:
        await state.clear()
        await message.answer(text, reply_markup=admin_keyboard())
        return
    await message.answer(text)


@admin_router.message(AdminStates.delete_channel, F.text, ~F.text.startswith("/"))
async def on_delete_channel_text(message: Message, state: FSMContext) -> None:
    ok, text = await delete_channel_by_ref(message.text or "")
    if ok:
        await state.clear()
        await message.answer(text, reply_markup=admin_keyboard())
        return
    await message.answer(text)

@admin_router.message(
    F.content_type.in_({"document", "video", "photo"}),
)
async def admin_file(message: Message, bot: Bot) -> None:
    extracted = extract_file(message)
    if extracted is None:
        return
    file_id, file_type = extracted
    caption = (message.caption or "").strip() or None
    try:
        file_hash = await save_content(file_type, file_id=file_id, caption=caption)
        await reply_with_share_link(message, bot, file_hash, "file")
    except Exception as exc:
        logger.warning("Предупреждение: не удалось сохранить файл (%s).", exc)
        await message.answer("Не удалось создать ссылку. Отправьте файл ещё раз.")
        return
    logger.info(
        "Файл сохранён: type=%s hash=%s caption=%s",
        file_type,
        file_hash,
        bool(caption),
    )


@admin_router.message(F.text, ~F.text.startswith("/"))
async def admin_text(message: Message, bot: Bot, state: FSMContext) -> None:
    current = await state.get_state()
    if current is not None:
        return
    text = (message.text or "").strip()
    if not text:
        return
    try:
        file_hash = await save_content("text", caption=text)
        await reply_with_share_link(message, bot, file_hash, "text")
    except Exception as exc:
        logger.warning("Предупреждение: не удалось сохранить текст (%s).", exc)
        await message.answer("Не удалось создать ссылку. Отправьте текст ещё раз.")
        return
    logger.info("Текст сохранён: hash=%s len=%s", file_hash, len(text))


@admin_router.callback_query(F.data == "admin:add")
async def cb_admin_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.add_channel)
    await callback.answer()
    await notify(
        callback,
        "Отправьте юзернейм канала (с @) или ID.\n"
        "С invite для кнопки:\n"
        "@channel https://t.me/+XXXX\n"
        "Отмена — /cancel",
    )


@admin_router.callback_query(F.data == "admin:link")
async def cb_admin_link(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.set_link_channel)
    await callback.answer()
    await notify(
        callback,
        "Отправьте @канал (или ID), для которого задать ссылку кнопки.\n"
        "Можно сразу: @channel https://t.me/+XXXX\n"
        "Отмена — /cancel",
    )


@admin_router.callback_query(F.data == "admin:list")
async def cb_admin_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    rows = await list_channels()
    await callback.answer()
    await notify(callback, await channel_list_text(rows), reply_markup=admin_keyboard())

@admin_router.callback_query(F.data == "admin:del")
async def cb_admin_del(callback: CallbackQuery, state: FSMContext) -> None:
    rows = await list_channels()
    if not rows:
        await state.clear()
        await callback.answer("Дополнительных каналов нет.", show_alert=True)
        return
    await state.set_state(AdminStates.delete_channel)
    await callback.answer()
    await notify(
        callback,
        "Выберите канал для удаления или отправьте его @юзернейм / ID.\nОтмена — /cancel",
        reply_markup=delete_keyboard(rows),
    )


@admin_router.callback_query(F.data.startswith("delch:"))
async def cb_delete_channel(callback: CallbackQuery, state: FSMContext) -> None:
    raw_id = callback.data.split(":", 1)[1]
    if not raw_id.isdigit():
        await callback.answer("Канал не найден", show_alert=True)
        return
    deleted = await delete_channel_by_id(int(raw_id))
    if deleted is None:
        await callback.answer("Канал не найден", show_alert=True)
        return

    await state.clear()
    await callback.answer("Канал удалён")
    rows = await list_channels()
    text = f"Канал {deleted} удалён.\n\n{await channel_list_text(rows)}"
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=admin_keyboard())
            return
        except TelegramBadRequest:
            pass
    await notify(callback, text, reply_markup=admin_keyboard())


async def setup_admin_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Получить файл"),
        BotCommand(command="admin", description="Админ-панель"),
        BotCommand(command="addchannel", description="Добавить канал"),
        BotCommand(command="setlink", description="Ссылка кнопки канала"),
        BotCommand(command="delchannel", description="Удалить канал"),
        BotCommand(command="channels", description="Список каналов"),
        BotCommand(command="cancel", description="Отмена"),
    ]
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as exc:
            logger.warning(
                "Предупреждение: не удалось установить команды админа %s (%s).",
                admin_id,
                exc,
            )


def health_port() -> int | None:
    """Render и подобные хосты ждут HTTP на PORT, иначе считают сервис мёртвым."""
    raw = os.getenv("PORT")
    if raw and raw.isdigit():
        return int(raw)
    if os.getenv("RENDER") or os.getenv("SPACE_ID"):
        return 10000 if os.getenv("RENDER") else 7860
    return None


async def keep_health_server() -> None:
    port = health_port()
    if port is None:
        return

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.read(1024), timeout=1.0)
        except Exception:
            pass
        body = b"ok"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n\r\n"
            + body
        )
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "0.0.0.0", port)
    logger.info("Health-check слушает 0.0.0.0:%s", port)
    async with server:
        await server.serve_forever()


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Задайте BOT_TOKEN в переменных окружения")

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.include_router(router)
    health_task = asyncio.create_task(keep_health_server())
    try:
        me = await bot.get_me()
        await setup_admin_commands(bot)
        logger.info("Бот @%s запущен (db=%s)", me.username, DB_PATH)
        await dp.start_polling(bot)
    finally:
        health_task.cancel()
        try:
            await health_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        if policy is not None:
            asyncio.set_event_loop_policy(policy())
    asyncio.run(main())
