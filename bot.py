import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

import config
import rentals
from utils import esc
from database import init_db
from handlers import router
import keyboards as kb
from playerok_client import client, load_credentials

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(os.path.join(config.BASE_DIR, "bot.log"), encoding="utf-8")],
    force=True,  # зависимости (tls_requests) настраивают логирование при импорте — перекрываем
)
logger = logging.getLogger("bot")


async def main():
    if not config.BOT_TOKEN:
        sys.exit("Не задан BOT_TOKEN в .env")
    if not config.ADMIN_IDS:
        sys.exit("Не заданы ADMIN_IDS в .env")
    await init_db()

    bot = Bot(token=config.BOT_TOKEN)
    rentals.set_bot(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="accounts", description="Товары и их статус"),
        BotCommand(command="status", description="Активные аренды и статистика"),
        BotCommand(command="balance", description="Баланс и вывод"),
        BotCommand(command="cookies", description="Вход в Playerok (cookies, прокси)"),
        BotCommand(command="add", description="Новый товар (аккаунт + лот)"),
    ])

    client.on_event = rentals.on_playerok_event
    client.loop = asyncio.get_running_loop()
    try:
        creds = await load_credentials()
        if not creds["cookies"].get("token"):
            raise ValueError("cookies ещё не заданы")
        await client.connect(creds)
    except Exception as e:
        # Бот продолжает работать: cookies можно прислать прямо в Telegram
        logger.error(f"Не удалось войти в Playerok: {e}")
        await rentals.notify_admins(
            f"❌ Не удалось войти в Playerok: {esc(e)}\n\nОбновите cookies в боте 👇",
            reply_markup=kb.back("cookies", "🍪 Обновить cookies"),
        )
    else:
        client.start_listener(client.loop)
        await rentals.notify_admins(f"✅ Бот запущен. Playerok: <b>{esc(client.acc.username)}</b>")
        asyncio.create_task(rentals.sync_lot_comments())
    asyncio.create_task(rentals.rental_watcher())
    asyncio.create_task(rentals.auth_watcher())
    asyncio.create_task(rentals.deals_poller())
    if config.TEST_RENTAL_MINUTES:
        await rentals.notify_admins(
            f"🧪 Тестовый режим: любая аренда длится {config.TEST_RENTAL_MINUTES} мин. "
            "Для работы поставьте TEST_RENTAL_MINUTES=0 в .env")
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
