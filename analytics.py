"""
AI Orchestrator — Analytics Module

Parses task results, log files, and queue events to produce aggregated
dashboard data.  Single public entry-point: ``get_dashboard_data()``.

Data sources
~~~~~~~~~~~~
1. Memory task-result MD files (vault ``memory/task_results/`` + ``archive/``)
2. Rotating log files (``logs/orchestrator.log*``) — heartbeat check-limits lines
3. Queue event log (``<!-- YYYY-MM-DD HH:MM | msg -->``) in ``agent-queue.md``
4. In-process session stats (``notifier._stats``)
"""

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config import CAPACITY_LOG_FILE, LOG_FILE, QUEUE_FILE, VAULT_PATH

logger = logging.getLogger(__name__)

# ── Data models ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TaskRecord:
    task: str
    provider: str
    cwd: str
    duration_sec: float
    timestamp: datetime
    success: bool
    source: str  # "task_results" | "archive"


@dataclass(frozen=True)
class LimitSnapshot:
    timestamp: datetime
    provider: str
    remaining_pct: float  # -1 if unavailable
    available: bool


@dataclass(frozen=True)
class QueueEvent:
    timestamp: datetime
    message: str


@dataclass(frozen=True)
class UsageSuggestEvent:
    timestamp: datetime
    event_type: str   # "picked" | "declined" | "timeout" | "suppressed" | "no_suggestions" | "result" | "info"
    detail: str       # raw message tail


# ── Parsing ──────────────────────────────────────────────────────────────────

