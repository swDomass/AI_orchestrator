"""Tests for _cleanup_capacity_log() in heartbeat.py."""
from datetime import date, datetime, timedelta
from unittest.mock import patch

import heartbeat
from heartbeat import _cleanup_capacity_log


HEADER = (
    "# AI Provider Capacity Log\n"
    "<!-- appended by orchestrator heartbeat -->\n\n"
)


def _make_line(dt: datetime, provider: str = "claude_five_hour", pct: float = 90.0) -> str:
    ts = dt.strftime("%Y-%m-%d %H:%M:%S")
    return f"{ts} | {provider} | {pct} | true\n"


def _reset_last_cleanup():
    """Reset the module-level rate-limit guard so tests are independent."""
    heartbeat._last_capacity_cleanup = None


def test_removes_old_entries(tmp_path):
    _reset_last_cleanup()
    log = tmp_path / "capacity-log.md"
    now = datetime.now()
    old = now - timedelta(days=100)
    recent = now - timedelta(days=10)

    log.write_text(
        HEADER + _make_line(old) + _make_line(recent),
        encoding="utf-8",
    )

    with patch("heartbeat.CAPACITY_LOG_FILE", log):
        with patch("heartbeat.CAPACITY_LOG_RETENTION_DAYS", 90):
            _cleanup_capacity_log()

    content = log.read_text(encoding="utf-8")
    assert _make_line(old) not in content
    assert _make_line(recent) in content


def test_preserves_header(tmp_path):
    _reset_last_cleanup()
    log = tmp_path / "capacity-log.md"
    now = datetime.now()
    recent = now - timedelta(days=5)

    log.write_text(HEADER + _make_line(recent), encoding="utf-8")

    with patch("heartbeat.CAPACITY_LOG_FILE", log):
        with patch("heartbeat.CAPACITY_LOG_RETENTION_DAYS", 90):
            _cleanup_capacity_log()

    content = log.read_text(encoding="utf-8")
    assert "# AI Provider Capacity Log" in content
    assert "<!-- appended by orchestrator heartbeat -->" in content


def test_no_rewrite_when_nothing_removed(tmp_path):
    _reset_last_cleanup()
    log = tmp_path / "capacity-log.md"
    now = datetime.now()
    recent = now - timedelta(days=1)
    original = HEADER + _make_line(recent)
    log.write_text(original, encoding="utf-8")
    mtime_before = log.stat().st_mtime

    with patch("heartbeat.CAPACITY_LOG_FILE", log):
        with patch("heartbeat.CAPACITY_LOG_RETENTION_DAYS", 90):
            _cleanup_capacity_log()

    # File should not have been rewritten (mtime unchanged)
    assert log.stat().st_mtime == mtime_before


def test_runs_at_most_once_per_day(tmp_path):
    _reset_last_cleanup()
    log = tmp_path / "capacity-log.md"
    now = datetime.now()
    old = now - timedelta(days=100)
    log.write_text(HEADER + _make_line(old), encoding="utf-8")

    with patch("heartbeat.CAPACITY_LOG_FILE", log):
        with patch("heartbeat.CAPACITY_LOG_RETENTION_DAYS", 90):
            _cleanup_capacity_log()  # first call — prunes
            # Restore old content to verify second call skips
            log.write_text(HEADER + _make_line(old), encoding="utf-8")
            _cleanup_capacity_log()  # same day — must skip

    # Old entry still present because second call was skipped
    content = log.read_text(encoding="utf-8")
    assert _make_line(old) in content


def test_handles_missing_file(tmp_path):
    _reset_last_cleanup()
    missing = tmp_path / "no-such-file.md"
    with patch("heartbeat.CAPACITY_LOG_FILE", missing):
        _cleanup_capacity_log()  # must not raise


def test_keeps_unparseable_lines(tmp_path):
    _reset_last_cleanup()
    log = tmp_path / "capacity-log.md"
    weird = "this is not a valid line\n"
    log.write_text(HEADER + weird, encoding="utf-8")

    with patch("heartbeat.CAPACITY_LOG_FILE", log):
        with patch("heartbeat.CAPACITY_LOG_RETENTION_DAYS", 90):
            _cleanup_capacity_log()

    assert weird in log.read_text(encoding="utf-8")


def test_cleanup_called_from_append(tmp_path, monkeypatch):
    """_append_capacity_log() must trigger _cleanup_capacity_log()."""
    _reset_last_cleanup()
    log = tmp_path / "capacity-log.md"
    monkeypatch.setattr(heartbeat, "CAPACITY_LOG_FILE", log)

    called = []
    monkeypatch.setattr(heartbeat, "_cleanup_capacity_log", lambda: called.append(1))

    # Build a minimal limits object
    from types import SimpleNamespace
    lim = SimpleNamespace(available=True, remaining_pct=80.0, windows={})
    limits = SimpleNamespace(claude=lim, gemini=None, codex=None)

    heartbeat._append_capacity_log(limits)
    assert called, "_cleanup_capacity_log was not called from _append_capacity_log"
