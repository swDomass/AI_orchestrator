"""Tests for Phase-2 live between-poll quota estimation + auto-recalibration.

(a) limits._live_estimated_usage / _apply_live_estimate / report routing /
    re-anchor; (b) quota_calibration.recalibrate_claude_factors +
    limits.set_calibrated_windows / _maybe_recalibrate.

All behaviour is flag-gated (QUOTA_LIVE_ESTIMATE_ENABLED, default OFF) — the
first test pins the no-op default.
"""

import csv
import datetime as dt
import time

import pytest

import config
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
                        lambda al, path, **k: captured.update(al=al) or True)
    monkeypatch.setattr(limits, "QUOTA_LIVE_ESTIMATE_ENABLED", True)
    limits.report_estimated_usage("claude", 5.0)
    assert captured["al"].claude.windows["five_hour"].remaining_pct < 80.0


# ───────────────────────── (b) auto-recalibration ─────────────────────────────


def _write_calib_csv(path, n_per_window, tpp_5h, tpp_7d, *, flagged=False,
                      days_ago=0.0, mode="w"):
    """Write ``n_per_window`` rows per Claude window, timestamped ``days_ago`` days
    before "now" (default: fresh — always inside any positive window). `mode="a"`
    appends without rewriting the header, so a test can mix an old batch and a fresh
    batch in one CSV to prove the window filter, not just that IT EXISTS."""
    flag = "true" if flagged else "false"
    ts = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago)).isoformat(
        timespec="seconds"
    )
    write_header = mode == "w" or not path.exists()
    with open(path, mode, encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=quota_calibration.CSV_FIELDS)
        if write_header:
            w.writeheader()
        for window, tpp in (("five_hour", tpp_5h), ("seven_day", tpp_7d)):
            for _ in range(n_per_window):
                row = {k: "" for k in quota_calibration.CSV_FIELDS}
                row.update({
                    "schema_version": "2", "window": window,
                    "timestamp_utc": ts,
                    "tokens_per_pct_io_only": f"{tpp}",
                    "flag_rolling_fallback": flag, "flag_low_pct": flag,
                    "flag_cm_unavailable": flag,
                })
                w.writerow(row)


_DEF = {"five_hour": 5400, "seven_day": 75000}
_WINDOW = config.QUOTA_RECALIBRATE_WINDOW_DAYS  # 60 by default; same constant limits.py uses


def test_recalibrate_returns_none_below_min_samples(tmp_path):
    csv_path = tmp_path / "c.csv"
    _write_calib_csv(csv_path, n_per_window=10, tpp_5h=6000, tpp_7d=80000)
    assert quota_calibration.recalibrate_claude_factors(
        csv_path, _DEF, min_samples=60, clamp=3.0, window_days=_WINDOW) is None


def test_recalibrate_returns_none_when_all_rows_flagged(tmp_path):
    csv_path = tmp_path / "c.csv"
    _write_calib_csv(csv_path, n_per_window=100, tpp_5h=6000, tpp_7d=80000, flagged=True)
    assert quota_calibration.recalibrate_claude_factors(
        csv_path, _DEF, min_samples=60, clamp=3.0, window_days=_WINDOW) is None


def test_recalibrate_computes_percentile(tmp_path):
    csv_path = tmp_path / "c.csv"
    _write_calib_csv(csv_path, n_per_window=100, tpp_5h=6000, tpp_7d=80000)
    out = quota_calibration.recalibrate_claude_factors(
        csv_path, _DEF, min_samples=60, clamp=3.0, window_days=_WINDOW, percentile=25.0)
    assert out == {"five_hour": 6000, "seven_day": 80000}   # constant column → percentile = value


def test_recalibrate_clamps_to_band(tmp_path):
    csv_path = tmp_path / "c.csv"
    _write_calib_csv(csv_path, n_per_window=100, tpp_5h=999999, tpp_7d=1)
    out = quota_calibration.recalibrate_claude_factors(
        csv_path, _DEF, min_samples=60, clamp=3.0, window_days=_WINDOW)
    assert out["five_hour"] == int(round(5400 * 3.0))   # clamped high
    assert out["seven_day"] == int(round(75000 / 3.0))  # clamped low


