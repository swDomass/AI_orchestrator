"""Phase-0 calibration logging for Claude quota windows.

Telemetry-only module that piggy-backs on the existing
``limits._bg_refresh_loop`` cclimits cycle.  Every successful cclimits
fetch (typically every 5 min while the queue is active, 10 min idle)
produces one CSV row per Claude window (``five_hour``, ``seven_day``)
that pairs the real cclimits utilization-% with locally-aggregated
JSONL token counts.

The goal is to collect 2-3 days of paired samples so we can decide
whether a single linear ``tokens_per_pct`` factor is sufficient to
estimate quota usage between cclimits polls, or whether a per-token-type
weighting (input / output / cache_creation / cache_read) is required.

**Operational notes**

- The hook runs in a dedicated single-worker ``ThreadPoolExecutor`` so
  the JSONL aggregation (which can take 10-20 s on installations with
  thousands of session files) does NOT block the cclimits refresh
  thread. Submissions that arrive while a previous sample is still
  being written are silently dropped — there is no queue build-up.
- The CSV append uses a thread-local lock; it is NOT safe across
  multiple OS processes writing to the same file. Phase-0 assumes a
  single orchestrator instance per machine.
- Phase-0 calibration is only valid for **single-machine workflows**:
  the local JSONL files reflect Claude Code activity on this host,
  whereas cclimits sums across all machines tied to the OAuth account.
- No operational dependency — failures are swallowed and logged at
  DEBUG so they never affect the background refresh loop.
"""

from __future__ import annotations

import concurrent.futures
import csv
import datetime as dt
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from limits import AllLimits

logger = logging.getLogger(__name__)

# Module-level lock — appender is process-local thread-safe (covers the
# dedicated calibration worker thread). NOT safe across multiple Python
# processes writing to the same CSV simultaneously.
_csv_lock = threading.Lock()

# Below this utilization %-value the tokens_per_pct ratio is dominated by
# cclimits' string-rounding noise (cclimits reports "0.0%", "0.1%" steps)
# plus the JSONL/server timestamp drift, so the calibration ratio is
# meaningless. Skip the division and leave the columns empty.
_MIN_PCT_FOR_CALIBRATION = 0.5

# When window_start is provided we load enough JSONL history to cover the
# elapsed time since window_start plus a margin. The multiplicative factor
# absorbs clock drift between local machine and Anthropic server (typical
# NTP <100 ms but allow 10%); the absolute floor catches JSONL flush
# latency (Claude Code buffers a tool turn before persisting it).
_LOAD_HOURS_BUFFER_FACTOR = 1.1
_LOAD_HOURS_BUFFER_ABS = 6

# Current CSV schema. Bump when columns are added/removed/renamed so
# downstream analysis can detect mixed-schema files and reject them.
_SCHEMA_VERSION = 2

# Worker pool for the non-blocking calibration writes. max_workers=1
# serialises samples (cheaper than a true thread-safe writer) and lets
# us drop overlapping submissions instead of building an unbounded queue.
_executor: "concurrent.futures.ThreadPoolExecutor | None" = None
_executor_lock = threading.Lock()
_pending_sample = threading.Event()


CSV_FIELDS = [
    "schema_version",
    "timestamp_utc",
    "window",                       # "five_hour" | "seven_day"
    "claude_plan",                  # from config.CLAUDE_PLAN ("" if unset)
    "queue_idle_at_sample",         # "true" | "false" | ""  — limits._queue_idle.is_set()
    "cclimits_pct_used",
    "cclimits_pct_remaining",
    "reset_in_sec",
    "window_start_utc",             # derived: reset_at - window_size (synthetic when resets_in_sec=0)
    "tokens_input",
    "tokens_output",
    "tokens_cache_creation",        # total of 1h + 5m ephemeral
    "tokens_cache_creation_1h",     # ephemeral_1h_input_tokens (raw JSONL subfield, "" if unavailable)
    "tokens_cache_creation_5m",     # ephemeral_5m_input_tokens (raw JSONL subfield, "" if unavailable)
    "tokens_cache_read",
    "tokens_inputoutput_only",      # input + output
    "tokens_weighted_billing",      # input + cc*1.25 + cr*0.1 + output*5  (analytics weighting)
    "tokens_per_pct_io_only",       # (input+output) / pct_used
    "tokens_per_pct_with_cc",       # (input+output+cache_creation) / pct_used
    "tokens_per_pct_all",           # (input+output+cc+cr) / pct_used
    "entries_count",                # raw JSONL UsageEntry count (already deduped by claude-monitor)
    "models",
    # Machine-readable flags so downstream analysis can filter without parsing the note column.
    "flag_rolling_fallback",        # "true" | "false" — cclimits returned resets_in_sec=0
    "flag_low_pct",                 # "true" | "false" — pct_used < _MIN_PCT_FOR_CALIBRATION
    "flag_cm_unavailable",          # "true" | "false" — claude-monitor missing or load failed
    "note",                         # human-readable, mirrors the flags for at-a-glance reading
]


