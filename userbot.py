"""
UserBot — основной функционал на базе Telethon.
"""

import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import (
    SendMessageRequest,
    DeleteMessagesRequest,
    GetFullChatRequest,
)
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    PeerUser,
    PeerChat,
    PeerChannel,
    Message as TLMessage,
    DocumentAttributeVideo,
    InputMessageReactionsList,
)
from telethon.errors import FloodWaitError, UserNotParticipantError

import database as db
from config import (
    API_ID, API_HASH,
    ALL_EMOJI_IDS, EMOJI_PACK_1, EMOJI_PACK_2,
    KAWAII_PHRASES, KAWAII_ACTIONS,
)

logger = logging.getLogger(__name__)

# Словарь активных клиентов: user_id -> TelegramClient
active_clients: dict[int, TelegramClient] = {}

# Словарь активных задач спама: user_id -> asyncio.Task
spam_tasks: dict[int, asyncio.Task] = {}

# Словарь активных алерт-задач: (owner_id, username) -> asyncio.Task
alert_tasks: dict[tuple, asyncio.Task] = {}

# Словарь временных данных (буфер удалённых сообщений)
# message_id -> {chat_id, text, media, sender_id, date}
message_cache: dict[int, dict] = {}


# ─────────────────────────────────────────────
#  Утилиты
# ─────────────────────────────────────────────
def parse_duration(text: str) -> int:
    """
    Парсит строку длительности в секунды.
    Примеры: 5h -> 18000, 30m -> 1800, 10s -> 10, 2d -> 172800
    """
    text = text.lower().strip()
    units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    match = re.match(r'^(\d+)([smhd]?)$', text)
    if not match:
        return 0
    value = int(match.group(1))
    unit = match.group(2) or 's'
    return value * units.get(unit, 1)


def get_random_emoji_id() -> str:
    """Возвращает случайный ID премиум-эмодзи из обоих паков"""
    return random.choice(ALL_EMOJI_IDS)


def make_custom_emoji_text(emoji_id: str, fallback: str = "✨") -> str:
    """
    Формирует текст с кастомным эмодзи для Telethon.
    В Telethon это делается через entities, здесь просто возвращаем fallback.
    """
    return fallback


async def get_peer_id(client: TelegramClient, peer) -> int | None:
    """Получить числовой ID из peer"""
    try:
        if isinstance(peer, PeerUser):
            return peer.user_id
        elif isinstance(peer, PeerChat):
            return peer.chat_id
        elif isinstance(peer, PeerChannel):
            return peer.channel_id
        return None
    except Exception:
        return None


# ─────────────────────────────────────────────
#  Kawaii преобразование текста
# ─────────────────────────────────────────────
KAWAII_REPLACEMENTS = {
    "скачай": "ськачай chuu~😻",
    "пожалуйста": "пооожаалуйста",
    "зайчик": "зь-зь-зьайчик😻",
    "хуесос": "я💖 хуесос✨",
    "вытаскивать": "не в-взьдумай🧁 вытаськивать❣️",
    "да": "о да🎀",
    "кончи": "кончи💖 наа меня uwu~",
    "скачай": "ну с-с-скачай",
}

KAWAII_SUFFIXES = [
    " прыгает от радости ❣️",
    " топает лапками ❣️",
    " улыбается 💝",
    " прыгает от радости 🍓",
    " хихикает 💖",
    " краснеет 😻",
    " задумчиво мяукает 🎀",
    " смотрит в пустоту 💝",
]

# Полные kawaii фразы (приоритет перед заменами)
KAWAII_FULL_PHRASES = [
    ("ськачай chuu~😻 макс\nпрыгает от радости ❣️",
     ["скачай"]),
    ("ну с-с-скачай макс💓\nтопает лапками ❣️",
     ["скачай"]),
    ("пооожаалуйста\nулыбается 💝",
     ["пожалуйста"]),
    ("зь-зь-зьайчик😻\nпрыгает от радости 🍓",
     ["зайчик"]),
    ("кончи💖 наа меня uwu~\nхихикает 💖",
     ["кончи"]),
    ("о да🎀 к-к-кончи (╯✧▽✧)╯\nкраснеет 😻",
     ["да", "кончи"]),
    ("не в-взьдумай🧁 вытаськивать❣️\nзадумчиво мяукает 🎀",
     ["вытаскивать"]),
    ("я💖 хуесос✨\nсмотрит в пустоту 💝",
     ["хуесос"]),
]


