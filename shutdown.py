"""
Graceful OS shutdown for the AI Orchestrator.

Trigger sources:
  - Queue task tag:   - [ ] Task X #shutdown
  - Telegram message: "#shutdown" in plain text
  - Telegram command: /cancel-shutdown  (cancels pending countdown)

State machine:
  IDLE → SHUTDOWN_PENDING → COUNTDOWN (60s) → OS SHUTDOWN
                         ↑ any incoming Telegram message cancels
                         ↑ new queue task appears → cancelled
"""

import logging
import subprocess
import sys
import threading
from typing import Callable

from config import SHUTDOWN_COMMAND, SHUTDOWN_DELAY_SEC

logger = logging.getLogger(__name__)

# Module-level state (volatile, session-only — no file persistence)
shutdown_pending = threading.Event()
shutdown_cancel = threading.Event()
_countdown_running = threading.Event()
_countdown_lock = threading.Lock()


def request_shutdown() -> bool:
    """Set shutdown_pending. Idempotent — returns False if already pending."""
    if shutdown_pending.is_set():
        return False
    shutdown_pending.set()
    logger.info("shutdown: pending flag set")
    return True


def cancel_shutdown() -> None:
    """Signal the countdown to abort."""
    shutdown_cancel.set()


def check_queue_abort() -> bool:
    """Return True if new tasks appeared (abort shutdown during countdown)."""
    try:
        from queue_manager import read_queue
        if read_queue():
            logger.info("shutdown: new tasks found — countdown aborted")
            shutdown_cancel.set()
            return True
    except Exception:
        pass
    return False


def execute_shutdown(
    delay_sec: int = SHUTDOWN_DELAY_SEC,
    cleanup_cb: Callable | None = None,
) -> None:
    """Countdown then OS shutdown. Blocks the calling thread for up to delay_sec.

    The countdown runs in 5-second chunks so new tasks can abort it.
    If shutdown_cancel is set (any Telegram message or /cancel-shutdown),
    the countdown aborts and both events are cleared.
    """
    from notifier import (
        notify_shutdown_cancelled,
        notify_shutdown_executing,
        notify_shutdown_pending,
    )
    from queue_manager import append_log

    with _countdown_lock:
        if _countdown_running.is_set():
            logger.info("shutdown: countdown already running; ignoring duplicate request")
            return
        _countdown_running.set()

    try:
        shutdown_cancel.clear()
        notify_shutdown_pending(delay_sec)
        logger.info("shutdown: countdown started (%ds)", delay_sec)

        elapsed = 0
        while elapsed < delay_sec:
            chunk = min(5, delay_sec - elapsed)
            cancelled = shutdown_cancel.wait(timeout=chunk)

            if cancelled or check_queue_abort():
                shutdown_cancel.clear()
                shutdown_pending.clear()
                notify_shutdown_cancelled()
                logger.info("shutdown: countdown cancelled")
                return

            elapsed += chunk

        # Execute — cleanup before OS kills us (Telegram first, logs last)
        logger.info("shutdown: executing OS shutdown command")
        notify_shutdown_executing()
        shutdown_pending.clear()

        if cleanup_cb:
            try:
                cleanup_cb()
            except Exception as e:
                logger.warning("shutdown: cleanup_cb error: %s", e)

        try:
            append_log("Shutdown initiated by #shutdown.")
        except Exception:
            pass

        try:
            result = subprocess.run(
                SHUTDOWN_COMMAND,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=sys.platform == "win32",
            )
            if result.returncode != 0:
                logger.error("shutdown: command returned %d — stderr: %s", result.returncode, result.stderr.strip())
        except Exception as e:
            logger.error("shutdown: OS shutdown command failed: %s", e)
    finally:
        _countdown_running.clear()