# ───────────────────────────── Entry loading ─────────────────────────────────


def _load_entries(load_hours: int) -> "list | None":
    """Load JSONL usage entries for the last ``load_hours`` hours.

    Returns ``None`` when claude-monitor is missing or its loader raises —
    callers must handle the None case as "claude-monitor unavailable".
    """
    try:
        from claude_monitor.core.models import CostMode
        from claude_monitor.data.reader import load_usage_entries
    except ImportError:
        return None
    except Exception as exc:
        # claude-monitor is installed but broken (e.g. dep conflict on import) —
        # mirror _get_claude_limits_from_local's logging style.
        logger.debug("calibration: claude-monitor import raised: %s", exc)
        return None

    try:
        entries, _ = load_usage_entries(hours_back=load_hours, mode=CostMode.AUTO)
    except Exception as exc:
        logger.debug("calibration: load_usage_entries failed: %s", exc)
        return None
    return entries


def _filter_entries_for_window(
    entries: list, window_start: "dt.datetime | None",
) -> list:
    """Drop entries older than window_start (treating naive timestamps as UTC)."""
    if window_start is None:
        return entries
    out = []
    for e in entries:
        ts = e.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        if ts >= window_start:
            out.append(e)
    return out


def _aggregate_entries(entries: list) -> dict:
    """Sum the per-entry token counts. claude-monitor already dedupes its
    input on ``(message_id, request_id)``, so we do not re-dedupe — that
    would mistakenly collapse legitimate server-side retries with the same
    ``message_id`` but different ``request_id``.
    """
    in_t = sum(int(getattr(e, "input_tokens", 0) or 0) for e in entries)
    out_t = sum(int(getattr(e, "output_tokens", 0) or 0) for e in entries)
    cc_t = sum(int(getattr(e, "cache_creation_tokens", 0) or 0) for e in entries)
    cr_t = sum(int(getattr(e, "cache_read_tokens", 0) or 0) for e in entries)
    models = sorted({e.model for e in entries if getattr(e, "model", "")})
    return {
        "input": in_t,
        "output": out_t,
        "cache_creation": cc_t,
        "cache_read": cr_t,
        "entries_count": len(entries),
        "models": ",".join(models),
    }


# Public for tests + backwards compatibility — combines load + filter + aggregate.
def _aggregate_tokens(
    window_hours: int,
    window_start: "dt.datetime | None" = None,
) -> "dict | None":
    """Load JSONL, filter for the Anthropic window, and aggregate.

    See module docstring for the window-anchoring rationale (Anthropic
    blocks start at first activity, not "last N hours").
    """
    if window_start is not None:
        now = dt.datetime.now(dt.timezone.utc)
        elapsed_h = max(0.0, (now - window_start).total_seconds() / 3600.0)
        load_hours = int(elapsed_h * _LOAD_HOURS_BUFFER_FACTOR) + _LOAD_HOURS_BUFFER_ABS
    else:
        load_hours = window_hours

    entries = _load_entries(load_hours)
    if entries is None:
        return None
    if not entries:
        return _aggregate_entries([])
    entries = _filter_entries_for_window(entries, window_start)
    return _aggregate_entries(entries)


# ───────────────────────────── Row building ──────────────────────────────────


def _str_bool(value: "bool | None") -> str:
    if value is None:
        return ""
    return "true" if value else "false"