def _parse_task_file(path: Path, source: str = "task_results") -> Optional[TaskRecord]:
    """Parse a single memory MD file with YAML-ish frontmatter."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    if not content.startswith("---"):
        return None

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        frontmatter_raw = parts[1].strip()
    else:
        offset = end_match.start()
        frontmatter_raw = content[3:3 + offset].strip()

    meta: dict[str, str] = {}
    for line in frontmatter_raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"').strip("'")

    try:
        ts = datetime.fromisoformat(meta.get("timestamp", ""))
    except ValueError:
        try:
            ts = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            return None

    return TaskRecord(
        task=meta.get("task", ""),
        provider=meta.get("provider", ""),
        cwd=meta.get("cwd", ""),
        duration_sec=float(meta.get("duration_sec", 0) or 0),
        timestamp=ts,
        success=meta.get("success", "true").lower() not in ("false", "0"),
        source=source,
    )


def _parse_memory_files(
    task_results_dir: Path,
    archive_dir: Path,
) -> list[TaskRecord]:
    """Parse all memory MD files from task_results and archive dirs."""
    records: list[TaskRecord] = []
    for src_dir, src_label in ((task_results_dir, "task_results"), (archive_dir, "archive")):
        if not src_dir.is_dir():
            continue
        for p in src_dir.glob("*.md"):
            rec = _parse_task_file(p, source=src_label)
            if rec is not None:
                records.append(rec)
    records.sort(key=lambda r: r.timestamp)
    return records


# Log line pattern:
#   2026-02-27 09:17:25,762 [heartbeat] INFO Heartbeat [Run check-limits ...]: claude: 71% remaining
_LIMIT_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[(?:usage_suggester|heartbeat)\] INFO "
    r"Heartbeat \[(?:Run )?check-limits.*?\]: (.+)",
    re.MULTILINE,
)
# Individual provider entry inside a check-limits block
_PROVIDER_LINE_RE = re.compile(
    r"(\w+):\s+(?:(\d+(?:\.\d+)?)%\s+remaining|❌\s*(.*))"
)

_SUGGEST_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[(?:usage_suggester|heartbeat)\] (?:INFO|WARNING) "
    r"(usage-suggest: .+)$",
    re.MULTILINE,
)


def _parse_log_limits(text: str) -> list[LimitSnapshot]:
    """Parse heartbeat check-limits entries from log text."""
    snapshots: list[LimitSnapshot] = []

    # Split into blocks: each check-limits log entry can span multiple lines
    # We find the timestamp line, then collect continuation lines
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _LIMIT_RE.match(line)
        if m:
            ts_str, first_data = m.group(1), m.group(2)
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                i += 1
                continue

            # Collect all provider data: may be on the same line or continuation lines
            block = first_data
            j = i + 1
            max_continuation = 50
            while j < len(lines) and (j - i - 1) < max_continuation:
                # Continuation lines don't start with a timestamp
                if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", lines[j]):
                    break
                block += "\n" + lines[j]
                j += 1

            for pm in _PROVIDER_LINE_RE.finditer(block):
                provider = pm.group(1).lower()
                pct_str = pm.group(2)
                if pct_str is not None:
                    pct = float(pct_str)
                    snapshots.append(LimitSnapshot(ts, provider, pct, True))
                else:
                    snapshots.append(LimitSnapshot(ts, provider, -1.0, False))
            i = j
        else:
            i += 1
    return snapshots


def _parse_log_suggest_events(text: str) -> list[UsageSuggestEvent]:
    """Parse usage-suggest log entries from log text."""
    _type_map = (
        ("usage-suggest: picked #", "picked"),
        ("usage-suggest: user declined", "declined"),
        ("usage-suggest: timeout", "timeout"),
        ("usage-suggest: 7-day pace", "suppressed"),
        ("usage-suggest: no suggestions found", "no_suggestions"),
        ("usage-suggest: result:", "result"),
    )

    events: list[UsageSuggestEvent] = []
    for m in _SUGGEST_RE.finditer(text):
        ts_str, detail = m.group(1), m.group(2)
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        event_type = "info"
        for prefix, etype in _type_map:
            if detail.startswith(prefix):
                event_type = etype
                break
        events.append(UsageSuggestEvent(ts, event_type, detail))
    return events


def _parse_all_logs(log_dir: Path) -> tuple[list[LimitSnapshot], list[str], list[UsageSuggestEvent]]:
    """Parse all rotated log files. Returns (snapshots, error_lines, suggest_events)."""
    all_snapshots: list[LimitSnapshot] = []
    error_lines: list[str] = []
    all_suggest: list[UsageSuggestEvent] = []

    if not log_dir.is_dir():
        return all_snapshots, error_lines, all_suggest

    log_files = sorted(log_dir.glob("orchestrator.log*"))
    for lf in log_files:
        try:
            text = lf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        all_snapshots.extend(_parse_log_limits(text))
        all_suggest.extend(_parse_log_suggest_events(text))

        # Collect ERROR lines
        for line in text.splitlines():
            if " ERROR " in line:
                error_lines.append(line.strip())

    all_snapshots.sort(key=lambda s: s.timestamp)
    all_suggest.sort(key=lambda e: e.timestamp)
    return all_snapshots, error_lines, all_suggest


_CAP_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*(\w+)\s*\|\s*([-\d.]+)\s*\|\s*(true|false)\s*$"
)


def _parse_capacity_log(path: Path) -> list[LimitSnapshot]:
    """Parse the persistent capacity log (one provider entry per line)."""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    snapshots: list[LimitSnapshot] = []
    for line in text.splitlines():
        m = _CAP_LINE_RE.match(line.strip())
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        provider = m.group(2).lower()
        pct = float(m.group(3))
        available = m.group(4) == "true"
        snapshots.append(LimitSnapshot(ts, provider, pct, available))
    snapshots.sort(key=lambda s: s.timestamp)
    return snapshots


_QUEUE_EVENT_RE = re.compile(
    r"<!--\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*\|\s*(.*?)\s*-->"
)


def _parse_queue_log(queue_file: Path) -> list[QueueEvent]:
    """Parse HTML comment log entries from the queue file."""
    if not queue_file.is_file():
        return []
    try:
        text = queue_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    events: list[QueueEvent] = []
    for m in _QUEUE_EVENT_RE.finditer(text):
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        events.append(QueueEvent(ts, m.group(2).strip()))
    events.sort(key=lambda e: e.timestamp, reverse=True)
    return events


def _parse_session_stats() -> dict:
    """Read in-process session stats from notifier (graceful fallback)."""
    try:
        from notifier import _stats, _stats_lock
        with _stats_lock:
            return {
                "tasks_done": _stats.get("tasks_done", 0),
                "tasks_failed": _stats.get("tasks_failed", 0),
                "providers_used": dict(_stats.get("providers_used", {})),
                "started_at": (
                    _stats["started_at"].isoformat()
                    if _stats.get("started_at")
                    else None
                ),
            }
    except (ImportError, OSError, AttributeError):
        return {}


# ── Aggregation ──────────────────────────────────────────────────────────────

def _tasks_per_day(records: list[TaskRecord], days: int = 90) -> tuple[list[str], list[int]]:
    """Aggregate task counts per day for the last N days (zero-filled)."""
    today = datetime.now().date()
    counts: dict[str, int] = {}
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        counts[d.isoformat()] = 0

    for r in records:
        key = r.timestamp.date().isoformat()
        if key in counts:
            counts[key] = counts[key] + 1

    labels = list(counts.keys())
    values = list(counts.values())
    return labels, values


def _success_rate(records: list[TaskRecord]) -> float:
    """Overall success rate as percentage (0-100)."""
    if not records:
        return 0.0
    ok = sum(1 for r in records if r.success)
    return round(ok / len(records) * 100, 1)


def _provider_distribution(records: list[TaskRecord]) -> tuple[list[str], list[int]]:
    """Counts per normalized provider (e.g. 'claude+review-loop' → 'claude')."""
    dist: dict[str, int] = defaultdict(int)
    for r in records:
        name = r.provider.split("+")[0].strip() if r.provider else "unknown"
        dist[name] += 1
    # Sort descending
    items = sorted(dist.items(), key=lambda x: x[1], reverse=True)
    return [i[0] for i in items], [i[1] for i in items]


def _avg_duration(records: list[TaskRecord]) -> float:
    """Average duration in seconds (success only)."""
    durations = [r.duration_sec for r in records if r.success and r.duration_sec > 0]
    if not durations:
        return 0.0
    return round(sum(durations) / len(durations), 1)


def _limits_timeline(
    snapshots: list[LimitSnapshot],
    hours: int = 7 * 24,
) -> dict[str, list[dict]]:
    """Per-provider timeline of capacity snapshots within the last N hours."""
    cutoff = datetime.now() - timedelta(hours=hours)
    result: dict[str, list[dict]] = defaultdict(list)
    for s in snapshots:
        if s.timestamp >= cutoff:
            result[s.provider].append({
                "ts": s.timestamp.isoformat(),
                "pct": s.remaining_pct if s.available else 0,
            })
    return dict(result)


def _recent_events(
    error_lines: list[str],
    queue_events: list[QueueEvent],
    suggest_events: list[UsageSuggestEvent] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Merge error lines, queue events, and suggest events into a unified recent-events list."""
    items: list[dict] = []

    # Parse timestamp from error lines: "2026-02-28 07:54:23,645 ..."
    _err_ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    for line in error_lines:
        m = _err_ts_re.match(line)
        if m:
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                ts = datetime.min
        else:
            ts = datetime.min
        items.append({"ts": ts.isoformat(), "type": "error", "msg": line[:200]})

    for ev in queue_events:
        items.append({"ts": ev.timestamp.isoformat(), "type": "queue", "msg": ev.message[:200]})

    for ev in (suggest_events or []):
        items.append({"ts": ev.timestamp.isoformat(), "type": "suggest", "msg": ev.detail[:200]})

    # Sort by timestamp descending
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


