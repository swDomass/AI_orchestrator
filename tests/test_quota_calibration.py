"""Tests for the Phase-0 quota calibration telemetry module."""

import csv
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import quota_calibration  # noqa: E402
from limits import AllLimits, ProviderLimits, WindowData  # noqa: E402


# ───────────────────────────── Helpers ────────────────────────────────────────


class _FakeEntry:
    def __init__(self, in_t, out_t, cc=0, cr=0, model="claude-opus-4-7", ts=None,
                 message_id="msg", request_id="req"):
        self.message_id = message_id
        self.request_id = request_id
        self.timestamp = ts if ts is not None else dt.datetime.now(dt.timezone.utc)
        self.input_tokens = in_t
        self.output_tokens = out_t
        self.cache_creation_tokens = cc
        self.cache_read_tokens = cr
        self.model = model


def _make_claude_limits(
    five_hour_remaining: float = 75.0,
    seven_day_remaining: float = 60.0,
    five_hour_reset_in: int = 7200,
    seven_day_reset_in: int = 432000,
    error: str = "",
) -> AllLimits:
    claude = ProviderLimits(
        available=True,
        remaining_pct=min(five_hour_remaining, seven_day_remaining),
        resets_in_sec=five_hour_reset_in,
        windows={
            "five_hour": WindowData(remaining_pct=five_hour_remaining, resets_in_sec=five_hour_reset_in),
            "seven_day": WindowData(remaining_pct=seven_day_remaining, resets_in_sec=seven_day_reset_in),
        },
        error=error,
    )
    return AllLimits(claude=claude)


def _patch_load_entries(monkeypatch, entries):
    """Replace quota_calibration._load_entries with a stub returning *entries*.

    Pass None to simulate claude-monitor missing/unavailable.
    """
    monkeypatch.setattr(quota_calibration, "_load_entries", lambda _load_hours: entries)


@pytest.fixture(autouse=True)
def _shutdown_executor_between_tests():
    """Async tests spin up a single-worker pool; make sure each test starts clean."""
    yield
    quota_calibration.shutdown_executor(wait=True)


# ───────────────────────────── log_calibration_sample ─────────────────────────


def test_log_writes_two_rows_with_header(tmp_path, monkeypatch):
    """One row per window, header on first write, schema v2 columns populated."""
    _patch_load_entries(monkeypatch, [
        _FakeEntry(1000, 500, cc=20000, cr=5000),
        _FakeEntry(0, 0, cc=0, cr=0, message_id="msg-2"),  # second entry, summed
    ])

    csv_path = tmp_path / "calib.csv"
    quota_calibration.log_calibration_sample(
        _make_claude_limits(), csv_path,
        queue_idle=False, claude_plan="max5",
    )

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 2
    windows = {r["window"] for r in rows}
    assert windows == {"five_hour", "seven_day"}

    five = next(r for r in rows if r["window"] == "five_hour")
    assert five["schema_version"] == str(quota_calibration._SCHEMA_VERSION)
    assert five["claude_plan"] == "max5"
    assert five["queue_idle_at_sample"] == "false"
    assert five["tokens_input"] == "1000"
    assert five["tokens_output"] == "500"
    assert five["cclimits_pct_used"] == "25.0000"
    # tokens_per_pct_io_only = (1000+500) / 25.0 = 60.0
    assert five["tokens_per_pct_io_only"] == "60.00"
    # tokens_per_pct_with_cc = (1500+20000) / 25.0 = 860.0
    assert five["tokens_per_pct_with_cc"] == "860.00"
    assert five["flag_rolling_fallback"] == "false"
    assert five["flag_low_pct"] == "false"
    assert five["flag_cm_unavailable"] == "false"
    assert five["note"] == ""
    assert five["entries_count"] == "2"


def test_log_appends_without_duplicate_header(tmp_path, monkeypatch):
    _patch_load_entries(monkeypatch, [_FakeEntry(100, 50)])
    csv_path = tmp_path / "calib.csv"

    quota_calibration.log_calibration_sample(_make_claude_limits(), csv_path)
    quota_calibration.log_calibration_sample(_make_claude_limits(), csv_path)

    content = csv_path.read_text(encoding="utf-8").splitlines()
    # 1 header + 2 calls × 2 windows = 5 lines
    assert len(content) == 5
    expected_header = ",".join(quota_calibration.CSV_FIELDS)
    assert content.count(expected_header) == 1


