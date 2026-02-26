"""
AI Orchestrator — Heartbeat Runner

Parses HEARTBEAT.md from the vault and runs scheduled checks at configurable
intervals. Sends Telegram notifications for non-empty results.

HEARTBEAT.md sections:
    ## Every 30 minutes   → interval_min = 30
    ## Every 2 hours      → interval_min = 120
    ## Daily (first run after 08:00) → daily_after = 480 (minute-of-day)

Items:
    - [ ] {description}

Built-in handlers matched by case-insensitive substring in item label:
    "queue"        → _check_queue_idle
    "git status"   → _check_git_status
    "disk space"   → _check_disk_space
    "check-limits" → _check_limits
    "summarize"    → _check_task_summary
    "stale branch" → _check_stale_branches
"""

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Callable, Optional

from config import (
    ALLOWED_CWD_ROOTS,
    HEARTBEAT_DISK_WARN_PCT,
    HEARTBEAT_FILE,
    HEARTBEAT_GIT_STALE_DAYS,
    HEARTBEAT_QUEUE_IDLE_HOURS,
    VAULT_PATH,
)

logger = logging.getLogger(__name__)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class HeartbeatItem:
    label: str
    interval_min: int      # 0 = daily
    daily_after: int = 480 # minute-of-day threshold (for daily items)
    last_run: Optional[datetime] = None
    last_run_date: Optional[date] = None   # for daily items
    handler_key: str = ""


# ── Module-level state for queue-idle tracking ─────────────────────────────

_queue_empty_since: Optional[datetime] = None


# ── Built-in handlers ─────────────────────────────────────────────────────────

def _check_queue_idle(queue_read_fn: Callable) -> Optional[str]:
    """Warn if the queue has been empty for >HEARTBEAT_QUEUE_IDLE_HOURS hours."""
    global _queue_empty_since

    try:
        tasks = queue_read_fn()
    except Exception:
        return None

    if tasks:
        _queue_empty_since = None
        return None

    now = datetime.now()
    if _queue_empty_since is None:
        _queue_empty_since = now
        return None

    idle_hours = (now - _queue_empty_since).total_seconds() / 3600
    if idle_hours >= HEARTBEAT_QUEUE_IDLE_HOURS:
        return (
            f"Queue seit {idle_hours:.1f}h leer — "
            f"keine neuen Tasks seit {_queue_empty_since.strftime('%H:%M')}."
        )
    return None


def _check_git_status() -> Optional[str]:
    """Run git status --short in each ALLOWED_CWD_ROOTS dir."""
    results: list[str] = []
    roots = ALLOWED_CWD_ROOTS or []

    for root in roots:
        if not root.is_dir():
            continue
        try:
            r = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                lines = r.stdout.strip().splitlines()
                results.append(
                    f"{root.name}: {len(lines)} uncommitted change(s)\n"
                    + "\n".join(f"  {l}" for l in lines[:5])
                    + ("\n  ..." if len(lines) > 5 else "")
                )
        except (OSError, subprocess.TimeoutExpired):
            pass

    return "\n\n".join(results) if results else None


def _check_disk_space() -> Optional[str]:
    """Check disk usage for drives in ALLOWED_CWD_ROOTS."""
    checked_drives: set[str] = set()
    warnings: list[str] = []
    roots = ALLOWED_CWD_ROOTS or []

    for root in roots:
        if not root.exists():
            continue
        # Use drive letter on Windows, mount point on Unix
        drive = root.anchor
        if drive in checked_drives:
            continue
        checked_drives.add(drive)

        try:
            usage = shutil.disk_usage(str(root))
            free_pct = (usage.free / usage.total) * 100
            if free_pct < HEARTBEAT_DISK_WARN_PCT:
                free_gb = usage.free / (1024 ** 3)
                total_gb = usage.total / (1024 ** 3)
                warnings.append(
                    f"Drive {drive}: {free_pct:.1f}% frei "
                    f"({free_gb:.1f} GB / {total_gb:.1f} GB)"
                )
        except OSError:
            pass

    return "\n".join(warnings) if warnings else None


