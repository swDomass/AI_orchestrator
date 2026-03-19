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
    "usage-suggest" → _check_usage_suggest
"""

import logging
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Callable, Optional

from config import (
    ALLOWED_CWD_ROOTS,
    CAPACITY_LOG_FILE,
    CAPACITY_LOG_RETENTION_DAYS,
    HEARTBEAT_DISK_WARN_PCT,
    HEARTBEAT_FILE,
    HEARTBEAT_GIT_STALE_DAYS,
    HEARTBEAT_QUEUE_IDLE_HOURS,
    MIN_CAPACITY_PERCENT,
    VAULT_PATH,
)
from queue_manager import _write_bytes_atomic

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
_queue_empty_lock = threading.Lock()
_usage_suggest_thread: Optional[threading.Thread] = None


# ── Built-in handlers ─────────────────────────────────────────────────────────

def _check_queue_idle(queue_read_fn: Callable) -> Optional[str]:
    """Warn if the queue has been empty for >HEARTBEAT_QUEUE_IDLE_HOURS hours."""
    global _queue_empty_since

    try:
        tasks = queue_read_fn()
    except (OSError, ValueError):
        return None

    now = datetime.now()
    with _queue_empty_lock:
        if tasks:
            _queue_empty_since = None
            return None

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
                encoding="utf-8",
                errors="replace",
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


def _append_capacity_log(limits) -> None:
    """Append one line per provider/window to the persistent capacity log in the vault."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines: list[str] = []
        for name in ("claude", "gemini", "codex"):
            lim = getattr(limits, name, None)
            if lim is None:
                continue
            avail = "true" if lim.available else "false"
            if name == "gemini" and lim.windows:
                # Skip vertex duplicates; prefer gemini_2 (current gen), fallback to all
                non_vertex = {k: v for k, v in lim.windows.items() if "vertex" not in k}
                selected = non_vertex if non_vertex else lim.windows
                for wname, wdata in selected.items():
                    pct = f"{wdata.remaining_pct:.1f}"
                    w_avail = "true" if wdata.remaining_pct >= MIN_CAPACITY_PERCENT else "false"
                    lines.append(f"{ts} | {name}_{wname} | {pct} | {w_avail}")
            elif lim.windows:
                for wname, wdata in lim.windows.items():
                    pct = f"{wdata.remaining_pct:.1f}"
                    w_avail = "true" if wdata.remaining_pct >= MIN_CAPACITY_PERCENT else "false"
                    lines.append(f"{ts} | {name}_{wname} | {pct} | {w_avail}")
            else:
                pct = f"{lim.remaining_pct:.1f}" if lim.available else "-1.0"
                lines.append(f"{ts} | {name} | {pct} | {avail}")

        if not lines:
            return

        if not CAPACITY_LOG_FILE.exists():
            CAPACITY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CAPACITY_LOG_FILE.write_text(
                "# AI Provider Capacity Log\n"
                "<!-- appended by orchestrator heartbeat -->\n\n",
                encoding="utf-8",
            )

        with CAPACITY_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        _cleanup_capacity_log()
    except Exception as e:
        logger.debug("capacity log append failed: %s", e)


# Module-level: track last cleanup date to run at most once per day
_last_capacity_cleanup: Optional[date] = None


