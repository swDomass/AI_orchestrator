"""
Wrapper around `cclimits --json`.
Parses usage limits for Claude, Gemini (all 3 tiers), and Codex.
Auto-refreshes expired OAuth tokens before querying.
"""

import json
import logging
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from config import (
    CLAUDE_PLAN,
    ESTIMATE_CHARS_PER_TOKEN,
    ESTIMATE_OUTPUT_TOKEN_WEIGHT,
    ESTIMATE_TOKENS_PER_PCT,
    MIN_CAPACITY_PERCENT,
)

logger = logging.getLogger(__name__)

# On Windows, npm-installed CLIs are .cmd files
_CMD_SUFFIX = ".cmd" if sys.platform == "win32" else ""
_CCLIMITS_CMD = f"cclimits{_CMD_SUFFIX}"
_CLAUDE_CMD = "claude.exe" if sys.platform == "win32" else "claude"
_GEMINI_CMD = f"gemini{_CMD_SUFFIX}"

# Background refresh intervals — the daemon thread owns all cclimits calls so
# get_limits() never blocks after the first call.
_BG_POLL_AVAILABLE_SEC = 90   # refresh every 90 s when capacity is available
_BG_POLL_ERROR_SEC     = 30   # initial retry after errors (thread backs off up to 90 s)
_BG_POLL_429_SEC       = 300  # back off when cclimits itself gets rate-limited (5 min)
_429_MAX_BASE_AGE_SEC  = 3600 # 1h maximum age for a 429 base snapshot
_CCLIMITS_TIMEOUT_SEC = 15
_CCLIMITS_429_RETRY_TIMEOUT_SEC = 5
_CCLIMITS_429_RETRY_SLEEP_SEC = (1, 2)
_CCLIMITS_CACHE_TTL_SEC = 600  # pass to cclimits --cache-ttl → max 6 real API calls/h

# Token limits per 5-hour window, by Claude subscription plan.
# Sourced from claude-monitor's plans.py; override via CLAUDE_PLAN in .env.
_CLAUDE_LOCAL_PLAN_LIMITS: dict[str, int] = {
    "pro":    19_000,
    "max5":   88_000,
    "max20": 220_000,
    "custom": 44_000,
}
_RUN_CCLIMITS_DEFAULT = None

_limits_cache: "tuple[AllLimits, float] | None" = None
_limits_cache_lock = threading.Lock()
_fresh_limits_lock = threading.Lock()

# Background-thread state
_bg_thread: "threading.Thread | None" = None
_bg_thread_lock = threading.Lock()
_bg_wake  = threading.Event()   # poke to interrupt the thread's sleep early
_cache_ready = threading.Event() # set after the first successful cache population

# HTTP 429 estimation state — tracks estimated provider usage when cclimits
# itself is rate-limited and real capacity data is unavailable.
_429_estimate_lock = threading.Lock()
# Maps provider name -> (ProviderLimits snapshot, time.monotonic() when taken)
_429_snapshots: dict[str, tuple[ProviderLimits, float]] = {}
# Maps provider name -> window name -> estimated percentage consumed.
_429_estimated_usage: dict[str, dict[str, float]] = {}
_429_notified: set[str] = set()

@dataclass
class WindowData:
    """Per-window usage data (e.g. five_hour, seven_day, 24h tier)."""
    remaining_pct: float = 0.0
    resets_in_sec: int = 0


@dataclass
class ProviderLimits:
    available: bool = False       # Has any usable capacity
    remaining_pct: float = 0.0   # Lowest remaining % across all tiers
    resets_in_sec: int = 0        # Seconds until earliest reset
    error: str = ""               # Error message if unavailable
    windows: "dict[str, WindowData]" = field(default_factory=dict)


@dataclass
class AllLimits:
    claude: ProviderLimits = field(default_factory=ProviderLimits)
    gemini: ProviderLimits = field(default_factory=ProviderLimits)
    codex: ProviderLimits = field(default_factory=ProviderLimits)

    def earliest_reset_sec(self) -> int:
        """Returns seconds until the earliest provider resets."""
        times = [
            p.resets_in_sec for p in [self.claude, self.gemini, self.codex]
            if p.resets_in_sec > 0
        ]
        return min(times) if times else 3600  # default 1h fallback

    def any_available(self) -> bool:
        return any((self.claude.available, self.gemini.available, self.codex.available))


def _parse_resets_in(resets_str: str) -> int:
    """Convert '2h 30m' or '45m' or '1d 2h' to seconds."""
    if not resets_str:
        return 0
    total = 0
    for match in re.finditer(r"(\d+)\s*(d|h|m|s)", resets_str):
        val, unit = int(match.group(1)), match.group(2)
        total += val * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total


