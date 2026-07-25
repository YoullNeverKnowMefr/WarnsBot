"""Определение удалённых / замороженных целевых ботов."""

from __future__ import annotations

from telethon.tl.functions.contacts import ResolveUsernameRequest
from telethon.tl.types import User

from tg_errors import is_tl_layer_error, is_username_gone, short_error

_DELETED_NAME_MARKERS = (
    "deleted account",
    "удалённый аккаунт",
    "удаленный аккаунт",
    "deleted user",
    "deactivated",
    "аккаунт удалён",
    "аккаунт удален",
)

_DEAD_PEER_MARKERS = (
    "user_deactivated",
    "user_is_deleted",
    "input_user_deactivated",
    "peer_id_invalid",
    "frozen",
    "banned",
    "deactivated",
    "bot was blocked",  # не всегда = удалён, но для цели часто мёртв
)


def entity_is_dead(entity, expected_username: str | None = None) -> bool:
    """True если User выглядит удалённым / деактивированным."""
    if not isinstance(entity, User):
        return False
    if getattr(entity, "deleted", False):
        return True

    fn = (getattr(entity, "first_name", None) or "").strip().lower()
    ln = (getattr(entity, "last_name", None) or "").strip().lower()
    full = f"{fn} {ln}".strip()
    if any(m in full for m in _DELETED_NAME_MARKERS):
        return True

    # username освобождён / не совпадает — цель по @ больше не та
    if expected_username:
        want = expected_username.lstrip("@").lower()
        have = (getattr(entity, "username", None) or "").lower()
        others = []
        for u in getattr(entity, "usernames", None) or []:
            un = getattr(u, "username", None)
            if un:
                others.append(un.lower())
        if have and have != want and want not in others:
            return True
        # удалённый peer часто без @username
        if not have and not others and (getattr(entity, "deleted", False) or full):
            if any(m in full for m in _DELETED_NAME_MARKERS):
                return True

    return False


def is_dead_peer_error(exc: BaseException) -> bool:
    if is_username_gone(exc):
        return True
    if is_tl_layer_error(exc):
        return False
    low = str(exc).lower()
    return any(m in low for m in _DEAD_PEER_MARKERS)


async def resolve_target(client, username: str):
    """
    Свежий resolve username (минуя устаревший кэш где возможно).
    Returns (entity|None, error|None). error='gone' если username свободен.
    """
    uname = username.lstrip("@").lower()
    try:
        result = await client(ResolveUsernameRequest(uname))
        users = list(getattr(result, "users", None) or [])
        entity = None
        for u in users:
            if isinstance(u, User):
                entity = u
                break
        if entity is None and users:
            entity = users[0]
        if entity is None:
            # fallback
            entity = await client.get_entity(uname)
        return entity, None
    except Exception as e:
        if is_username_gone(e) or is_dead_peer_error(e):
            return None, "gone"
        # кэш get_entity как запасной вариант
        try:
            entity = await client.get_entity(uname)
            return entity, None
        except Exception as e2:
            if is_username_gone(e2) or is_dead_peer_error(e2):
                return None, "gone"
            return None, short_error(e2)
