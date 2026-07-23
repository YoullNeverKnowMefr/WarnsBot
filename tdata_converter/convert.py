"""tdata → Telethon .session

1. Положите папку tdata рядом с этим скриптом
2. Закройте Telegram Desktop
3. Запустите:  python convert.py

Готовые .session появятся в этой же папке.

Нужно (Python 3.14 — только opentele-ng):
  pip uninstall -y opentele
  pip install -r requirements.txt
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TDATA = HERE / "tdata"


async def main() -> None:
    if not TDATA.is_dir():
        print(f"Папка не найдена: {TDATA}")
        print("Скопируйте tdata в эту папку и запустите снова.")
        sys.exit(1)

    try:
        from opentele.api import UseCurrentSession
        from opentele.td import TDesktop
    except Exception as e:
        print(f"Не удалось импортировать opentele: {e}")
        print()
        print("На Python 3.13+ старый opentele не работает. Установите:")
        print("  pip uninstall -y opentele")
        print("  pip install -r requirements.txt")
        sys.exit(1)

    print(f"Читаю: {TDATA}")
    tdesk = TDesktop(str(TDATA))
    if not tdesk.isLoaded():
        print("В tdata нет авторизованных аккаунтов.")
        sys.exit(1)

    accounts = list(tdesk.accounts)
    print(f"Аккаунтов: {len(accounts)}")

    for i, account in enumerate(accounts):
        tmp = HERE / f"_tmp_{i}"
        session_file = tmp.with_suffix(".session")
        if session_file.exists():
            session_file.unlink()

        client = await account.ToTelethon(session=str(tmp), flag=UseCurrentSession)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            print(f"[{i}] не авторизован — пропуск")
            continue

        me = await client.get_me()
        await client.disconnect()

        name = (me.phone or "").lstrip("+") or str(me.id)
        out = HERE / f"{name}.session"
        if out.exists() and out.resolve() != session_file.resolve():
            out.unlink()
        session_file.rename(out)

        journal = Path(str(session_file) + "-journal")
        if journal.exists():
            dest_j = Path(str(out) + "-journal")
            if dest_j.exists():
                dest_j.unlink()
            journal.rename(dest_j)

        uname = f"@{me.username}" if me.username else "-"
        print(f"OK: {out.name} | id={me.id} {uname} phone={me.phone or '-'}")

    print()
    print("Готово. Отправьте .session боту: Аккаунты → Добавить .session")


if __name__ == "__main__":
    asyncio.run(main())