def kawaii_transform(text: str) -> str:
    """Преобразует текст в kawaii-стиль"""
    text_lower = text.lower()

    # Проверяем полные фразы
    for kawaii_text, keywords in KAWAII_FULL_PHRASES:
        if any(kw in text_lower for kw in keywords):
            return kawaii_text

    # Базовые замены
    result = text
    for original, kawaii in KAWAII_REPLACEMENTS.items():
        pattern = re.compile(re.escape(original), re.IGNORECASE)
        result = pattern.sub(kawaii, result)

    # Добавляем случайный суффикс
    result += "\n" + random.choice(KAWAII_SUFFIXES)
    return result


# ─────────────────────────────────────────────
#  HELP текст
# ─────────────────────────────────────────────
HELP_TEXT = """```
╔══════════════════════════════╗
║      USERBOT — КОМАНДЫ       ║
╠══════════════════════════════╣
║ .help                        ║
║   Список всех команд         ║
╠══════════════════════════════╣
║ .spam [кол-во] [сек]         ║
║   Спам сообщениями           ║
║   Пример: .spam 20 5         ║
╠══════════════════════════════╣
║ .spamstop                    ║
║   Остановить спам            ║
╠══════════════════════════════╣
║ .info                        ║
║   Инфо о собеседнике         ║
╠══════════════════════════════╣
║ .check @username             ║
║   Последний онлайн           ║
╠══════════════════════════════╣
║ .allertCheck @username       ║
║   Алерт при появлении онлайн ║
╠══════════════════════════════╣
║ .offallert @username         ║
║   Отключить алерт            ║
╠══════════════════════════════╣
║ .mute [время]                ║
║   Мут собеседника            ║
║   Пример: .mute 5h           ║
╠══════════════════════════════╣
║ .unmute                      ║
║   Снять мут                  ║
╠══════════════════════════════╣
║ .kawaii                      ║
║   Вкл/выкл kawaii-режим      ║
╠══════════════════════════════╣
║ .saveoff                     ║
║   Выкл. сохранение сообщений ║
╠══════════════════════════════╣
║ .saveon                      ║
║   Вкл. сохранение сообщений  ║
╚══════════════════════════════╝
```"""


# ─────────────────────────────────────────────
#  Запуск UserBot для конкретного пользователя
# ─────────────────────────────────────────────
async def launch_userbot(user_id: int):
    """Запускает UserBot для пользователя"""
    user = await db.get_user(user_id)
    if not user or not user.get("session_string"):
        logger.warning(f"No session for user_id={user_id}")
        return

    # Если уже запущен — отключаем старый
    if user_id in active_clients:
        try:
            await active_clients[user_id].disconnect()
        except Exception:
            pass

    client = TelegramClient(
        StringSession(user["session_string"]),
        API_ID, API_HASH,
        device_model="UserBot",
        system_version="Windows 10",
        app_version="1.0",
    )

    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error(f"User {user_id} is not authorized")
            return

        active_clients[user_id] = client
        me = await client.get_me()
        logger.info(f"UserBot started for @{me.username} (user_id={user_id})")

        # Регистрируем обработчики
        _register_handlers(client, user_id)

        # Восстанавливаем алерты
        await _restore_alerts(client, user_id)

        # Держим клиент активным
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"UserBot error for user_id={user_id}: {e}")
        if user_id in active_clients:
            del active_clients[user_id]


async def _restore_alerts(client: TelegramClient, owner_id: int):
    """Восстанавливаем активные алерты после перезапуска"""
    try:
        alerts = await db.get_active_alerts(owner_id)
        for alert in alerts:
            key = (owner_id, alert["target_username"])
            if key not in alert_tasks:
                task = asyncio.create_task(
                    _alert_monitor(client, owner_id, alert["target_username"], alert["chat_id"])
                )
                alert_tasks[key] = task
    except Exception as e:
        logger.error(f"_restore_alerts error: {e}")


