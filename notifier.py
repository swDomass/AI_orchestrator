"""
Telegram notification support for the AI Orchestrator.
Sends messages on task completion, errors, provider exhaustion, and queue summary.
"""

import threading
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
_stats_lock = threading.Lock()


def _escape_markdown(text: str) -> str:
    """Escape Telegram legacy Markdown control chars in dynamic text.

    Note: This escapes for *inline* use only. Text placed inside backtick
    blocks (``...``) should NOT be escaped — use _strip_backticks() instead.
    """
    escaped = []
    for ch in str(text):
        if ch in "\\_*`[]()":
            escaped.append("\\")
        escaped.append(ch)
    return "".join(escaped)


def _strip_backticks(text: str) -> str:
    """Remove backticks from text intended for use inside Telegram backtick blocks."""
    return str(text).replace("`", "'")


def send_message(text: str) -> bool:
    """Send a raw Telegram message. Public API for use by other modules."""
    return _send(text)


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
    with _stats_lock:
        _stats["started_at"] = datetime.now()
        _stats["tasks_done"] = 0
        _stats["tasks_failed"] = 0
        _stats["providers_used"] = {}


def notify_task_done(task: str, provider: str, output: str, change_summary: str | None = None) -> None:
    """Notify that a task was completed successfully."""
    if not NOTIFY_ON_TASK_DONE:
        return

    with _stats_lock:
        _stats["tasks_done"] += 1
        _stats["providers_used"][provider] = _stats["providers_used"].get(provider, 0) + 1

    provider_safe = _escape_markdown(provider)
    task_safe = _strip_backticks(_truncate(task, 100))
    output_safe = _escape_markdown(_truncate(output))

    changes_block = ""
    if change_summary:
        changes_block = f"\n\n📁 *Änderungen:*\n{_escape_markdown(_truncate(change_summary, 500))}"

    _send(
        f"✅ *Task erledigt* ({provider_safe})\n"
        f"`{task_safe}`\n\n"
        f"{output_safe}{changes_block}"
    )


def notify_error(task: str, provider: str, error: str) -> None:
    """Notify about a task error."""
    if not NOTIFY_ON_ERROR:
        return

    with _stats_lock:
        _stats["tasks_failed"] += 1

    provider_safe = _escape_markdown(provider)
    task_safe = _strip_backticks(_truncate(task, 100))
    error_safe = _escape_markdown(str(error))

    _send(
        f"❌ *Fehler* ({provider_safe})\n"
        f"`{task_safe}`\n\n"
        f"Fehler: {error_safe}"
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

    with _stats_lock:
        started = _stats["started_at"]
        tasks_done = _stats["tasks_done"]
        tasks_failed = _stats["tasks_failed"]
        providers_used = dict(_stats["providers_used"])

    duration = ""
    if started:
        elapsed = datetime.now() - started
        mins = int(elapsed.total_seconds() // 60)
        duration = f"\nDauer: {mins} Min"

    providers_str = ", ".join(
        f"{name}: {count}" for name, count in providers_used.items()
    ) or "keine"
    providers_safe = _escape_markdown(providers_str)

    status = "✅ Alle Tasks erledigt!" if remaining == 0 else f"⚠️ {remaining} Task(s) noch offen"

    _send(
        f"📊 *Orchestrator Zusammenfassung*\n\n"
        f"{status}\n"
        f"Erledigt: {tasks_done}\n"
        f"Fehler: {tasks_failed}\n"
        f"Provider: {providers_safe}{duration}"
    )


def notify_shutdown_pending(delay_sec: int) -> None:
    """Notify that an OS shutdown countdown has started."""
    _send(f"⏾ Shutting down in {delay_sec}s. Send any message to cancel.")


def notify_shutdown_cancelled() -> None:
    """Notify that the shutdown countdown was cancelled."""
    _send("✋ Shutdown cancelled.")


def notify_shutdown_executing() -> None:
    """Notify that the OS shutdown command is about to be executed."""
    _send("⏾ Shutting down now.")


def notify_approval_required(task_text: str, reasons: list[str], timeout_sec: int) -> None:
    """Send a Telegram approval request for a risky action."""
    task_safe = _strip_backticks(_truncate(task_text, 100))
    reasons_safe = _escape_markdown("; ".join(reasons[:5]))
    timeout_min = timeout_sec // 60

    _send(
        f"🔒 *Approval required*\n\n"
        f"Task: `{task_safe}`\n"
        f"Action: {reasons_safe}\n\n"
        f"Reply within {timeout_min} min:\n"
        f"/approve — allow this action\n"
        f"/approve\\-all \\<category\\> — allow all in session\n"
        f"/deny — block, pause task\n"
        f"/skip — skip for now, task retries later"
    )


def notify_approval_timeout(task_text: str) -> None:
    """Notify that the approval request timed out."""
    task_safe = _strip_backticks(_truncate(task_text, 100))
    _send(f"⏱ *Approval timeout*\nTask paused: `{task_safe}`")


def notify_approval_result(task_text: str, result: str) -> None:
    """Notify about an approval decision."""
    icons = {"approved": "✅", "denied": "❌", "skipped": "⏭️"}
    icon = icons.get(result, "ℹ️")
    task_safe = _strip_backticks(_truncate(task_text, 100))
    result_safe = _escape_markdown(result)
    _send(f"{icon} *Approval {result_safe}*\nTask: `{task_safe}`")


def notify_tool_progress(tool_name: str, iteration: int, max_iter: int, message: str) -> None:
    """Notify about tool progress (e.g. review loop iteration)."""
    _send(
        f"🔄 *{_escape_markdown(tool_name)}* ({iteration}/{max_iter})\n"
        f"{_escape_markdown(message)}"
    )


def notify_tool_done(tool_name: str, iterations: int, success: bool, summary: str) -> None:
    """Notify when a tool loop finishes."""
    icon = "✅" if success else "⚠️"
    _send(
        f"{icon} *{_escape_markdown(tool_name)} abgeschlossen*\n"
        f"Iterationen: {iterations}\n\n"
        f"{_escape_markdown(_truncate(summary))}"
    )