def _check_limits(get_limits_fn: Callable) -> Optional[str]:
    """Call get_limits() and return a formatted summary."""
    try:
        limits = get_limits_fn()
        parts: list[str] = []
        for name in ("claude", "gemini", "codex"):
            lim = getattr(limits, name)
            if lim.available:
                parts.append(f"{name}: {lim.remaining_pct:.0f}% remaining")
            else:
                parts.append(f"{name}: ❌ {lim.error or 'unavailable'}")
        return "\n".join(parts) if parts else None
    except Exception as e:
        logger.debug("check-limits failed: %s", e)
        return None


def _check_task_summary(dispatcher: Optional[object] = None) -> Optional[str]:
    """Summarize yesterday's completed tasks from memory/task_results/."""
    try:
        from memory import _TASK_RESULTS_DIR, _parse_memory_file
        yesterday = (datetime.now().date() - timedelta(days=1))

        completed: list[str] = []
        if _TASK_RESULTS_DIR.exists():
            for path in _TASK_RESULTS_DIR.glob("*.md"):
                mem = _parse_memory_file(path)
                if mem and mem["timestamp"].date() == yesterday:
                    status = "✅" if mem["success"] else "❌"
                    completed.append(f"{status} {mem['task'][:60]}")

        if not completed:
            return f"Gestern ({yesterday}) keine abgeschlossenen Tasks."

        header = f"Gestern ({yesterday}) abgeschlossen ({len(completed)}):"
        return header + "\n" + "\n".join(completed[:20])

    except Exception as e:
        logger.debug("task-summary failed: %s", e)
        return None


def _check_stale_branches() -> Optional[str]:
    """Warn about branches with last commit >HEARTBEAT_GIT_STALE_DAYS days old."""
    results: list[str] = []
    roots = ALLOWED_CWD_ROOTS or []

    for root in roots:
        if not root.is_dir():
            continue
        try:
            r = subprocess.run(
                ["git", "branch", "--list", "--sort=-creatordate",
                 "--format=%(refname:short) %(creatordate:relative)"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode != 0 or not r.stdout.strip():
                continue

            stale: list[str] = []
            for line in r.stdout.strip().splitlines():
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                branch, age_str = parts
                # Detect stale: "N weeks ago", "N months ago"
                m = re.search(r"(\d+)\s+(week|month)", age_str)
                if m:
                    n = int(m.group(1))
                    unit = m.group(2)
                    days = n * 7 if unit == "week" else n * 30
                    if days > HEARTBEAT_GIT_STALE_DAYS:
                        stale.append(f"  {branch}: {age_str}")

            if stale:
                results.append(f"{root.name} — stale branches:\n" + "\n".join(stale[:10]))

        except (OSError, subprocess.TimeoutExpired):
            pass

    return "\n\n".join(results) if results else None


# ── Handler dispatch table ────────────────────────────────────────────────────

HANDLER_KEYS: list[tuple[str, str]] = [
    ("queue",         "_check_queue_idle"),
    ("git status",    "_check_git_status"),
    ("disk space",    "_check_disk_space"),
    ("check-limits",  "_check_limits"),
    ("summarize",     "_check_task_summary"),
    ("stale branch",  "_check_stale_branches"),
]


def _match_handler_key(label: str) -> str:
    """Return the first matching handler key for a label (case-insensitive)."""
    label_lower = label.lower()
    for keyword, handler_key in HANDLER_KEYS:
        if keyword in label_lower:
            return handler_key
    return ""


# ── HEARTBEAT.md parser ───────────────────────────────────────────────────────

_INTERVAL_RE = re.compile(
    r"^##\s+Every\s+(\d+)\s+(minutes?|hours?)\s*$", re.IGNORECASE
)
_DAILY_RE = re.compile(
    r"^##\s+Daily.*?(\d{1,2}):(\d{2})", re.IGNORECASE
)
_ITEM_RE = re.compile(r"^-\s+\[\s*\]\s+(.+)$")


def _parse_heartbeat_md(content: str) -> list[HeartbeatItem]:
    """Parse HEARTBEAT.md sections into HeartbeatItem list."""
    items: list[HeartbeatItem] = []
    current_interval_min = 0
    current_daily_after = 0
    is_daily = False

    for line in content.splitlines():
        # Section heading: ## Every N minutes/hours
        m = _INTERVAL_RE.match(line)
        if m:
            n = int(m.group(1))
            unit = m.group(2).lower()
            if "hour" in unit:
                n *= 60
            current_interval_min = n
            is_daily = False
            continue

        # Section heading: ## Daily ...
        m = _DAILY_RE.match(line)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2))
            current_daily_after = hour * 60 + minute
            current_interval_min = 0
            is_daily = True
            continue

        # Task item: - [ ] description
        m = _ITEM_RE.match(line)
        if m:
            label = m.group(1).strip()
            handler_key = _match_handler_key(label)
            items.append(HeartbeatItem(
                label=label,
                interval_min=current_interval_min,
                daily_after=current_daily_after if is_daily else 0,
                handler_key=handler_key,
            ))

    return items


