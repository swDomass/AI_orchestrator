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
    NOTIFY_ON_TASK_STARTED,
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


def _truncate(text: str, max_len: int = 3500) -> str:
    """Truncate text for Telegram (4096 byte limit per message)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_len:
        return text
    return encoded[:max_len].decode("utf-8", errors="ignore") + "..."


def start_session() -> None:
    """Mark the start of an orchestrator session."""
    with _stats_lock:
        _stats["started_at"] = datetime.now()
        _stats["tasks_done"] = 0
        _stats["tasks_failed"] = 0
        _stats["providers_used"] = {}


def notify_task_started(task: str, provider: str) -> None:
    """Notify that a task is about to be executed."""
    if not NOTIFY_ON_TASK_STARTED:
        return

    provider_safe = _escape_markdown(provider)
    task_safe = _strip_backticks(_truncate(task, 300))

    _send(
        f"🚀 *Task gestartet* ({provider_safe})\n"
        f"`{task_safe}`"
    )


def notify_task_done(task: str, provider: str, output: str, change_summary: str | None = None) -> None:
    """Notify that a task was completed successfully."""
    if not NOTIFY_ON_TASK_DONE:
        return

    with _stats_lock:
        _stats["tasks_done"] += 1
        _stats["providers_used"][provider] = _stats["providers_used"].get(provider, 0) + 1

    provider_safe = _escape_markdown(provider)
    task_safe = _strip_backticks(_truncate(task, 300))

    changes_block = ""
    if change_summary:
        changes_block = f"\n\n📁 *Änderungen:*\n{_escape_markdown(_truncate(change_summary, 500))}"

    header = f"✅ *Task erledigt* ({provider_safe})\n`{task_safe}`\n\n"
    # Dynamically cap output so header + output + changes_block stays within Telegram's 4096 byte limit.
    # Escape output first, then truncate based on remaining byte budget.
    output_escaped = _escape_markdown(output)
    max_output = max(200, 4096 - len(header.encode("utf-8")) - len(changes_block.encode("utf-8")) - 10)
    output_safe = _truncate(output_escaped, min(3900, max_output))

    _send(header + output_safe + changes_block)


def notify_error(task: str, provider: str, error: str) -> None:
    """Notify about a task error."""
    if not NOTIFY_ON_ERROR:
        return

    with _stats_lock:
        _stats["tasks_failed"] += 1

    provider_safe = _escape_markdown(provider)
    task_safe = _strip_backticks(_truncate(task, 300))
    error_safe = _escape_markdown(_truncate(str(error), 3500))

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


def notify_usage_suggestions(
    suggestions: list,
    remaining_pct: float,
    resets_in_sec: int,
    *,
    pace_info: "dict | None" = None,
) -> None:
    """Send usage suggestions with /pick and /decline options."""
    if not suggestions:
        return
    mins = resets_in_sec // 60
    max_pick = len(suggestions)
    lines = [
        "💡 *Freie Kapazität verfügbar*",
        "",
        f"Claude: {remaining_pct:.0f}% übrig, Reset in ~{mins} Min",
    ]
    if pace_info:
        from usage_budget import format_pace_status
        lines.append(_escape_markdown(format_pace_status(pace_info)))
    lines += [
        "",
        "Vorschläge:",
    ]
    for s in suggestions:
        label_safe = _escape_markdown(s.label)
        lines.append(f"  {s.rank}. {label_safe}")

    lines.append("")
    pick_hint = "/pick 1" if max_pick == 1 else f"/pick 1-{max_pick}"
    lines.append(f"{pick_hint} — Auswahl treffen")
    lines.append("/decline — Nichts davon")

    _send("\n".join(lines))


def notify_limits_429_fallback(provider_name: str, remaining_pct: float) -> None:
    """Notify that cclimits is rate-limited and using cached/estimated data."""
    provider_safe = _escape_markdown(provider_name)
    _send(
        f"*cclimits HTTP 429* ({provider_safe})\n"
        f"Monitoring-API rate-limited. Provider weiterhin verfuegbar.\n"
        f"Geschaetzte Kapazitaet: {remaining_pct:.0f}% remaining (cached)\n"
        f"Naechster Versuch in 5 Min."
    )


def notify_limits_429_cleared(provider_name: str, remaining_pct: float) -> None:
    """Notify that cclimits 429 has cleared and real data is available again."""
    provider_safe = _escape_markdown(provider_name)
    _send(
        f"*cclimits 429 aufgeloest* ({provider_safe})\n"
        f"Echte Kapazitaet: {remaining_pct:.0f}% remaining"
    )


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


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        msg = " ".join(sys.argv[1:])
        if _send(msg):
            print(f"  [telegram] Nachricht gesendet: {msg}")
        else:
            print("  [telegram] Fehler beim Senden (Check .env / TELEGRAM_ENABLED)")
