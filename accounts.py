from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    UserDeactivatedBanError,
)
from telethon.tl.functions.contacts import UnblockRequest

from config import API_HASH, API_ID, SESSIONS_DIR, SKIP_SPAMBLOCKED
from storage import storage, utc_now
from tg_errors import short_error

logger = logging.getLogger(__name__)

PHONE_RE = re.compile(r"^\+?\d{8,15}$")


class AccountManager:
    def __init__(self) -> None:
        self._clients: dict[str, TelegramClient] = {}
        self._pending_logins: dict[str, TelegramClient] = {}

    def session_path(self, account_id: str) -> Path:
        return SESSIONS_DIR / f"{account_id}.session"

    @staticmethod
    def normalize_phone(raw: str) -> str:
        phone = re.sub(r"[\s\-()]", "", (raw or "").strip())
        if not phone.startswith("+"):
            phone = "+" + phone
        if not PHONE_RE.match(phone):
            raise ValueError("Некорректный номер. Пример: +12025550123")
        return phone

    @staticmethod
    def phone_to_account_id(phone: str) -> str:
        return phone.lstrip("+")

    async def start_phone_login(self, phone: str, account_id: str | None = None) -> dict:
        phone = self.normalize_phone(phone)
        account_id = (account_id or self.phone_to_account_id(phone)).replace(" ", "_")

        if len(storage.list_accounts()) >= 20 and account_id not in storage.list_accounts():
            raise ValueError("Лимит 20 аккаунтов. Удалите лишние перед добавлением.")

        await self.cancel_phone_login(account_id)

        path = self.session_path(account_id)
        client = TelegramClient(str(path.with_suffix("")), API_ID, API_HASH)
        await client.connect()

        try:
            sent = await client.send_code_request(phone)
        except PhoneNumberInvalidError as e:
            await client.disconnect()
            raise ValueError("Неверный номер телефона") from e
        except FloodWaitError as e:
            await client.disconnect()
            raise ValueError(f"FloodWait {e.seconds}с — подождите") from e
        except Exception:
            await client.disconnect()
            raise

        self._pending_logins[account_id] = client
        return {
            "account_id": account_id,
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
        }

    async def confirm_phone_code(
        self,
        account_id: str,
        phone: str,
        phone_code_hash: str,
        code: str,
    ) -> dict:
        client = self._pending_logins.get(account_id)
        if client is None:
            raise ValueError("Сессия входа истекла. Начните заново.")

        code = re.sub(r"\D", "", code or "")
        if not code:
            raise ValueError("Введите код из Telegram/SMS")

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            return {"status": "2fa", "account_id": account_id}
        except PhoneCodeInvalidError as e:
            raise ValueError("Неверный код") from e
        except PhoneCodeExpiredError as e:
            await self.cancel_phone_login(account_id)
            raise ValueError("Код истёк. Начните вход заново.") from e
        except FloodWaitError as e:
            raise ValueError(f"FloodWait {e.seconds}с") from e

        return await self._finish_phone_login(account_id, client)

    async def confirm_2fa(self, account_id: str, password: str) -> dict:
        client = self._pending_logins.get(account_id)
        if client is None:
            raise ValueError("Сессия входа истекла. Начните заново.")

        password = (password or "").strip()
        if not password:
            raise ValueError("Введите облачный пароль 2FA")

        try:
            await client.sign_in(password=password)
        except Exception as e:
            raise ValueError(f"2FA ошибка: {e}") from e

        return await self._finish_phone_login(account_id, client)

    async def _finish_phone_login(self, account_id: str, client: TelegramClient) -> dict:
        self._pending_logins.pop(account_id, None)
        self._clients[account_id] = client

        me = await client.get_me()
        await storage.upsert_account(
            account_id,
            {
                "session_file": f"{account_id}.session",
                "phone": me.phone,
                "username": me.username,
                "user_id": me.id,
                "status": "ok",
                "spamblock": None,
                "last_error": None,
            },
        )
        info = await self.refresh_account(account_id, check_spam=True)
        return {"status": "ok", "account_id": account_id, "info": info}

    async def cancel_phone_login(self, account_id: str | None = None) -> None:
        ids = [account_id] if account_id else list(self._pending_logins)
        for aid in ids:
            if not aid:
                continue
            client = self._pending_logins.pop(aid, None)
            if client is None:
                continue
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass
            if aid not in storage.list_accounts():
                path = self.session_path(aid)
                if path.exists():
                    try:
                        path.unlink()
                    except Exception:
                        pass
                journal = Path(str(path) + "-journal")
                if journal.exists():
                    try:
                        journal.unlink()
                    except Exception:
                        pass

    async def add_session_file(self, src_path: Path, account_id: str | None = None) -> str:
        if len(storage.list_accounts()) >= 20 and account_id not in storage.list_accounts():
            raise ValueError("Лимит 20 аккаунтов. Удалите лишние перед добавлением.")

        account_id = (account_id or src_path.stem).replace(" ", "_")
        dest = self.session_path(account_id)
        dest.write_bytes(src_path.read_bytes())

        journal = Path(str(src_path) + "-journal")
        if journal.exists():
            (SESSIONS_DIR / f"{account_id}.session-journal").write_bytes(journal.read_bytes())

        await storage.upsert_account(
            account_id,
            {
                "session_file": dest.name,
                "phone": None,
                "username": None,
                "status": "unknown",
                "spamblock": None,
                "last_error": None,
            },
        )
        await self.refresh_account(account_id, check_spam=True)
        return account_id

    async def add_from_tdata(
        self,
        tdata_path: Path,
        passcode: str | None = None,
    ) -> list[dict]:
        """Конвертировать tdata → .session и зарегистрировать аккаунты."""
        try:
            from opentele.api import UseCurrentSession
            from opentele.td import TDesktop
        except Exception as e:
            raise RuntimeError(
                "Для tdata установите: pip install opentele-ng tgcrypto-pyrofork\n"
                f"Импорт: {e}"
            ) from e

        try:
            tdesk = TDesktop(str(tdata_path), passcode=passcode or None)
        except TypeError:
            tdesk = TDesktop(str(tdata_path))
        except Exception as e:
            raise ValueError(
                f"Не удалось прочитать tdata: {e}. "
                "Закройте Telegram Desktop и проверьте passcode."
            ) from e

        if not tdesk.isLoaded():
            raise ValueError("В tdata нет авторизованных аккаунтов.")

        accounts = list(tdesk.accounts)
        if not accounts:
            raise ValueError("В tdata нет аккаунтов.")

        free_slots = 20 - len(storage.list_accounts())
        if free_slots <= 0:
            raise ValueError("Лимит 20 аккаунтов. Удалите лишние перед добавлением.")

        results: list[dict] = []
        for i, account in enumerate(accounts):
            if free_slots <= 0:
                results.append(
                    {
                        "status": "skipped",
                        "error": "Достигнут лимит 20 аккаунтов",
                        "index": i,
                    }
                )
                continue

            tmp_stem = SESSIONS_DIR / f"_tdata_import_{i}"
            tmp_session = tmp_stem.with_suffix(".session")
            for p in (tmp_session, Path(str(tmp_session) + "-journal")):
                if p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass

            client = None
            try:
                client = await account.ToTelethon(
                    session=str(tmp_stem),
                    flag=UseCurrentSession,
                )
                await client.connect()
                if not await client.is_user_authorized():
                    results.append(
                        {
                            "status": "error",
                            "error": "Сессия не авторизована",
                            "index": i,
                        }
                    )
                    continue

                me = await client.get_me()
                await client.disconnect()
                client = None

                account_id = (me.phone or "").lstrip("+") or str(me.id)
                account_id = account_id.replace(" ", "_")

                # переносим во финальный .session через add_session_file
                aid = await self.add_session_file(tmp_session, account_id=account_id)
                info = storage.get_account(aid) or {}
                results.append(
                    {
                        "status": "ok",
                        "account_id": aid,
                        "phone": me.phone,
                        "username": me.username,
                        "user_id": me.id,
                        "spamblock": info.get("spamblock"),
                        "index": i,
                    }
                )
                free_slots = 20 - len(storage.list_accounts())
            except Exception as e:
                logger.exception("tdata account[%s] failed", i)
                err = str(e)
                if "two different IP" in err.lower() or "authorization key" in err.lower():
                    err = (
                        "Сессия убита: tdata использовалась с другого IP "
                        "(закройте Desktop и добавьте заново / войдите по номеру). "
                        f"Детали: {e}"
                    )
                results.append({"status": "error", "error": err, "index": i})
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                for p in (tmp_session, Path(str(tmp_session) + "-journal")):
                    if p.exists():
                        try:
                            p.unlink()
                        except Exception:
                            pass

        if not any(r.get("status") == "ok" for r in results):
            errors = "; ".join(
                r.get("error") or "unknown" for r in results if r.get("status") != "ok"
            )
            raise ValueError(f"Не удалось добавить аккаунты из tdata: {errors}")

        return results

    async def get_client(self, account_id: str) -> TelegramClient:
        if account_id in self._clients:
            client = self._clients[account_id]
            if client.is_connected():
                return client
        path = self.session_path(account_id)
        if not path.exists():
            raise FileNotFoundError(f"Session not found: {path}")
        client = TelegramClient(str(path.with_suffix("")), API_ID, API_HASH)
        await client.connect()
        self._clients[account_id] = client
        return client

    async def disconnect(self, account_id: str) -> None:
        client = self._clients.pop(account_id, None)
        if client and client.is_connected():
            await client.disconnect()

    async def disconnect_all(self) -> None:
        for account_id in list(self._clients):
            await self.disconnect(account_id)
        await self.cancel_phone_login()

    async def remove_account(self, account_id: str) -> bool:
        await self.cancel_phone_login(account_id)
        await self.disconnect(account_id)
        path = self.session_path(account_id)
        if path.exists():
            path.unlink()
        journal = Path(str(path) + "-journal")
        if journal.exists():
            journal.unlink()
        return await storage.remove_account(account_id)

    async def refresh_account(
        self, account_id: str, check_spam: bool = True
    ) -> dict | None:
        try:
            client = await self.get_client(account_id)
            if not await client.is_user_authorized():
                await storage.upsert_account(
                    account_id,
                    {
                        "status": "unauthorized",
                        "spamblock": None,
                        "last_error": "Session not authorized",
                        "checked_at": utc_now(),
                    },
                )
                await self.disconnect(account_id)
                return storage.get_account(account_id)

            me = await client.get_me()
            if check_spam:
                spamblock = await self._check_spamblock(client)
            else:
                prev = storage.get_account(account_id) or {}
                spamblock = prev.get("spamblock")

            await storage.upsert_account(
                account_id,
                {
                    "phone": me.phone,
                    "username": me.username,
                    "user_id": me.id,
                    "status": "ok" if not spamblock else "spamblock",
                    "spamblock": spamblock,
                    "last_error": None,
                    "checked_at": utc_now(),
                },
            )
            return storage.get_account(account_id)
        except (AuthKeyUnregisteredError, UserDeactivatedBanError) as e:
            logger.warning("Account %s dead: %s", account_id, e)
            await storage.upsert_account(
                account_id,
                {
                    "status": "dead",
                    "spamblock": None,
                    "last_error": short_error(e),
                    "checked_at": utc_now(),
                },
            )
            await self.disconnect(account_id)
            return storage.get_account(account_id)
        except FloodWaitError as e:
            await storage.upsert_account(
                account_id,
                {
                    "status": "floodwait",
                    "last_error": f"FloodWait {e.seconds}s",
                    "checked_at": utc_now(),
                },
            )
            return storage.get_account(account_id)
        except SessionPasswordNeededError:
            await storage.upsert_account(
                account_id,
                {
                    "status": "2fa",
                    "last_error": "2FA password required",
                    "checked_at": utc_now(),
                },
            )
            await self.disconnect(account_id)
            return storage.get_account(account_id)
        except Exception as e:
            logger.exception("refresh_account %s failed", account_id)
            await storage.upsert_account(
                account_id,
                {
                    "status": "error",
                    "last_error": short_error(e),
                    "checked_at": utc_now(),
                },
            )
            await self.disconnect(account_id)
            return storage.get_account(account_id)

    async def refresh_all(self) -> list[dict]:
        results = []
        for account_id in list(storage.list_accounts()):
            info = await self.refresh_account(account_id, check_spam=True)
            if info:
                results.append(info)
        return results

    async def _check_spamblock(self, client: TelegramClient) -> bool | None:
        """True = spamblocked, False = ok, None = unknown."""
        try:
            entity = await client.get_entity("SpamBot")
            try:
                await client(UnblockRequest(entity))
            except Exception:
                pass
            await client.send_message(entity, "/start")
            await asyncio.sleep(1.5)
            messages = await client.get_messages(entity, limit=5)
            text = " ".join((m.message or "") for m in messages).lower()
            blocked_markers = [
                "ограничен",
                "limited",
                "blocked from",
                "spam",
                "не можете",
                "cannot send",
                "you've been",
                "вы были",
                "unfortunately",
                "к сожалению",
            ]
            ok_markers = [
                "нет ограничений",
                "good news",
                "no limits",
                "no restrictions",
                "свободен",
                "free from",
                "не ограничен",
            ]
            if any(m in text for m in ok_markers):
                return False
            if any(m in text for m in blocked_markers):
                return True
            return None
        except Exception as e:
            logger.warning("SpamBot check failed: %s", e)
            return None

    def alive_accounts(self) -> list[str]:
        return [
            aid
            for aid, acc in storage.list_accounts().items()
            if acc.get("status") in ("ok", "spamblock", "unknown", "floodwait")
        ]

    def campaign_accounts(self) -> list[str]:
        result = []
        for aid, acc in storage.list_accounts().items():
            if acc.get("status") in ("dead", "unauthorized", "2fa", "error"):
                continue
            if SKIP_SPAMBLOCKED and acc.get("spamblock") is True:
                continue
            result.append(aid)
        return result


account_manager = AccountManager()
