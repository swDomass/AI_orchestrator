"""
Wrapper around `npx cclimits --json`.
Parses usage limits for Claude, Gemini (all 3 tiers), and Codex.
"""

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

# On Windows, npm-installed CLIs are .cmd files
_CMD_SUFFIX = ".cmd" if sys.platform == "win32" else ""
NPX_CMD = f"npx{_CMD_SUFFIX}"


@dataclass
class ProviderLimits:
    available: bool = False       # Has any usable capacity
    remaining_pct: float = 0.0   # Lowest remaining % across all tiers
    resets_in_sec: int = 0        # Seconds until earliest reset
    error: str = ""               # Error message if unavailable


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
        return ProviderLimits(error=data.get("error", "unknown"))

    windows = []
    for key in ("five_hour", "seven_day"):
        w = data.get(key, {})
        if "remaining" in w:
            windows.append((_parse_percent(w["remaining"]), _parse_resets_in(w.get("resets_in", ""))))

    if not windows:
        return ProviderLimits(error="no window data")

    remaining = min(r for r, _ in windows)
    resets_in = min(t for _, t in windows if t > 0) if any(t > 0 for _, t in windows) else 0

    return ProviderLimits(
        available=remaining > 5,
        remaining_pct=remaining,
        resets_in_sec=resets_in,
    )


def _parse_gemini(data: dict) -> ProviderLimits:
    if data.get("status") != "ok":
        return ProviderLimits(error=data.get("error", "unknown"))

    # All three tiers: 3-Flash, Flash, Pro (let Gemini CLI decide which to use)
    models = data.get("models", {})
    if not models:
        return ProviderLimits(error="no model data")

    tier_remaining = []
    tier_resets = []
    for model_data in models.values():
        r = _parse_percent(model_data.get("remaining", "0%"))
        t = _parse_resets_in(model_data.get("resets_in", ""))
        tier_remaining.append(r)
        if t > 0:
            tier_resets.append(t)

    # Available if ANY tier has capacity (Gemini CLI picks internally)
    max_remaining = max(tier_remaining) if tier_remaining else 0
    min_reset = min(tier_resets) if tier_resets else 0

    return ProviderLimits(
        available=max_remaining > 5,
        remaining_pct=max_remaining,
        resets_in_sec=min_reset,
    )


def _parse_codex(data: dict) -> ProviderLimits:
    if data.get("status") != "ok":
        return ProviderLimits(error=data.get("error", "unknown"))

    windows = []
    for key in ("primary_window", "secondary_window"):
        w = data.get(key, {})
        if "remaining" in w:
            windows.append((_parse_percent(w["remaining"]), _parse_resets_in(w.get("resets_in", ""))))

    if not windows:
        return ProviderLimits(error="no window data")

    remaining = min(r for r, _ in windows)
    resets_in = min(t for _, t in windows if t > 0) if any(t > 0 for _, t in windows) else 0

    return ProviderLimits(
        available=remaining > 5,
        remaining_pct=remaining,
        resets_in_sec=resets_in,
    )


def get_limits() -> AllLimits:
    """Run npx cclimits --json and return parsed limits for all providers."""
    try:
        result = subprocess.run(
            [NPX_CMD, "cclimits", "--json"],
            capture_output=True, text=True, timeout=30
        )
        raw = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return AllLimits(
            claude=ProviderLimits(error="cclimits timeout"),
            gemini=ProviderLimits(error="cclimits timeout"),
            codex=ProviderLimits(error="cclimits timeout"),
        )
    except (json.JSONDecodeError, Exception) as e:
        return AllLimits(
            claude=ProviderLimits(error=str(e)),
            gemini=ProviderLimits(error=str(e)),
            codex=ProviderLimits(error=str(e)),
        )

    return AllLimits(
        claude=_parse_claude(raw.get("claude", {"status": "missing"})),
        gemini=_parse_gemini(raw.get("gemini", {"status": "missing"})),
        codex=_parse_codex(raw.get("codex", {"status": "missing"})),
    )
