"""
Logging configuration for the AI Orchestrator.

Sets up root logger with both console (StreamHandler) and file
(RotatingFileHandler) output. Call setup_logging() once at startup.
"""

import logging
from logging.handlers import RotatingFileHandler

from config import LOG_BACKUP_COUNT, LOG_FILE, LOG_MAX_BYTES

_initialized = False


def setup_logging() -> None:
    """Configure root logger with console + rotating file handler."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