def _build_row(
    window_name: str,
    window_hours: int,
    pct_used: float,
    resets_in_sec: int,
    now: dt.datetime,
    *,
    preloaded_entries: "list | None" = None,
    claude_plan: str = "",
    queue_idle: "bool | None" = None,
) -> dict:
    """Build one CSV row. If preloaded_entries is given (already loaded for
    the largest window in this poll), filter+aggregate in-memory; otherwise
    call ``_aggregate_tokens`` which does its own load.
    """
    # Window-start derivation. resets_in_sec=0 (just after a reset) → use
    # a synthetic ``now - window_hours`` start so we still produce a row,
    # but flag it so downstream analysis can drop these samples.
    rolling_fallback = False
    if resets_in_sec > 0:
        reset_at = now + dt.timedelta(seconds=resets_in_sec)
        window_start = reset_at - dt.timedelta(hours=window_hours)
    else:
        rolling_fallback = True
        window_start = now - dt.timedelta(hours=window_hours)

    if preloaded_entries is not None:
        filtered = _filter_entries_for_window(preloaded_entries, window_start)
        tokens = _aggregate_entries(filtered)
        cm_unavailable = False
    else:
        tokens = _aggregate_tokens(window_hours, window_start=window_start)
        cm_unavailable = tokens is None
        if tokens is None:
            tokens = {
                "input": 0, "output": 0, "cache_creation": 0, "cache_read": 0,
                "entries_count": 0, "models": "",
            }

    low_pct = pct_used < _MIN_PCT_FOR_CALIBRATION

    in_t = tokens["input"]
    out_t = tokens["output"]
    cc_t = tokens["cache_creation"]
    cr_t = tokens["cache_read"]
    io_only = in_t + out_t
    weighted = in_t + cc_t * 1.25 + cr_t * 0.1 + out_t * 5.0

    row = {key: "" for key in CSV_FIELDS}
    row.update({
        "schema_version": _SCHEMA_VERSION,
        "timestamp_utc": now.isoformat(timespec="seconds"),
        "window": window_name,
        "claude_plan": claude_plan,
        "queue_idle_at_sample": _str_bool(queue_idle),
        "cclimits_pct_used": f"{pct_used:.4f}",
        "cclimits_pct_remaining": f"{100.0 - pct_used:.4f}",
        "reset_in_sec": resets_in_sec,
        "window_start_utc": window_start.isoformat(timespec="seconds"),
        "tokens_input": in_t,
        "tokens_output": out_t,
        "tokens_cache_creation": cc_t,
        # 1h/5m ephemeral sub-fields are not yet exposed via claude-monitor's
        # UsageEntry; left empty for now, will be populated in a future
        # iteration via direct JSONL parsing if calibration shows the
        # 1h/5m mix materially affects the ratio.
        "tokens_cache_creation_1h": "",
        "tokens_cache_creation_5m": "",
        "tokens_cache_read": cr_t,
        "tokens_inputoutput_only": io_only,
        "tokens_weighted_billing": f"{weighted:.2f}",
        "entries_count": tokens["entries_count"],
        "models": tokens["models"],
        "flag_rolling_fallback": _str_bool(rolling_fallback),
        "flag_low_pct": _str_bool(low_pct),
        "flag_cm_unavailable": _str_bool(cm_unavailable),
    })

    if not low_pct and not cm_unavailable:
        row["tokens_per_pct_io_only"] = f"{io_only / pct_used:.2f}"
        row["tokens_per_pct_with_cc"] = f"{(io_only + cc_t) / pct_used:.2f}"
        row["tokens_per_pct_all"] = f"{(io_only + cc_t + cr_t) / pct_used:.2f}"

    # Human-readable note mirrors the flags for at-a-glance grepping.
    parts = []
    if rolling_fallback:
        parts.append("rolling-fallback (resets_in_sec=0)")
    if cm_unavailable:
        parts.append("claude-monitor unavailable")
    if low_pct:
        parts.append(f"pct_used={pct_used:.3f} too low for calibration")
    row["note"] = "; ".join(parts)

    return row


# ───────────────────────────── CSV writing ───────────────────────────────────


def _write_csv_row(csv_path, row: dict) -> None:
    """Append a single row to the CSV, creating header on first write.

    Accepts ``csv_path`` as either ``Path`` or ``str``.
    """
    csv_path = Path(csv_path)
    with _csv_lock:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not csv_path.exists() or csv_path.stat().st_size == 0
        with csv_path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)


# ───────────────────────────── Public API ────────────────────────────────────


def _resolve_claude_plan() -> str:
    """Best-effort read of CLAUDE_PLAN from config (empty string if missing)."""
    try:
        from config import CLAUDE_PLAN
        return CLAUDE_PLAN or ""
    except Exception:
        return ""


def log_calibration_sample(
    limits_result: "AllLimits",
    csv_path,
    *,
    queue_idle: "bool | None" = None,
    claude_plan: "str | None" = None,
) -> None:
    """Log one calibration row each for the 5h and 7d Claude windows
    (synchronous variant).

    Must never raise. Skips logging when Claude has any provider error
    (cclimits unavailable, 429 fallback, token expired, etc.) or no
    window data is present.

    Loads the JSONL data ONCE (sized for the 7d window) and filters it
    per-window in memory — avoids two consecutive multi-second loader
    calls in environments with thousands of session files.
    """
    try:
        claude = limits_result.claude

        if claude.error:
            return
        if not claude.windows:
            return

        now = dt.datetime.now(dt.timezone.utc)
        plan = claude_plan if claude_plan is not None else _resolve_claude_plan()

        # Single load sized for the largest window we might inspect.
        largest_hours = 168
        sevd = claude.windows.get("seven_day")
        if sevd is not None and sevd.resets_in_sec > 0:
            elapsed_h = largest_hours - (sevd.resets_in_sec / 3600.0)
            load_hours = int(max(elapsed_h, 0) * _LOAD_HOURS_BUFFER_FACTOR) + _LOAD_HOURS_BUFFER_ABS
        else:
            load_hours = largest_hours + _LOAD_HOURS_BUFFER_ABS
        preloaded = _load_entries(load_hours)
        # preloaded is None ⇒ pass it through so _build_row goes via _aggregate_tokens
        # (which will set flag_cm_unavailable). Pre-loaded as [] is a valid "empty" set.

        for window_name, hours in (("five_hour", 5), ("seven_day", 168)):
            window = claude.windows.get(window_name)
            if window is None:
                continue
            pct_used = 100.0 - window.remaining_pct
            row = _build_row(
                window_name, hours, pct_used, window.resets_in_sec, now,
                preloaded_entries=preloaded,
                claude_plan=plan,
                queue_idle=queue_idle,
            )
            _write_csv_row(csv_path, row)
    except Exception as exc:
        logger.debug("calibration: sample logging failed: %s", exc)


