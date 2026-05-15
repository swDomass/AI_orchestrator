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

from config import ALLOWED_CWD_ROOTS, CAPACITY_LOG_FILE, LOG_FILE, QUEUE_EVENTS_LOG_FILE, QUEUE_FILE, VAULT_PATH

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
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


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


@dataclass(frozen=True)
class ToolTraceEvent:
    """One event from a tool's JSONL action trace.

    See tools/base_tool.py::ToolTracer for the writer side. Schema is open via
    ``details`` so we don't need to bump the dataclass when tools add fields.
    """
    timestamp: datetime
    elapsed_sec: float
    run_id: str
    tool: str
    action: str
    details: tuple  # frozen-friendly: tuple of (k, v) pairs from the JSON dict


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

    def _int(field: str) -> int:
        try:
            return int(meta.get(field, 0) or 0)
        except (ValueError, TypeError):
            return 0

    return TaskRecord(
        task=meta.get("task", ""),
        provider=meta.get("provider", ""),
        cwd=meta.get("cwd", ""),
        duration_sec=float(meta.get("duration_sec", 0) or 0),
        timestamp=ts,
        success=meta.get("success", "true").lower() not in ("false", "0"),
        source=source,
        input_tokens=_int("input_tokens"),
        output_tokens=_int("output_tokens"),
        cache_creation_input_tokens=_int("cache_creation_input_tokens"),
        cache_read_input_tokens=_int("cache_read_input_tokens"),
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
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*\|\s*(.*?)\s*$",
    re.MULTILINE,
)


def _parse_queue_log(events_log: Path) -> list[QueueEvent]:
    """Parse plain-text queue event log (logs/queue-events.log)."""
    if not events_log.is_file():
        return []
    try:
        text = events_log.read_text(encoding="utf-8", errors="replace")
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


# Anthropic billing weights — billing only, NOT used for 5h/7d quota estimation.
# Source: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
# Output tokens cost ~5× input tokens (3× to 5× depending on model — averaged).
_BILLING_WEIGHT_INPUT          = 1.0
_BILLING_WEIGHT_CACHE_CREATION = 1.25
_BILLING_WEIGHT_CACHE_READ     = 0.1
_BILLING_WEIGHT_OUTPUT         = 5.0


def _billing_cost_units(records: list[TaskRecord]) -> dict[str, float]:
    """Aggregate weighted billing-token-units across records (NOT $ — relative cost).

    Used for cost-trend visualization on the dashboard. Quota tracking
    (5h/7d) does NOT use these weights — see limits.py.
    """
    inp = sum(r.input_tokens for r in records)
    out = sum(r.output_tokens for r in records)
    cc = sum(r.cache_creation_input_tokens for r in records)
    cr = sum(r.cache_read_input_tokens for r in records)
    weighted = (
        inp * _BILLING_WEIGHT_INPUT
        + cc * _BILLING_WEIGHT_CACHE_CREATION
        + cr * _BILLING_WEIGHT_CACHE_READ
        + out * _BILLING_WEIGHT_OUTPUT
    )
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
        "weighted_units": round(weighted, 1),
    }


def _cache_hit_rate(records: list[TaskRecord]) -> float:
    """Cache hit rate as percentage of total INPUT path (cache_read /
    (input + cache_creation + cache_read)). Range 0-100. Higher is better.
    """
    cc = sum(r.cache_creation_input_tokens for r in records)
    cr = sum(r.cache_read_input_tokens for r in records)
    inp = sum(r.input_tokens for r in records)
    total = inp + cc + cr
    if total == 0:
        return 0.0
    return round(cr / total * 100, 1)


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


def _parse_tool_traces(
    allowed_roots: list[Path],
    *,
    max_age_days: int = 30,
    max_events_per_file: int = 1000,
) -> list[ToolTraceEvent]:
    """Glob ``<root>/**/.<tool>/traces/*.jsonl`` over the allowed CWD roots
    and parse JSONL events. Falls back to no-op when roots are empty (test
    isolation: never traverses an unscoped filesystem).

    Skips files older than ``max_age_days`` to bound memory. Caps the number
    of events read per trace file.
    """
    import json as _json

    if not allowed_roots:
        return []

    events: list[ToolTraceEvent] = []
    cutoff = datetime.now() - timedelta(days=max_age_days)
    cutoff_ts = cutoff.timestamp()

    for root in allowed_roots:
        if not root.exists():
            continue
        # Each tool writes to .<tool_name>/traces/*.jsonl in its CWD.
        # Glob the pattern broadly — tool subdirs start with "." so they're
        # not crawled by default; we explicitly traverse them here.
        try:
            for trace_file in root.glob("**/.*/traces/*.jsonl"):
                try:
                    if trace_file.stat().st_mtime < cutoff_ts:
                        continue
                    lines = trace_file.read_text(encoding="utf-8", errors="replace").splitlines()
                    if len(lines) > max_events_per_file:
                        lines = lines[-max_events_per_file:]
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = _json.loads(line)
                        except _json.JSONDecodeError:
                            continue
                        ts_raw = entry.get("ts")
                        if not ts_raw:
                            continue
                        try:
                            ts = datetime.fromisoformat(ts_raw)
                        except (TypeError, ValueError):
                            continue
                        events.append(ToolTraceEvent(
                            timestamp=ts,
                            elapsed_sec=float(entry.get("elapsed_sec", 0)),
                            run_id=str(entry.get("run_id", "")),
                            tool=str(entry.get("tool", "")),
                            action=str(entry.get("action", "")),
                            details=tuple(sorted((entry.get("details") or {}).items())),
                        ))
                except OSError:
                    continue
        except OSError as exc:
            logger.warning("Tool trace glob failed for %s: %s", root, exc)

    events.sort(key=lambda e: e.timestamp)
    return events