def test_log_skips_when_claude_has_error(tmp_path, monkeypatch):
    """No row when cclimits failed or returned 429-fallback data."""
    _patch_load_entries(monkeypatch, [])
    csv_path = tmp_path / "calib.csv"

    for err in ("HTTP 429 (cached)", "cclimits timeout", "HTTP 429 (local-files)"):
        quota_calibration.log_calibration_sample(
            _make_claude_limits(error=err), csv_path,
        )

    assert not csv_path.exists()


def test_log_skips_when_no_windows(tmp_path, monkeypatch):
    """No row when provider has no window data (pre-auth or parsing failure)."""
    _patch_load_entries(monkeypatch, [])
    csv_path = tmp_path / "calib.csv"

    empty = AllLimits(claude=ProviderLimits(available=False, windows={}))
    quota_calibration.log_calibration_sample(empty, csv_path)

    assert not csv_path.exists()


def test_log_flags_cm_unavailable_when_load_returns_none(tmp_path, monkeypatch):
    """When claude-monitor is missing, _load_entries returns None — row must
    still be written with the flag and empty token columns."""
    _patch_load_entries(monkeypatch, None)
    csv_path = tmp_path / "calib.csv"

    quota_calibration.log_calibration_sample(_make_claude_limits(), csv_path)

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 2
    for row in rows:
        assert row["flag_cm_unavailable"] == "true"
        assert "claude-monitor unavailable" in row["note"]
        assert row["tokens_input"] == "0"
        assert row["tokens_per_pct_io_only"] == ""
        # Format-consistency: even the default path uses 2-decimal billing format
        assert row["tokens_weighted_billing"] == "0.00"


def test_low_utilization_flags_and_skips_division(tmp_path, monkeypatch):
    """pct_used < 0.5 yields no calibration ratio (division would be unstable)."""
    _patch_load_entries(monkeypatch, [_FakeEntry(100, 50, cc=200, cr=10)])
    csv_path = tmp_path / "calib.csv"

    # remaining 99.8 → used 0.2 (below the 0.5% floor)
    quota_calibration.log_calibration_sample(
        _make_claude_limits(five_hour_remaining=99.8, seven_day_remaining=99.9),
        csv_path,
    )

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    for row in rows:
        assert row["tokens_per_pct_io_only"] == ""
        assert row["tokens_per_pct_with_cc"] == ""
        assert row["tokens_per_pct_all"] == ""
        assert row["flag_low_pct"] == "true"
        assert "too low" in row["note"]
        # Raw token counts are still logged
        assert row["tokens_input"] == "100"


def test_log_never_raises(tmp_path):
    """The hook must swallow all exceptions — bg thread safety."""
    class Broken:
        claude = None
    quota_calibration.log_calibration_sample(Broken(), tmp_path / "calib.csv")


def test_log_never_raises_when_csv_write_fails(tmp_path, monkeypatch):
    """A read-only filesystem or any OSError on write must not crash the bg thread."""
    _patch_load_entries(monkeypatch, [_FakeEntry(100, 50)])

    def boom(*args, **kwargs):
        raise PermissionError("simulated read-only filesystem")
    monkeypatch.setattr(quota_calibration, "_write_csv_row", boom)

    quota_calibration.log_calibration_sample(_make_claude_limits(), tmp_path / "x.csv")


# ───────────────────────────── window_start edge cases ───────────────────────


def test_log_flags_row_when_resets_in_sec_is_zero(tmp_path, monkeypatch):
    """When cclimits returns resets_in_sec=0 (right after a reset), the row
    must be flagged so downstream analysis can filter it out."""
    _patch_load_entries(monkeypatch, [_FakeEntry(50, 25)])
    csv_path = tmp_path / "calib.csv"

    quota_calibration.log_calibration_sample(
        _make_claude_limits(five_hour_reset_in=0, seven_day_reset_in=0),
        csv_path,
    )

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 2
    for row in rows:
        assert row["flag_rolling_fallback"] == "true"
        assert "rolling-fallback" in row["note"]
        assert row["reset_in_sec"] == "0"
        assert row["window_start_utc"] != ""  # synthetic now - window_size


def test_log_combines_flags_when_multiple_conditions_apply(tmp_path, monkeypatch):
    """rolling-fallback + claude-monitor-unavailable + low-pct must all appear
    in flags AND note."""
    _patch_load_entries(monkeypatch, None)  # cm unavailable
    csv_path = tmp_path / "calib.csv"

    quota_calibration.log_calibration_sample(
        _make_claude_limits(
            five_hour_reset_in=0, seven_day_reset_in=0,    # → rolling-fallback
            five_hour_remaining=99.95, seven_day_remaining=99.99,  # → low_pct
        ),
        csv_path,
    )

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    for row in rows:
        assert row["flag_rolling_fallback"] == "true"
        assert row["flag_cm_unavailable"] == "true"
        assert row["flag_low_pct"] == "true"
        assert "rolling-fallback" in row["note"]
        assert "claude-monitor unavailable" in row["note"]
        assert "too low" in row["note"]


