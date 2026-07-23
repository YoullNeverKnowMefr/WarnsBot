from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from config import LOGS_DIR


def setup_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / "bot.log"

    root = logging.getLogger()
    if root.handlers:
        return

    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.INFO)

    root.addHandler(file_handler)
    root.addHandler(console)


class LogBuffer:
    """In-memory ring buffer of recent log lines for the admin bot UI."""

    def __init__(self, limit: int = 200) -> None:
        self.limit = limit
        self.lines: list[str] = []

    def add(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > self.limit:
            self.lines = self.lines[-self.limit :]

    def tail(self, n: int = 30) -> str:
        if not self.lines:
            return "Логов пока нет."
        return "\n".join(self.lines[-n:])


log_buffer = LogBuffer()


class BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            log_buffer.add(msg)
        except Exception:
            self.handleError(record)


def attach_buffer_handler() -> None:
    handler = BufferHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
    )
    logging.getLogger().addHandler(handler)
