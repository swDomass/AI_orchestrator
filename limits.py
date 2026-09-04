"""
Wrapper around `cclimits --json`.
Parses usage limits for Claude, Gemini (all 3 tiers), and Codex.
Auto-refreshes expired OAuth tokens before querying.

opencode's limits are NOT from cclimits (opencode has no OAuth quota - it pays
per token via an OpenRouter key). Its ProviderLimits comes from a live HTTP
budget check instead; see _apply_opencode_budget_override() / openrouter_budget.py.
"""

import json
import logging
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import config
import openrouter_budget
from config import (
    CLAUDE_FIVE_HOUR_MIN_CAPACITY_PCT,
    CLAUDE_PLAN,
    CLAUDE_SEVEN_DAY_MIN_CAPACITY_PCT,
    CODEX_PRIMARY_MIN_CAPACITY_PCT,
    CODEX_SECONDARY_MIN_CAPACITY_PCT,
    ESTIMATE_CHARS_PER_TOKEN,
    ESTIMATE_OUTPUT_TOKEN_WEIGHT,
    ESTIMATE_TOKENS_PER_PCT,
    ESTIMATE_TOKENS_PER_PCT_CLAUDE_WINDOWS,
    MIN_CAPACITY_PERCENT,
    QUOTA_LIVE_ESTIMATE_ENABLED,
)

logger = logging.getLogger(__name__)

# On Windows, npm-installed CLIs are .cmd files
_CMD_SUFFIX = ".cmd" if sys.platform == "win32" else ""
_CCLIMITS_CMD = f"cclimits{_CMD_SUFFIX}"
_CLAUDE_CMD = "claude.exe" if sys.platform == "win32" else "claude"
_GEMINI_CMD = f"gemini{_CMD_SUFFIX}"

# Background refresh intervals — the daemon thread owns all cclimits calls so
# get_limits() never blocks after the first call.
_BG_POLL_AVAILABLE_SEC = 300   # refresh every 5 min when capacity is available + queue active.
                               # set_queue_idle(False) already wakes the thread before the first
                               # task in a batch, and rate_limit errors trigger force_refresh, so
                               # this interval only needs to cover within-batch drift between
                               # consecutive tasks in run_once. 5 min keeps drift well under the
                               # MIN_CAPACITY_PERCENT=10% threshold.
_BG_POLL_IDLE_SEC      = 600   # refresh every 10 min when queue is empty — matches the
                               # cclimits --cache-ttl, so every idle poll bypasses the disk
                               # cache and hits the real API (no wasted subprocess calls on
                               # stale cache hits). set_queue_idle(False) wakes the thread
                               # on task arrival so the cache is fresh before the next task.
_BG_POLL_ERROR_SEC     = 30    # initial retry after errors (thread backs off up to 90 s)
_BG_POLL_429_SEC       = 300   # back off when cclimits itself gets rate-limited (5 min)
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
_queue_idle = threading.Event()  # set when queue is empty → longer poll interval
_paused     = threading.Event()  # set while orchestrator is paused → bg thread skips refreshes

# HTTP 429 estimation state — tracks estimated provider usage when cclimits
# itself is rate-limited and real capacity data is unavailable.
_429_estimate_lock = threading.Lock()
# Maps provider name -> (ProviderLimits snapshot, time.monotonic() when taken)
_429_snapshots: "dict[str, tuple[ProviderLimits, float]]" = {}
# Maps provider name -> window name -> estimated percentage consumed.
_429_estimated_usage: dict[str, dict[str, float]] = {}
_429_notified: set[str] = set()

# Providers whose OAuth token _refresh_token() can actually refresh via CLI.
# Gemini is deliberately excluded: retired from routing since 2026-08-15
# (dispatcher._PRIORITY), nobody authenticates the gemini CLI anymore, so the
# background loop's refresh attempts only produced dead log noise. Codex is
# excluded too: _refresh_token() has no branch for it and would silently
# return False, fabricating a bogus failed-refresh cooldown. Single source
# for the 4 loop sites below — keeps them from drifting from each other or
# from _refresh_token()'s own if/elif set.
_TOKEN_REFRESH_PROVIDERS = ("claude",)

# Cooldown after failed token refresh — avoids hammering the CLI every 90 s
# when re-auth requires manual intervention (e.g. browser login).
_REFRESH_FAILED_BACKOFF_SEC = 600   # 10 min; matches cclimits disk-cache TTL
_refresh_failed_until: dict[str, float] = {}   # provider -> monotonic deadline


@dataclass
class WindowData:
    """Per-window usage data (e.g. five_hour, seven_day, 24h tier)."""
    remaining_pct: float = 0.0
    resets_in_sec: int = 0


@dataclass
class ProviderLimits:
    available: bool = False       # Has any usable capacity
    remaining_pct: float = 0.0   # Lowest remaining % across all tiers
    resets_in_sec: int = 0        # Seconds until earliest reset (relative, at fetch time)
    reset_at_epoch: float = 0.0   # Absolute unix timestamp of reset (set in __post_init__)
    error: str = ""               # Error message if unavailable
    windows: "dict[str, WindowData]" = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Auto-compute absolute reset epoch so earliest_reset_sec() stays accurate
        # across long cache lifetimes without requiring callers to set this manually.
        if self.reset_at_epoch == 0.0 and self.resets_in_sec > 0:
            self.reset_at_epoch = time.time() + self.resets_in_sec