# ── Public API ───────────────────────────────────────────────────────────────

_cache: dict[str, object] = {"data": None, "ts": 0.0}
_CACHE_TTL = 30  # seconds


def _get_current_limits() -> dict:
    """Read current limits from the persistent capacity log.

    Uses the most recent entry per provider — written by the --watch process
    every 20 min (heartbeat) and at startup.  Reading from the log avoids
    starting a duplicate limits bg-daemon thread in standalone dashboard
    processes (which would double the JSONL I/O from claude-monitor).
    Returns {} if the log is empty or unreadable.
    """
    try:
        snaps = _parse_capacity_log(CAPACITY_LOG_FILE)
        if not snaps:
            return {}
        # Find the most recent timestamp per base provider
        # (provider strings like "claude_five_hour" → base "claude").
        latest_ts: dict[str, datetime] = {}
        for s in snaps:
            base = s.provider.split("_")[0]
            if base not in latest_ts or s.timestamp > latest_ts[base]:
                latest_ts[base] = s.timestamp
        # Aggregate windows: available = all ok; remaining = minimum across windows.
        result: dict[str, dict] = {}
        for s in snaps:
            base = s.provider.split("_")[0]
            if s.timestamp < latest_ts.get(base, s.timestamp):
                continue
            if base not in result:
                result[base] = {"available": True, "remaining_pct": 100.0, "error": ""}
            result[base]["available"] = result[base]["available"] and s.available
            result[base]["remaining_pct"] = min(result[base]["remaining_pct"], s.remaining_pct)
        return result
    except Exception:
        return {}