# ─────────────────────────────────────────────
#  Регистрация обработчиков
# ─────────────────────────────────────────────
def _register_handlers(client: TelegramClient, user_id: int):
    """Регистрирует все обработчики событий для клиента"""

    # ── .help ────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r'^\.help$'))
    async def handler_help(event):
        await event.delete()
        await event.respond(HELP_TEXT)

    # ── .spam N S ────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r'^\.spam\s+(\d+)\s+(\d+)$'))
    async def handler_spam(event):
        await event.delete()
        match = event.pattern_match
        count = int(match.group(1))
        delay = int(match.group(2))
        chat_id = event.chat_id

        if user_id in spam_tasks and not spam_tasks[user_id].done():
            await event.respond("⚠️ Спам уже запущен! Используйте `.spamstop` для остановки.")
            return

        await event.respond(
            f"🚀 Запускаю спам: {count} сообщений, каждые {delay} сек.\n"
            f"Для остановки: `.spamstop`"
        )

        task = asyncio.create_task(
            _spam_loop(client, chat_id, count, delay, user_id)
        )
        spam_tasks[user_id] = task

    # ── .spamstop ────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r'^\.spamstop$'))
    async def handler_spamstop(event):
        await event.delete()
        if user_id in spam_tasks and not spam_tasks[user_id].done():
            spam_tasks[user_id].cancel()
            del spam_tasks[user_id]
            await event.respond("✅ Спам остановлен.")
        else:
            await event.respond("⚠️ Нет активного спама.")

    # ── .info ────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r'^\.info$'))
    async def handler_info(event):
        await event.delete()
        chat = await event.get_chat()
        target = None

        try:
            # Если это приватный чат — цель = собеседник
            if hasattr(chat, 'first_name'):
                target = chat
            else:
                await event.respond("⚠️ Эта команда работает только в личных чатах.")
                return
        except Exception as e:
            await event.respond(f"❌ Ошибка: {e}")
            return

        info_text = await _get_user_info(client, target)
        await event.respond(info_text)

    # ── .check @username ─────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r'^\.check\s+@?(\w+)$'))
    async def handler_check(event):
        await event.delete()
        username = event.pattern_match.group(1)

        try:
            user_entity = await client.get_entity(username)
            status = user_entity.status
            status_text = _parse_user_status(status)

            await event.respond(
                f"👁 <b>Последний онлайн @{username}:</b>\n\n"
                f"{status_text}",
                parse_mode='html'
            )
        except Exception as e:
            await event.respond(f"❌ Ошибка: {e}")

    # ── .allertCheck @username ───────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r'^\.allertCheck\s+@?(\w+)$'))
    async def handler_alert_check(event):
        await event.delete()
        username = event.pattern_match.group(1)
        chat_id = event.chat_id

        key = (user_id, username.lower())
        if key in alert_tasks and not alert_tasks[key].done():
            await event.respond(f"⚠️ Алерт на @{username} уже активен.")
            return

        await db.add_alert(user_id, username, chat_id)
        task = asyncio.create_task(
            _alert_monitor(client, user_id, username, chat_id)
        )
        alert_tasks[key] = task

        await event.respond(
            f"🔔 Алерт установлен на @{username}.\n"
            f"Получу уведомление когда появится онлайн.\n"
            f"Отключить: `.offallert @{username}`"
        )

    # ── .offallert @username ─────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r'^\.offallert\s+@?(\w+)$'))
    async def handler_off_alert(event):
        await event.delete()
        username = event.pattern_match.group(1)
        key = (user_id, username.lower())

        if key in alert_tasks:
            alert_tasks[key].cancel()
            del alert_tasks[key]

        await db.remove_alert(user_id, username)
        await event.respond(f"🔕 Алерт на @{username} отключён.")

    # ── .mute [время] ────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r'^\.mute\s+(\S+)$'))
    async def handler_mute(event):
        await event.delete()
        duration_str = event.pattern_match.group(1)
        seconds = parse_duration(duration_str)

        if seconds <= 0:
            await event.respond(
                "❌ Неверный формат времени.\n"
                "Примеры: `.mute 30s` `.mute 5m` `.mute 2h` `.mute 1d`"
            )
            return

        chat_id = event.chat_id
        until_ts = int(time.time()) + seconds

        await db.add_mute(user_id, chat_id, until_ts)
        until_dt = datetime.fromtimestamp(until_ts)

        await event.respond(
            f"🔇 Мут активен до {until_dt.strftime('%H:%M:%S %d.%m.%Y')}\n"
            f"Снять: `.unmute`"
        )

    # ── .unmute ──────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r'^\.unmute$'))
    async def handler_unmute(event):
        await event.delete()
        chat_id = event.chat_id
        await db.remove_mute(user_id, chat_id)
        await event.respond("🔊 Мут снят.")

    # ── .kawaii ──────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r'^\.kawaii$'))
    async def handler_kawaii(event):
        await event.delete()
        user_data = await db.get_user(user_id)
        current = user_data.get("kawaii_enabled", 0) if user_data else 0
        new_val = 0 if current else 1
        await db.update_user_field(user_id, "kawaii_enabled", new_val)

        if new_val:
            await event.respond(
                "🎀 Kawaii-режим <b>включён!</b>\n"
                "Все ваши сообщения будут преобразованы в няшный рп-стиль 😻\n"
                "Отключить: `.kawaii`",
                parse_mode='html'
            )
        else:
            await event.respond("✅ Kawaii-режим <b>выключен.</b>", parse_mode='html')

    # ── .saveoff ─────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r'^\.saveoff$'))
    async def handler_saveoff(event):
        await event.delete()
        await db.update_user_field(user_id, "save_enabled", 0)
        await event.respond("💾 Сохранение сообщений <b>выключено.</b>", parse_mode='html')

    # ── .saveon ──────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r'^\.saveon$'))
    async def handler_saveon(event):
        await event.delete()
        await db.update_user_field(user_id, "save_enabled", 1)
        await event.respond("💾 Сохранение сообщений <b>включено.</b>", parse_mode='html')

    # ── Kawaii-режим: перехват исходящих сообщений ──
    @client.on(events.NewMessage(outgoing=True))
    async def handler_kawaii_transform(event):
        # Пропускаем команды
        if event.text and event.text.startswith('.'):
            return

        user_data = await db.get_user(user_id)
        if not user_data or not user_data.get("kawaii_enabled"):
            return

        if not event.text:
            return

        transformed = kawaii_transform(event.text)
        if transformed != event.text:
            try:
                await event.delete()
                await event.respond(transformed)
            except Exception as e:
                logger.error(f"kawaii transform error: {e}")

    # ── Мут: удаление входящих сообщений ────
    @client.on(events.NewMessage(incoming=True))
    async def handler_mute_delete(event):
        try:
            chat_id = event.chat_id
            mute = await db.get_active_mute(user_id, chat_id)
            if not mute:
                return

            until_ts = mute.get("until_timestamp", 0)
            if time.time() > until_ts:
                # Мут истёк — убираем
                await db.remove_mute(user_id, chat_id)
                return

            # Удаляем входящее сообщение
            await event.delete()
        except Exception as e:
            logger.error(f"mute delete error: {e}")

    # ── Кэширование входящих/исходящих сообщений ──
    @client.on(events.NewMessage())
    async def handler_cache_message(event):
        try:
            msg = event.message
            if not msg:
                return

            # Сохраняем в кэш
            media_data = None
            if msg.media:
                # Скачиваем медиа в память для сохранения
                try:
                    media_bytes = await client.download_media(msg.media, bytes)
                    media_data = media_bytes
                except Exception:
                    media_data = None

            message_cache[msg.id] = {
                "chat_id": event.chat_id,
                "text": msg.text or msg.message or "",
                "media": media_data,
                "media_obj": msg.media,
                "sender_id": msg.sender_id,
                "date": msg.date,
                "is_out": msg.out,
            }
        except Exception as e:
            logger.error(f"cache_message error: {e}")

    # ── Сохранение удалённых сообщений ──────
    @client.on(events.MessageDeleted())
    async def handler_deleted(event):
        try:
            user_data = await db.get_user(user_id)
            if not user_data or not user_data.get("save_enabled", 1):
                return

            save_chat_id = user_data.get("save_chat_id")
            if not save_chat_id:
                return

            deleted_ids = event.deleted_ids
            for msg_id in deleted_ids:
                if msg_id not in message_cache:
                    continue

                cached = message_cache[msg_id]
                text = cached.get("text", "")
                sender_id = cached.get("sender_id")
                date = cached.get("date")
                media = cached.get("media")
                media_obj = cached.get("media_obj")
                chat_id = cached.get("chat_id")

                date_str = date.strftime("%H:%M:%S %d.%m.%Y") if date else "неизвестно"
                direction = "📤 Исходящее" if cached.get("is_out") else "📥 Входящее"

                # Определяем тип медиа
                media_type = _get_media_type(media_obj)

                header = (
                    f"🗑 <b>УДАЛЁННОЕ СООБЩЕНИЕ</b>\n"
                    f"📅 {date_str}\n"
                    f"{direction}\n"
                    f"💬 Чат ID: <code>{chat_id}</code>\n"
                    f"👤 Отправитель: <code>{sender_id}</code>\n"
                )

                if media_type:
                    header += f"📎 Тип: {media_type}\n"

                header += f"\n<b>Текст:</b>\n{text or '— (без текста) —'}"

                await _send_to_save_chat(
                    client, save_chat_id,
                    header, media, media_obj
                )

                # Удаляем из кэша
                del message_cache[msg_id]

        except Exception as e:
            logger.error(f"handler_deleted error: {e}")

    # ── Сохранение изменённых сообщений ─────
    @client.on(events.MessageEdited())
    async def handler_edited(event):
        try:
            user_data = await db.get_user(user_id)
            if not user_data or not user_data.get("save_enabled", 1):
                return

            save_chat_id = user_data.get("save_chat_id")
            if not save_chat_id:
                return

            msg = event.message
            msg_id = msg.id

            # Получаем старую версию из кэша
            old_cached = message_cache.get(msg_id)
            old_text = old_cached.get("text", "— (не сохранён) —") if old_cached else "— (не сохранён) —"

            new_text = msg.text or msg.message or ""
            date = msg.date
            date_str = date.strftime("%H:%M:%S %d.%m.%Y") if date else "неизвестно"
            direction = "📤 Исходящее" if msg.out else "📥 Входящее"

            header = (
                f"✏️ <b>ИЗМЕНЁННОЕ СООБЩЕНИЕ</b>\n"
                f"📅 {date_str}\n"
                f"{direction}\n"
                f"💬 Чат ID: <code>{event.chat_id}</code>\n"
                f"👤 Отправитель: <code>{msg.sender_id}</code>\n\n"
                f"<b>Было:</b>\n{old_text or '— (без текста) —'}\n\n"
                f"<b>Стало:</b>\n{new_text or '— (без текста) —'}"
            )

            await _send_to_save_chat(client, save_chat_id, header, None, None)

            # Обновляем кэш
            if msg_id in message_cache:
                message_cache[msg_id]["text"] = new_text

        except Exception as e:
            logger.error(f"handler_edited error: {e}")