def _tool_trace_stats(events: list[ToolTraceEvent]) -> dict:
    """Aggregate trace events per tool.

    Returns a dict keyed by tool name:
        {tool: {runs, completed_runs, success_runs, avg_duration_sec, total_events}}

    A run is "completed" when its run_id has a run_end event; "success" when
    that run_end has details.success == True.
    """
    by_run: dict[str, dict] = {}  # run_id → metadata accumulator
    by_tool_total: dict[str, int] = {}  # tool → total event count

    for ev in events:
        by_tool_total[ev.tool] = by_tool_total.get(ev.tool, 0) + 1
        r = by_run.setdefault(ev.run_id, {
            "tool": ev.tool,
            "start_ts": None, "end_ts": None,
            "success": None, "events": 0,
        })
        r["events"] += 1
        if ev.action == "run_start" and r["start_ts"] is None:
            r["start_ts"] = ev.timestamp
        elif ev.action == "run_end":
            r["end_ts"] = ev.timestamp
            details_dict = dict(ev.details)
            r["success"] = bool(details_dict.get("success", False))

    per_tool: dict[str, dict] = {}
    for run_id, r in by_run.items():
        tool = r["tool"]
        bucket = per_tool.setdefault(tool, {
            "runs": 0, "completed_runs": 0, "success_runs": 0,
            "durations": [], "total_events": 0,
        })
        bucket["runs"] += 1
        bucket["total_events"] += r["events"]
        if r["end_ts"] is not None and r["start_ts"] is not None:
            bucket["completed_runs"] += 1
            bucket["durations"].append((r["end_ts"] - r["start_ts"]).total_seconds())
            if r["success"]:
                bucket["success_runs"] += 1

    out: dict[str, dict] = {}
    for tool, b in per_tool.items():
        avg = sum(b["durations"]) / len(b["durations"]) if b["durations"] else 0.0
        out[tool] = {
            "runs": b["runs"],
            "completed_runs": b["completed_runs"],
            "success_runs": b["success_runs"],
            "avg_duration_sec": round(avg, 1),
            "total_events": b["total_events"],
        }
    return out


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
    queue_events = _parse_queue_log(QUEUE_EVENTS_LOG_FILE)
    session = _parse_session_stats()
    tool_trace_events = _parse_tool_traces(ALLOWED_CWD_ROOTS, max_age_days=max(days * 2, 30))
    tool_trace_stats = _tool_trace_stats(tool_trace_events)

    # Aggregate
    tpd_labels, tpd_values = _tasks_per_day(records, days=days)
    pd_labels, pd_values = _provider_distribution(records)

    cutoff_24h = datetime.now() - timedelta(hours=24)
    suggest_today = sum(1 for e in suggest_events if e.timestamp >= cutoff_24h)

    # Window cutoff for "recent" billing/cache stats (last `days` days).
    cutoff_window = datetime.now() - timedelta(days=days)
    recent_records = [r for r in records if r.timestamp >= cutoff_window]

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
        # Billing analytics — weighted token cost + cache-hit-rate (last `days` days).
        # NOT used for quota gates (see limits.py for the quota path).
        "billing_recent": _billing_cost_units(recent_records),
        "billing_total":  _billing_cost_units(records),
        "cache_hit_rate_recent": _cache_hit_rate(recent_records),
        "cache_hit_rate_total":  _cache_hit_rate(records),
        # Tool action traces — per-tool run counts, success rate, avg duration.
        # Populated by tools/base_tool.py::ToolTracer (one JSONL file per run).
        "tool_trace_stats": tool_trace_stats,
    }

    _cache["data"] = data
    _cache["ts"] = now
    _cache["days"] = days
    return data
