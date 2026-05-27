"""Tests for Phase-2 live between-poll quota estimation + auto-recalibration.

(a) limits._live_estimated_usage / _apply_live_estimate / report routing /
    re-anchor; (b) quota_calibration.recalibrate_claude_factors +
    limits.set_calibrated_windows / _maybe_recalibrate.

All behaviour is flag-gated (QUOTA_LIVE_ESTIMATE_ENABLED, default OFF) — the
first test pins the no-op default.
"""

import csv
import time

import pytest

import limits
import quota_calibration
import quota_state


def _claude_limits(rem5h=80.0, rem7d=80.0):
    return limits.AllLimits(
        claude=limits.ProviderLimits(
            available=True, remaining_pct=min(rem5h, rem7d), resets_in_sec=3600,
            windows={
                "five_hour": limits.WindowData(remaining_pct=rem5h, resets_in_sec=1200),
                "seven_day": limits.WindowData(remaining_pct=rem7d, resets_in_sec=3600),
            },
        ),
        gemini=limits.ProviderLimits(available=True, remaining_pct=99.0),
        codex=limits.ProviderLimits(available=True, remaining_pct=90.0),
    )


@pytest.fixture
def live_env(monkeypatch):
    """Hermetic state: empty 429 + live accumulators, a claude cache, default
    calibration factors, and a no-op SoTH write (no real file I/O)."""
    monkeypatch.setattr(limits, "_429_snapshots", {})
    monkeypatch.setattr(limits, "_429_estimated_usage", {})
    monkeypatch.setattr(limits, "_live_estimated_usage", {})
    monkeypatch.setattr(limits, "_limits_cache", (_claude_limits(), time.monotonic()))
    monkeypatch.setattr(
        limits, "_active_calibrated_windows",
        {"claude": dict(limits.ESTIMATE_TOKENS_PER_PCT_CLAUDE_WINDOWS)},
    )
    monkeypatch.setattr(quota_state, "write_quota_state", lambda *a, **k: True)
    return monkeypatch


# ───────────────────────── (a) live between-poll estimate ─────────────────────


def test_report_is_noop_when_flag_off(live_env):
    live_env.setattr(limits, "QUOTA_LIVE_ESTIMATE_ENABLED", False)
    limits.report_estimated_usage("claude", 5.0)
    assert limits._live_estimated_usage == {}
    assert limits.get_cached_provider_pct("claude") == 80.0
    assert limits.is_cached_provider_available("claude") is True


def test_live_estimate_accumulates_and_is_applied(live_env):
    live_env.setattr(limits, "QUOTA_LIVE_ESTIMATE_ENABLED", True)
    limits.report_estimated_usage("claude", 5.0)

    scalar = limits.ESTIMATE_TOKENS_PER_PCT["claude"]
    cal = limits._get_calibrated_windows("claude")
    acc = limits._live_estimated_usage["claude"]
    assert abs(acc["five_hour"] - round(5.0 * scalar / cal["five_hour"], 2)) < 0.05
    assert abs(acc["seven_day"] - round(5.0 * scalar / cal["seven_day"], 2)) < 0.05
    # 5h is the binding (min) window for claude → served pct drops by the 5h usage
    assert abs(limits.get_cached_provider_pct("claude") - (80.0 - acc["five_hour"])) < 0.1


def test_live_estimate_accumulates_across_tasks(live_env):
    live_env.setattr(limits, "QUOTA_LIVE_ESTIMATE_ENABLED", True)
    limits.report_estimated_usage("claude", 3.0)
    first = limits._live_estimated_usage["claude"]["five_hour"]
    limits.report_estimated_usage("claude", 3.0)
    second = limits._live_estimated_usage["claude"]["five_hour"]
    assert abs(second - 2 * first) < 0.05


def test_reset_live_estimate_reanchors(live_env):
    live_env.setattr(limits, "QUOTA_LIVE_ESTIMATE_ENABLED", True)
    limits.report_estimated_usage("claude", 5.0)
    assert limits.get_cached_provider_pct("claude") < 80.0
    limits._reset_live_estimate()
    assert limits._live_estimated_usage == {}
    assert limits.get_cached_provider_pct("claude") == 80.0


def test_429_mode_takes_precedence_over_live(live_env):
    live_env.setattr(limits, "QUOTA_LIVE_ESTIMATE_ENABLED", True)
    base_pl = limits.ProviderLimits(
        available=True, remaining_pct=80.0,
        windows={"five_hour": limits.WindowData(remaining_pct=80.0, resets_in_sec=3600)},
    )
    live_env.setattr(limits, "_429_snapshots", {"claude": (base_pl, time.monotonic())})

    limits.report_estimated_usage("claude", 5.0)
    assert "claude" in limits._429_estimated_usage   # went to the 429 path
    assert limits._live_estimated_usage == {}         # NOT the live path


def test_live_estimate_can_flip_availability(live_env):
    live_env.setattr(limits, "QUOTA_LIVE_ESTIMATE_ENABLED", True)
    live_env.setattr(limits, "_limits_cache", (_claude_limits(rem5h=12.0, rem7d=50.0), time.monotonic()))
    assert limits.is_cached_provider_available("claude") is True   # 12% >= MIN_CAPACITY_PERCENT
    # a task burning ~5.6% of the 5h window pushes remaining below the 10% gate
    limits.report_estimated_usage("claude", 2.0)
    assert limits.is_cached_provider_available("claude") is False