# ─────────────────────────────────────────────
#  Вспомогательные функции
# ─────────────────────────────────────────────
def _get_media_type(media) -> str:
    """Определяет тип медиа"""
    if media is None:
        return ""
    if isinstance(media, MessageMediaPhoto):
        return "🖼 Фото"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if doc and doc.attributes:
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeVideo):
                    if hasattr(attr, 'round_message') and attr.round_message:
                        return "🎥 Видеосообщение"
                    return "🎬 Видео"
        # Проверяем ttl_seconds (одноразовые)
        if hasattr(media, 'ttl_seconds') and media.ttl_seconds:
            return "⏱ Одноразовое медиа"
        return "📎 Документ"
    return "📎 Медиа"


async def _send_to_save_chat(
    client: TelegramClient,
    save_chat_id: int,
    text: str,
    media_bytes: bytes | None,
    media_obj=None
):
    """Отправляет сохранённое сообщение в приватный чат"""
    try:
        target = await client.get_entity(save_chat_id)

        if media_bytes and len(media_bytes) > 0:
            # Отправляем медиа
            import io
            file = io.BytesIO(media_bytes)

            # Определяем расширение
            ext = ".jpg"
            if media_obj and isinstance(media_obj, MessageMediaDocument):
                ext = ".mp4"

            file.name = f"saved_media{ext}"
            await client.send_file(
                target,
                file=file,
                caption=text,
                parse_mode='html'
            )
        else:
            await client.send_message(
                target,
                text,
                parse_mode='html'
            )

    except Exception as e:
        logger.error(f"_send_to_save_chat error: {e}")


