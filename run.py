"""
Точка входа: запускает основной бот и UserBot'ы параллельно.
"""

import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("userbot.log", encoding="utf-8"),
    ]
)

async def main():
    # Инициализируем БД
    import database as db
    await db.init_db()
    logging.info("Database initialized")

    # Запускаем основной бот
    from main_bot import main as run_main_bot
    await run_main_bot()


if __name__ == "__main__":
    asyncio.run(main())