# ───────────────────────────── _aggregate_tokens ─────────────────────────────


def test_aggregate_tokens_returns_none_when_claude_monitor_missing(monkeypatch):
    """Graceful no-op when the optional dependency is absent."""
    monkeypatch.setitem(sys.modules, "claude_monitor", None)
    monkeypatch.setitem(sys.modules, "claude_monitor.core", None)
    monkeypatch.setitem(sys.modules, "claude_monitor.core.models", None)
    monkeypatch.setitem(sys.modules, "claude_monitor.data", None)
    monkeypatch.setitem(sys.modules, "claude_monitor.data.reader", None)

    assert quota_calibration._aggregate_tokens(5) is None


def test_aggregate_tokens_does_not_re_dedupe(monkeypatch):
    """claude-monitor already dedupes on (message_id, request_id) before
    returning entries. A re-dedup on message_id alone would mistakenly
    collapse legitimate server-side retries that share message_id but have
    different request_ids. Verify we just sum what claude-monitor gives us."""
    _patch_load_entries(monkeypatch, [
        _FakeEntry(100, 50, message_id="msg-1", request_id="req-1"),
        _FakeEntry(100, 50, message_id="msg-1", request_id="req-2"),  # same message_id but distinct from claude-monitor's perspective
        _FakeEntry(200, 80, message_id="msg-2", request_id="req-3"),
    ])

    result = quota_calibration._aggregate_tokens(5)
    assert result is not None
    assert result["input"] == 400  # 100 + 100 + 200 (all three)
    assert result["output"] == 180
    assert result["entries_count"] == 3


def test_aggregate_tokens_filters_entries_before_window_start(monkeypatch):
    """Entries older than window_start are dropped — not part of the current Anthropic window."""
    now = dt.datetime.now(dt.timezone.utc)
    window_start = now - dt.timedelta(hours=2)   # block started 2h ago

    _patch_load_entries(monkeypatch, [
        # Older than window_start — belongs to the previous block, must be excluded
        _FakeEntry(5000, 1000, ts=now - dt.timedelta(hours=6), message_id="old-1"),
        _FakeEntry(3000, 500,  ts=now - dt.timedelta(hours=3), message_id="old-2"),
        # Inside the current window
        _FakeEntry(100, 50,    ts=now - dt.timedelta(minutes=90), message_id="cur-1"),
        _FakeEntry(200, 80,    ts=now - dt.timedelta(minutes=10), message_id="cur-2"),
    ])

    result = quota_calibration._aggregate_tokens(5, window_start=window_start)
    assert result is not None
    assert result["input"] == 300   # only the 100 + 200 from current window
    assert result["output"] == 130
    assert result["entries_count"] == 2


def test_aggregate_tokens_treats_naive_timestamps_as_utc(monkeypatch):
    """If claude-monitor returns naive datetimes, treat them as UTC for the window filter."""
    now = dt.datetime.now(dt.timezone.utc)
    window_start = now - dt.timedelta(hours=1)
    naive_inside = (now - dt.timedelta(minutes=30)).replace(tzinfo=None)
    naive_outside = (now - dt.timedelta(hours=3)).replace(tzinfo=None)

    _patch_load_entries(monkeypatch, [
        _FakeEntry(999, 999, ts=naive_outside, message_id="naive-out"),
        _FakeEntry(10,  20,  ts=naive_inside,  message_id="naive-in"),
    ])

    result = quota_calibration._aggregate_tokens(5, window_start=window_start)
    assert result is not None
    assert result["entries_count"] == 1
    assert result["input"] == 10


def test_aggregate_tokens_without_window_start_falls_back_to_rolling(monkeypatch):
    """When window_start is None, _load_entries is called with hours_back=window_hours."""
    captured = {}

    def fake_load(load_hours):
        captured["load_hours"] = load_hours
        return [_FakeEntry(50, 25)]
    monkeypatch.setattr(quota_calibration, "_load_entries", fake_load)

    result = quota_calibration._aggregate_tokens(5)
    assert captured["load_hours"] == 5
    assert result["input"] == 50