def get_dashboard_data(days: int = 7) -> dict:
    """Single entry-point: aggregate all data sources into a dict.

    Results are cached for 30 seconds (single-slot: new days value invalidates).
    """
    now = time.time()
    if (
        _cache.get("days") == days
        and _cache.get("data") is not None
        and now - _cache.get("ts", 0) < _CACHE_TTL
    ):
        return _cache["data"]  # type: ignore[return-value]

    # Paths
    memory_root = VAULT_PATH / "99_System" / "AI" / "memory"
    task_results_dir = memory_root / "task_results"
    archive_dir = memory_root / "archive"
    log_dir = LOG_FILE.parent

    # Parse
    records = _parse_memory_files(task_results_dir, archive_dir)
    snapshots, error_lines, suggest_events = _parse_all_logs(log_dir)
    # Merge persistent capacity log (deduplicates by keeping both; later sort handles order)
    persistent_snaps = _parse_capacity_log(CAPACITY_LOG_FILE)
    if persistent_snaps:
        seen = {(s.timestamp, s.provider) for s in snapshots}
        for s in persistent_snaps:
            if (s.timestamp, s.provider) not in seen:
                snapshots.append(s)
        snapshots.sort(key=lambda s: s.timestamp)
    queue_events = _parse_queue_log(QUEUE_FILE)
    session = _parse_session_stats()

    # Aggregate
    tpd_labels, tpd_values = _tasks_per_day(records, days=days)
    pd_labels, pd_values = _provider_distribution(records)

    cutoff_24h = datetime.now() - timedelta(hours=24)
    suggest_today = sum(1 for e in suggest_events if e.timestamp >= cutoff_24h)

    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_tasks": len(records),
        "success_rate": _success_rate(records),
        "avg_duration_sec": _avg_duration(records),
        "active_providers": pd_labels[:5],
        "tasks_per_day": {"labels": tpd_labels, "values": tpd_values},
        "provider_distribution": {"labels": pd_labels, "values": pd_values},
        "limits_timeline": _limits_timeline(snapshots, hours=90 * 24),
        "current_limits": _get_current_limits(),
        "recent_events": _recent_events(error_lines, queue_events, suggest_events),
        "usage_suggest_today": suggest_today,
        "session": session,
    }

    _cache["data"] = data
    _cache["ts"] = now
    _cache["days"] = days
    return data
