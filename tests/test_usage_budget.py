"""Tests for usage_budget module."""

import pytest

import usage_budget as ub


class TestComputeWindowPace:
    def test_window_just_started_returns_ok(self):
        """When days_elapsed < 0.1, pace_factor=0 and status=ok."""
        # 7-day window, 6 days 23.9h remaining → only 0.004 days elapsed
        result = ub.compute_window_pace(
            remaining_pct=99.0,
            resets_in_sec=6 * 86400 + 23 * 3600 + 56 * 60,
            window_days=7,
        )
        assert result["pace_factor"] == 0.0
        assert result["status"] == "ok"
        assert result["days_elapsed"] < 0.1

    def test_window_on_pace(self):
        """14% consumed in 1 day of 7-day window → pace ~1.0 → ok."""
        # consumed_pct = 14.3%, days_elapsed = 1, target = 14.3%/day
        result = ub.compute_window_pace(
            remaining_pct=85.7,
            resets_in_sec=6 * 86400,
            window_days=7,
        )
        assert abs(result["pace_factor"] - 1.0) < 0.05
        assert result["status"] == "ok"

    def test_moderate_pace(self):
        """26% consumed in 1.4 days of 7-day → rate = 18.6%/day, target = 14.3%/day → factor = 1.3 → moderate."""
        # consumed = 26% in 1.4 days → rate = 18.6%/day, target = 14.3%/day → factor = 1.3
        result = ub.compute_window_pace(
            remaining_pct=74.0,
            resets_in_sec=int(5.6 * 86400),
            window_days=7,
        )
        assert result["pace_factor"] > 1.2
        assert result["status"] == "moderate"

    def test_high_pace(self):
        """50% consumed in 2 days of 7-day → 25%/day vs 14.3%/day → factor ~1.75 → high."""
        result = ub.compute_window_pace(
            remaining_pct=50.0,
            resets_in_sec=5 * 86400,
            window_days=7,
        )
        # consumed=50, elapsed=2, rate=25, target=14.3 → factor≈1.75
        assert abs(result["pace_factor"] - 1.75) < 0.05
        assert result["status"] == "high"

    def test_high_pace_near_critical(self):
        """85% consumed in 3 days → ~28%/day vs 14.3%/day → factor ~1.98 → high."""
        result = ub.compute_window_pace(
            remaining_pct=15.0,
            resets_in_sec=4 * 86400,
            window_days=7,
        )
        # consumed=85, elapsed=3, rate≈28.3, target=14.3 → factor≈1.98 → high
        assert abs(result["pace_factor"] - 1.98) < 0.05
        assert result["status"] == "high"

    def test_truly_critical_pace(self):
        """90% consumed in 2 days → 45%/day vs 14.3%/day → factor ~3.15 → critical."""
        result = ub.compute_window_pace(
            remaining_pct=10.0,
            resets_in_sec=5 * 86400,
            window_days=7,
        )
        # consumed=90, elapsed=2, rate=45, target=14.3 → factor≈3.15 → critical
        assert result["pace_factor"] >= 2.5
        assert result["status"] == "critical"

    def test_consumed_pct_cannot_be_negative(self):
        """remaining_pct > 100 should not produce negative consumed_pct."""
        result = ub.compute_window_pace(
            remaining_pct=105.0,
            resets_in_sec=5 * 86400,
            window_days=7,
        )
        assert result["consumed_pct"] >= 0.0

    def test_days_remaining_computed_correctly(self):
        result = ub.compute_window_pace(
            remaining_pct=50.0,
            resets_in_sec=3 * 86400,
            window_days=7,
        )
        assert abs(result["days_remaining"] - 3.0) < 0.01


class TestShouldSuppressSuggestions:
    def test_suppress_when_over_pace_and_time_remaining(self):
        pace_info = {"pace_factor": 2.5, "days_remaining": 3.0}
        assert ub.should_suppress_suggestions(pace_info, max_pace_factor=2.0) is True

    def test_no_suppress_at_end_of_window(self):
        """Even if over-pace, last 6h should not suppress."""
        pace_info = {"pace_factor": 3.0, "days_remaining": 0.1}
        assert ub.should_suppress_suggestions(pace_info, max_pace_factor=2.0) is False

    def test_no_suppress_when_pace_ok(self):
        pace_info = {"pace_factor": 1.1, "days_remaining": 3.0}
        assert ub.should_suppress_suggestions(pace_info, max_pace_factor=2.0) is False

    def test_no_suppress_when_pace_exactly_at_limit(self):
        """pace_factor == max_pace_factor is NOT suppressed (strictly greater than)."""
        pace_info = {"pace_factor": 2.0, "days_remaining": 3.0}
        assert ub.should_suppress_suggestions(pace_info, max_pace_factor=2.0) is False

    def test_suppress_boundary_days_remaining(self):
        """days_remaining = 0.25 is the boundary — exactly 6h remaining suppresses (< 0.25 exempts)."""
        pace_info = {"pace_factor": 3.0, "days_remaining": 0.25}
        assert ub.should_suppress_suggestions(pace_info, max_pace_factor=2.0) is True

    def test_suppress_just_above_boundary(self):
        """days_remaining = 0.26 → still suppresses if over-pace."""
        pace_info = {"pace_factor": 3.0, "days_remaining": 0.26}
        assert ub.should_suppress_suggestions(pace_info, max_pace_factor=2.0) is True


class TestFormatPaceStatus:
    def test_ok_status_format(self):
        pace_info = {
            "status": "ok",
            "consumed_pct": 25.0,
            "daily_rate": 3.4,
            "target_daily_rate": 14.3,
            "pace_factor": 0.24,
        }
        result = ub.format_pace_status(pace_info)
        assert "✅" in result
        assert "75%" in result   # 100 - 25 = 75
        assert "3.4%/Tag" in result
        assert "14.3%" in result
        assert "0.2x" in result

    def test_critical_status_icon(self):
        pace_info = {
            "status": "critical",
            "consumed_pct": 90.0,
            "daily_rate": 45.0,
            "target_daily_rate": 14.3,
            "pace_factor": 3.15,
        }
        result = ub.format_pace_status(pace_info)
        assert "🔴" in result