def test_recalibrate_missing_file_returns_none(tmp_path):
    assert quota_calibration.recalibrate_claude_factors(
        tmp_path / "nope.csv", _DEF, min_samples=60, clamp=3.0, window_days=_WINDOW) is None


# ─────────────────────── (b.1) recalibration lookback window ──────────────────


def test_recalibrate_ignores_rows_older_than_the_window(tmp_path):
    """100 fresh rows per window (tpp=6000/80000) plus 100 OLD rows (tpp=999999/1,
    which would clamp the result if counted) outside a 60-day window — the result
    must reflect only the fresh rows, proving old data is actually excluded rather
    than merely tolerated."""
    csv_path = tmp_path / "c.csv"
    _write_calib_csv(csv_path, n_per_window=100, tpp_5h=999999, tpp_7d=1, days_ago=90)
    _write_calib_csv(csv_path, n_per_window=100, tpp_5h=6000, tpp_7d=80000, mode="a")

    out = quota_calibration.recalibrate_claude_factors(
        csv_path, _DEF, min_samples=60, clamp=3.0, window_days=60)
    assert out == {"five_hour": 6000, "seven_day": 80000}, (
        "old, out-of-window rows leaked into the result"
    )


def test_recalibrate_none_when_only_old_rows_exist_outside_the_window(tmp_path):
    """Gegenprobe: 100 rows per window is comfortably above min_samples=60 — but all
    of them are 90 days old, so a 60-day window must still return None, not silently
    fall back to counting them anyway."""
    csv_path = tmp_path / "c.csv"
    _write_calib_csv(csv_path, n_per_window=100, tpp_5h=6000, tpp_7d=80000, days_ago=90)

    assert quota_calibration.recalibrate_claude_factors(
        csv_path, _DEF, min_samples=60, clamp=3.0, window_days=60) is None


def test_recalibrate_wider_window_recovers_the_old_rows(tmp_path):
    """Same data as the Gegenprobe above, but with a window wide enough to cover it —
    proves the exclusion is really about the window boundary, not some other filter
    that happens to reject these particular rows."""
    csv_path = tmp_path / "c.csv"
    _write_calib_csv(csv_path, n_per_window=100, tpp_5h=6000, tpp_7d=80000, days_ago=90)

    out = quota_calibration.recalibrate_claude_factors(
        csv_path, _DEF, min_samples=60, clamp=3.0, window_days=120)
    assert out == {"five_hour": 6000, "seven_day": 80000}


def test_recalibrate_drops_rows_with_unparsable_timestamp(tmp_path):
    """A row that cannot prove it is in the window is treated as NOT in the window,
    not as automatically included."""
    csv_path = tmp_path / "c.csv"
    _write_calib_csv(csv_path, n_per_window=100, tpp_5h=6000, tpp_7d=80000)
    # Corrupt every timestamp in the file to an unparsable value, keep everything else.
    text = csv_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    header = lines[0]
    ts_col = quota_calibration.CSV_FIELDS.index("timestamp_utc")
    fixed = [header]
    for line in lines[1:]:
        parts = line.rstrip("\n").split(",")
        if len(parts) > ts_col:
            parts[ts_col] = "not-a-timestamp"
        fixed.append(",".join(parts) + "\n")
    csv_path.write_text("".join(fixed), encoding="utf-8")

    assert quota_calibration.recalibrate_claude_factors(
        csv_path, _DEF, min_samples=60, clamp=3.0, window_days=60) is None


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


def test_maybe_recalibrate_passes_the_configured_window(monkeypatch):
    """Wiring check: `_maybe_recalibrate` must forward `config.
    QUOTA_RECALIBRATE_WINDOW_DAYS`, not some other value or none at all — the whole
    point of adding the parameter is that production actually uses it."""
    monkeypatch.setattr(limits, "QUOTA_LIVE_ESTIMATE_ENABLED", True)
    monkeypatch.setattr(config, "QUOTA_AUTO_RECALIBRATE_ENABLED", True)
    monkeypatch.setattr(limits, "_last_recalibration_date", None)

    captured = {}

    def fake_recalibrate(*_a, **kw):
        captured.update(kw)

    monkeypatch.setattr(quota_calibration, "recalibrate_claude_factors", fake_recalibrate)
    limits._maybe_recalibrate()

    assert captured.get("window_days") == config.QUOTA_RECALIBRATE_WINDOW_DAYS
