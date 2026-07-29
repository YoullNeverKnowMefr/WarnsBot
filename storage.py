from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import STORAGE_FILE

logger = logging.getLogger(__name__)

DEFAULT_STORAGE: dict[str, Any] = {
    "accounts": {},
    "targets": {},
    "campaign": {
        "active": False,
        "started_at": None,
        "finished_at": None,
        "current_round": 0,
        "total_rounds": 0,
        "target_usernames": [],
        "status": "idle",
        "last_error": None,
        "report_language": None,
        "report_category": None,
        "checkpoints_done": [],
    },
    "settings": {
        "report_language": "ru",
        "report_category": "illegal_docs",
    },
    "deleted_history": [],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, path: Path = STORAGE_FILE) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            data = deepcopy(DEFAULT_STORAGE)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return data
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to read storage, recreating")
            raw = deepcopy(DEFAULT_STORAGE)
        for key, value in DEFAULT_STORAGE.items():
            raw.setdefault(key, deepcopy(value))
        return raw

    async def save(self) -> None:
        async with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    # ---- accounts ----
    def list_accounts(self) -> dict[str, dict]:
        return self.data["accounts"]

    def get_account(self, account_id: str) -> dict | None:
        return self.data["accounts"].get(account_id)

    async def upsert_account(self, account_id: str, payload: dict) -> None:
        current = self.data["accounts"].get(account_id, {})
        current.update(payload)
        current["id"] = account_id
        current.setdefault("added_at", utc_now())
        current["updated_at"] = utc_now()
        self.data["accounts"][account_id] = current
        await self.save()

    async def remove_account(self, account_id: str) -> bool:
        if account_id not in self.data["accounts"]:
            return False
        del self.data["accounts"][account_id]
        await self.save()
        return True

    # ---- targets ----
    def list_targets(self) -> dict[str, dict]:
        return self.data["targets"]

    async def upsert_target(self, username: str, payload: dict | None = None) -> None:
        key = username.lstrip("@").lower()
        current = self.data["targets"].get(key, {})
        current.update(payload or {})
        current["username"] = key
        current.setdefault("added_at", utc_now())
        current.setdefault("status", "active")
        current.setdefault("selected", True)
        current["updated_at"] = utc_now()
        self.data["targets"][key] = current
        await self.save()

    async def remove_target(self, username: str) -> bool:
        key = username.lstrip("@").lower()
        if key not in self.data["targets"]:
            return False
        del self.data["targets"][key]
        await self.save()
        return True

    async def remove_targets(self, usernames: list[str]) -> list[str]:
        removed: list[str] = []
        for raw in usernames:
            key = raw.lstrip("@").lower()
            if key in self.data["targets"]:
                del self.data["targets"][key]
                removed.append(key)
        if removed:
            await self.save()
        return removed

    async def remove_targets_by_statuses(self, statuses: set[str] | list[str]) -> list[str]:
        want = {s.lower() for s in statuses}
        removed = [
            uname
            for uname, t in list(self.data["targets"].items())
            if (t.get("status") or "").lower() in want
        ]
        for uname in removed:
            del self.data["targets"][uname]
        if removed:
            await self.save()
        return removed

    def targets_by_statuses(self, statuses: set[str] | list[str]) -> list[dict]:
        want = {s.lower() for s in statuses}
        return [
            t
            for t in self.data["targets"].values()
            if (t.get("status") or "").lower() in want
        ]

    async def set_target_selected(self, username: str, selected: bool) -> bool:
        key = username.lstrip("@").lower()
        target = self.data["targets"].get(key)
        if not target:
            return False
        target["selected"] = selected
        target["updated_at"] = utc_now()
        await self.save()
        return True

    def selected_targets(self) -> list[str]:
        return [
            t["username"]
            for t in self.data["targets"].values()
            if t.get("selected") and t.get("status") != "deleted"
        ]

    def is_target_active(self, username: str) -> bool:
        """Выбран и не удалён — можно слать жалобы."""
        key = username.lstrip("@").lower()
        t = self.data["targets"].get(key)
        if not t:
            return False
        return bool(t.get("selected")) and t.get("status") != "deleted"

    def deleted_targets(self) -> list[dict]:
        """Текущие цели со статусом deleted."""
        return [
            t
            for t in self.data["targets"].values()
            if t.get("status") == "deleted"
        ]

    def dead_accounts(self) -> list[dict]:
        return [
            acc
            for acc in self.data["accounts"].values()
            if acc.get("status") in ("dead", "unauthorized", "error")
        ]

    # ---- campaign ----
    async def update_campaign(self, **kwargs: Any) -> None:
        self.data["campaign"].update(kwargs)
        await self.save()

    def campaign(self) -> dict:
        return self.data["campaign"]

    async def mark_target_deleted(self, username: str, detected_at: str | None = None) -> None:
        key = username.lstrip("@").lower()
        target = self.data["targets"].get(key)
        already = bool(target and target.get("status") == "deleted")
        ts = detected_at or utc_now()
        if target:
            target["status"] = "deleted"
            target["deleted_at"] = ts
            target["selected"] = False
        if not already:
            hist = self.data.setdefault("deleted_history", [])
            # не дублировать ту же цель за последние сутки
            recent_same = False
            try:
                now_ts = datetime.fromisoformat(ts).timestamp()
            except Exception:
                now_ts = datetime.now(timezone.utc).timestamp()
            for item in reversed(hist[-50:]):
                if item.get("username") != key:
                    continue
                try:
                    prev = datetime.fromisoformat(item["detected_at"]).timestamp()
                except Exception:
                    continue
                if now_ts - prev < 86400:
                    recent_same = True
                    break
            if not recent_same:
                hist.append({"username": key, "detected_at": ts})
        await self.save()

    def deleted_last_days(self, days: int = 3) -> list[dict]:
        """Уникальные удаления за N суток (последняя фиксация по username)."""
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        latest: dict[str, dict] = {}
        for item in self.data.get("deleted_history", []):
            try:
                ts = datetime.fromisoformat(item["detected_at"]).timestamp()
            except Exception:
                continue
            if ts < cutoff:
                continue
            uname = item.get("username") or ""
            if not uname:
                continue
            prev = latest.get(uname)
            if prev is None:
                latest[uname] = item
                continue
            try:
                prev_ts = datetime.fromisoformat(prev["detected_at"]).timestamp()
            except Exception:
                latest[uname] = item
                continue
            if ts > prev_ts:
                latest[uname] = item
        # добавить текущие deleted, если их ещё нет в истории периода
        for t in self.deleted_targets():
            uname = t.get("username") or ""
            if not uname or uname in latest:
                continue
            detected = t.get("deleted_at") or t.get("updated_at") or utc_now()
            try:
                ts = datetime.fromisoformat(detected).timestamp()
            except Exception:
                ts = datetime.now(timezone.utc).timestamp()
            if ts >= cutoff:
                latest[uname] = {"username": uname, "detected_at": detected}
        return sorted(latest.values(), key=lambda x: x.get("detected_at") or "")

    def get_report_language(self) -> str:
        settings = self.data.setdefault("settings", {})
        lang = settings.get("report_language") or "ru"
        return lang

    async def set_report_language(self, lang: str) -> None:
        self.data.setdefault("settings", {})["report_language"] = lang
        await self.save()

    def get_report_category(self) -> str:
        from config import DEFAULT_REPORT_CATEGORY, REPORT_TYPE_CHOICES

        settings = self.data.setdefault("settings", {})
        cat = settings.get("report_category") or DEFAULT_REPORT_CATEGORY
        if cat not in REPORT_TYPE_CHOICES:
            return DEFAULT_REPORT_CATEGORY
        return cat

    async def set_report_category(self, category_id: str) -> None:
        from config import REPORT_TYPE_CHOICES

        if category_id not in REPORT_TYPE_CHOICES:
            return
        self.data.setdefault("settings", {})["report_category"] = category_id
        await self.save()


storage = Storage()