@dataclass
class AllLimits:
    claude: ProviderLimits = field(default_factory=ProviderLimits)
    gemini: ProviderLimits = field(default_factory=ProviderLimits)
    codex: ProviderLimits = field(default_factory=ProviderLimits)
    opencode: ProviderLimits = field(default_factory=ProviderLimits)

    # NOTE for whoever adds a 5th field: the three methods below enumerate the
    # provider fields BY HAND — that hand-enumeration is the drift source this
    # class already got bitten by once (opencode's own addition needed all
    # three, and the plan that introduced it only remembered two of them).
    # tests/test_limits_opencode.py::test_all_limits_drift_guard iterates
    # dataclasses.fields(AllLimits) and fails loudly if a new field is missing
    # from any of the three — update all three together, or that test tells you.

    def earliest_reset_sec(self) -> int:
        """Live calculation from absolute epoch — accurate regardless of cache age."""
        epochs = [
            p.reset_at_epoch for p in (self.claude, self.gemini, self.codex, self.opencode)
            if p.reset_at_epoch > 0
        ]
        if not epochs:
            return 3600  # default 1h fallback
        return max(0, int(min(epochs) - time.time()))

    def any_available(self) -> bool:
        return any((
            self.claude.available, self.gemini.available, self.codex.available,
            self.opencode.available,
        ))

    def has_transient_token_refresh(self) -> bool:
        """True when any provider is unavailable solely because its OAuth token is
        mid-refresh (an "expired" snapshot). The dispatcher uses this to force a
        synchronous limits refresh before declaring a task unroutable at boot.

        opencode has no OAuth token (it's a bare API key), so this is always
        False for it in practice — but it is still enumerated here, because this
        function counts the same fields by hand as the other two above and is
        exactly the same drift source if a field is skipped."""
        return any(
            is_transient_token_refresh(p)
            for p in (self.claude, self.gemini, self.codex, self.opencode)
        )


def is_transient_token_refresh(pl: ProviderLimits) -> bool:
    """True when *pl* is unavailable only because its OAuth token is being refreshed
    (an "expired" snapshot), NOT because of genuine capacity exhaustion.

    Genuine exhaustion always carries a known reset window (``resets_in_sec > 0``);
    a token-expired snapshot has no reset and an "expired" error string (see
    ``_needs_token_refresh`` and ``_parse_dual_window_provider``).

    Trade-off: this inspects the collapsed ``ProviderLimits.error``, which
    ``_parse_dual_window_provider`` fills as ``error or token_status``. It matches
    every observed cclimits boot state (``token_status="expired"`` → ``error="expired"``).
    It would miss a hypothetical snapshot carrying a distinct non-"expired" ``error``
    string *alongside* ``token_status="expired"``. Broadening to "any error present"
    is deliberately avoided so transient transport errors (e.g. "cclimits timeout")
    are NOT misclassified as a token refresh."""
    if pl.available or pl.resets_in_sec > 0:
        return False
    return "expired" in (pl.error or "").lower()


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


def _parse_dual_window_provider(
    data: dict,
    window_keys: tuple[str, str],
    thresholds: dict[str, int],
) -> ProviderLimits:
    """Generic parser for two-window providers (Claude, Codex)."""
    if data.get("status") != "ok":
        return ProviderLimits(error=data.get("error") or data.get("token_status") or "unknown")

    window_tuples: list[tuple[float, int]] = []
    window_avail: list[bool] = []
    window_data: dict[str, WindowData] = {}
    for key in window_keys:
        w = data.get(key, {})
        if "remaining" in w:
            pct = _parse_percent(w["remaining"])
            sec = _parse_resets_in(w.get("resets_in", ""))
            window_tuples.append((pct, sec))
            window_avail.append(pct >= thresholds[key])
            window_data[key] = WindowData(remaining_pct=pct, resets_in_sec=sec)

    if not window_tuples:
        return ProviderLimits(error="no window data")

    remaining = min(r for r, _ in window_tuples)
    resets_in = min((t for _, t in window_tuples if t > 0), default=0)

    return ProviderLimits(
        available=all(window_avail),
        remaining_pct=remaining,
        resets_in_sec=resets_in,
        windows=window_data,
    )


