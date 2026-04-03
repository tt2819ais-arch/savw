import aiosqlite
import json
from config import DB_PATH


async def init_db():
    """Инициализация базы данных"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица пользователей (авторизованных)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                phone TEXT,
                session_string TEXT,
                save_chat_id INTEGER,
                save_enabled INTEGER DEFAULT 1,
                kawaii_enabled INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица состояний авторизации
        await db.execute("""
            CREATE TABLE IF NOT EXISTS auth_states (
                user_id INTEGER PRIMARY KEY,
                state TEXT,
                phone TEXT,
                phone_code_hash TEXT,
                data TEXT
            )
        """)

        # Таблица алертов (мониторинг онлайн)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                target_username TEXT,
                chat_id INTEGER,
                active INTEGER DEFAULT 1
            )
        """)

        # Таблица мутов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mutes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                chat_id INTEGER,
                until_timestamp INTEGER,
                active INTEGER DEFAULT 1
            )
        """)

        # Таблица активного спама
        await db.execute("""
            CREATE TABLE IF NOT EXISTS spam_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                chat_id INTEGER,
                active INTEGER DEFAULT 1
            )
        """)

        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def save_user(user_id: int, phone: str, session_string: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO users (user_id, phone, session_string)
            VALUES (?, ?, ?)
        """, (user_id, phone, session_string))
        await db.commit()


async def update_user_field(user_id: int, field: str, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE users SET {field} = ? WHERE user_id = ?",
            (value, user_id)
        )
        await db.commit()


async def get_auth_state(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM auth_states WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def set_auth_state(user_id: int, state: str, **kwargs):
    async with aiosqlite.connect(DB_PATH) as db:
        data = json.dumps(kwargs.get("data", {}))
        await db.execute("""
            INSERT OR REPLACE INTO auth_states
            (user_id, state, phone, phone_code_hash, data)
            VALUES (?, ?, ?, ?, ?)
        """, (
            user_id,
            state,
            kwargs.get("phone", ""),
            kwargs.get("phone_code_hash", ""),
            data
        ))
        await db.commit()


async def clear_auth_state(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM auth_states WHERE user_id = ?", (user_id,)
        )
        await db.commit()


async def add_alert(owner_id: int, target_username: str, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # Деактивируем старый алерт на этот username если есть
        await db.execute("""
            UPDATE alerts SET active = 0
            WHERE owner_id = ? AND target_username = ?
        """, (owner_id, target_username.lower()))
        # Добавляем новый
        await db.execute("""
            INSERT INTO alerts (owner_id, target_username, chat_id, active)
            VALUES (?, ?, ?, 1)
        """, (owner_id, target_username.lower(), chat_id))
        await db.commit()


async def remove_alert(owner_id: int, target_username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE alerts SET active = 0
            WHERE owner_id = ? AND target_username = ?
        """, (owner_id, target_username.lower()))
        await db.commit()


async def get_active_alerts(owner_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM alerts WHERE owner_id = ? AND active = 1
        """, (owner_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_all_active_alerts():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM alerts WHERE active = 1"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def add_mute(owner_id: int, chat_id: int, until_ts: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # Деактивируем старый мут на этот чат
        await db.execute("""
            UPDATE mutes SET active = 0 WHERE owner_id = ? AND chat_id = ?
        """, (owner_id, chat_id))
        await db.execute("""
            INSERT INTO mutes (owner_id, chat_id, until_timestamp, active)
            VALUES (?, ?, ?, 1)
        """, (owner_id, chat_id, until_ts))
        await db.commit()


async def remove_mute(owner_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE mutes SET active = 0 WHERE owner_id = ? AND chat_id = ?
        """, (owner_id, chat_id))
        await db.commit()


async def get_active_mute(owner_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM mutes
            WHERE owner_id = ? AND chat_id = ? AND active = 1
        """, (owner_id, chat_id)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