def _parse_user_status(status) -> str:
    """Парсит статус пользователя в читаемый вид"""
    if status is None:
        return "❓ Статус скрыт или недоступен"

    status_type = type(status).__name__

    if status_type == "UserStatusOnline":
        return "🟢 <b>Онлайн прямо сейчас</b>"
    elif status_type == "UserStatusOffline":
        if hasattr(status, 'was_online') and status.was_online:
            dt = status.was_online
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = now - dt
            return (
                f"⚫️ Был(а) в сети: "
                f"<b>{dt.strftime('%H:%M %d.%m.%Y')}</b>\n"
                f"({_human_delta(delta)} назад)"
            )
        return "⚫️ Был(а) в сети: <b>недавно</b>"
    elif status_type == "UserStatusRecently":
        return "🟡 Был(а) в сети <b>недавно</b>"
    elif status_type == "UserStatusLastWeek":
        return "🟠 Был(а) в сети на <b>этой неделе</b>"
    elif status_type == "UserStatusLastMonth":
        return "🔴 Был(а) в сети в <b>этом месяце</b>"
    elif status_type == "UserStatusEmpty":
        return "❓ Статус неизвестен"
    else:
        return f"❓ Статус: {status_type}"


def _human_delta(delta: timedelta) -> str:
    """Переводит timedelta в человекочитаемый формат"""
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds} сек."
    elif total_seconds < 3600:
        return f"{total_seconds // 60} мин."
    elif total_seconds < 86400:
        return f"{total_seconds // 3600} ч."
    else:
        return f"{total_seconds // 86400} дн."