def test_aggregate_tokens_loads_buffered_history_when_window_is_old(monkeypatch):
    """For an aging block (e.g. 4h into a 5h window) we load enough history."""
    now = dt.datetime.now(dt.timezone.utc)
    window_start = now - dt.timedelta(hours=4)
    captured = {}

    def fake_load(load_hours):
        captured["load_hours"] = load_hours
        return []
    monkeypatch.setattr(quota_calibration, "_load_entries", fake_load)

    quota_calibration._aggregate_tokens(5, window_start=window_start)
    # elapsed ≈ 4h, factor=1.1, +abs=6 → ~10h
    assert captured["load_hours"] >= 9


def test_aggregate_tokens_buffer_scales_with_factor_and_absolute_terms(monkeypatch):
    """Verify the buffer formula: ceil(elapsed * 1.1) + 6."""
    now = dt.datetime.now(dt.timezone.utc)
    window_start = now - dt.timedelta(hours=160)  # 7d-style aging
    captured = {}

    def fake_load(load_hours):
        captured["load_hours"] = load_hours
        return []
    monkeypatch.setattr(quota_calibration, "_load_entries", fake_load)

    quota_calibration._aggregate_tokens(168, window_start=window_start)
    # 160 * 1.1 + 6 = 182 (int cast → 182)
    assert captured["load_hours"] == int(160 * 1.1) + 6


# ───────────────────────────── _write_csv_row ────────────────────────────────


def test_write_csv_row_accepts_str_path(tmp_path):
    """_write_csv_row must accept either Path or str (defensive cast)."""
    row = {key: "" for key in quota_calibration.CSV_FIELDS}
    row["timestamp_utc"] = "2026-05-21T10:00:00+00:00"
    row["window"] = "five_hour"
    csv_path_str = str(tmp_path / "subdir" / "via-str.csv")

    quota_calibration._write_csv_row(csv_path_str, row)

    p = Path(csv_path_str)
    assert p.exists()
    content = p.read_text(encoding="utf-8").splitlines()
    assert content[0] == ",".join(quota_calibration.CSV_FIELDS)


def test_log_concurrent_writes_produce_one_header(tmp_path, monkeypatch):
    """Parallel synchronous calls to log_calibration_sample must not duplicate
    the CSV header or interleave row bytes (thread-level concurrency)."""
    import concurrent.futures

    _patch_load_entries(monkeypatch, [_FakeEntry(1, 1)])

    csv_path = tmp_path / "concurrent.csv"
    N = 20

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(quota_calibration.log_calibration_sample, _make_claude_limits(), csv_path)
            for _ in range(N)
        ]
        for f in concurrent.futures.as_completed(futures):
            f.result()  # surfaces any thread exception

    lines = csv_path.read_text(encoding="utf-8").splitlines()
    expected_header = ",".join(quota_calibration.CSV_FIELDS)
    assert lines.count(expected_header) == 1
    assert len(lines) == 1 + N * 2  # 1 header + N calls × 2 windows


# ───────────────────────────── log_calibration_sample_async ──────────────────


def test_async_drops_overlapping_submissions(tmp_path, monkeypatch):
    """When a previous sample is still being written, a new submit must be
    silently dropped — no unbounded queue."""
    import time
    blocker = __import__("threading").Event()

    def slow_load(_load_hours):
        # Block until released, simulating a slow JSONL scan
        blocker.wait(timeout=5)
        return [_FakeEntry(1, 1)]
    monkeypatch.setattr(quota_calibration, "_load_entries", slow_load)

    csv_path = tmp_path / "calib.csv"

    quota_calibration.log_calibration_sample_async(_make_claude_limits(), csv_path)
    # Tiny pause so the worker has time to dequeue and start
    time.sleep(0.05)
    quota_calibration.log_calibration_sample_async(_make_claude_limits(), csv_path)
    quota_calibration.log_calibration_sample_async(_make_claude_limits(), csv_path)

    blocker.set()
    quota_calibration.shutdown_executor(wait=True)

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    # Only the first submission ran — others were dropped, so 2 rows (5h + 7d).
    assert len(rows) == 2


def test_async_executes_when_no_pending_sample(tmp_path, monkeypatch):
    """Without overlap, a single async submission produces both window rows."""
    _patch_load_entries(monkeypatch, [_FakeEntry(100, 50)])

    csv_path = tmp_path / "calib.csv"
    quota_calibration.log_calibration_sample_async(
        _make_claude_limits(), csv_path, queue_idle=True, claude_plan="pro",
    )
    quota_calibration.shutdown_executor(wait=True)

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 2
    for row in rows:
        assert row["queue_idle_at_sample"] == "true"
        assert row["claude_plan"] == "pro"
