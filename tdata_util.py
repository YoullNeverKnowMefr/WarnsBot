"""Импорт аккаунтов из Telegram Desktop tdata (через opentele-ng)."""

from __future__ import annotations

import logging
import shutil
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Признаки папки tdata
_TDATA_MARKERS = ("key_datas", "key_data", "settingss", "usertag")


def find_tdata_root(extracted: Path) -> Path:
    """Найти корень tdata внутри распакованного архива."""
    if _looks_like_tdata(extracted):
        return extracted

    direct = extracted / "tdata"
    if direct.is_dir() and _looks_like_tdata(direct):
        return direct

    candidates: list[Path] = []
    for path in extracted.rglob("*"):
        if path.is_dir() and path.name.lower() == "tdata" and _looks_like_tdata(path):
            candidates.append(path)
    if candidates:
        # самый короткий путь — ближе к корню
        return min(candidates, key=lambda p: len(p.parts))

    for path in extracted.iterdir():
        if path.is_dir() and _looks_like_tdata(path):
            return path

    raise ValueError(
        "В архиве не найдена папка tdata. "
        "Заархивируйте папку tdata целиком (внутри должны быть key_datas и др.)."
    )


def _looks_like_tdata(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    names = {p.name.lower() for p in folder.iterdir()}
    if any(m in names for m in _TDATA_MARKERS):
        return True
    # иногда есть только hex-папки DC
    hex_dirs = [
        p
        for p in folder.iterdir()
        if p.is_dir() and len(p.name) >= 16 and all(c in "0123456789abcdefABCDEF" for c in p.name)
    ]
    return len(hex_dirs) >= 1


def extract_tdata_zip(zip_path: Path, dest_dir: Path) -> Path:
    """Распаковать zip и вернуть путь к tdata. Защита от zip-slip."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            target = (dest_dir / info.filename).resolve()
            if not str(target).startswith(str(dest_dir.resolve())):
                raise ValueError(f"Некорректный путь в архиве: {info.filename}")
        zf.extractall(dest_dir)
    return find_tdata_root(dest_dir)


def cleanup_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