def _cleanup_capacity_log() -> None:
    """Remove capacity log entries older than CAPACITY_LOG_RETENTION_DAYS.

    Runs at most once per calendar day. Preserves the header comment lines
    and rewrites the file in-place only when old entries are actually removed.
    """
    global _last_capacity_cleanup
    today = date.today()
    if _last_capacity_cleanup == today:
        return

    if not CAPACITY_LOG_FILE.exists():
        _last_capacity_cleanup = today
        return

    cutoff = datetime.now() - timedelta(days=CAPACITY_LOG_RETENTION_DAYS)
    try:
        text = CAPACITY_LOG_FILE.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)

        kept: list[str] = []
        removed = 0
        for line in lines:
            # Header / comment lines are always kept
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("<!--"):
                kept.append(line)
                continue
            # Data lines start with a timestamp: "YYYY-MM-DD HH:MM:SS | ..."
            ts_part = stripped.split("|", 1)[0].strip()
            try:
                ts = datetime.strptime(ts_part, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                kept.append(line)  # unparseable → keep safe
                continue
            if ts >= cutoff:
                kept.append(line)
            else:
                removed += 1

        if removed:
            _write_bytes_atomic(CAPACITY_LOG_FILE, "".join(kept).encode("utf-8"))
            logger.info(
                "capacity log pruned %d entries older than %d days",
                removed,
                CAPACITY_LOG_RETENTION_DAYS,
            )
    except Exception as e:
        logger.debug("capacity log cleanup failed: %s", e)
    finally:
        _last_capacity_cleanup = today


def _check_limits(get_limits_fn: Callable) -> Optional[str]:
    """Call get_limits() and return a formatted summary with per-window detail."""
    try:
        limits = get_limits_fn()
        parts: list[str] = []
        for name in ("claude", "gemini", "codex"):
            lim = getattr(limits, name)
            if lim.available:
                parts.append(f"{name}: {lim.remaining_pct:.0f}% remaining")
            else:
                parts.append(f"{name}: ❌ {lim.error or 'unavailable'}")
            for wname, wdata in sorted(lim.windows.items()):
                reset_min = wdata.resets_in_sec // 60
                parts.append(
                    f"  {wname}: {wdata.remaining_pct:.0f}% remaining, "
                    f"reset in {reset_min}m"
                )
            if name == "claude" and "seven_day" in lim.windows:
                try:
                    from usage_budget import compute_window_pace, format_pace_status
                    w = lim.windows["seven_day"]
                    pace = compute_window_pace(w.remaining_pct, w.resets_in_sec, 7)
                    parts.append(f"  {format_pace_status(pace)}")
                except (ImportError, OSError, KeyError, TypeError, ValueError):
                    pass
        _append_capacity_log(limits)
        return "\n".join(parts) if parts else None
    except Exception as e:
        logger.debug("check-limits failed: %s", e)
        return None


def _log_capacity() -> None:
    """Silently append a capacity snapshot to the persistent log. No Telegram output."""
    try:
        from limits import get_limits
        _append_capacity_log(get_limits())
    except Exception as e:
        logger.debug("log-capacity failed: %s", e)


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


def _check_usage_suggest(queue_read_fn: Callable) -> Optional[str]:
    """Check if Claude limits are about to reset with unused capacity and suggest tasks."""
    global _usage_suggest_thread
    try:
        from usage_suggester import get_suggester

        # Run asynchronously so heartbeat never blocks the main watch loop.
        if _usage_suggest_thread and _usage_suggest_thread.is_alive():
            return None

        def _worker() -> None:
            try:
                result = get_suggester().check_and_suggest(queue_read_fn)
                if result:
                    logger.info("usage-suggest result: %s", result)
            except Exception as e:
                logger.debug("usage-suggest worker failed: %s", e)

        _usage_suggest_thread = threading.Thread(
            target=_worker,
            name="usage-suggest",
            daemon=True,
        )
        _usage_suggest_thread.start()
        return None
    except Exception as e:
        logger.debug("usage-suggest failed: %s", e)
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
                encoding="utf-8",
                errors="replace",
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
    ("log-capacity",  "_log_capacity"),
    ("summarize",     "_check_task_summary"),
    ("stale branch",  "_check_stale_branches"),
    ("usage-suggest", "_check_usage_suggest"),
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
        self._lock = threading.Lock()  # prevents concurrent run_due() calls
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
        Safe for concurrent calls: returns immediately if already running.
        """
        if not self._lock.acquire(blocking=False):
            return  # bg thread or main loop already running — skip
        try:
            self._run_due_locked(queue_read_fn, dispatcher)
        finally:
            self._lock.release()

    def _run_due_locked(
        self,
        queue_read_fn: Callable,
        dispatcher: Optional[object] = None,
    ) -> None:
        """Inner implementation of run_due — must be called with _lock held."""
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
                    logger.info("Heartbeat [%s]: %s", item.label, result[:1000])
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
        elif key == "_log_capacity":
            _log_capacity()
            return None
        elif key == "_check_task_summary":
            return _check_task_summary(dispatcher)
        elif key == "_check_stale_branches":
            return _check_stale_branches()
        elif key == "_check_usage_suggest":
            return _check_usage_suggest(queue_read_fn)
        else:
            logger.debug("No handler for heartbeat item: %s", item.label)
            return None


def start_heartbeat_thread(
    heartbeat: HeartbeatRunner,
    queue_read_fn: Callable,
    stop_event: threading.Event,
    poll_sec: int = 60,
) -> threading.Thread:
    """Start a daemon thread that calls heartbeat.run_due() every *poll_sec* seconds.

    This ensures scheduled checks (log-capacity, usage-suggest, etc.) fire on time
    even when the main thread is blocked for hours inside a long-running task.
    The thread is safe to run alongside the existing run_due() calls in the main loop
    because HeartbeatRunner.run_due() is protected by a non-blocking lock.
    """
    def _loop() -> None:
        while not stop_event.wait(timeout=poll_sec):
            try:
                heartbeat.run_due(queue_read_fn)
            except Exception:
                logger.debug("heartbeat bg thread error", exc_info=True)

    t = threading.Thread(target=_loop, name="heartbeat-bg", daemon=True)
    t.start()
    logger.debug("Heartbeat background thread started (poll=%ds)", poll_sec)
    return t