def _parse_claude(data: dict) -> ProviderLimits:
    return _parse_dual_window_provider(
        data,
        ("five_hour", "seven_day"),
        {"five_hour": CLAUDE_FIVE_HOUR_MIN_CAPACITY_PCT, "seven_day": CLAUDE_SEVEN_DAY_MIN_CAPACITY_PCT},
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


def _gemini_http_snapshot() -> ProviderLimits:
    """Synthetic limits for Gemini HTTP-API mode (GEMINI_API_KEY set).

    The public Gemini REST API exposes no pollable subscription-quota endpoint,
    so report fully available — rate-limit recovery is cooldown-driven via the
    provider's HTTP 429 handling, exactly like OpenRouter. cclimits (which only
    knows the dead consumer CLI/OAuth quota) is bypassed for Gemini in this mode.
    """
    return ProviderLimits(available=True, remaining_pct=100.0, resets_in_sec=0)


def _apply_gemini_http_override(result: AllLimits) -> AllLimits:
    """Replace the cclimits-derived Gemini limits with the HTTP-API snapshot when
    a key is configured. No-op in CLI mode (no key)."""
    if config.GEMINI_API_KEY:
        result.gemini = _gemini_http_snapshot()
    return result


def _opencode_reset_epoch(reset_cadence: "str | None") -> float:
    """Absolute unix timestamp of the next OpenRouter budget reset, from a
    cadence string ("daily"/"weekly"/"monthly").

    ASSUMPTION, not a documented fact — flagged, not sold as measured: the API
    (GET /api/v1/key) returns only the cadence, no reset timestamp, so this
    assumes the reset lands at UTC midnight (weekly: next UTC Monday 00:00;
    monthly: the 1st of next month, UTC 00:00). Only feeds
    AllLimits.earliest_reset_sec() (a "how soon might capacity return" estimate
    for the dispatcher's backoff/retry timing) — it never affects `available`
    itself, so a wrong guess here delays a retry, it does not risk spend.
    """
    if reset_cadence not in ("daily", "weekly", "monthly"):
        return 0.0
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if reset_cadence == "daily":
        target = midnight + timedelta(days=1)
    elif reset_cadence == "weekly":
        days_ahead = 7 - now.weekday()  # Monday == 0; always lands on next Monday
        target = midnight + timedelta(days=days_ahead)
    elif now.month == 12:  # "monthly"
        target = midnight.replace(year=now.year + 1, month=1, day=1)
    else:  # "monthly"
        target = midnight.replace(month=now.month + 1, day=1)
    return target.timestamp()


def _opencode_budget_snapshot() -> ProviderLimits:
    """Live snapshot for opencode from OpenRouter's GET /api/v1/key (the only
    capacity source opencode has — see openrouter_budget.fetch_budget()).

    Deliberately fail-CLOSED, the opposite of Claude/Codex (which fail open on
    a missing/unreadable policy or transport hiccup, see CLAUDE.md's
    "_UNCAPPED_PROVIDERS" section): a Claude/Codex run that slips through on
    stale/unknown capacity costs nothing extra (subscription quota, not
    metered spend). An opencode run costs real OpenRouter dollars, so "we
    don't know the remaining budget" must default to "assume none available",
    not "assume plenty".

    Registration is checked FIRST, and that is not an optimisation. The budget
    lives on the OpenRouter key, so it answers "is there money" even on a
    machine where opencode is not installed at all -- and `available=True` then
    propagates into aggregates that mean something else entirely. Measured
    2026-09-04 with claude/gemini/codex all False and a budget hit faked:
    `any_available()` flips False -> True, which makes run_watch sleep 30 s
    instead of up to SLEEP_POLL_INTERVAL*10 and re-run a real cclimits
    subprocess every round; `_compute_next_poll_sec` goes 30 -> 300 s; and
    `earliest_reset_sec()` takes a min() over opencode's ASSUMED UTC midnight,
    so an unreachable provider can set the retry time and the
    notify_providers_exhausted message. `available` has to mean "work can
    actually run here", and without the binary it cannot.

    The dispatcher import is deliberately lazy: dispatcher imports limits at
    module level, so a top-level import here would be a cycle. Same pattern
    limits.py already uses for quota_state/quota_calibration/notifier.
    """
    from dispatcher import get_provider_by_name  # lazy: see docstring

    if get_provider_by_name("opencode") is None:
        # Deliberately NOT a fetch: no binary means no run, so the key's balance
        # is irrelevant -- and this is what keeps machines without opencode from
        # paying an HTTPS round-trip on every single limits refresh.
        return ProviderLimits(available=False, remaining_pct=0.0, error="not_registered")

    limit, remaining, reset_cadence = openrouter_budget.fetch_budget()

    if remaining is None:
        # fetch_budget() collapses EVERY non-success outcome to (None, None,
        # None) by design (see its docstring) — an outright failed query
        # (network down, invalid key, malformed JSON) and a structurally
        # valid response reporting no configured cap are NOT distinguishable
        # from this triple alone. "budget_unavailable" covers both.
        return ProviderLimits(available=False, remaining_pct=0.0, error="budget_unavailable")

    if limit is None:
        # Reachable only if fetch_budget() is ever extended to report
        # limit_remaining without limit (it does not today - both collapse
        # together, see above) - kept as an explicit, distinct branch/message
        # because "the key has no spending cap" and "the query failed" are
        # different operational situations even though today's fetch_budget()
        # contract cannot yet tell them apart.
        return ProviderLimits(available=False, remaining_pct=0.0, error="uncapped_key")

    try:
        remaining_pct = (remaining / limit) * 100.0
    except ZeroDivisionError:
        remaining_pct = 0.0

    min_remaining_usd = config.OPENCODE_MIN_REMAINING_USD
    available = remaining > min_remaining_usd

    reset_at_epoch = _opencode_reset_epoch(reset_cadence)
    resets_in_sec = max(0, int(reset_at_epoch - time.time())) if reset_at_epoch > 0 else 0

    return ProviderLimits(
        available=available,
        remaining_pct=remaining_pct,
        resets_in_sec=resets_in_sec,
        reset_at_epoch=reset_at_epoch,
        error="" if available else f"below OPENCODE_MIN_REMAINING_USD (${remaining:.2f} <= ${min_remaining_usd:.2f})",
    )


def _apply_opencode_budget_override(result: AllLimits) -> AllLimits:
    """Fill AllLimits.opencode from a live OpenRouter budget check.

    Same override pattern as _apply_gemini_http_override() above, but there is
    no cclimits-derived opencode data to replace — opencode isn't polled by
    cclimits at all (see the comments on _providers_with_429() and
    _apply_429_fallback() below) — so this always runs, unconditionally,
    unlike the Gemini override which only fires when a key is configured.
    """
    result.opencode = _opencode_budget_snapshot()
    return result


def _parse_codex(data: dict) -> ProviderLimits:
    return _parse_dual_window_provider(
        data,
        ("primary_window", "secondary_window"),
        {"primary_window": CODEX_PRIMARY_MIN_CAPACITY_PCT, "secondary_window": CODEX_SECONDARY_MIN_CAPACITY_PCT},
    )


def _is_provider_429(provider_data: dict) -> bool:
    """Check if a provider's cclimits data indicates HTTP 429 rate limiting."""
    error = str(provider_data.get("error", ""))
    details = str(provider_data.get("details", ""))
    return "429" in error or "429" in details


def _providers_with_429(raw: dict) -> set[str]:
    """Return set of provider names that have 429 errors in cclimits output.

    opencode is deliberately NOT in the ("claude", "gemini", "codex") tuple
    below — this whole function operates on cclimits' raw JSON, and opencode
    was never a cclimits provider (it has no OAuth quota to poll; its capacity
    comes from openrouter_budget.fetch_budget() instead, applied separately by
    _apply_opencode_budget_override()). Omission is intentional, not an
    oversight — a bare rate_limit/HTTP 429 from the opencode CLI itself is
    handled by the provider's own error classification, same as Codex/Vibe.
    """
    p429 = {
        name for name in ("claude", "gemini", "codex")
        if _is_provider_429(raw.get(name, {}))
    }
    # Gemini in HTTP-API mode doesn't use cclimits, so a cclimits 429 for it is
    # irrelevant — must not trigger retry sleeps, _apply_429_fallback, 429 state
    # or Telegram notifications. The snapshot is overridden at return anyway.
    if config.GEMINI_API_KEY:
        p429.discard("gemini")
    return p429


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


# Per-provider calibrated tokens-per-pct windows. Phase-1: only Claude is
# calibrated (Phase-0, 2026-05-27). Providers absent here fall back to the
# reset-time heuristic in _estimate_window_usage. Phase-2: the Claude factors
# may be updated at runtime by auto-recalibration (set_calibrated_windows);
# guarded by a lock so reads/writes are consistent across threads.
_active_calibration_lock = threading.Lock()
_active_calibrated_windows: "dict[str, dict[str, int]]" = {
    "claude": dict(ESTIMATE_TOKENS_PER_PCT_CLAUDE_WINDOWS),
}


def _get_calibrated_windows(provider_name: str) -> "dict[str, int] | None":
    """Snapshot of the (possibly recalibrated) per-window factors, or None."""
    with _active_calibration_lock:
        windows = _active_calibrated_windows.get(provider_name)
        return dict(windows) if windows else None


def set_calibrated_windows(provider_name: str, windows: "dict[str, int]") -> None:
    """Replace the live per-window tokens-per-pct factors (Phase-2 auto-recal)."""
    with _active_calibration_lock:
        _active_calibrated_windows[provider_name] = dict(windows)


_last_recalibration_date = None


def _maybe_recalibrate() -> None:
    """Phase-2 (b): once per day, refresh the live Claude tokens-per-pct factors
    from the running calibration CSV (drift correction). Flag-gated, day-cached,
    guarded (min samples + clamp inside ``recalibrate_claude_factors``). Never
    raises into the bg refresh loop."""
    global _last_recalibration_date
    try:
        from config import QUOTA_AUTO_RECALIBRATE_ENABLED
        if not (QUOTA_LIVE_ESTIMATE_ENABLED and QUOTA_AUTO_RECALIBRATE_ENABLED):
            return
        import datetime as _dt
        today = _dt.date.today()
        if _last_recalibration_date == today:
            return
        _last_recalibration_date = today
        from config import (
            ESTIMATE_TOKENS_PER_PCT_CLAUDE_WINDOWS,
            QUOTA_CALIBRATION_LOG_FILE,
            QUOTA_RECALIBRATE_CLAMP,
            QUOTA_RECALIBRATE_MIN_SAMPLES,
            QUOTA_RECALIBRATE_PERCENTILE,
        )
        from quota_calibration import recalibrate_claude_factors
        new_factors = recalibrate_claude_factors(
            QUOTA_CALIBRATION_LOG_FILE,
            ESTIMATE_TOKENS_PER_PCT_CLAUDE_WINDOWS,
            min_samples=QUOTA_RECALIBRATE_MIN_SAMPLES,
            clamp=QUOTA_RECALIBRATE_CLAMP,
            percentile=QUOTA_RECALIBRATE_PERCENTILE,
        )
        if new_factors:
            set_calibrated_windows("claude", new_factors)
            logger.info("quota factors auto-recalibrated from CSV: %s", new_factors)
    except Exception:
        logger.debug("quota recalibration failed", exc_info=True)


def _estimate_window_usage_calibrated(
    provider_name: str,
    base: ProviderLimits,
    estimated_pct: float,
) -> dict[str, float]:
    """Split a headline usage estimate across windows using Phase-0 calibrated
    tokens-per-pct ratios.

    ``estimated_pct`` was produced by ``estimate_task_usage_pct`` with the scalar
    ``ESTIMATE_TOKENS_PER_PCT[provider]``, so the implied billable token count is
    ``estimated_pct * scalar``. Dividing that by each window's calibrated
    tokens-per-pct yields the true per-window usage — and because the same scalar
    multiplies then divides, its exact value cancels and does not affect
    correctness (it stays a back-compat headline reference).

    Windows present on ``base`` but absent from the calibration table keep the
    headline ``estimated_pct`` (conservative). Providers without calibration fall
    back entirely to the reset-time heuristic.
    """
    calibration = _get_calibrated_windows(provider_name)
    scalar = ESTIMATE_TOKENS_PER_PCT.get(provider_name, 0)
    if not calibration or not base.windows or scalar <= 0:
        return _estimate_window_usage(base, estimated_pct)

    pct = max(0.0, float(estimated_pct))
    effective_tokens = pct * scalar
    usage: dict[str, float] = {}
    for name in base.windows:
        tpp = calibration.get(name)
        usage[name] = effective_tokens / tpp if tpp and tpp > 0 else pct
    return usage


# ── Phase-2: live between-poll usage estimate (Closed-Loop-Rebalancing) ───────
# Estimated usage (%) accumulated since the last successful cclimits poll, per
# provider/window. Applied at serve time (get_limits / _effective_provider) and
# reset on each fresh poll (re-anchor). Distinct from _429_estimated_usage (429
# fallback, applied at poll time). Only active when QUOTA_LIVE_ESTIMATE_ENABLED.
_live_estimate_lock = threading.Lock()
_live_estimated_usage: "dict[str, dict[str, float]]" = {}


def _reset_live_estimate() -> None:
    """Re-anchor: drop the between-poll estimate (a fresh cclimits read already
    reflects that consumption)."""
    with _live_estimate_lock:
        _live_estimated_usage.clear()


def _subtract_usage(base: ProviderLimits, usage: "dict[str, float]") -> ProviderLimits:
    """Copy of *base* with per-window remaining_pct reduced by *usage* (percent),
    recomputing the aggregate remaining_pct and availability. Reset times and
    error string are preserved (this is not a 429 fallback)."""
    usage_by_window = _normalize_estimated_usage(base, usage)
    adjusted_windows: dict[str, WindowData] = {}
    for wname, wdata in base.windows.items():
        used = usage_by_window.get(wname, 0.0)
        adjusted_windows[wname] = WindowData(
            remaining_pct=max(0.0, wdata.remaining_pct - used),
            resets_in_sec=wdata.resets_in_sec,
        )
    if adjusted_windows:
        new_remaining = _aggregate_remaining_pct(base, adjusted_windows)
    else:
        new_remaining = max(0.0, base.remaining_pct - usage_by_window.get("__provider__", 0.0))
    return ProviderLimits(
        available=new_remaining >= MIN_CAPACITY_PERCENT,
        remaining_pct=new_remaining,
        resets_in_sec=base.resets_in_sec,
        reset_at_epoch=base.reset_at_epoch,
        error=base.error,
        windows=adjusted_windows,
    )


def _apply_live_estimate_to_provider(
    provider_name: str, pl: "ProviderLimits | None",
) -> "ProviderLimits | None":
    """Apply the accumulated live estimate to one provider (no-op when the flag
    is off, the snapshot is None, or nothing has accumulated)."""
    if pl is None or not QUOTA_LIVE_ESTIMATE_ENABLED:
        return pl
    with _live_estimate_lock:
        usage = dict(_live_estimated_usage.get(provider_name, {}))
    return _subtract_usage(pl, usage) if usage else pl


def _apply_live_estimate(all_limits: AllLimits) -> AllLimits:
    """Apply the live between-poll estimate to every provider (serve-time)."""
    if not QUOTA_LIVE_ESTIMATE_ENABLED:
        return all_limits
    with _live_estimate_lock:
        any_usage = bool(_live_estimated_usage)
    if not any_usage:
        return all_limits
    return AllLimits(
        claude=_apply_live_estimate_to_provider("claude", all_limits.claude),
        gemini=_apply_live_estimate_to_provider("gemini", all_limits.gemini),
        codex=_apply_live_estimate_to_provider("codex", all_limits.codex),
    )


def _write_live_quota_state() -> None:
    """Rewrite the SoTH file with the live-adjusted snapshot so the statusline
    reflects between-poll consumption. Best-effort, never raises."""
    try:
        with _limits_cache_lock:
            cached = _limits_cache
        if cached is None:
            return
        from config import CC_QUOTA_STATE_FILE
        from quota_state import write_quota_state
        write_quota_state(
            _apply_live_estimate(cached[0]), CC_QUOTA_STATE_FILE,
            claude_factors=_get_calibrated_windows("claude"),
        )
    except Exception:
        logger.debug("live quota-state write failed", exc_info=True)


def _accumulate_live_estimate(provider_name: str, estimated_pct: float) -> None:
    """Add one task's calibrated per-window usage to the live estimate. The base
    (anchor) is the current cached cclimits snapshot."""
    base_pl = _get_cached_provider(provider_name)
    if base_pl is None or not (base_pl.available or base_pl.windows):
        return
    usage_delta = _estimate_window_usage_calibrated(provider_name, base_pl, estimated_pct)
    with _live_estimate_lock:
        current = _live_estimated_usage.get(provider_name, {})
        for key, pct in usage_delta.items():
            current[key] = round(current.get(key, 0.0) + pct, 2)
        _live_estimated_usage[provider_name] = current
    _write_live_quota_state()


def report_estimated_usage(provider_name: str, estimated_pct: float) -> None:
    """Track estimated capacity consumption between real cclimits readings.

    Two modes:
    - **HTTP 429 fallback** (provider has a 429 snapshot): accumulate into
      ``_429_estimated_usage``, applied at the next poll via the 429 fallback.
    - **Normal operation** (Phase-2, flag-gated): accumulate into the live
      between-poll estimate, applied at serve time and re-anchored each poll.
    Called by the orchestrator after each task.
    """
    with _429_estimate_lock:
        if provider_name in _429_snapshots:
            base_pl, _ = _429_snapshots[provider_name]
            if not (base_pl.available or base_pl.windows):
                return
            usage_delta = _estimate_window_usage_calibrated(
                provider_name, base_pl, estimated_pct,
            )
            current_usage = _normalize_estimated_usage(
                base_pl, _429_estimated_usage.get(provider_name, {}),
            )
            for key, pct in usage_delta.items():
                current_usage[key] = round(current_usage.get(key, 0.0) + pct, 2)
            _429_estimated_usage[provider_name] = current_usage
            return
    # Not in 429 fallback → Phase-2 live between-poll estimation (flag-gated).
    if QUOTA_LIVE_ESTIMATE_ENABLED:
        _accumulate_live_estimate(provider_name, estimated_pct)


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
    """Replace 429-error providers with cached + estimated data.

    opencode is deliberately NOT among the ("claude", "gemini", "codex")
    providers this function walks below — same reason as _providers_with_429()
    above: this machinery reconstructs cclimits-shaped snapshots, and opencode
    was never in cclimits' output to begin with. Its own budget-exhaustion
    handling lives entirely in _apply_opencode_budget_override(), which runs
    unconditionally after this function regardless of p429's contents.
    """
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
        # Use only input + output tokens.  cache_creation tokens are charged at 1.25×
        # for billing but are NOT counted against the 5-hour rate-limit quota at all.
        # cache_read tokens are counted at a much lower weight (~0.1×) and omitting
        # them causes only slight under-reporting — far better than the 100-1000×
        # over-reporting that total_tokens produced when cache tokens dominated.
        tokens_used = active.token_counts.input_tokens + active.token_counts.output_tokens
        logger.debug(
            "  [claude] local JSONL: input=%d output=%d cache_creation=%d cache_read=%d"
            " → tokens_used=%d / limit=%d",
            active.token_counts.input_tokens,
            active.token_counts.output_tokens,
            active.token_counts.cache_creation_tokens,
            active.token_counts.cache_read_tokens,
            tokens_used,
            token_limit,
        )
        remaining_pct = max(0.0, (1.0 - tokens_used / token_limit) * 100)
        now = _dt.datetime.now(_dt.UTC)
        end = active.end_time
        if end.tzinfo is None:
            end = end.replace(tzinfo=_dt.UTC)
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
    # Gemini in HTTP-API mode (GEMINI_API_KEY set) has no CLI/OAuth token to
    # refresh — the consumer endpoint cclimits reads is dead. Never refresh it.
    if provider == "gemini" and config.GEMINI_API_KEY:
        return False
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


def _get_limits_fresh(on_preliminary=None, force_fresh=False) -> AllLimits:
    """Actually run cclimits and parse the result.

    on_preliminary: optional callback(AllLimits) called with the pre-refresh
    result so callers see accurate data ("token expired") instead of blocking
    for the full refresh cycle.

    force_fresh: bypass the cclimits disk cache on the initial call (used by
    force_refresh and after known failures).
    """
    with _fresh_limits_lock:
        raw = _run_cclimits_with_timeout(
            _CCLIMITS_TIMEOUT_SEC, use_cache=not force_fresh,
        )
        if raw is None:
            return _apply_opencode_budget_override(_apply_gemini_http_override(AllLimits(
                claude=ProviderLimits(error="cclimits timeout"),
                gemini=ProviderLimits(error="cclimits timeout"),
                codex=ProviderLimits(error="cclimits timeout"),
            )))

        # Auto-refresh expired tokens and re-query
        refresh_attempted = False
        needs_refresh = any(
            _needs_token_refresh(raw, p) for p in _TOKEN_REFRESH_PROVIDERS
        )

        # Publish preliminary result before the slow token refresh so that
        # get_limits() callers don't time out waiting for _cache_ready.
        if needs_refresh and on_preliminary is not None:
            # NOTE: this adds an opencode HTTP round-trip (<=10s, fail-closed)
            # to the preliminary-publish path, which exists specifically to
            # avoid making callers wait for the slow Claude token refresh.
            # Kept anyway, at the same place as the Gemini override, per the
            # explicit instruction this override follows — a stale/slow
            # opencode budget reading here is corrected on the next poll,
            # same as every other field in this preliminary snapshot.
            preliminary = _apply_opencode_budget_override(_apply_gemini_http_override(AllLimits(
                claude=_parse_claude(raw.get("claude", {"status": "missing"})),
                gemini=_parse_gemini(raw.get("gemini", {"status": "missing"})),
                codex=_parse_codex(raw.get("codex", {"status": "missing"})),
            )))
            on_preliminary(preliminary)

        now = time.monotonic()
        for provider in _TOKEN_REFRESH_PROVIDERS:
            if _needs_token_refresh(raw, provider):
                cooldown_until = _refresh_failed_until.get(provider, 0.0)
                if now < cooldown_until:
                    remaining = int(cooldown_until - now)
                    logger.info(
                        "  [%s] Token expired — refresh on cooldown (%ds remaining), skipping",
                        provider, remaining,
                    )
                    continue
                refresh_attempted = True
                logger.info("  [%s] Token expired → refreshing...", provider)
                if _refresh_token(provider):
                    _refresh_failed_until.pop(provider, None)
                else:
                    _refresh_failed_until[provider] = now + _REFRESH_FAILED_BACKOFF_SEC
                    logger.error("  [%s] Token-Refresh fehlgeschlagen — Provider wird als unavailable gemeldet", provider)

        if refresh_attempted:
            # Re-query after any refresh attempt.  MUST bypass disk cache —
            # the first cclimits call wrote the "expired" result into the cache
            # and --cache-ttl would just re-read that stale entry.
            for retry_i in range(3):
                fresh = _run_cclimits_with_timeout(
                    _CCLIMITS_TIMEOUT_SEC, use_cache=False,
                )
                if fresh is not None:
                    raw = fresh
                    if not any(_needs_token_refresh(raw, p) for p in _TOKEN_REFRESH_PROVIDERS):
                        break
                time.sleep(2)

            # Guard against false-positive refreshes: if a provider's CLI reported
            # success but cclimits still shows the token as expired, treat it as a
            # failed refresh and activate the backoff cooldown.
            post_now = time.monotonic()
            for provider in _TOKEN_REFRESH_PROVIDERS:
                if _needs_token_refresh(raw, provider) and provider not in _refresh_failed_until:
                    _refresh_failed_until[provider] = post_now + _REFRESH_FAILED_BACKOFF_SEC
                    logger.warning(
                        "  [%s] Token still expired after refresh → cooldown %ds",
                        provider, _REFRESH_FAILED_BACKOFF_SEC,
                    )

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

        # Gemini HTTP-API mode bypasses cclimits entirely (applied last so neither
        # the 429 fallback nor _clear_429_state can clobber the synthetic snapshot).
        # opencode's live budget check is the same kind of bypass (see
        # _apply_opencode_budget_override()'s docstring) and is applied last for
        # the identical reason — nothing above this line touches AllLimits.opencode.
        return _apply_opencode_budget_override(_apply_gemini_http_override(result))


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
        # When queue is empty there's no reason to poll aggressively — save
        # the monitoring API calls and reduce 429 risk.
        if _queue_idle.is_set():
            return _BG_POLL_IDLE_SEC
        return _BG_POLL_AVAILABLE_SEC
    # At limit: wait until the earliest reset; no point hammering cclimits sooner.
    return max(60, min(result.earliest_reset_sec(), 3600))


def _bg_refresh_loop() -> None:
    """Daemon: keeps _limits_cache fresh so get_limits() never blocks."""
    global _limits_cache
    backoff = _BG_POLL_ERROR_SEC
    while True:
        try:
            # Clear the wake event at loop start so a concurrent force_refresh()
            # that happens during refresh/scheduling is still observed by wait().
            _bg_wake.clear()

            # Skip refreshes while the orchestrator is paused. Wake-up events
            # (set_paused(False) / force_refresh) still interrupt the wait so the
            # next real fetch happens immediately on resume.
            if _paused.is_set():
                _bg_wake.wait(timeout=_BG_POLL_IDLE_SEC)
                continue

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
                def _publish_preliminary(preliminary: AllLimits) -> None:
                    """Cache pre-refresh result so get_limits() doesn't time out."""
                    global _limits_cache
                    with _limits_cache_lock:
                        _limits_cache = (preliminary, time.monotonic())
                    _cache_ready.set()

                result = _get_limits_fresh(on_preliminary=_publish_preliminary)
                with _limits_cache_lock:
                    _limits_cache = (result, time.monotonic())
                _cache_ready.set()
                if _is_timeout_snapshot(result) or _is_transient_error_snapshot(result):
                    sleep_sec = backoff
                    backoff = min(backoff * 3, _BG_POLL_AVAILABLE_SEC)
                else:
                    backoff = _BG_POLL_ERROR_SEC
                    sleep_sec = _compute_next_poll_sec(result)
                    # Phase-0 telemetry: pair real cclimits utilization with
                    # JSONL token counts so we can later validate the
                    # tokens_per_pct calibration. Telemetry-only — must never
                    # break the refresh loop. Uses the async variant so the
                    # multi-second JSONL load runs on a separate worker thread
                    # and does NOT delay the next _bg_wake.wait() cycle.
                    try:
                        from config import QUOTA_CALIBRATION_LOG_FILE
                        from quota_calibration import log_calibration_sample_async
                        log_calibration_sample_async(
                            result, QUOTA_CALIBRATION_LOG_FILE,
                            queue_idle=_queue_idle.is_set(),
                        )
                    except Exception:
                        logger.debug("calibration hook failed", exc_info=True)
                    # Phase-1 SoTH: persist the fresh per-window snapshot so the
                    # statusline / --check-limits can read real 5h/7d usage
                    # without re-polling the rate-limited cclimits endpoint.
                    # Tiny atomic JSON write (no JSONL load) — synchronous is
                    # fine; never raises.
                    try:
                        from config import CC_QUOTA_STATE_FILE
                        from quota_state import write_quota_state
                        write_quota_state(
                            result, CC_QUOTA_STATE_FILE,
                            claude_factors=_get_calibrated_windows("claude"),
                        )
                    except Exception:
                        logger.debug("quota-state write hook failed", exc_info=True)
                    # Phase-2: this fresh reading already reflects consumption →
                    # re-anchor the live between-poll estimate; then (if enabled)
                    # refresh the calibrated factors from the running CSV.
                    _reset_live_estimate()
                    _maybe_recalibrate()

            _bg_wake.wait(timeout=sleep_sec)
        except Exception:
            logger.exception("limits bg refresh crashed — retrying in %ds", backoff)
            # Ensure _cache_ready is set even on crash so get_limits() callers
            # don't block forever waiting for the first result.
            _cache_ready.set()
            _bg_wake.wait(timeout=backoff)
            backoff = min(backoff * 2, _BG_POLL_AVAILABLE_SEC)


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
        result = _get_limits_fresh(force_fresh=True)
        with _limits_cache_lock:
            _limits_cache = (result, time.monotonic())
        _reset_live_estimate()   # fresh reading = re-anchor the live estimate
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
            return _apply_live_estimate(_limits_cache[0])
        # Deliberately NOT wrapped in _apply_opencode_budget_override() here,
        # unlike the three call sites inside _get_limits_fresh(): this branch
        # runs under _limits_cache_lock, and adding a (fail-closed, but still
        # up-to-10s) HTTP round-trip while holding that lock would stall every
        # other get_limits() caller during the one corner case this fallback
        # exists for (cclimits hung on the very first call). AllLimits.opencode
        # keeps its safe ProviderLimits() default (available=False) here, which
        # is the same fail-closed outcome the override would produce anyway.
        fallback = _apply_gemini_http_override(AllLimits(
            claude=ProviderLimits(error="cclimits unavailable"),
            gemini=ProviderLimits(error="cclimits unavailable"),
            codex=ProviderLimits(error="cclimits unavailable"),
        ))
        _limits_cache = (fallback, time.monotonic())
        _cache_ready.set()
        return fallback


def _get_cached_provider(provider_name: str) -> "ProviderLimits | None":
    """Return the cached ProviderLimits for *provider_name*, or None if cache is empty."""
    with _limits_cache_lock:
        cached = _limits_cache
    if cached is None:
        return None
    limits, _ = cached
    return getattr(limits, provider_name, None)


def get_cached_provider_pct(provider_name: str) -> float:
    """Return remaining_pct for *provider_name* from the in-memory cache.

    No API call is made.  Returns 100.0 (safe default) when the cache is empty
    so tools don't abort prematurely on first startup.
    """
    p = _apply_live_estimate_to_provider(provider_name, _get_cached_provider(provider_name))
    return p.remaining_pct if p is not None else 100.0


def is_cached_provider_available(provider_name: str) -> bool:
    """Return the ``available`` flag for *provider_name* from the in-memory cache.

    Unlike :func:`get_cached_provider_pct` (which returns the raw min-across-
    windows percentage), this function respects the **per-window** thresholds
    used by ``_parse_claude`` / ``_parse_codex``.  Tools should prefer this
    over comparing ``remaining_pct`` against ``MIN_CAPACITY_PERCENT``.

    Returns ``True`` (safe default) when the cache is empty so tools don't
    abort prematurely on first startup.
    """
    p = _apply_live_estimate_to_provider(provider_name, _get_cached_provider(provider_name))
    return p.available if p is not None else True


def set_paused(paused: bool) -> None:
    """Pause/resume the background cclimits-refresh thread.

    While paused, the bg thread skips all cclimits calls so we don't burn the
    monitoring API quota or trigger 429s while the user has explicitly paused
    the orchestrator. Resume wakes the thread immediately to refresh the cache
    before the next task runs.
    """
    if paused:
        _paused.set()
    else:
        was_paused = _paused.is_set()
        _paused.clear()
        if was_paused:
            _bg_wake.set()   # wake immediately so resume sees fresh limits


def set_queue_idle(idle: bool) -> None:
    """Signal whether the task queue is currently empty.

    When idle=True the background refresh thread uses a longer poll interval
    (_BG_POLL_IDLE_SEC = 10 min) instead of the default 5 min, matching the
    cclimits --cache-ttl so each idle poll hits the real API.

    When idle=False (task found) the thread is woken immediately so the next
    get_limits() call returns a fresh snapshot before the task starts.
    """
    if idle:
        _queue_idle.set()
    else:
        was_idle = _queue_idle.is_set()
        _queue_idle.clear()
        if was_idle:
            _bg_wake.set()   # wake thread immediately to refresh before task runs
