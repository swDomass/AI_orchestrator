"""
Wrapper around `npx cclimits --json`.
Parses usage limits for Claude, Gemini (all 3 tiers), and Codex.
Auto-refreshes expired OAuth tokens before querying.
"""

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from config import MIN_CAPACITY_PERCENT

# On Windows, npm-installed CLIs are .cmd files
_CMD_SUFFIX = ".cmd" if sys.platform == "win32" else ""
NPX_CMD = f"npx{_CMD_SUFFIX}"
_CLAUDE_CMD = "claude.exe" if sys.platform == "win32" else "claude"
_GEMINI_CMD = f"gemini{_CMD_SUFFIX}"


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
        return any([self.claude.available, self.gemini.available, self.codex.available])


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


def _run_cclimits() -> dict | None:
    """Run npx cclimits --json and return parsed dict, or None on failure."""
    try:
        result = subprocess.run(
            [NPX_CMD, "cclimits", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        return None


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
            )
            combined_out = f"{r.stdout or ''}\n{r.stderr or ''}".lower()
            if r.returncode == 0 and "expired" not in combined_out:
                # Token is genuinely valid, no refresh needed
                return True
            print(f"  [claude] auth status: token ist expired (rc={r.returncode})")

            # Strategy 2: actual API call forces OAuth token refresh
            print("  [claude] Versuche Token-Refresh via claude --print ...")
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
            print(f"  [claude] Token-Refresh fehlgeschlagen (rc={r2.returncode}): {stderr2[:200]}")
            print("  [claude] ⚠ Manuelles 'claude' in der CLI nötig um Token zu erneuern!")
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


def get_limits() -> AllLimits:
    """Run npx cclimits --json and return parsed limits for all providers.
    Auto-refreshes expired OAuth tokens for Claude/Gemini and retries."""
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
            print(f"  [{provider}] Token expired → refreshing...")
            if not _refresh_token(provider):
                print(f"  [{provider}] Token-Refresh fehlgeschlagen — Provider wird als unavailable gemeldet")

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

    return AllLimits(
        claude=_parse_claude(raw.get("claude", {"status": "missing"})),
        gemini=_parse_gemini(raw.get("gemini", {"status": "missing"})),
        codex=_parse_codex(raw.get("codex", {"status": "missing"})),
    )
