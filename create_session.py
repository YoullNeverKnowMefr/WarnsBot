"""Создать Telethon .session по номеру телефона.

Пример:
  .\\.venv\\Scripts\\python create_session.py +12025550123 my_usa_1
"""

from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient

from config import API_HASH, API_ID, SESSIONS_DIR


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python create_session.py <+phone> [session_name]")
        sys.exit(1)

    phone = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else phone.lstrip("+")
    path = SESSIONS_DIR / name

    if not API_ID or not API_HASH or API_HASH == "your_api_hash":
        print("Сначала заполните API_ID и API_HASH в .env")
        sys.exit(1)

    client = TelegramClient(str(path), API_ID, API_HASH)
    await client.start(phone=phone)
    me = await client.get_me()
    print(f"OK: {name}.session | id={me.id} @{me.username} phone={me.phone}")
    print(f"Файл: {path}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
