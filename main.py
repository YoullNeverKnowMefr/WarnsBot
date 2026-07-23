from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from accounts import account_manager
from bot_handlers import router
from campaign import campaign_engine
from config import ADMIN_IDS, API_HASH, API_ID, BOT_TOKEN
from logger_setup import attach_buffer_handler, setup_logging
from monitor import monitor_service

logger = logging.getLogger(__name__)


async def notify_admins(bot: Bot, text: str) -> None:
    if not ADMIN_IDS:
        return
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.warning("Cannot notify admin %s", admin_id)


async def main() -> None:
    setup_logging()
    attach_buffer_handler()

    if not BOT_TOKEN or BOT_TOKEN.startswith("123456"):
        logger.error("Укажите BOT_TOKEN в .env")
        sys.exit(1)
    if not API_ID or not API_HASH or API_HASH == "your_api_hash":
        logger.error("Укажите API_ID и API_HASH в .env (my.telegram.org)")
        sys.exit(1)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    async def _notify(text: str) -> None:
        await notify_admins(bot, text)

    campaign_engine.notify_callback = _notify
    monitor_service.notify_callback = _notify
    monitor_service.start_background()

    logger.info("Starting control bot… admins=%s", ADMIN_IDS or "ANY")

    try:
        await dp.start_polling(bot)
    finally:
        await campaign_engine.stop()
        await monitor_service.stop()
        await account_manager.disconnect_all()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
