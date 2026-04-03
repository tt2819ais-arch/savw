"""
Основной бот — авторизация пользователя в UserBot через Telegram Bot API.
"""

import asyncio
import logging
import json

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    BotCommand,
    ReactionTypeEmoji,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    FloodWaitError,
)
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import (
    AddChatUserRequest,
    ExportChatInviteRequest,
)
from telethon.tl.types import InputPeerUser

import database as db
from config import BOT_TOKEN, API_ID, API_HASH, SESSIONS_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  FSM States
# ─────────────────────────────────────────────
class AuthStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


# ─────────────────────────────────────────────
#  Bot & Dispatcher
# ─────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# Хранилище клиентов Telethon в процессе авторизации
pending_clients: dict[int, TelegramClient] = {}


# ─────────────────────────────────────────────
#  Inline keyboard helpers
# ─────────────────────────────────────────────
def make_inline_kb(buttons: list[dict]) -> InlineKeyboardMarkup:
    """
    buttons = [
        {"text": "...", "callback_data": "...", "style": "primary"},
        ...
    ]
    style: primary=синяя, success=зелёная, danger=красная, None=серая
    Telegram не поддерживает кастомные цвета напрямую, реализуем через эмодзи-префиксы
    """
    style_prefix = {
        "primary": "🔵 ",
        "success": "🟢 ",
        "danger": "🔴 ",
        None: "⚪️ ",
    }
    rows = []
    for btn in buttons:
        prefix = style_prefix.get(btn.get("style"), "⚪️ ")
        rows.append([
            InlineKeyboardButton(
                text=prefix + btn["text"],
                callback_data=btn["callback_data"]
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def phone_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с кнопкой «Поделиться номером»"""
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(
                text="📱 Поделиться номером",
                request_contact=True
            )
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# ─────────────────────────────────────────────
#  set_my_commands
# ─────────────────────────────────────────────
async def set_bot_commands():
    commands = [
        BotCommand(command="start",   description="Запустить бота"),
        BotCommand(command="help",    description="Помощь"),
        BotCommand(command="cancel",  description="Отменить текущее действие"),
        BotCommand(command="status",  description="Статус авторизации"),
    ]
    await bot.set_my_commands(commands)


# ─────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await db.get_user(message.from_user.id)

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await asyncio.sleep(1)

    if user and user.get("session_string"):
        await message.answer(
            "✅ <b>Вы уже авторизованы!</b>\n\n"
            "UserBot активен и работает.\n"
            "Используйте /status для проверки.",
            reply_markup=make_inline_kb([
                {"text": "Статус", "callback_data": "status", "style": "primary"},
                {"text": "Выйти", "callback_data": "logout", "style": "danger"},
            ])
        )
        return

    await message.answer(
        "👋 <b>Добро пожаловать в UserBot!</b>\n\n"
        "Этот бот авторизует UserBot в ваш Telegram-аккаунт.\n\n"
        "🔐 <b>Шаг 1:</b> Нажмите кнопку ниже, чтобы поделиться номером телефона.",
        reply_markup=phone_keyboard()
    )
    await state.set_state(AuthStates.waiting_phone)


# ─────────────────────────────────────────────
#  /help
# ─────────────────────────────────────────────
@router.message(Command("help"))
async def cmd_help(message: Message):
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await asyncio.sleep(0.5)
    await message.answer(
        "📖 <b>Помощь</b>\n\n"
        "/start — начать авторизацию\n"
        "/status — статус UserBot\n"
        "/cancel — отменить действие\n"
        "/help — это сообщение\n\n"
        "<b>После авторизации</b> UserBot работает автоматически.\n"
        "Все команды UserBot начинаются с точки: <code>.help</code>"
    )


# ─────────────────────────────────────────────
#  /cancel
# ─────────────────────────────────────────────
@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять.", reply_markup=ReplyKeyboardRemove())
        return
    # Закрываем pending клиент если есть
    uid = message.from_user.id
    if uid in pending_clients:
        try:
            await pending_clients[uid].disconnect()
        except Exception:
            pass
        del pending_clients[uid]
    await state.clear()
    await db.clear_auth_state(uid)
    await message.answer(
        "❌ Действие отменено.",
        reply_markup=ReplyKeyboardRemove()
    )


# ─────────────────────────────────────────────
#  /status
# ─────────────────────────────────────────────
@router.message(Command("status"))
async def cmd_status(message: Message):
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await asyncio.sleep(0.5)
    user = await db.get_user(message.from_user.id)
    if user and user.get("session_string"):
        save_status = "✅ включено" if user.get("save_enabled", 1) else "❌ выключено"
        kawaii_status = "✅ включено" if user.get("kawaii_enabled", 0) else "❌ выключено"
        await message.answer(
            f"✅ <b>UserBot активен</b>\n\n"
            f"📱 Телефон: <code>{user.get('phone', 'неизвестно')}</code>\n"
            f"💾 Сохранение сообщений: {save_status}\n"
            f"🎀 Kawaii режим: {kawaii_status}\n"
            f"💬 Чат для сохранений: "
            f"{'<code>' + str(user.get('save_chat_id', 'не создан')) + '</code>' if user.get('save_chat_id') else 'не создан'}",
            reply_markup=make_inline_kb([
                {"text": "Обновить", "callback_data": "status", "style": "primary"},
                {"text": "Выйти из аккаунта", "callback_data": "logout", "style": "danger"},
            ])
        )
    else:
        await message.answer(
            "❌ <b>UserBot не авторизован</b>\n\nИспользуйте /start для авторизации.",
            reply_markup=make_inline_kb([
                {"text": "Авторизоваться", "callback_data": "start_auth", "style": "success"},
            ])
        )


# ─────────────────────────────────────────────
#  Callback: status / logout / start_auth
# ─────────────────────────────────────────────
@router.callback_query(F.data == "status")
async def cb_status(call: CallbackQuery):
    await call.answer()
    user = await db.get_user(call.from_user.id)
    if user and user.get("session_string"):
        save_status = "✅ включено" if user.get("save_enabled", 1) else "❌ выключено"
        kawaii_status = "✅ включено" if user.get("kawaii_enabled", 0) else "❌ выключено"
        await call.message.edit_text(
            f"✅ <b>UserBot активен</b>\n\n"
            f"📱 Телефон: <code>{user.get('phone', 'неизвестно')}</code>\n"
            f"💾 Сохранение сообщений: {save_status}\n"
            f"🎀 Kawaii режим: {kawaii_status}",
            reply_markup=make_inline_kb([
                {"text": "Обновить", "callback_data": "status", "style": "primary"},
                {"text": "Выйти из аккаунта", "callback_data": "logout", "style": "danger"},
            ])
        )
    else:
        await call.message.edit_text(
            "❌ <b>UserBot не авторизован</b>",
            reply_markup=make_inline_kb([
                {"text": "Авторизоваться", "callback_data": "start_auth", "style": "success"},
            ])
        )


@router.callback_query(F.data == "logout")
async def cb_logout(call: CallbackQuery, state: FSMContext):
    await call.answer("Выход...")
    uid = call.from_user.id
    # Закрываем сессию
    user = await db.get_user(uid)
    if user and user.get("session_string"):
        try:
            client = TelegramClient(
                StringSession(user["session_string"]),
                API_ID, API_HASH
            )
            await client.connect()
            await client.log_out()
            await client.disconnect()
        except Exception as e:
            logger.error(f"Logout error: {e}")
    await db.update_user_field(uid, "session_string", None)
    await state.clear()
    await call.message.edit_text(
        "✅ Вы вышли из аккаунта. Используйте /start для повторной авторизации.",
        reply_markup=None
    )


@router.callback_query(F.data == "start_auth")
async def cb_start_auth(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await call.message.answer(
        "📱 Нажмите кнопку ниже, чтобы поделиться номером:",
        reply_markup=phone_keyboard()
    )
    await state.set_state(AuthStates.waiting_phone)


# ─────────────────────────────────────────────
#  Шаг 1: Получение номера телефона (contact)
# ─────────────────────────────────────────────
@router.message(AuthStates.waiting_phone, F.contact)
async def process_phone_contact(message: Message, state: FSMContext):
    phone = message.contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone

    uid = message.from_user.id

    # Реакция на сообщение с номером
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji="❤️")],
            is_big=False
        )
    except Exception as e:
        logger.warning(f"Reaction error: {e}")

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await asyncio.sleep(1)

    await message.answer(
        f"📱 Номер получен: <code>{phone}</code>\n"
        f"⏳ Отправляю код подтверждения...",
        reply_markup=ReplyKeyboardRemove()
    )

    # Создаём Telethon клиент
    client = TelegramClient(
        StringSession(),
        API_ID, API_HASH,
        device_model="UserBot",
        system_version="Windows 10",
        app_version="1.0",
    )

    try:
        await client.connect()
        result = await client.send_code_request(phone)
        phone_code_hash = result.phone_code_hash

        pending_clients[uid] = client
        await state.update_data(
            phone=phone,
            phone_code_hash=phone_code_hash
        )
        await state.set_state(AuthStates.waiting_code)

        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        await asyncio.sleep(0.5)
        await message.answer(
            "📨 <b>Код подтверждения отправлен!</b>\n\n"
            "Введите код из Telegram (без пробелов):\n"
            "<i>Например: 12345</i>"
        )

    except FloodWaitError as e:
        await client.disconnect()
        await state.clear()
        await message.answer(
            f"⚠️ Слишком много попыток. Подождите {e.seconds} секунд."
        )
    except Exception as e:
        logger.error(f"send_code_request error: {e}")
        await client.disconnect()
        await state.clear()
        await message.answer(
            f"❌ Ошибка при отправке кода: <code>{str(e)}</code>\n"
            "Попробуйте снова: /start"
        )


# Если ввели текст вместо контакта
@router.message(AuthStates.waiting_phone, F.text)
async def process_phone_text(message: Message):
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await asyncio.sleep(0.5)
    await message.answer(
        "⚠️ Пожалуйста, используйте кнопку <b>«📱 Поделиться номером»</b> ниже.",
        reply_markup=phone_keyboard()
    )


# ─────────────────────────────────────────────
#  Шаг 2: Получение кода подтверждения
# ─────────────────────────────────────────────
@router.message(AuthStates.waiting_code, F.text)
async def process_code(message: Message, state: FSMContext):
    uid = message.from_user.id
    code = message.text.strip().replace(" ", "")
    data = await state.get_data()
    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash")

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await asyncio.sleep(0.7)

    if uid not in pending_clients:
        await message.answer("❌ Сессия истекла. Начните заново: /start")
        await state.clear()
        return

    client = pending_clients[uid]

    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        # Успешно вошли без 2FA
        await _finalize_auth(message, state, client, phone)

    except SessionPasswordNeededError:
        # Нужен пароль 2FA
        await state.set_state(AuthStates.waiting_password)
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        await asyncio.sleep(0.5)
        await message.answer(
            "🔐 <b>Включена двухфакторная аутентификация.</b>\n\n"
            "Введите ваш пароль 2FA:\n"
            "<i>Пароль будет удалён из чата после ввода.</i>"
        )

    except PhoneCodeInvalidError:
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        await message.answer(
            "❌ <b>Неверный код.</b> Попробуйте ещё раз:"
        )

    except PhoneCodeExpiredError:
        await state.clear()
        if uid in pending_clients:
            await pending_clients[uid].disconnect()
            del pending_clients[uid]
        await message.answer(
            "⏰ <b>Код устарел.</b> Начните заново: /start"
        )

    except Exception as e:
        logger.error(f"sign_in error: {e}")
        await message.answer(
            f"❌ Ошибка: <code>{str(e)}</code>\n"
            "Попробуйте снова: /start"
        )
        await state.clear()
        if uid in pending_clients:
            await pending_clients[uid].disconnect()
            del pending_clients[uid]


# ─────────────────────────────────────────────
#  Шаг 3: Получение пароля 2FA
# ─────────────────────────────────────────────
@router.message(AuthStates.waiting_password, F.text)
async def process_password(message: Message, state: FSMContext):
    uid = message.from_user.id
    password = message.text.strip()

    # Удаляем сообщение с паролем из чата
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        logger.warning(f"Can't delete password message: {e}")

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await asyncio.sleep(0.7)

    data = await state.get_data()
    phone = data.get("phone")

    if uid not in pending_clients:
        await message.answer("❌ Сессия истекла. Начните заново: /start")
        await state.clear()
        return

    client = pending_clients[uid]

    try:
        await client.sign_in(password=password)
        await _finalize_auth(message, state, client, phone)

    except PasswordHashInvalidError:
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        await message.answer(
            "❌ <b>Неверный пароль.</b> Попробуйте ещё раз:"
        )

    except Exception as e:
        logger.error(f"2FA sign_in error: {e}")
        await message.answer(
            f"❌ Ошибка: <code>{str(e)}</code>\n"
            "Попробуйте снова: /start"
        )
        await state.clear()
        if uid in pending_clients:
            await pending_clients[uid].disconnect()
            del pending_clients[uid]


# ─────────────────────────────────────────────
#  Финализация авторизации
# ─────────────────────────────────────────────
async def _finalize_auth(
    message: Message,
    state: FSMContext,
    client: TelegramClient,
    phone: str
):
    uid = message.from_user.id

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await asyncio.sleep(0.5)

    # Получаем строку сессии
    session_string = client.session.save()

    # Сохраняем в БД
    await db.save_user(uid, phone, session_string)

    # Получаем данные о пользователе
    me = await client.get_me()
    username = me.username or f"user_{me.id}"

    # Создаём приватный чат для сохранений
    save_chat_id = await _create_save_chat(client, me, username)
    if save_chat_id:
        await db.update_user_field(uid, "save_chat_id", save_chat_id)

    # Отключаем клиент (UserBot будет запущен отдельно)
    await client.disconnect()
    del pending_clients[uid]

    await state.clear()
    await db.clear_auth_state(uid)

    await message.answer(
        f"🎉 <b>Авторизация успешна!</b>\n\n"
        f"👤 Аккаунт: @{username}\n"
        f"📱 Телефон: <code>{phone}</code>\n\n"
        f"✅ UserBot запущен!\n"
        f"💬 Создан приватный чат для сохранений.\n\n"
        f"<b>Команды UserBot</b> (вводить в любом чате):\n"
        f"<code>.help</code> — список всех команд",
        reply_markup=make_inline_kb([
            {"text": "Статус", "callback_data": "status", "style": "primary"},
            {"text": "Справка", "callback_data": "help_info", "style": "success"},
        ])
    )

    # Запускаем UserBot для этого пользователя
    from userbot import launch_userbot
    asyncio.create_task(launch_userbot(uid))


async def _create_save_chat(
    client: TelegramClient,
    me,
    username: str
) -> int | None:
    """Создаём приватный канал/группу и добавляем основного бота"""
    try:
        # Создаём канал (супергруппу)
        result = await client(CreateChannelRequest(
            title=f"@{username} — Сохранения",
            about="Сохранённые удалённые и изменённые сообщения",
            megagroup=True,
        ))
        channel = result.chats[0]
        chat_id = channel.id

        # Пытаемся добавить основного бота в чат
        try:
            bot_entity = await client.get_entity("@" + (await get_bot_username()))
            await client(AddChatUserRequest(
                chat_id=chat_id,
                user_id=bot_entity,
                fwd_limit=0
            ))
        except Exception as e:
            logger.warning(f"Can't add bot to chat: {e}")

        # Закрепляем чат (архив/папка — закрепляем в диалогах)
        try:
            from telethon.tl.functions.messages import ToggleDialogPinRequest
            from telethon.tl.types import InputDialogPeer
            await client(ToggleDialogPinRequest(
                peer=InputDialogPeer(peer=await client.get_input_entity(channel)),
                pinned=True
            ))
        except Exception as e:
            logger.warning(f"Can't pin chat: {e}")

        logger.info(f"Created save chat: {chat_id} for @{username}")
        return chat_id

    except Exception as e:
        logger.error(f"_create_save_chat error: {e}")
        return None


async def get_bot_username() -> str:
    """Получить юзернейм основного бота"""
    me = await bot.get_me()
    return me.username


# ─────────────────────────────────────────────
#  Callback: help_info
# ─────────────────────────────────────────────
@router.callback_query(F.data == "help_info")
async def cb_help_info(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "📖 <b>Команды UserBot</b>\n\n"
        "<code>.help</code> — список команд\n"
        "<code>.spam N S</code> — спам N сообщений каждые S сек\n"
        "<code>.spamstop</code> — остановить спам\n"
        "<code>.info</code> — инфо о собеседнике\n"
        "<code>.check @user</code> — последний онлайн\n"
        "<code>.allertCheck @user</code> — алерт при появлении онлайн\n"
        "<code>.offallert @user</code> — отключить алерт\n"
        "<code>.mute 5h</code> — мут собеседника на время\n"
        "<code>.unmute</code> — снять мут\n"
        "<code>.kawaii</code> — няшный рп-режим\n"
        "<code>.saveoff</code> — выкл. сохранение сообщений\n"
        "<code>.saveon</code> — вкл. сохранение сообщений"
    )


# ─────────────────────────────────────────────
#  Inline Mode
# ─────────────────────────────────────────────
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)


@router.inline_query()
async def inline_query_handler(query: InlineQuery):
    """Inline-режим бота"""
    results = [
        InlineQueryResultArticle(
            id="status",
            title="🔵 Статус UserBot",
            description="Проверить статус UserBot",
            input_message_content=InputTextMessageContent(
                message_text="🔵 Проверьте статус UserBot командой /status"
            ),
            reply_markup=make_inline_kb([
                {"text": "Открыть бота", "callback_data": "status", "style": "primary"}
            ])
        ),
        InlineQueryResultArticle(
            id="help",
            title="🟢 Помощь",
            description="Список команд UserBot",
            input_message_content=InputTextMessageContent(
                message_text=(
                    "📖 <b>Команды UserBot:</b>\n"
                    "<code>.help .spam .spamstop .info .check</code>\n"
                    "<code>.allertCheck .offallert .mute .unmute .kawaii</code>"
                ),
                parse_mode=ParseMode.HTML
            ),
            reply_markup=make_inline_kb([
                {"text": "Справка", "callback_data": "help_info", "style": "success"}
            ])
        ),
        InlineQueryResultArticle(
            id="auth",
            title="🟢 Авторизоваться",
            description="Войти в аккаунт через UserBot",
            input_message_content=InputTextMessageContent(
                message_text="Используйте /start для авторизации UserBot"
            ),
            reply_markup=make_inline_kb([
                {"text": "Авторизоваться", "callback_data": "start_auth", "style": "success"}
            ])
        ),
        InlineQueryResultArticle(
            id="logout",
            title="🔴 Выйти из аккаунта",
            description="Выйти из аккаунта UserBot",
            input_message_content=InputTextMessageContent(
                message_text="Используйте /status для управления аккаунтом"
            ),
            reply_markup=make_inline_kb([
                {"text": "Выйти", "callback_data": "logout", "style": "danger"}
            ])
        ),
        InlineQueryResultArticle(
            id="info",
            title="⚪️ Инфо",
            description="Информация о системе",
            input_message_content=InputTextMessageContent(
                message_text="UserBot — система мониторинга и управления аккаунтом Telegram"
            )
        ),
    ]
    await query.answer(results, cache_time=1, is_personal=True)


# ─────────────────────────────────────────────
#  Запуск
# ─────────────────────────────────────────────
async def main():
    await db.init_db()
    await set_bot_commands()

    # Запускаем UserBot-ы для всех авторизованных пользователей
    users = await db.get_all_users()
    from userbot import launch_userbot
    for user in users:
        if user.get("session_string"):
            asyncio.create_task(launch_userbot(user["user_id"]))
            logger.info(f"Restoring UserBot for user_id={user['user_id']}")

    logger.info("Main bot starting...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