async def _get_user_info(client: TelegramClient, user_entity) -> str:
    """Формирует информацию о пользователе"""
    try:
        from telethon.tl.functions.users import GetFullUserRequest
        full = await client(GetFullUserRequest(user_entity))
        user = full.users[0] if hasattr(full, 'users') and full.users else user_entity
        full_user = full.full_user if hasattr(full, 'full_user') else None

        # Базовая информация
        uid = user.id
        first_name = user.first_name or ""
        last_name = user.last_name or ""
        username = f"@{user.username}" if user.username else "нет"
        phone = user.phone or "скрыт"
        is_premium = "✅" if getattr(user, 'premium', False) else "❌"
        is_bot = "🤖 Да" if getattr(user, 'bot', False) else "👤 Нет"
        is_verified = "✅" if getattr(user, 'verified', False) else "❌"
        is_scam = "⚠️ Да" if getattr(user, 'scam', False) else "✅ Нет"

        # Дата регистрации (приблизительно по ID)
        reg_date = _estimate_reg_date(uid)

        # Статус
        status_text = _parse_user_status(user.status)

        # Подарки (если доступны через full_user)
        gifts_text = "недоступно"
        if full_user and hasattr(full_user, 'stargifts_count'):
            gifts_count = full_user.stargifts_count or 0
            gifts_text = f"{gifts_count} подарков"

        # Биография
        bio = ""
        if full_user and hasattr(full_user, 'about') and full_user.about:
            bio = full_user.about

        info = (
            f"👤 <b>Информация о пользователе</b>\n\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"📛 Имя: {first_name} {last_name}\n"
            f"🔗 Username: {username}\n"
            f"📱 Телефон: <code>{phone}</code>\n"
            f"⭐️ Premium: {is_premium}\n"
            f"🤖 Бот: {is_bot}\n"
            f"✅ Verified: {is_verified}\n"
            f"🚨 Scam: {is_scam}\n"
            f"📅 Рег. (прибл.): {reg_date}\n"
            f"🎁 Подарки: {gifts_text}\n"
            f"📡 Статус: {status_text}\n"
        )

        if bio:
            info += f"\n📝 Bio: {bio}"

        return info

    except Exception as e:
        logger.error(f"_get_user_info error: {e}")
        return f"❌ Ошибка получения информации: {e}"


def _estimate_reg_date(user_id: int) -> str:
    """Приблизительная дата регистрации по Telegram User ID"""
    # Ориентировочные пороги ID
    thresholds = [
        (100000000, "до 2013"),
        (200000000, "2013"),
        (300000000, "2014"),
        (400000000, "2015"),
        (500000000, "2016"),
        (600000000, "2017"),
        (700000000, "2018"),
        (800000000, "2019"),
        (900000000, "2019-2020"),
        (1000000000, "2020"),
        (1100000000, "2020"),
        (1200000000, "2020-2021"),
        (1300000000, "2021"),
        (1400000000, "2021"),
        (1500000000, "2021-2022"),
        (1600000000, "2022"),
        (1700000000, "2022"),
        (1800000000, "2022-2023"),
        (1900000000, "2023"),
        (2000000000, "2023"),
        (5000000000, "2023-2024"),
        (7000000000, "2024"),
    ]
    for threshold, year in thresholds:
        if user_id < threshold:
            return f"~{year}"
    return "2024-2025"