# ── HeartbeatRunner ───────────────────────────────────────────────────────────

class HeartbeatRunner:
    """Loads HEARTBEAT.md, reloads on mtime change, runs due items."""

    def __init__(self) -> None:
        self._items: list[HeartbeatItem] = []
        self._mtime: float = 0.0
        self._reload_if_changed()

    def _reload_if_changed(self) -> None:
        """Reload HEARTBEAT.md if file has changed since last load."""
        if not HEARTBEAT_FILE.exists():
            return
        try:
            mtime = HEARTBEAT_FILE.stat().st_mtime
            if mtime == self._mtime:
                return
            content = HEARTBEAT_FILE.read_text(encoding="utf-8")
            new_items = _parse_heartbeat_md(content)

            # Preserve last_run state for items that survived a reload
            old_by_label = {i.label: i for i in self._items}
            for item in new_items:
                if item.label in old_by_label:
                    item.last_run = old_by_label[item.label].last_run
                    item.last_run_date = old_by_label[item.label].last_run_date

            self._items = new_items
            self._mtime = mtime
            logger.debug("HEARTBEAT.md loaded: %d items", len(self._items))
        except Exception as e:
            logger.warning("Failed to load HEARTBEAT.md: %s", e)

    def due_items(self) -> list[HeartbeatItem]:
        """Return items that are due for execution."""
        self._reload_if_changed()
        now = datetime.now()
        due: list[HeartbeatItem] = []

        for item in self._items:
            if item.interval_min == 0:
                # Daily item
                today = now.date()
                minute_of_day = now.hour * 60 + now.minute
                if item.last_run_date == today:
                    continue  # already ran today
                if minute_of_day < item.daily_after:
                    continue  # not yet reached the daily threshold
                due.append(item)
            else:
                # Periodic item
                if item.last_run is None:
                    due.append(item)
                    continue
                elapsed_min = (now - item.last_run).total_seconds() / 60
                if elapsed_min >= item.interval_min:
                    due.append(item)

        return due

    def run_due(
        self,
        queue_read_fn: Callable,
        dispatcher: Optional[object] = None,
    ) -> None:
        """Run all due items, send Telegram notifications for non-empty results.

        Never raises — all errors are caught and logged.
        """
        self._reload_if_changed()
        due = self.due_items()
        if not due:
            return

        try:
            from notifier import send_message
        except Exception:
            send_message = None

        for item in due:
            try:
                result = self._run_item(item, queue_read_fn, dispatcher)
                now = datetime.now()
                item.last_run = now
                item.last_run_date = now.date()

                if result:
                    logger.info("Heartbeat [%s]: %s", item.label, result[:100])
                    msg = f"🫀 Heartbeat — {item.label}\n\n{result}"
                    if send_message:
                        try:
                            send_message(msg)
                        except Exception as e:
                            logger.warning("Heartbeat notify failed: %s", e)
            except Exception as e:
                logger.warning("Heartbeat item '%s' failed: %s", item.label, e)

    def _run_item(
        self,
        item: HeartbeatItem,
        queue_read_fn: Callable,
        dispatcher: Optional[object],
    ) -> Optional[str]:
        """Dispatch to the correct handler."""
        key = item.handler_key

        if key == "_check_queue_idle":
            return _check_queue_idle(queue_read_fn)
        elif key == "_check_git_status":
            return _check_git_status()
        elif key == "_check_disk_space":
            return _check_disk_space()
        elif key == "_check_limits":
            try:
                from limits import get_limits
                return _check_limits(get_limits)
            except ImportError:
                return None
        elif key == "_check_task_summary":
            return _check_task_summary(dispatcher)
        elif key == "_check_stale_branches":
            return _check_stale_branches()
        else:
            logger.debug("No handler for heartbeat item: %s", item.label)
            return None