def _get_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Lazy-init the single-worker pool used by ``log_calibration_sample_async``."""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="quota-calibration",
                )
    return _executor


def log_calibration_sample_async(
    limits_result: "AllLimits",
    csv_path,
    *,
    queue_idle: "bool | None" = None,
    claude_plan: "str | None" = None,
) -> None:
    """Non-blocking variant — schedules the sample write on a single worker
    thread so the cclimits BG-refresh loop is never blocked by the multi-
    second JSONL aggregation.

    If a previous sample is still being written when this call arrives, the
    new sample is silently dropped (logged at DEBUG). This avoids unbounded
    queue growth on machines where load_usage_entries is slow.
    """
    if _pending_sample.is_set():
        logger.debug("calibration: previous sample still pending — skipping submit")
        return
    _pending_sample.set()

    def _job() -> None:
        try:
            log_calibration_sample(
                limits_result, csv_path,
                queue_idle=queue_idle, claude_plan=claude_plan,
            )
        finally:
            _pending_sample.clear()

    try:
        _get_executor().submit(_job)
    except Exception as exc:
        _pending_sample.clear()
        logger.debug("calibration: executor submit failed: %s", exc)


def shutdown_executor(wait: bool = False) -> None:
    """Stop the worker pool. Tests and graceful shutdown call this; the BG
    refresh thread runs as a daemon so this is not required for clean exit.
    """
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=wait)
            _executor = None
    _pending_sample.clear()


# ───────────────────────────── Phase-2 auto-recalibration ────────────────────


def _percentile(values: "list[float]", p: float) -> float:
    """Linear-interpolation percentile (p in 0..100)."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def recalibrate_claude_factors(
    csv_path,
    defaults: "dict[str, int]",
    *,
    min_samples: int,
    clamp: float,
    percentile: float = 25.0,
) -> "dict[str, int] | None":
    """Recompute conservative per-window ``io_only`` tokens-per-pct from the
    running calibration CSV (Phase-2 drift correction).

    Returns ``{window: int}`` or ``None`` (caller keeps the defaults). Guards:

    - **Schema-aware:** trusts only rows whose column count matches the current
      schema, sidestepping a stale/mixed CSV header (no DictReader).
    - **Filtered:** drops rolling-fallback / low-pct / cm-unavailable rows.
    - **Min samples:** each window needs ``>= min_samples`` usable rows.
    - **Clamped:** each factor is clamped to ``[default/clamp, default*clamp]``.
    - **All-or-nothing:** if either Claude window lacks data, returns None.

    The ``percentile`` (default 25) is a conservative low percentile — a smaller
    tokens-per-pct overestimates consumption, the safe side for gating.
    """
    try:
        path = Path(csv_path)
        if not path.exists() or path.stat().st_size == 0:
            return None
        expected = len(CSV_FIELDS)
        usable: list = []
        with path.open(encoding="utf-8", newline="") as fh:
            for raw in csv.reader(fh):
                if len(raw) != expected:
                    continue
                row = dict(zip(CSV_FIELDS, raw))
                if (
                    row.get("flag_rolling_fallback") == "false"
                    and row.get("flag_low_pct") == "false"
                    and row.get("flag_cm_unavailable") == "false"
                    and (row.get("tokens_per_pct_io_only") or "").strip()
                ):
                    usable.append(row)

        result: dict[str, int] = {}
        for window in ("five_hour", "seven_day"):
            default = defaults.get(window)
            if not default:
                return None
            vals = []
            for row in usable:
                if row.get("window") != window:
                    continue
                try:
                    vals.append(float(row["tokens_per_pct_io_only"]))
                except (ValueError, KeyError, TypeError):
                    continue
            if len(vals) < min_samples:
                return None
            factor = _percentile(vals, percentile)
            lo, hi = default / clamp, default * clamp
            result[window] = int(round(max(lo, min(hi, factor))))
        return result
    except Exception as exc:
        logger.debug("recalibrate_claude_factors failed: %s", exc)
        return None
