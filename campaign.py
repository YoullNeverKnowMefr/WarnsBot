from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path

from telethon.errors import FloodWaitError, UserIsBlockedError
from telethon.tl.functions.account import ReportPeerRequest
from telethon.tl.functions.messages import ReportRequest, StartBotRequest
from telethon.tl.types import (
    InputReportReasonFake,
    InputReportReasonOther,
    ReportResultAddComment,
    ReportResultChooseOption,
    ReportResultReported,
    User,
)

from accounts import account_manager
from config import (
    ACCOUNT_DELAY_SEC,
    BOT_TEXTS_FILE,
    CAMPAIGN_ROUNDS,
    CHECK_SPAMBLOCK_EACH_ROUND,
    DEFAULT_BOT_REPORTS,
    GREETING_TEXT,
    LANGUAGE_LABELS,
    PHRASE_FILES,
    REPORTS_PER_BOT,
    REPORTS_PER_MESSAGE,
    ROUND_DELAY_MAX_SEC,
    ROUND_DELAY_MIN_SEC,
    SKIP_SPAMBLOCKED,
    get_report_categories,
    report_categories_label,
)
from storage import storage, utc_now
from target_check import entity_is_dead, is_dead_peer_error, resolve_target
from tg_errors import is_tl_layer_error, short_error

logger = logging.getLogger(__name__)


def load_texts(path: Path, fallback: list[str] | None = None) -> list[str]:
    if path.exists():
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if lines:
            return lines
    return list(fallback or [])


def load_phrases(lang: str) -> list[str]:
    path = PHRASE_FILES.get(lang) or PHRASE_FILES["ru"]
    phrases = load_texts(path)
    if phrases:
        return phrases
    # fallback other language then defaults
    for other in PHRASE_FILES:
        if other != lang:
            phrases = load_texts(PHRASE_FILES[other])
            if phrases:
                return phrases
    return load_texts(BOT_TEXTS_FILE, DEFAULT_BOT_REPORTS)


def random_complaint_text(phrases: list[str], lang: str = "ru") -> str:
    """Фраза выбранного языка + уникальный номер (как в видео)."""
    n = random.randint(1, 200)
    phrase = random.choice(phrases) if phrases else ""
    if lang == "en":
        tag = f"[Complaint #{n}]"
    else:
        tag = f"[Жалоба номер {n}]"
    if phrase:
        # Telegram comment length soft limit — keep reasonable
        combined = f"{phrase} {tag}"
        return combined[:900]
    return tag


def peer_reason_for(category: dict):
    kind = category.get("peer_reason", "other")
    if kind == "fake":
        return InputReportReasonFake()
    return InputReportReasonOther()


