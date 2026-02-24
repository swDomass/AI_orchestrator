"""
Telegram notification support for the AI Orchestrator.
Sends messages on task completion, errors, provider exhaustion, and queue summary.
"""

import urllib.request
import urllib.parse
import json
from datetime import datetime

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_ENABLED,
    NOTIFY_ON_TASK_DONE,
    NOTIFY_ON_ERROR,
    NOTIFY_ON_QUEUE_COMPLETE,
    NOTIFY_ON_ALL_PROVIDERS_EXHAUSTED,
)

_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# Track stats for summary
_stats = {
    "tasks_done": 0,
    "tasks_failed": 0,
    "providers_used": {},
    "started_at": None,
}


def _send(text: str) -> bool:
    """Send a Telegram message. Returns True on success."""
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    try:
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
        }).encode("utf-8")

        req = urllib.request.Request(_API_URL, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"  [telegram] Fehler beim Senden: {e}")
        return False


def _truncate(text: str, max_len: int = 300) -> str:
    """Truncate text for Telegram (4096 char limit)."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def start_session() -> None:
    """Mark the start of an orchestrator session."""
    _stats["started_at"] = datetime.now()
    _stats["tasks_done"] = 0
    _stats["tasks_failed"] = 0
    _stats["providers_used"] = {}


def notify_task_done(task: str, provider: str, output: str) -> None:
    """Notify that a task was completed successfully."""
    if not NOTIFY_ON_TASK_DONE:
        return

    _stats["tasks_done"] += 1
    _stats["providers_used"][provider] = _stats["providers_used"].get(provider, 0) + 1

    _send(
        f"✅ *Task erledigt* ({provider})\n"
        f"`{_truncate(task, 100)}`\n\n"
        f"{_truncate(output)}"
    )


def notify_error(task: str, provider: str, error: str) -> None:
    """Notify about a task error."""
    if not NOTIFY_ON_ERROR:
        return

    _stats["tasks_failed"] += 1

    _send(
        f"❌ *Fehler* ({provider})\n"
        f"`{_truncate(task, 100)}`\n\n"
        f"Fehler: {error}"
    )


def notify_providers_exhausted(reset_in: str) -> None:
    """Notify that all providers are exhausted."""
    if not NOTIFY_ON_ALL_PROVIDERS_EXHAUSTED:
        return

    _send(
        f"⏸️ *Alle Provider voll*\n"
        f"Nächster Reset in: {reset_in}\n"
        f"Orchestrator schläft und retried automatisch."
    )


def notify_queue_complete(remaining: int = 0) -> None:
    """Send summary when queue is finished or orchestrator stops."""
    if not NOTIFY_ON_QUEUE_COMPLETE:
        return

    started = _stats["started_at"]
    duration = ""
    if started:
        elapsed = datetime.now() - started
        mins = int(elapsed.total_seconds() // 60)
        duration = f"\nDauer: {mins} Min"

    providers_str = ", ".join(
        f"{name}: {count}" for name, count in _stats["providers_used"].items()
    ) or "keine"

    status = "✅ Alle Tasks erledigt!" if remaining == 0 else f"⚠️ {remaining} Task(s) noch offen"

    _send(
        f"📊 *Orchestrator Zusammenfassung*\n\n"
        f"{status}\n"
        f"Erledigt: {_stats['tasks_done']}\n"
        f"Fehler: {_stats['tasks_failed']}\n"
        f"Provider: {providers_str}{duration}"
    )


def notify_tool_progress(tool_name: str, iteration: int, max_iter: int, message: str) -> None:
    """Notify about tool progress (e.g. review loop iteration)."""
    _send(
        f"🔄 *{tool_name}* ({iteration}/{max_iter})\n"
        f"{message}"
    )


def notify_tool_done(tool_name: str, iterations: int, success: bool, summary: str) -> None:
    """Notify when a tool loop finishes."""
    icon = "✅" if success else "⚠️"
    _send(
        f"{icon} *{tool_name} abgeschlossen*\n"
        f"Iterationen: {iterations}\n\n"
        f"{_truncate(summary)}"
    )