def _parse_percent(pct_str: str) -> float:
    """Convert '93.0%' to 93.0."""
    try:
        return float(str(pct_str).replace("%", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _parse_claude(data: dict) -> ProviderLimits:
    if data.get("status") != "ok":
        return ProviderLimits(error=data.get("error") or data.get("token_status") or "unknown")

    window_tuples = []
    window_data: dict[str, WindowData] = {}
    for key in ("five_hour", "seven_day"):
        w = data.get(key, {})
        if "remaining" in w:
            pct = _parse_percent(w["remaining"])
            sec = _parse_resets_in(w.get("resets_in", ""))
            window_tuples.append((pct, sec))
            window_data[key] = WindowData(remaining_pct=pct, resets_in_sec=sec)

    if not window_tuples:
        return ProviderLimits(error="no window data")

    remaining = min(r for r, _ in window_tuples)
    resets_in = min(t for _, t in window_tuples if t > 0) if any(t > 0 for _, t in window_tuples) else 0

    return ProviderLimits(
        available=remaining >= MIN_CAPACITY_PERCENT,
        remaining_pct=remaining,
        resets_in_sec=resets_in,
        windows=window_data,
    )


def _parse_gemini(data: dict) -> ProviderLimits:
    if data.get("status") != "ok":
        return ProviderLimits(error=data.get("error") or data.get("token_status") or "unknown")

    # All three tiers: 3-Flash, Flash, Pro (let Gemini CLI decide which to use)
    models = data.get("models", {})
    if not models:
        return ProviderLimits(error="no model data")

    tier_remaining = []
    tier_resets = []
    window_data: dict[str, WindowData] = {}
    for model_name, model_data in models.items():
        r = _parse_percent(model_data.get("remaining", "0%"))
        t = _parse_resets_in(model_data.get("resets_in", ""))
        tier_remaining.append(r)
        if t > 0:
            tier_resets.append(t)
        safe_key = re.sub(r"[^a-z0-9_]", "_", model_name.lower())
        window_data[safe_key] = WindowData(remaining_pct=r, resets_in_sec=t)

    # Available if ANY tier has capacity (Gemini CLI picks internally)
    max_remaining = max(tier_remaining) if tier_remaining else 0
    min_reset = min(tier_resets) if tier_resets else 0

    return ProviderLimits(
        available=max_remaining >= MIN_CAPACITY_PERCENT,
        remaining_pct=max_remaining,
        resets_in_sec=min_reset,
        windows=window_data,
    )


def _parse_codex(data: dict) -> ProviderLimits:
    if data.get("status") != "ok":
        return ProviderLimits(error=data.get("error") or data.get("token_status") or "unknown")

    window_tuples = []
    window_data: dict[str, WindowData] = {}
    for key in ("primary_window", "secondary_window"):
        w = data.get(key, {})
        if "remaining" in w:
            pct = _parse_percent(w["remaining"])
            sec = _parse_resets_in(w.get("resets_in", ""))
            window_tuples.append((pct, sec))
            window_data[key] = WindowData(remaining_pct=pct, resets_in_sec=sec)

    if not window_tuples:
        return ProviderLimits(error="no window data")

    remaining = min(r for r, _ in window_tuples)
    resets_in = min(t for _, t in window_tuples if t > 0) if any(t > 0 for _, t in window_tuples) else 0

    return ProviderLimits(
        available=remaining >= MIN_CAPACITY_PERCENT,
        remaining_pct=remaining,
        resets_in_sec=resets_in,
        windows=window_data,
    )


def _is_provider_429(provider_data: dict) -> bool:
    """Check if a provider's cclimits data indicates HTTP 429 rate limiting."""
    error = str(provider_data.get("error", ""))
    details = str(provider_data.get("details", ""))
    return "429" in error or "429" in details


def _providers_with_429(raw: dict) -> set[str]:
    """Return set of provider names that have 429 errors in cclimits output."""
    return {
        name for name in ("claude", "gemini", "codex")
        if _is_provider_429(raw.get(name, {}))
    }


def estimate_task_usage_pct(
    duration_sec: float = 0,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    prompt_text: str = "",
    output_text: str = "",
    provider: str = "claude",
) -> float:
    """Estimate capacity percentage consumed by a task.

    Three tiers (best → worst):
    1. Actual token counts from provider JSON output (Claude --output-format json)
    2. Text-based estimate from prompt/output character lengths
    3. Duration-based heuristic (fallback when no text available)

    For P3 (orchestrator.py/limits.py): If Tier 1 is missing, we use the MAX of Tier 2 and Tier 3
    to avoid underestimating multi-step tools that return short summaries but take a long time.
    """
    tokens_per_pct = ESTIMATE_TOKENS_PER_PCT.get(
        provider, ESTIMATE_TOKENS_PER_PCT.get("claude", 15_000),
    )

    # Tier 1: actual token counts from provider
    if input_tokens > 0 or output_tokens > 0:
        effective = input_tokens + output_tokens * ESTIMATE_OUTPUT_TOKEN_WEIGHT
        return max(0.1, effective / tokens_per_pct)

    # Tier 2 & 3: estimate from text lengths and duration
    # We take the maximum of both to be conservative (P3 finding)
    text_pct = 0.0
    if prompt_text or output_text:
        est_input = len(prompt_text) // ESTIMATE_CHARS_PER_TOKEN
        est_output = len(output_text) // ESTIMATE_CHARS_PER_TOKEN
        effective = est_input + est_output * ESTIMATE_OUTPUT_TOKEN_WEIGHT
        text_pct = max(0.1, effective / tokens_per_pct)

    # Duration heuristic
    dur_pct = 0.0
    if duration_sec > 0:
        if duration_sec < 60:
            dur_pct = 2.0
        elif duration_sec < 300:
            dur_pct = 5.0
        elif duration_sec < 600:
            dur_pct = 10.0
        else:
            dur_pct = 15.0

    return max(text_pct, dur_pct)


def _provider_window_mode(base: ProviderLimits) -> str:
    """Infer whether provider availability is driven by min- or max-window semantics."""
    if not base.windows:
        return "provider"

    values = [window.remaining_pct for window in base.windows.values()]
    min_pct = min(values)
    max_pct = max(values)
    if abs(base.remaining_pct - max_pct) < abs(base.remaining_pct - min_pct):
        return "max"
    return "min"


def _estimate_window_usage(
    base: ProviderLimits,
    estimated_pct: float,
) -> dict[str, float]:
    """Translate provider usage into per-window usage for 429 fallback.

    For nested windows (Claude/Codex), the shortest reset horizon is treated as
    the base budget and longer windows receive a proportionally smaller
    percentage deduction. For alternative tiers (Gemini-style max semantics), we
    conservatively apply the same deduction to every tier because the actual
    tier choice is unknown during fallback.
    """
    pct = max(0.0, float(estimated_pct))
    if not base.windows:
        return {"__provider__": pct}

    if _provider_window_mode(base) != "min":
        return {name: pct for name in base.windows}

    positive_resets = [
        window.resets_in_sec for window in base.windows.values() if window.resets_in_sec > 0
    ]
    shortest_reset = min(positive_resets) if positive_resets else 0
    if shortest_reset <= 0:
        return {name: pct for name in base.windows}

    usage: dict[str, float] = {}
    for name, window in base.windows.items():
        if window.resets_in_sec > 0:
            scale = max(1.0, window.resets_in_sec / shortest_reset)
        else:
            scale = 1.0
        usage[name] = pct / scale
    return usage


def _normalize_estimated_usage(
    base: ProviderLimits,
    estimated_usage: "dict[str, float] | float",
) -> dict[str, float]:
    if isinstance(estimated_usage, dict):
        if base.windows:
            return {
                name: max(0.0, float(estimated_usage.get(name, 0.0)))
                for name in base.windows
            }
        return {"__provider__": max(0.0, float(estimated_usage.get("__provider__", 0.0)))}
    return _estimate_window_usage(base, estimated_usage)


def _aggregate_remaining_pct(
    base: ProviderLimits,
    adjusted_windows: dict[str, WindowData],
) -> float:
    if not adjusted_windows:
        return base.remaining_pct

    values = [window.remaining_pct for window in adjusted_windows.values()]
    if _provider_window_mode(base) == "max":
        return max(values)
    return min(values)


def report_estimated_usage(provider_name: str, estimated_pct: float) -> None:
    """Track estimated capacity consumption during HTTP 429 periods.

    Called by the orchestrator after each task to maintain running estimates.
    Only accumulates if we're in a 429 fallback state for this provider.
    """
    with _429_estimate_lock:
        if provider_name not in _429_snapshots:
            return
        base_pl, _ = _429_snapshots[provider_name]
        if not (base_pl.available or base_pl.windows):
            return
        usage_delta = _estimate_window_usage(base_pl, estimated_pct)
        current_usage = _normalize_estimated_usage(
            base_pl,
            _429_estimated_usage.get(provider_name, {}),
        )
        for key, pct in usage_delta.items():
            current_usage[key] = current_usage.get(key, 0.0) + pct
        _429_estimated_usage[provider_name] = current_usage


def _build_429_fallback_provider(
    base: ProviderLimits,
    estimated_usage: "dict[str, float] | float",
    snapshot_time: float,
) -> ProviderLimits:
    """Build an adjusted ProviderLimits using cached data minus estimated consumption."""
    elapsed = int(time.monotonic() - snapshot_time)
    usage_by_window = _normalize_estimated_usage(base, estimated_usage)
    adjusted_windows: dict[str, WindowData] = {}
    for wname, wdata in base.windows.items():
        adj_w_pct = max(0.0, wdata.remaining_pct - usage_by_window.get(wname, 0.0))
        # Resets in sec should decrease as time passes
        adj_resets = max(0, wdata.resets_in_sec - elapsed)
        adjusted_windows[wname] = WindowData(
            remaining_pct=adj_w_pct,
            resets_in_sec=adj_resets,
        )

    if adjusted_windows:
        adjusted_pct = _aggregate_remaining_pct(base, adjusted_windows)
    else:
        provider_pct = usage_by_window.get("__provider__", 0.0)
        adjusted_pct = max(0.0, base.remaining_pct - provider_pct)

    estimated_pct = max(usage_by_window.values(), default=0.0)

    if estimated_pct > 0:
        error_detail = f"HTTP 429 (estimated, {estimated_pct:.0f}% consumed)"
    else:
        error_detail = "HTTP 429 (cached)"

    # Also decrement resets_in_sec at provider level
    adj_resets_top = max(0, base.resets_in_sec - elapsed)

    return ProviderLimits(
        available=adjusted_pct >= MIN_CAPACITY_PERCENT,
        remaining_pct=adjusted_pct,
        resets_in_sec=adj_resets_top,
        windows=adjusted_windows,
        error=error_detail,
    )


def _optimistic_429_provider() -> ProviderLimits:
    """Optimistic fallback when 429 occurs without any cached data."""
    return ProviderLimits(
        available=True,
        remaining_pct=100.0,
        resets_in_sec=300,
        error="HTTP 429 (assumed available)",
    )


def _is_snapshot_fresh(snapshot_time: float, now: float | None = None) -> bool:
    """Return whether a cached base snapshot is still fresh enough for 429 fallback."""
    current = time.monotonic() if now is None else now
    return current - snapshot_time <= _429_MAX_BASE_AGE_SEC


def _is_reliable_429_base_snapshot(provider_limits: ProviderLimits) -> bool:
    """Return whether a provider snapshot represents real capacity data."""
    if provider_limits.error:
        return False
    return bool(
        provider_limits.windows
        or provider_limits.available
        or provider_limits.remaining_pct > 0
        or provider_limits.resets_in_sec > 0
    )


def _apply_429_fallback(result: AllLimits, p429: set[str]) -> AllLimits:
    """Replace 429-error providers with cached + estimated data."""
    with _limits_cache_lock:
        cached_tuple = _limits_cache

    # For Claude: try reading local JSONL files before acquiring the state lock.
    # This is IO-bound and must not run while holding _429_estimate_lock.
    local_claude_pl: "ProviderLimits | None" = (
        _get_claude_limits_from_local(CLAUDE_PLAN) if "claude" in p429 else None
    )

    with _429_estimate_lock:
        now = time.monotonic()

        # Invalidate snapshots that are too old, even if the provider is still
        # rate-limited. Otherwise we keep extrapolating from arbitrarily stale data.
        to_remove = [
            name for name, (_, snap_time) in _429_snapshots.items()
            if now - snap_time > _429_MAX_BASE_AGE_SEC
        ]
        for name in to_remove:
            logger.warning("HTTP 429 snapshot for %s is older than 1h — resetting state", name)
            _429_snapshots.pop(name, None)
            _429_estimated_usage.pop(name, None)

        # For providers NOT in p429, update their snapshot with fresh data and reset usage
        for name in ("claude", "gemini", "codex"):
            if name not in p429:
                fresh_pl = getattr(result, name)
                # If we have usable fresh data, use it as the new base for future 429 periods
                if (fresh_pl.available or fresh_pl.windows) and not fresh_pl.error:
                    _429_snapshots[name] = (fresh_pl, now)
                    _429_estimated_usage[name] = {}
                    _429_notified.discard(name)

        for name in p429:
            # 0. For Claude: use local JSONL data (no API calls, always fresh)
            if name == "claude" and local_claude_pl is not None:
                _429_snapshots[name] = (local_claude_pl, now)
                _429_estimated_usage[name] = {}
                setattr(result, name, local_claude_pl)
                logger.info(
                    "  [claude] HTTP 429 -> local JSONL files (%.0f%% remaining, resets in %ds)",
                    local_claude_pl.remaining_pct, local_claude_pl.resets_in_sec,
                )
                continue

            # 1. Try existing per-provider snapshot
            if name in _429_snapshots:
                base_pl, snapshot_time = _429_snapshots[name]
                estimated = _429_estimated_usage.get(name, {})
                adjusted = _build_429_fallback_provider(base_pl, estimated, snapshot_time)
                setattr(result, name, adjusted)
                logger.info(
                    "  [%s] HTTP 429 -> cached provider snapshot (%.0f%% remaining, %.0f%% estimated consumed, resets in %ds)",
                    name,
                    adjusted.remaining_pct,
                    max(estimated.values(), default=0.0) if isinstance(estimated, dict) else estimated,
                    adjusted.resets_in_sec,
                )
            # 2. Try global cache if no provider snapshot exists yet
            elif cached_tuple is not None:
                cached, cached_time = cached_tuple
                base_pl = getattr(cached, name)
                cache_is_fresh = _is_snapshot_fresh(cached_time, now)
                cache_is_reliable = _is_reliable_429_base_snapshot(base_pl)
                if cache_is_fresh and cache_is_reliable:
                    _429_snapshots[name] = (base_pl, cached_time)
                    _429_estimated_usage[name] = {}
                    adjusted = _build_429_fallback_provider(base_pl, {}, cached_time)
                    setattr(result, name, adjusted)
                    logger.info(
                        "  [%s] HTTP 429 -> initialized snapshot from global cache (%.0f%% remaining)",
                        name, adjusted.remaining_pct
                    )
                else:
                    opt_pl = _optimistic_429_provider()
                    _429_snapshots[name] = (opt_pl, now)
                    _429_estimated_usage[name] = {}
                    setattr(result, name, opt_pl)
                    if not cache_is_fresh:
                        logger.info("  [%s] HTTP 429, global cache too old -> assuming available", name)
                    elif not cache_is_reliable:
                        logger.info(
                            "  [%s] HTTP 429, global cache has no reliable capacity snapshot -> assuming available",
                            name,
                        )
                    else:
                        logger.info("  [%s] HTTP 429, cache is also fallback -> assuming available", name)
            # 3. Last resort: optimistic fallback
            else:
                opt_pl = _optimistic_429_provider()
                _429_snapshots[name] = (opt_pl, now)
                _429_estimated_usage[name] = {}
                setattr(result, name, opt_pl)
                logger.info("  [%s] HTTP 429, cold start (no base snapshot) -> assuming available", name)

    # Telegram notification (once per 429 period per provider)
    for name in p429:
        if name not in _429_notified:
            _429_notified.add(name)
            try:
                from notifier import notify_limits_429_fallback
                pl = getattr(result, name)
                notify_limits_429_fallback(name, pl.remaining_pct)
            except ImportError:
                pass

    return result


def _clear_429_state(result: AllLimits) -> None:
    """Reset 429 estimation state and notify that real data is available again."""
    cleared = set(_429_notified)
    with _429_estimate_lock:
        _429_snapshots.clear()
        _429_estimated_usage.clear()
    _429_notified.clear()

    for name in cleared:
        try:
            from notifier import notify_limits_429_cleared
            pl = getattr(result, name)
            notify_limits_429_cleared(name, pl.remaining_pct)
        except ImportError:
            pass


def _get_claude_limits_from_local(plan: str) -> "ProviderLimits | None":
    """Read Claude usage from ~/.claude/projects JSONL files via claude-monitor.

    No HTTP requests — immune to rate limiting on the Anthropic monitoring API.
    Returns None if claude-monitor is not installed, the plan is unknown, or data
    is unavailable (e.g. no recent sessions).
    """
    if not plan:
        return None
    token_limit = _CLAUDE_LOCAL_PLAN_LIMITS.get(plan.lower())
    if not token_limit:
        logger.debug("CLAUDE_PLAN=%r not recognised — skipping local fallback", plan)
        return None
    try:
        from claude_monitor.core.models import CostMode
        from claude_monitor.data.analyzer import SessionAnalyzer
        from claude_monitor.data.reader import load_usage_entries
    except ImportError:
        logger.debug("claude-monitor not installed — skipping local fallback")
        return None
    except Exception as e:
        logger.debug("Failed to import claude-monitor: %s", e)
        return None
    try:
        import datetime as _dt
        entries, _ = load_usage_entries(hours_back=10, mode=CostMode.AUTO)
        if not entries:
            logger.debug("No claude-monitor usage entries found in last 10h")
            return None
        blocks = SessionAnalyzer(session_duration_hours=5).transform_to_blocks(entries)
        if not blocks:
            logger.debug("No claude-monitor session blocks found")
            return None
        active = next((b for b in reversed(blocks) if b.is_active), None)
        if active is None:
            # No active 5-hour block means the previous window already ended.
            # Treat the current window as fully reset rather than reusing stale usage.
            window = WindowData(remaining_pct=100.0, resets_in_sec=0)
            return ProviderLimits(
                available=True,
                remaining_pct=100.0,
                resets_in_sec=0,
                windows={"five_hour": window},
                error="HTTP 429 (local-files)",
            )
        tokens_used = active.token_counts.total_tokens  # input + output + cache_creation + cache_read
        remaining_pct = max(0.0, (1.0 - tokens_used / token_limit) * 100)
        now = _dt.datetime.now(_dt.timezone.utc)
        end = active.end_time
        if end.tzinfo is None:
            end = end.replace(tzinfo=_dt.timezone.utc)
        resets_in_sec = max(0, int((end - now).total_seconds()))
        window = WindowData(remaining_pct=remaining_pct, resets_in_sec=resets_in_sec)
        return ProviderLimits(
            available=remaining_pct >= MIN_CAPACITY_PERCENT,
            remaining_pct=remaining_pct,
            resets_in_sec=resets_in_sec,
            windows={"five_hour": window},
            error="HTTP 429 (local-files)",
        )
    except Exception as e:
        logger.debug("claude-monitor local fallback failed: %s", e)
        return None


def _run_cclimits_impl(timeout_sec: int, *, use_cache: bool = True) -> dict | None:
    """Run cclimits --json and return parsed dict, or None on failure.

    use_cache=True passes --cache-ttl to cclimits so it reads/writes a local
    disk cache and only hits the real Anthropic API every _CCLIMITS_CACHE_TTL_SEC
    seconds.  use_cache=False bypasses the cache for 429 retry probes.
    """
    try:
        cmd = [_CCLIMITS_CMD, "--json"]
        if use_cache:
            cmd += ["--cache-ttl", str(_CCLIMITS_CACHE_TTL_SEC)]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            shell=sys.platform == "win32",
        )
        if result.returncode != 0:
            # Some cclimits versions write valid JSON to stdout even on non-zero exit
            # (e.g. partial data with a warning). Try to honour that data first.
            if result.stdout and result.stdout.strip():
                try:
                    return json.loads(result.stdout)
                except json.JSONDecodeError:
                    pass
            # No usable JSON. If --cache-ttl was used, retry without it. Some CLI
            # frameworks don't echo the unknown flag name in the error text, so we
            # treat any non-zero exit during a cached call as a potential flag-compat
            # issue rather than relying solely on _cache_ttl_flag_unsupported().
            if use_cache:
                logger.info("cclimits exited non-zero with --cache-ttl; retrying without cache")
                return _run_cclimits_impl(timeout_sec, use_cache=False)
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        return None


def _run_cclimits_with_timeout(timeout_sec: int, *, use_cache: bool = True) -> dict | None:
    runner = globals().get("_run_cclimits")
    if runner is not None and runner is not _RUN_CCLIMITS_DEFAULT:
        return runner()
    return _run_cclimits_impl(timeout_sec, use_cache=use_cache)


def _run_cclimits() -> dict | None:
    return _run_cclimits_with_timeout(_CCLIMITS_TIMEOUT_SEC)


_RUN_CCLIMITS_DEFAULT = _run_cclimits


def _needs_token_refresh(data: dict, provider: str) -> bool:
    """Check if a provider's cclimits data indicates an expired token."""
    pdata = data.get(provider, {})
    if pdata.get("status") == "ok":
        return False
    token_status = pdata.get("token_status", "")
    error = pdata.get("error", "")
    return "expired" in token_status.lower() or "expired" in error.lower()


def _refresh_token(provider: str) -> bool:
    """Start the CLI briefly to refresh its OAuth token. Returns True on success.

    For Claude, tries multiple strategies in order:
    1. ``claude auth status`` — check if token is actually valid (not just readable)
    2. Minimal ``claude --print`` request to force OAuth refresh
    """
    try:
        if provider == "claude":
            # Strategy 1: check auth status output — only trust if NOT expired
            r = subprocess.run(
                [_CLAUDE_CMD, "auth", "status"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                shell=sys.platform == "win32",
            )
            combined_out = f"{r.stdout or ''}\n{r.stderr or ''}".lower()
            if r.returncode == 0 and "expired" not in combined_out:
                # Token is genuinely valid, no refresh needed
                return True
            logger.info("  [claude] auth status: token ist expired (rc=%d)", r.returncode)

            # Strategy 2: actual API call forces OAuth token refresh
            logger.info("  [claude] Versuche Token-Refresh via claude --print ...")
            r2 = subprocess.run(
                [_CLAUDE_CMD, "--print", "--model", "claude-haiku-4-5-20251001", "-p", "ping"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30,
                shell=sys.platform == "win32",
            )
            if r2.returncode == 0:
                return True

            # Strategy 2 failed — maybe token needs interactive re-auth
            stderr2 = (r2.stderr or "").strip()
            logger.warning("  [claude] Token-Refresh fehlgeschlagen (rc=%d): %s", r2.returncode, stderr2[:200])
            logger.warning("  [claude] ⚠ Manuelles 'claude' in der CLI nötig um Token zu erneuern!")
            return False

        elif provider == "gemini":
            # No auth-only command available; use a short non-interactive request
            # (same pattern as provider runner) to force OAuth refresh if needed.
            r = subprocess.run(
                [_GEMINI_CMD, "--prompt", "", "--yolo", "--output-format", "text"],
                input="ping",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                shell=sys.platform == "win32",
            )
            if r.returncode == 0:
                return True

            # Some CLI versions can still refresh credentials before exiting non-zero.
            combined = f"{r.stdout or ''}\n{r.stderr or ''}".lower()
            return "loaded cached credentials" in combined
        else:
            return False
    except Exception:
        return False


def _get_limits_fresh() -> AllLimits:
    """Actually run cclimits and parse the result (no caching)."""
    with _fresh_limits_lock:
        raw = _run_cclimits()
        if raw is None:
            return AllLimits(
                claude=ProviderLimits(error="cclimits timeout"),
                gemini=ProviderLimits(error="cclimits timeout"),
                codex=ProviderLimits(error="cclimits timeout"),
            )

        # Auto-refresh expired tokens and re-query
        refresh_attempted = False
        for provider in ("claude", "gemini"):
            if _needs_token_refresh(raw, provider):
                refresh_attempted = True
                logger.info("  [%s] Token expired → refreshing...", provider)
                if not _refresh_token(provider):
                    logger.error("  [%s] Token-Refresh fehlgeschlagen — Provider wird als unavailable gemeldet", provider)

        if refresh_attempted:
            # Re-query after any refresh attempt.
            # Some CLIs refresh OAuth asynchronously and may still return non-zero once.
            for _ in range(3):
                fresh = _run_cclimits()
                if fresh is not None:
                    raw = fresh
                    if not any(_needs_token_refresh(raw, p) for p in ("claude", "gemini")):
                        break
                time.sleep(2)

        # Detect HTTP 429 rate-limiting on the monitoring API itself
        p429 = _providers_with_429(raw)

        # Retry with a bounded budget for 429 so cold-start callers still see
        # the fallback-or-recovered result inside get_limits()' 30 s wait.
        if p429:
            for sleep_sec in _CCLIMITS_429_RETRY_SLEEP_SEC:
                time.sleep(sleep_sec)
                # Bypass cache so we probe the real API instead of re-reading the
                # cached 429 that was just written by the first call.
                fresh = _run_cclimits_with_timeout(_CCLIMITS_429_RETRY_TIMEOUT_SEC, use_cache=False)
                if fresh is not None:
                    raw = fresh
                    p429 = _providers_with_429(raw)
                    if not p429:
                        break

        result = AllLimits(
            claude=_parse_claude(raw.get("claude", {"status": "missing"})),
            gemini=_parse_gemini(raw.get("gemini", {"status": "missing"})),
            codex=_parse_codex(raw.get("codex", {"status": "missing"})),
        )

        # Apply 429 fallback or clear 429 state
        if p429:
            result = _apply_429_fallback(result, p429)
        else:
            with _429_estimate_lock:
                had_429 = len(_429_snapshots) > 0
            if had_429:
                _clear_429_state(result)

        return result


def _is_timeout_snapshot(result: AllLimits) -> bool:
    """True when cclimits failed before provider parsing (transient transport error)."""
    providers = (result.claude, result.gemini, result.codex)
    return all((not p.available) and p.error == "cclimits timeout" for p in providers)


def _is_transient_error_snapshot(result: AllLimits) -> bool:
    """True when providers are unavailable due non-resettable errors.

    Example: auth glitches or malformed tool output where resets are unknown.
    These should be retried sooner than normal steady-state snapshots.
    """
    providers = (result.claude, result.gemini, result.codex)
    if result.any_available():
        return False
    if any((p.resets_in_sec or 0) > 0 for p in providers):
        return False
    return any(bool(p.error) for p in providers)


def _compute_next_poll_sec(result: AllLimits) -> int:
    """Seconds until the background thread should next call cclimits."""
    if _is_timeout_snapshot(result) or _is_transient_error_snapshot(result):
        return _BG_POLL_ERROR_SEC
    with _429_estimate_lock:
        if _429_snapshots:
            return _BG_POLL_429_SEC
    if result.any_available():
        return _BG_POLL_AVAILABLE_SEC
    # At limit: wait until the earliest reset; no point hammering cclimits sooner.
    return max(60, min(result.earliest_reset_sec(), 3600))


def _bg_refresh_loop() -> None:
    """Daemon: keeps _limits_cache fresh so get_limits() never blocks."""
    global _limits_cache
    backoff = _BG_POLL_ERROR_SEC
    while True:
        # Clear the wake event at loop start so a concurrent force_refresh()
        # that happens during refresh/scheduling is still observed by wait().
        _bg_wake.clear()

        # Single lock acquisition: check skip and read result atomically.
        with _limits_cache_lock:
            cached = _limits_cache
            skip = cached is not None and (time.monotonic() - cached[1]) < 5

        if skip:
            # Cache was freshly updated by force_refresh — just recalibrate sleep.
            # Reset backoff if the fresh snapshot is healthy so the next real error
            # doesn't inherit an elevated retry interval from a previous error streak.
            result = cached[0]
            is_error = _is_timeout_snapshot(result) or _is_transient_error_snapshot(result)
            sleep_sec = _BG_POLL_ERROR_SEC if is_error else _compute_next_poll_sec(result)
            if not is_error:
                backoff = _BG_POLL_ERROR_SEC
        else:
            result = _get_limits_fresh()
            with _limits_cache_lock:
                _limits_cache = (result, time.monotonic())
            _cache_ready.set()
            if _is_timeout_snapshot(result) or _is_transient_error_snapshot(result):
                sleep_sec = backoff
                backoff = min(backoff * 3, _BG_POLL_AVAILABLE_SEC)
            else:
                backoff = _BG_POLL_ERROR_SEC
                sleep_sec = _compute_next_poll_sec(result)

        _bg_wake.wait(timeout=sleep_sec)


def _start_bg_thread() -> None:
    global _bg_thread
    if _bg_thread is not None and _bg_thread.is_alive():
        return
    with _bg_thread_lock:
        if _bg_thread is None or not _bg_thread.is_alive():
            _bg_thread = threading.Thread(
                target=_bg_refresh_loop, daemon=True, name="limits-bg-refresh"
            )
            _bg_thread.start()


def get_limits(force_refresh: bool = False) -> AllLimits:
    """Return the current limits snapshot.

    Non-blocking after the first call: a background daemon keeps the cache
    fresh continuously.  The very first call blocks up to 15 s while cclimits
    returns its initial result.

    force_refresh=True runs a synchronous cclimits call on the calling thread,
    updates the cache, then resets the background thread's sleep timer.  Use
    this only after known provider failures (e.g. rate-limit errors) so the
    next task sees accurate data without waiting for the next background poll.
    """
    global _limits_cache
    if force_refresh:
        result = _get_limits_fresh()
        with _limits_cache_lock:
            _limits_cache = (result, time.monotonic())
        _cache_ready.set()
        _bg_wake.set()          # reset bg thread sleep timer with fresh data
        _start_bg_thread()
        return result

    _start_bg_thread()
    _cache_ready.wait(timeout=30)   # blocks only on the very first call

    # Timed out on first call (cclimits unresponsive). Cache the fallback so
    # later callers do not keep paying the cold-start wait while the background
    # thread is still hung on its first refresh.
    with _limits_cache_lock:
        if _limits_cache is not None:
            return _limits_cache[0]
        fallback = AllLimits(
            claude=ProviderLimits(error="cclimits unavailable"),
            gemini=ProviderLimits(error="cclimits unavailable"),
            codex=ProviderLimits(error="cclimits unavailable"),
        )
        _limits_cache = (fallback, time.monotonic())
        _cache_ready.set()
        return fallback
