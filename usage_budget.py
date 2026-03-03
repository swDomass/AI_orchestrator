"""
AI Orchestrator — Usage Budget / Pace Analysis

Computes a pace factor for rolling usage windows (e.g. Claude 7-day window)
and provides formatting helpers for CLI and Telegram output.
"""

_PACE_THRESHOLDS = [
    (2.5, "critical"),
    (1.75, "high"),
    (1.2, "moderate"),
    (0.0, "ok"),
]

_STATUS_ICONS = {
    "ok": "✅",
    "moderate": "⚠️",
    "high": "🔶",
    "critical": "🔴",
}


def compute_window_pace(remaining_pct: float, resets_in_sec: int, window_days: int) -> dict:
    """Compute pace metrics for a rolling window.

    Args:
        remaining_pct: Percentage of quota remaining (0–100).
        resets_in_sec: Seconds until the window resets.
        window_days: Total duration of the window in days.

    Returns a dict with keys:
        days_elapsed, days_remaining, consumed_pct,
        daily_rate, target_daily_rate, pace_factor, status
    """
    window_total_sec = window_days * 86400
    elapsed_sec = max(0, window_total_sec - resets_in_sec)
    days_elapsed = elapsed_sec / 86400
    days_remaining = resets_in_sec / 86400
    consumed_pct = max(0.0, 100.0 - remaining_pct)
    target_daily_rate = 100.0 / window_days

    if days_elapsed < 0.1:
        return {
            "days_elapsed": days_elapsed,
            "days_remaining": days_remaining,
            "consumed_pct": consumed_pct,
            "daily_rate": 0.0,
            "target_daily_rate": target_daily_rate,
            "pace_factor": 0.0,
            "status": "ok",
        }

    daily_rate = consumed_pct / days_elapsed
    pace_factor = daily_rate / target_daily_rate if target_daily_rate > 0 else 0.0

    status = "ok"
    for threshold, label in _PACE_THRESHOLDS:
        if pace_factor >= threshold:
            status = label
            break

    return {
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "consumed_pct": consumed_pct,
        "daily_rate": daily_rate,
        "target_daily_rate": target_daily_rate,
        "pace_factor": pace_factor,
        "status": status,
    }


def format_pace_status(pace_info: dict) -> str:
    """Format pace info as a human-readable status line.

    Example: '✅ 7-Tage: 75% verbleibend, 3.4%/Tag (Ziel: 14.3%) — Pace: 0.2x'
    """
    status = pace_info.get("status", "ok")
    icon = _STATUS_ICONS.get(status, "ℹ️")
    remaining = 100.0 - pace_info.get("consumed_pct", 0.0)
    daily_rate = pace_info.get("daily_rate", 0.0)
    target = pace_info.get("target_daily_rate", 0.0)
    pace_factor = pace_info.get("pace_factor", 0.0)
    return (
        f"{icon} 7-Tage: {remaining:.0f}% verbleibend, "
        f"{daily_rate:.1f}%/Tag (Ziel: {target:.1f}%) — Pace: {pace_factor:.1f}x"
    )


def should_suppress_suggestions(pace_info: dict, max_pace_factor: float) -> bool:
    """Return True if suggestions should be suppressed due to over-pace consumption.

    Suppression is skipped when < 6h remain in the window (let it burn naturally).
    """
    if pace_info.get("days_remaining", 999) < 0.25:
        return False  # last 6h — don't suppress
    return pace_info.get("pace_factor", 0.0) > max_pace_factor