class CampaignEngine:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.notify_callback = None
        self._language = "ru"
        self._categories: list[dict] = get_report_categories(None)
        self._category_id = "illegal_docs"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _notify(self, text: str, *, chat: bool = False) -> None:
        """chat=True — короткое сообщение в Telegram; иначе только в лог."""
        logger.info(text)
        if chat and self.notify_callback:
            try:
                await self.notify_callback(text)
            except Exception:
                logger.exception("notify failed")

    def _current_targets(self) -> list[str]:
        """Актуальный выбор галочек (не снимок на старте)."""
        return storage.selected_targets()

    async def start(
        self,
        target_usernames: list[str] | None = None,
        language: str | None = None,
        category_id: str | None = None,
    ) -> str:
        if self.running:
            return "Кампания уже запущена."

        lang = (language or storage.get_report_language() or "ru").lower()
        if lang not in PHRASE_FILES:
            lang = "ru"
        phrases = load_phrases(lang)
        if not phrases:
            return f"Нет фраз для языка {lang}. Добавьте файл phrases_{lang}.txt"

        cat_id = category_id or storage.get_report_category()
        categories = get_report_categories(cat_id)

        targets = target_usernames or storage.selected_targets()
        if not targets:
            return "Нет выбранных целевых ботов. Добавьте/выберите их в меню."

        accounts = account_manager.campaign_accounts()
        if not accounts:
            return "Нет доступных аккаунтов (без спамблока/мёртвых). Добавьте аккаунты."

        self._language = lang
        self._categories = categories
        self._category_id = cat_id
        self._stop.clear()
        await storage.set_report_language(lang)
        await storage.set_report_category(cat_id)
        await storage.update_campaign(
            active=True,
            started_at=utc_now(),
            finished_at=None,
            current_round=0,
            total_rounds=CAMPAIGN_ROUNDS,
            target_usernames=targets,
            status="running",
            last_error=None,
            checkpoints_done=[],
            report_language=lang,
            report_category=cat_id,
        )
        self._task = asyncio.create_task(
            self._run(targets, accounts, phrases, lang, categories, cat_id),
            name="campaign",
        )
        label = LANGUAGE_LABELS.get(lang, lang)
        cat_label = report_categories_label(cat_id)
        if len(categories) > 1:
            per = f"по 1 жалобе каждого вида на бота и на сообщение ({len(categories)} вида)"
        else:
            per = f"жалоб на бота/сообщение: {REPORTS_PER_BOT}/{REPORTS_PER_MESSAGE}"
        return (
            f"Кампания запущена.\n"
            f"Язык фраз: {label} ({len(phrases)} шт.)\n"
            f"Вид жалобы: {cat_label}\n"
            f"Ботов: {len(targets)}\n"
            f"Аккаунтов: {len(accounts)}\n"
            f"Раундов: {CAMPAIGN_ROUNDS} (пауза ~{ROUND_DELAY_MIN_SEC // 60} мин)\n"
            f"Между аккаунтами: ~{ACCOUNT_DELAY_SEC // 60} мин\n"
            f"{per}"
        )

    async def stop(self) -> str:
        if not self.running:
            await storage.update_campaign(active=False, status="idle")
            return "Кампания не запущена."
        self._stop.set()
        await storage.update_campaign(status="stopping")
        await self._notify("Остановка кампании…", chat=True)
        return "Остановка кампании запрошена."

    async def _run(
        self,
        targets: list[str],
        accounts: list[str],
        phrases: list[str],
        lang: str,
        categories: list[dict],
        category_id: str,
    ) -> None:
        cat_label = report_categories_label(category_id)
        try:
            for round_no in range(1, CAMPAIGN_ROUNDS + 1):
                if self._stop.is_set():
                    break

                targets = self._current_targets()
                await storage.update_campaign(
                    current_round=round_no,
                    status="running",
                    target_usernames=targets,
                )
                if not targets:
                    await self._notify(
                        f"⚠ Раунд {round_no}: нет выбранных ботов — стоп",
                        chat=True,
                    )
                    break

                await self._notify(
                    f"▶ Раунд {round_no}/{CAMPAIGN_ROUNDS} | "
                    f"ботов={len(targets)} | {cat_label}",
                    chat=True,
                )

                alive = await self._filter_accounts(accounts)
                if not alive:
                    await self._notify("⚠ Нет живых аккаунтов — стоп", chat=True)
                    break

                round_peer = 0
                round_msg = 0
                skipped = 0
                for idx, account_id in enumerate(alive):
                    if self._stop.is_set():
                        break

                    if CHECK_SPAMBLOCK_EACH_ROUND:
                        info = await account_manager.refresh_account(
                            account_id, check_spam=True
                        )
                        if not info or info.get("status") in ("dead", "unauthorized", "2fa"):
                            await self._notify(f"⏭ {account_id} недоступен — пропуск")
                            skipped += 1
                            continue
                        if SKIP_SPAMBLOCKED and info.get("spamblock") is True:
                            await self._notify(f"⏭ {account_id} спамблок — пропуск")
                            skipped += 1
                            continue

                    targets = self._current_targets()
                    if not targets:
                        await self._notify("Нет выбранных ботов — выход из раунда")
                        break

                    acc_peer = 0
                    acc_msg = 0
                    acc_bots = 0
                    await self._notify(
                        f"Аккаунт {account_id} ({idx + 1}/{len(alive)}) → {len(targets)} бот(ов)"
                    )
                    for username in list(targets):
                        if self._stop.is_set():
                            break
                        if not storage.is_target_active(username):
                            await self._notify(f"пропуск @{username} (снят/удалён)")
                            continue
                        try:
                            stats = await self._process_target(
                                account_id, username, phrases, lang, categories
                            )
                            acc_peer += stats.get("peer", 0)
                            acc_msg += stats.get("msg", 0)
                            if stats.get("peer", 0) or stats.get("msg", 0):
                                acc_bots += 1
                        except Exception as e:
                            logger.exception("process %s via %s", username, account_id)
                            await self._notify(
                                f"Ошибка {account_id} → @{username}: {short_error(e)}"
                            )

                    round_peer += acc_peer
                    round_msg += acc_msg
                    await self._notify(
                        f"📊 {account_id}: на бота={acc_peer}, "
                        f"на сообщ.={acc_msg}, ботов={acc_bots} "
                        f"(всего жалоб={acc_peer + acc_msg})",
                        chat=True,
                    )

                    if idx < len(alive) - 1 and not self._stop.is_set():
                        await self._notify(
                            f"⏸ Пауза {ACCOUNT_DELAY_SEC // 60} мин до следующего аккаунта"
                        )
                        await self._sleep(ACCOUNT_DELAY_SEC)

                await self._notify(
                    f"■ Раунд {round_no}/{CAMPAIGN_ROUNDS}: "
                    f"жалоб={round_peer + round_msg} "
                    f"(бот={round_peer}, сообщ.={round_msg}), "
                    f"пропуск акк.={skipped}",
                    chat=True,
                )

                if round_no < CAMPAIGN_ROUNDS and not self._stop.is_set():
                    delay = random.randint(ROUND_DELAY_MIN_SEC, ROUND_DELAY_MAX_SEC)
                    await self._notify(
                        f"⏸ Ждём {delay // 60} мин до раунда {round_no + 1}",
                        chat=True,
                    )
                    await self._sleep(delay)

            status = "stopped" if self._stop.is_set() else "finished"
            await storage.update_campaign(
                active=False,
                status=status,
                finished_at=utc_now(),
            )
            await self._notify(f"■ Кампания завершена ({status}).", chat=True)
        except Exception as e:
            logger.exception("campaign crashed")
            await storage.update_campaign(
                active=False,
                status="error",
                last_error=short_error(e),
                finished_at=utc_now(),
            )
            await self._notify(f"✖ Кампания упала: {short_error(e)}", chat=True)

    async def _filter_accounts(self, accounts: list[str]) -> list[str]:
        alive = []
        for aid in accounts:
            info = storage.get_account(aid)
            if not info:
                continue
            if info.get("status") in ("dead", "unauthorized", "2fa", "error"):
                continue
            if SKIP_SPAMBLOCKED and info.get("spamblock") is True:
                continue
            alive.append(aid)
        return alive

    async def _sleep(self, seconds: float) -> None:
        end = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < end:
            if self._stop.is_set():
                return
            await asyncio.sleep(min(1.0, end - asyncio.get_event_loop().time()))

    async def _process_target(
        self,
        account_id: str,
        username: str,
        phrases: list[str],
        lang: str,
        categories: list[dict],
    ) -> dict:
        """Вернуть {"peer": N, "msg": N} — сколько жалоб ушло."""
        stats = {"peer": 0, "msg": 0}
        username = username.lstrip("@").lower()
        client = await account_manager.get_client(account_id)

        try:
            entity, err = await resolve_target(client, username)
        except Exception as e:
            entity, err = None, short_error(e)

        if err == "gone":
            await storage.mark_target_deleted(username)
            await self._notify(f"@{username} удалён/не найден ({account_id})")
            return stats
        if entity is None:
            brief = err or "unknown"
            if is_tl_layer_error(Exception(brief)):
                await storage.upsert_target(
                    username, {"last_error": brief, "status": "active"}
                )
                await self._notify(f"@{username}: {brief} — пропуск ({account_id})")
            else:
                await storage.upsert_target(
                    username, {"status": "unreachable", "last_error": brief}
                )
                await self._notify(f"@{username} недоступен ({account_id}): {brief}")
            return stats

        if entity_is_dead(entity, expected_username=username):
            await storage.mark_target_deleted(username)
            await self._notify(f"@{username} удалён/заморожен ({account_id})")
            return stats

        if isinstance(entity, User) and not getattr(entity, "bot", False):
            await storage.mark_target_deleted(username)
            await self._notify(f"@{username} больше не бот ({account_id})")
            return stats

        try:
            try:
                await client(
                    StartBotRequest(bot=entity, peer=entity, start_param="start")
                )
            except Exception:
                await client.send_message(entity, "/start")
            await asyncio.sleep(random.uniform(1.5, 3.0))
            await client.send_message(entity, GREETING_TEXT)
            await asyncio.sleep(random.uniform(2.5, 4.5))
        except UserIsBlockedError:
            await self._notify(f"{account_id} заблокирован ботом @{username}")
            return stats
        except FloodWaitError as e:
            await self._notify(f"FloodWait {e.seconds}s на {account_id} — пауза")
            await asyncio.sleep(min(e.seconds, 180))
            return stats
        except Exception as e:
            if is_dead_peer_error(e):
                await storage.mark_target_deleted(username)
                await self._notify(f"@{username} мёртв при /start ({account_id})")
                return stats
            await self._notify(
                f"Не удалось написать @{username} с {account_id}: {short_error(e)}"
            )

        messages = await client.get_messages(entity, limit=12)
        bot_message = None
        for msg in messages:
            if msg and msg.sender_id and getattr(entity, "id", None) == msg.sender_id:
                bot_message = msg
                break
        if bot_message is None and messages:
            bot_message = messages[0]

        # одна категория → N жалоб; две → по 1 каждого вида
        peer_times = REPORTS_PER_BOT if len(categories) == 1 else 1
        msg_times = REPORTS_PER_MESSAGE if len(categories) == 1 else 1

        for category in categories:
            for _ in range(peer_times):
                if self._stop.is_set():
                    return stats
                text = random_complaint_text(phrases, lang)
                try:
                    await self._report_peer(client, entity, text, category)
                    stats["peer"] += 1
                    logger.info(
                        "Peer report ok | %s → @%s | %s | %s",
                        account_id,
                        username,
                        category["id"],
                        text[:80],
                    )
                except FloodWaitError as e:
                    await self._notify(f"FloodWait {e.seconds}s (peer) {account_id}")
                    await asyncio.sleep(min(e.seconds, 120))
                except Exception as e:
                    logger.warning(
                        "Peer report fail %s @%s [%s]: %s",
                        account_id,
                        username,
                        category["id"],
                        e,
                    )
                await asyncio.sleep(random.uniform(1.2, 2.5))

        if bot_message is not None:
            for category in categories:
                for _ in range(msg_times):
                    if self._stop.is_set():
                        return stats
                    text = random_complaint_text(phrases, lang)
                    try:
                        await self._report_message(
                            client, entity, bot_message.id, text, category
                        )
                        stats["msg"] += 1
                        logger.info(
                            "Msg report ok | %s → @%s msg=%s | %s | %s",
                            account_id,
                            username,
                            bot_message.id,
                            category["id"],
                            text[:80],
                        )
                    except FloodWaitError as e:
                        await self._notify(f"FloodWait {e.seconds}s (msg) {account_id}")
                        await asyncio.sleep(min(e.seconds, 120))
                    except Exception as e:
                        logger.warning(
                            "Msg report fail %s @%s [%s]: %s",
                            account_id,
                            username,
                            category["id"],
                            e,
                        )
                    await asyncio.sleep(random.uniform(1.2, 2.5))
        else:
            await self._notify(f"@{username}: нет сообщения для жалобы ({account_id})")

        await storage.upsert_target(
            username,
            {"last_reported_at": utc_now(), "last_account": account_id, "status": "active"},
        )
        return stats

    async def _report_peer(self, client, entity, text: str, category: dict) -> None:
        await client(
            ReportPeerRequest(
                peer=entity,
                reason=peer_reason_for(category),
                message=f"{category['title']}. {text}",
            )
        )

    async def _report_message(
        self, client, entity, msg_id: int, text: str, category: dict
    ) -> None:
        """Многошаговое меню Telegram: категория → подкатегория → комментарий."""
        preferred = tuple(category.get("keywords") or ())
        result = await client(
            ReportRequest(peer=entity, id=[msg_id], option=b"", message="")
        )
        option = b""

        for _ in range(6):
            if isinstance(result, ReportResultReported):
                return

            if isinstance(result, ReportResultChooseOption):
                option = self._pick_report_option(result.options, preferred)
                result = await client(
                    ReportRequest(
                        peer=entity,
                        id=[msg_id],
                        option=option,
                        message=text,
                    )
                )
                continue

            if isinstance(result, ReportResultAddComment):
                option = result.option or option
                result = await client(
                    ReportRequest(
                        peer=entity,
                        id=[msg_id],
                        option=option,
                        message=text,
                    )
                )
                continue

            break

    @staticmethod
    def _pick_report_option(options, preferred: tuple[str, ...] = ()) -> bytes:
        if not options:
            return b""
        # сначала предпочитаемые ключевые слова категории
        if preferred:
            for opt in options:
                title = (getattr(opt, "text", None) or "").lower()
                if any(k in title for k in preferred):
                    return opt.option
        # общий fallback
        fallback = (
            "незакон",
            "illegal",
            "мошен",
            "fraud",
            "scam",
            "наркот",
            "drug",
            "поддел",
            "спам",
            "spam",
            "другое",
            "other",
        )
        for opt in options:
            title = (getattr(opt, "text", None) or "").lower()
            if any(k in title for k in fallback):
                return opt.option
        return options[0].option


campaign_engine = CampaignEngine()
