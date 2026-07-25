from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telethon.tl.types import User

from accounts import account_manager
from storage import storage, utc_now
from target_check import entity_is_dead, is_dead_peer_error, resolve_target
from tg_errors import is_tl_layer_error, short_error

logger = logging.getLogger(__name__)

# Hours after campaign start to check deletion (12ч — основной отчёт из видео)
CHECK_HOURS = (2, 5, 12, 24)


class MonitorService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.notify_callback = None

    async def _notify(self, text: str) -> None:
        logger.info(text)
        if self.notify_callback:
            try:
                await self.notify_callback(text)
            except Exception:
                logger.exception("monitor notify failed")

    def start_background(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="monitor")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_targets(reason="periodic")
                await self.maybe_campaign_checkpoints()
            except Exception:
                logger.exception("monitor loop error")
            await asyncio.sleep(600)

    async def maybe_campaign_checkpoints(self) -> None:
        camp = storage.campaign()
        started = camp.get("started_at")
        if not started:
            return
        try:
            start_dt = datetime.fromisoformat(started)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        except Exception:
            return

        elapsed_h = (datetime.now(timezone.utc) - start_dt).total_seconds() / 3600
        done = set(camp.get("checkpoints_done") or [])

        for hour in CHECK_HOURS:
            key = str(hour)
            if key in done:
                continue
            if elapsed_h >= hour:
                report = await self.check_targets(reason=f"checkpoint_{hour}h")
                done.add(key)
                await storage.update_campaign(checkpoints_done=sorted(done))
                prefix = "📋 ОСНОВНОЙ ОТЧЁТ 12ч" if hour == 12 else f"📋 Контроль через {hour}ч"
                await self._notify(f"{prefix}:\n{report}")

    async def check_targets(self, reason: str = "manual") -> str:
        targets = list(storage.list_targets().values())
        if not targets:
            return "Нет целевых ботов."

        accounts = account_manager.alive_accounts()
        if not accounts:
            return "Нет аккаунтов для проверки."

        client = None
        account_id = None
        for aid in accounts:
            try:
                client = await account_manager.get_client(aid)
                if await client.is_user_authorized():
                    account_id = aid
                    break
            except Exception:
                continue

        if client is None:
            return "Не удалось подключить аккаунт для проверки."

        deleted: list[str] = []
        alive: list[str] = []
        unknown: list[str] = []
        restored = 0

        for target in targets:
            username = target["username"]
            prev_deleted = target.get("status") == "deleted"
            try:
                entity, err = await resolve_target(client, username)
                if err == "gone" or entity is None:
                    await storage.mark_target_deleted(username)
                    deleted.append(username)
                    logger.info("Target @%s gone (%s) via %s", username, err, reason)
                    continue

                if entity_is_dead(entity, expected_username=username):
                    await storage.mark_target_deleted(username)
                    deleted.append(username)
                    logger.info(
                        "Target @%s dead entity deleted=%s name=%r via %s",
                        username,
                        getattr(entity, "deleted", None),
                        getattr(entity, "first_name", None),
                        reason,
                    )
                    continue

                # не бот и не канал — считаем мёртвой/невалидной целью
                if isinstance(entity, User) and not getattr(entity, "bot", False):
                    await storage.mark_target_deleted(username)
                    deleted.append(username)
                    logger.info("Target @%s is not a bot anymore via %s", username, reason)
                    continue

                await storage.upsert_target(
                    username,
                    {
                        "status": "active",
                        "last_check": utc_now(),
                        "last_error": None,
                        "deleted_at": None,
                    },
                )
                alive.append(username)
                if prev_deleted:
                    restored += 1
            except Exception as e:
                brief = short_error(e)
                if is_dead_peer_error(e):
                    await storage.mark_target_deleted(username)
                    deleted.append(username)
                    logger.info("Target @%s deleted (%s) via %s", username, brief, reason)
                elif is_tl_layer_error(e):
                    unknown.append(f"@{username}: {brief}")
                    await storage.upsert_target(
                        username,
                        {"last_check": utc_now(), "last_error": brief},
                    )
                    logger.warning("check @%s TL error: %s", username, brief)
                else:
                    unknown.append(f"@{username}: {brief}")
                    await storage.upsert_target(
                        username,
                        {"last_check": utc_now(), "last_error": brief},
                    )
                    logger.warning("check @%s failed: %s", username, brief)

        lines = [
            f"Проверка ({reason}) акк. {account_id}",
            f"✅ Активны: {len(alive)}",
            f"🗑 Удалены: {len(deleted)}",
            f"❓ Неясно: {len(unknown)}",
        ]
        if restored:
            lines.append(f"♻️ Восстановлены из «удалённых»: {restored}")
        if alive:
            lines.append("Активны: " + ", ".join(f"@{u}" for u in alive[:30]))
        if deleted:
            lines.append("Удалены: " + ", ".join(f"@{u}" for u in deleted[:30]))
        if unknown:
            lines.append("Ошибки:\n" + "\n".join(unknown[:10]))

        recent = storage.deleted_last_days(3)
        lines.append(f"Счётчик удалённых за 3 суток: {len(recent)}")
        return "\n".join(lines)


monitor_service = MonitorService()
