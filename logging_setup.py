"""
Logging configuration for the AI Orchestrator.

Sets up root logger with both console (StreamHandler) and file
(RotatingFileHandler) output. Call setup_logging() once at startup.
"""

import logging
import threading
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


def install_thread_excepthook() -> None:
    """Route uncaught exceptions in worker threads into the log file.

    Python's default ``threading.excepthook`` prints to stderr, and
    ``run_orchestrator.ps1`` starts ``--watch`` with a hidden window and no
    stdout/stderr redirection — so a background thread that dies (heartbeat,
    telegram listener, limits refresher) takes its traceback with it and the
    orchestrator just quietly loses that job. Most loops already log with
    ``logger.exception``; this is the net under the ones that do not.

    Called explicitly from ``main()`` rather than from ``setup_logging()``:
    replacing ``threading.excepthook`` is a process-wide mutation, and a test or
    an embedder importing this module must not get it by accident.

    A dying THREAD never charges the process-crash circuit breaker: it does not
    end the process, so the watchdog does not restart into the same queue task,
    so it is not an unsuccessful attempt AT a task.

    ``SystemExit`` is ignored, wider than the stdlib default (see the comment below) — a thread
    calling ``sys.exit()`` is asking to end itself, not reporting a failure.
    """

    def _hook(args) -> None:
        # issubclass, deliberately WIDER than the stdlib default. Measured on
        # CPython 3.14.2: threading.py compares `args.exc_type == SystemExit`,
        # so the stdlib prints a SystemExit SUBCLASS rather than ignoring it —
        # this is an extension, not parity, and saying otherwise would invite a
        # future cleanup to "restore stdlib parity" and undo it. The reason to
        # be wider: a subclass carrying an exit code is an ordinary way to end a
        # thread, and logging that as CRITICAL is exactly the unattended noise
        # this hook exists to avoid.
        if isinstance(args.exc_type, type) and issubclass(args.exc_type, SystemExit):
            return
        logging.getLogger("thread").critical(
            "Unbehandelte Exception in Thread '%s' (%s)",
            getattr(args.thread, "name", "?"),
            getattr(args.exc_type, "__name__", args.exc_type),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _hook