# ─────────────────────────────────────────────
#  Spam Loop
# ─────────────────────────────────────────────
async def _spam_loop(
    client: TelegramClient,
    chat_id: int,
    count: int,
    delay: int,
    user_id: int
):
    """Цикл отправки спама"""
    try:
        for i in range(1, count + 1):
            # Проверяем что задача не отменена
            if asyncio.current_task().cancelled():
                break

            spam_messages = [
                "💌", "❤️", "✨", "🌟", "💫",
                "🎭", "🎪", "🎨", "🎬", "🎯",
            ]
            msg = random.choice(spam_messages)
            text = f"{msg} Сообщение {i}/{count}"

            try:
                await client.send_message(chat_id, text)
            except FloodWaitError as e:
                logger.warning(f"FloodWait {e.seconds}s during spam")
                await asyncio.sleep(e.seconds)
                continue
            except Exception as e:
                logger.error(f"spam send error: {e}")
                break

            await asyncio.sleep(delay)

        # Спам завершён
        if user_id in spam_tasks:
            del spam_tasks[user_id]
        try:
            await client.send_message(chat_id, "✅ Спам завершён.")
        except Exception:
            pass

    except asyncio.CancelledError:
        logger.info(f"Spam cancelled for user_id={user_id}")


# ─────────────────────────────────────────────
#  Alert Monitor
# ─────────────────────────────────────────────
async def _alert_monitor(
    client: TelegramClient,
    owner_id: int,
    username: str,
    notify_chat_id: int
):
    """Мониторит появление пользователя онлайн"""
    logger.info(f"Alert monitor started: @{username} for owner {owner_id}")
    last_status_online = False
    check_interval = 30  # секунд

    while True:
        try:
            # Проверяем, активен ли алерт в БД
            alerts = await db.get_active_alerts(owner_id)
            active_usernames = [a["target_username"].lower() for a in alerts]
            if username.lower() not in active_usernames:
                logger.info(f"Alert for @{username} deactivated")
                break

            user_entity = await client.get_entity(username)
            status = user_entity.status
            is_online = type(status).__name__ == "UserStatusOnline"

            if is_online and not last_status_online:
                # Пользователь появился онлайн!
                await client.send_message(
                    notify_chat_id,
                    f"🔔 <b>Алерт!</b>\n\n"
                    f"@{username} сейчас <b>онлайн</b>! 🟢\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S %d.%m.%Y')}",
                    parse_mode='html'
                )
                logger.info(f"Alert triggered: @{username} is online")

            last_status_online = is_online
            await asyncio.sleep(check_interval)

        except asyncio.CancelledError:
            logger.info(f"Alert monitor cancelled: @{username}")
            break
        except FloodWaitError as e:
            logger.warning(f"FloodWait in alert monitor: {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"_alert_monitor error (@{username}): {e}")
            await asyncio.sleep(check_interval * 2)


# ─────────────────────────────────────────────
#  Получение премиум эмодзи (демонстрация)
# ─────────────────────────────────────────────
async def get_all_custom_emoji_info(bot_instance) -> list[dict]:
    """
    Получает информацию обо всех кастомных эмодзи из обоих паков
    через метод bot.get_custom_emoji_stickers
    """
    results = []
    # Разбиваем на чанки по 200 (лимит API)
    chunk_size = 200
    all_ids = ALL_EMOJI_IDS.copy()

    for i in range(0, len(all_ids), chunk_size):
        chunk = all_ids[i:i + chunk_size]
        try:
            stickers = await bot_instance.get_custom_emoji_stickers(
                custom_emoji_ids=chunk
            )
            for s in stickers:
                results.append({
                    "emoji": s.emoji,
                    "custom_emoji_id": s.custom_emoji_id,
                    "file_id": s.file_id,
                    "is_animated": s.is_animated,
                    "is_video": s.is_video,
                })
                logger.info(
                    f"Custom emoji: {s.emoji} | ID: {s.custom_emoji_id}"
                )
        except Exception as e:
            logger.error(f"get_custom_emoji_stickers error (chunk {i}): {e}")

    return results
