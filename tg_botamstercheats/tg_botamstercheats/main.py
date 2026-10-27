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

BOT_TOKEN = os.getenv("BOT_TOKEN", "8885848241:AAEC_CBMNYpB-u-L_QYR-UXm89kRzW7ydks").strip()
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
NO_LINK = LinkPreviewOptions(is_disabled=True)
MANDATORY_LOOKUP = {item.lower() for item in MANDATORY_CHANNELS}

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


def chat_target(channel: str) -> int | str:
    if channel.lstrip("-").isdigit():
        return int(channel)
    return channel


def normalize_channel(raw: str) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None

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


def member_status(member: object) -> str:
    status = getattr(member, "status", "")
    if hasattr(status, "value"):
        return str(status.value)
    return str(status)


async def db_conn() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH, timeout=30)
    db.row_factory = aiosqlite.Row
    return db


async def init_db() -> None:
    db = await db_conn()
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY,
                channel_id TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                file_id TEXT,
                file_hash TEXT UNIQUE,
                file_type TEXT
            )
            """
        )
        await db.commit()
        # Удаляем старые обязательные и дубликаты текущих обязательных из таблицы.
        cursor = await db.execute("SELECT id, channel_id FROM channels")
        rows = await cursor.fetchall()
        for row in rows:
            channel_id = (row["channel_id"] or "").strip().lower()
            if channel_id in _REMOVED_CHANNELS or channel_id in MANDATORY_LOOKUP:
                await db.execute("DELETE FROM channels WHERE id = ?", (row["id"],))
        await db.commit()
    finally:
        await db.close()


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
        cursor = await db.execute("SELECT id, channel_id FROM channels ORDER BY id")
        return await cursor.fetchall()
    finally:
        await db.close()


async def add_channel(raw: str, bot: Bot | None = None) -> tuple[bool, str]:
    channel = normalize_channel(raw)
    if channel is None:
        return False, "Отправьте юзернейм канала (с @) или числовой ID."

    current = await required_channels()
    if any(channel.lower() == item.lower() for item in current):
        return False, "Этот канал уже есть в списке проверки."

    db = await db_conn()
    try:
        await db.execute("INSERT INTO channels (channel_id) VALUES (?)", (channel,))
        await db.commit()
    finally:
        await db.close()

    text = f"Канал {channel} добавлен."
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


async def save_file(file_id: str, file_type: str) -> str:
    db = await db_conn()
    try:
        for _ in range(8):
            file_hash = secrets.token_urlsafe(6)
            try:
                await db.execute(
                    "INSERT INTO files (file_id, file_hash, file_type) VALUES (?, ?, ?)",
                    (file_id, file_hash, file_type),
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
            "SELECT file_id, file_type FROM files WHERE file_hash = ?",
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


def channel_list_text(rows: list[sqlite3.Row]) -> str:
    lines = ["Обязательные каналы:"]
    lines.extend(f"• {channel}" for channel in MANDATORY_CHANNELS)
    if not rows:
        lines.append("Дополнительные каналы не добавлены.")
        return "\n".join(lines)
    lines.append("Дополнительные каналы:")
    lines.extend(f"• {row['channel_id']}" for row in rows)
    return "\n".join(lines)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Добавить канал", callback_data="admin:add")],
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
    url: str | None = f"https://t.me/{channel[1:]}" if channel.startswith("@") else None
    if url is None:
        try:
            chat = await bot.get_chat(chat_target(channel))
        except Exception as exc:
            logger.warning(
                "Предупреждение: не удалось получить ссылку на канал %s (%s).",
                channel,
                exc,
            )
        else:
            title = chat.title or channel
            if chat.username:
                url = f"https://t.me/{chat.username}"
            elif chat.invite_link:
                url = chat.invite_link
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


async def send_stored_file(bot: Bot, chat_id: int, file_id: str, file_type: str) -> None:
    if file_type == "photo":
        await bot.send_photo(chat_id, file_id)
    elif file_type == "video":
        await bot.send_video(chat_id, file_id)
    else:
        await bot.send_document(chat_id, file_id)


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
        await send_stored_file(bot, chat_id, row["file_id"], row["file_type"] or "document")
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
                "Админ-панель.\nОтправьте документ, фото или видео — бот вернёт ссылку на файл.",
                reply_markup=admin_keyboard(),
            )
            return
        await message.answer("Перейдите по ссылке на файл, чтобы получить его.")
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
        logger.warning("Предупреждение: не удалось отправить файл %s (%s).", payload, exc)
        await message.answer("Не удалось отправить файл. Попробуйте позже.")
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
        await send_stored_file(
            bot,
            callback.from_user.id,
            row["file_id"],
            row["file_type"] or "document",
        )
    except Exception as exc:
        logger.warning("Предупреждение: не удалось отправить файл %s (%s).", file_hash, exc)
        await bot.send_message(callback.from_user.id, "Не удалось отправить файл. Попробуйте позже.")


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
        "Админ-панель.\nОтправьте документ, фото или видео — бот вернёт ссылку на файл.",
        reply_markup=admin_keyboard(),
    )


@admin_router.message(Command("channels"))
async def cmd_channels(message: Message) -> None:
    rows = await list_channels()
    await message.answer(channel_list_text(rows), reply_markup=admin_keyboard())


def command_argument(command: CommandObject) -> str:
    if not command.args:
        return ""
    return command.args.strip().split(maxsplit=1)[0].strip(",;")


@admin_router.message(Command("addchannel"))
async def cmd_addchannel(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    argument = command_argument(command)
    if argument:
        await state.clear()
        ok, text = await add_channel(argument, bot)
        await message.answer(text, reply_markup=admin_keyboard() if ok else None)
        return
    await state.set_state(AdminStates.add_channel)
    await message.answer("Отправьте юзернейм канала (с @) или его числовой ID.\nОтмена — /cancel")


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
    try:
        file_hash = await save_file(file_id, file_type)
        username = await ensure_username(bot)
    except Exception as exc:
        logger.warning("Предупреждение: не удалось сохранить файл (%s).", exc)
        await message.answer("Не удалось создать ссылку. Отправьте файл ещё раз.")
        return

    shown = f"t.me/{username}?start={file_hash}"
    url = f"https://t.me/{username}?start={file_hash}"
    logger.info("Файл сохранён: type=%s hash=%s", file_type, file_hash)
    await message.answer(
        f'Ссылка на файл:\n<a href="{url}">{shown}</a>',
        parse_mode=ParseMode.HTML,
        link_preview_options=NO_LINK,
    )


@admin_router.callback_query(F.data == "admin:add")
async def cb_admin_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.add_channel)
    await callback.answer()
    await notify(callback, "Отправьте юзернейм канала (с @) или его числовой ID.\nОтмена — /cancel")


@admin_router.callback_query(F.data == "admin:list")
async def cb_admin_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    rows = await list_channels()
    await callback.answer()
    await notify(callback, channel_list_text(rows), reply_markup=admin_keyboard())


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
    text = f"Канал {deleted} удалён.\n\n{channel_list_text(rows)}"
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
