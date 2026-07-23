"""Короткие и точные классификации ошибок Telethon."""

from __future__ import annotations

from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError
from telethon.errors.common import TypeNotFoundError


def short_error(exc: BaseException | str, limit: int = 120) -> str:
    text = str(exc)
    low = text.lower()
    if "constructor id" in low or isinstance(exc, TypeNotFoundError):
        return "TL layer mismatch (обновите telethon)"
    if "two different ip" in low or "authorization key" in low:
        return "Сессия убита (два IP)"
    if "flood" in low:
        return text[:limit]
    if "remaining bytes" in low:
        text = text.split("Remaining bytes", 1)[0].strip().rstrip(".")
    text = text.replace("\n", " ").strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text or "unknown error"


def is_tl_layer_error(exc: BaseException) -> bool:
    if isinstance(exc, TypeNotFoundError):
        return True
    low = str(exc).lower()
    return "constructor id" in low or "matching constructor" in low


def is_username_gone(exc: BaseException) -> bool:
    """True только если username точно свободен / не существует."""
    if isinstance(exc, (UsernameNotOccupiedError, UsernameInvalidError)):
        return True
    if is_tl_layer_error(exc):
        return False
    low = str(exc).lower()
    if "nobody is using this username" in low:
        return True
    if "username not occupied" in low:
        return True
    if "no user has" in low and "username" in low:
        return True
    return False