def test_apply_live_estimate_is_noop_when_empty(live_env):
    live_env.setattr(limits, "QUOTA_LIVE_ESTIMATE_ENABLED", True)
    base = _claude_limits()
    assert limits._apply_live_estimate(base) is base   # same object, no copy


def test_write_live_quota_state_reflects_estimate(live_env, monkeypatch):
    captured = {}
    monkeypatch.setattr(quota_state, "write_quota_state",
                        lambda al, path: captured.update(al=al) or True)
    monkeypatch.setattr(limits, "QUOTA_LIVE_ESTIMATE_ENABLED", True)
    limits.report_estimated_usage("claude", 5.0)
    assert captured["al"].claude.windows["five_hour"].remaining_pct < 80.0


# ───────────────────────── (b) auto-recalibration ─────────────────────────────


def _write_calib_csv(path, n_per_window, tpp_5h, tpp_7d, *, flagged=False):
    flag = "true" if flagged else "false"
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=quota_calibration.CSV_FIELDS)
        w.writeheader()
        for window, tpp in (("five_hour", tpp_5h), ("seven_day", tpp_7d)):
            for _ in range(n_per_window):
                row = {k: "" for k in quota_calibration.CSV_FIELDS}
                row.update({
                    "schema_version": "2", "window": window,
                    "tokens_per_pct_io_only": f"{tpp}",
                    "flag_rolling_fallback": flag, "flag_low_pct": flag,
                    "flag_cm_unavailable": flag,
                })
                w.writerow(row)


_DEF = {"five_hour": 5400, "seven_day": 75000}


def test_recalibrate_returns_none_below_min_samples(tmp_path):
    csv_path = tmp_path / "c.csv"
    _write_calib_csv(csv_path, n_per_window=10, tpp_5h=6000, tpp_7d=80000)
    assert quota_calibration.recalibrate_claude_factors(
        csv_path, _DEF, min_samples=60, clamp=3.0) is None


def test_recalibrate_returns_none_when_all_rows_flagged(tmp_path):
    csv_path = tmp_path / "c.csv"
    _write_calib_csv(csv_path, n_per_window=100, tpp_5h=6000, tpp_7d=80000, flagged=True)
    assert quota_calibration.recalibrate_claude_factors(
        csv_path, _DEF, min_samples=60, clamp=3.0) is None


def test_recalibrate_computes_percentile(tmp_path):
    csv_path = tmp_path / "c.csv"
    _write_calib_csv(csv_path, n_per_window=100, tpp_5h=6000, tpp_7d=80000)
    out = quota_calibration.recalibrate_claude_factors(
        csv_path, _DEF, min_samples=60, clamp=3.0, percentile=25.0)
    assert out == {"five_hour": 6000, "seven_day": 80000}   # constant column → percentile = value


def test_recalibrate_clamps_to_band(tmp_path):
    csv_path = tmp_path / "c.csv"
    _write_calib_csv(csv_path, n_per_window=100, tpp_5h=999999, tpp_7d=1)
    out = quota_calibration.recalibrate_claude_factors(
        csv_path, _DEF, min_samples=60, clamp=3.0)
    assert out["five_hour"] == int(round(5400 * 3.0))   # clamped high
    assert out["seven_day"] == int(round(75000 / 3.0))  # clamped low


def test_recalibrate_missing_file_returns_none(tmp_path):
    assert quota_calibration.recalibrate_claude_factors(
        tmp_path / "nope.csv", _DEF, min_samples=60, clamp=3.0) is None


def test_set_and_get_calibrated_windows_roundtrip(monkeypatch):
    monkeypatch.setattr(limits, "_active_calibrated_windows",
                        {"claude": dict(limits.ESTIMATE_TOKENS_PER_PCT_CLAUDE_WINDOWS)})
    limits.set_calibrated_windows("claude", {"five_hour": 10000, "seven_day": 100000})
    assert limits._get_calibrated_windows("claude") == {"five_hour": 10000, "seven_day": 100000}

    base = limits.ProviderLimits(available=True, remaining_pct=80.0, windows={
        "five_hour": limits.WindowData(remaining_pct=80.0, resets_in_sec=3600),
        "seven_day": limits.WindowData(remaining_pct=80.0, resets_in_sec=86400),
    })
    out = limits._estimate_window_usage_calibrated("claude", base, 10.0)
    scalar = limits.ESTIMATE_TOKENS_PER_PCT["claude"]
    assert abs(out["five_hour"] - 10.0 * scalar / 10000) < 1e-6
    assert abs(out["seven_day"] - 10.0 * scalar / 100000) < 1e-6


def test_maybe_recalibrate_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(limits, "QUOTA_LIVE_ESTIMATE_ENABLED", False)
    monkeypatch.setattr(limits, "_last_recalibration_date", None)
    before = limits._get_calibrated_windows("claude")
    limits._maybe_recalibrate()   # must not raise
    assert limits._get_calibrated_windows("claude") == before
    assert limits._last_recalibration_date is None   # returned before touching the day-cache
