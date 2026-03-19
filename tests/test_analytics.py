"""Tests for analytics.py — parsing, aggregation, and caching."""

import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_dotenv():
    with patch("config._load_dotenv"):
        yield


@pytest.fixture()
def task_results_dir(tmp_path):
    d = tmp_path / "task_results"
    d.mkdir()
    return d


@pytest.fixture()
def archive_dir(tmp_path):
    d = tmp_path / "archive"
    d.mkdir()
    return d


def _write_task_file(directory, name, task="Test task", provider="claude",
                     duration=10.0, success=True, ts=None):
    ts = ts or datetime.now().isoformat(timespec="seconds")
    content = (
        f"---\n"
        f'task: "{task}"\n'
        f"provider: {provider}\n"
        f"cwd: /d/test\n"
        f"duration_sec: {duration}\n"
        f"timestamp: {ts}\n"
        f"success: {str(success).lower()}\n"
        f"---\n\n"
        f"Result summary."
    )
    (directory / name).write_text(content, encoding="utf-8")


# ── Parsing tests ────────────────────────────────────────────────────────────

class TestParseTaskFile:
    def test_valid_file(self, tmp_path):
        from analytics import _parse_task_file
        _write_task_file(tmp_path, "t.md", task="Fix bug", provider="gemini",
                         duration=42.5, ts="2026-02-28T10:00:00")
        rec = _parse_task_file(tmp_path / "t.md")
        assert rec is not None
        assert rec.task == "Fix bug"
        assert rec.provider == "gemini"
        assert rec.duration_sec == 42.5
        assert rec.success is True

    def test_missing_file(self, tmp_path):
        from analytics import _parse_task_file
        assert _parse_task_file(tmp_path / "nope.md") is None

    def test_no_frontmatter(self, tmp_path):
        from analytics import _parse_task_file
        (tmp_path / "bad.md").write_text("Just text", encoding="utf-8")
        assert _parse_task_file(tmp_path / "bad.md") is None

    def test_failed_task(self, tmp_path):
        from analytics import _parse_task_file
        _write_task_file(tmp_path, "f.md", success=False)
        rec = _parse_task_file(tmp_path / "f.md")
        assert rec is not None
        assert rec.success is False


class TestParseMemoryFiles:
    def test_combines_both_dirs(self, task_results_dir, archive_dir):
        from analytics import _parse_memory_files
        _write_task_file(task_results_dir, "a.md", task="A")
        _write_task_file(archive_dir, "b.md", task="B")
        records = _parse_memory_files(task_results_dir, archive_dir)
        assert len(records) == 2
        sources = {r.source for r in records}
        assert sources == {"task_results", "archive"}

    def test_empty_dirs(self, tmp_path):
        from analytics import _parse_memory_files
        records = _parse_memory_files(tmp_path / "nope", tmp_path / "nope2")
        assert records == []


class TestParseLogLimits:
    def test_single_line(self, tmp_path):
        from analytics import _parse_log_limits
        text = (
            "2026-02-28 10:00:00,000 [heartbeat] INFO "
            "Heartbeat [Run check-limits and log to memory]: "
            "claude: 85% remaining\n"
        )
        snaps = _parse_log_limits(text)
        assert len(snaps) == 1
        assert snaps[0].provider == "claude"
        assert snaps[0].remaining_pct == 85.0
        assert snaps[0].available is True

    def test_multiline_block(self, tmp_path):
        from analytics import _parse_log_limits
        text = (
            "2026-02-28 10:00:00,000 [heartbeat] INFO "
            "Heartbeat [Run check-limits and log to memory]: "
            "claude: 50% remaining\n"
            "gemini: 100% remaining\n"
            "codex: ❌ expired\n"
        )
        snaps = _parse_log_limits(text)
        assert len(snaps) == 3
        providers = {s.provider: s for s in snaps}
        assert providers["claude"].remaining_pct == 50.0
        assert providers["gemini"].remaining_pct == 100.0
        assert providers["codex"].available is False

    def test_empty_text(self):
        from analytics import _parse_log_limits
        assert _parse_log_limits("") == []


class TestParseQueueLog:
    def test_parses_plain_log(self, tmp_path):
        from analytics import _parse_queue_log
        lf = tmp_path / "queue-events.log"
        lf.write_text(
            "2026-03-01 08:19 | Orchestrator gestartet (watch)\n"
            "2026-03-01 08:31 | Alle Tasks erledigt.\n",
            encoding="utf-8",
        )
        events = _parse_queue_log(lf)
        assert len(events) == 2
        assert events[0].message == "Alle Tasks erledigt."  # most recent first

    def test_missing_file_returns_empty(self, tmp_path):
        from analytics import _parse_queue_log
        assert _parse_queue_log(tmp_path / "nonexistent.log") == []


# ── Aggregation tests ────────────────────────────────────────────────────────

class TestAggregation:
    def _make_records(self, n=5, days_back=3):
        from analytics import TaskRecord
        records = []
        for i in range(n):
            records.append(TaskRecord(
                task=f"Task {i}",
                provider="claude" if i % 2 == 0 else "gemini",
                cwd="/d/test",
                duration_sec=10.0 + i,
                timestamp=datetime.now() - timedelta(days=i % days_back),
                success=i != 2,  # one failure
                source="task_results",
            ))
        return records

    def test_tasks_per_day_zero_filled(self):
        from analytics import _tasks_per_day
        labels, values = _tasks_per_day([], days=7)
        assert len(labels) == 7
        assert all(v == 0 for v in values)

    def test_success_rate(self):
        from analytics import _success_rate
        recs = self._make_records(5)
        rate = _success_rate(recs)
        assert rate == 80.0  # 4/5

    def test_success_rate_empty(self):
        from analytics import _success_rate
        assert _success_rate([]) == 0.0

    def test_provider_distribution(self):
        from analytics import _provider_distribution, TaskRecord
        recs = [
            TaskRecord("t", "claude+review-loop", "", 1, datetime.now(), True, "x"),
            TaskRecord("t", "claude", "", 1, datetime.now(), True, "x"),
            TaskRecord("t", "gemini", "", 1, datetime.now(), True, "x"),
        ]
        labels, values = _provider_distribution(recs)
        assert "claude" in labels
        idx = labels.index("claude")
        assert values[idx] == 2  # normalized: claude+review-loop → claude

    def test_avg_duration_only_success(self):
        from analytics import _avg_duration, TaskRecord
        recs = [
            TaskRecord("t", "c", "", 100, datetime.now(), True, "x"),
            TaskRecord("t", "c", "", 200, datetime.now(), True, "x"),
            TaskRecord("t", "c", "", 999, datetime.now(), False, "x"),  # ignored
        ]
        assert _avg_duration(recs) == 150.0

    def test_limits_timeline_filters_old(self):
        from analytics import _limits_timeline, LimitSnapshot
        old = datetime.now() - timedelta(hours=100)
        recent = datetime.now() - timedelta(hours=1)
        snaps = [
            LimitSnapshot(old, "claude", 50, True),
            LimitSnapshot(recent, "claude", 80, True),
        ]
        tl = _limits_timeline(snaps, hours=48)
        assert len(tl.get("claude", [])) == 1


class TestParseLogSuggestEvents:
    def _get_log_text(self, msg, logger_name="usage_suggester", level="INFO"):
        return f"2026-03-03 10:00:00,000 [{logger_name}] {level} {msg}\n"

    def test_picked_event(self):
        from analytics import _parse_log_suggest_events
        text = self._get_log_text("usage-suggest: picked #2 – Fix tests")
        events = _parse_log_suggest_events(text)
        assert len(events) == 1
        assert events[0].event_type == "picked"
        assert "picked" in events[0].detail

    def test_declined_event(self):
        from analytics import _parse_log_suggest_events
        text = self._get_log_text("usage-suggest: user declined")
        events = _parse_log_suggest_events(text)
        assert len(events) == 1
        assert events[0].event_type == "declined"

    def test_timeout_event(self):
        from analytics import _parse_log_suggest_events
        text = self._get_log_text("usage-suggest: timeout — no response after 300s")
        events = _parse_log_suggest_events(text)
        assert len(events) == 1
        assert events[0].event_type == "timeout"

    def test_suppressed_event(self):
        from analytics import _parse_log_suggest_events
        text = self._get_log_text("usage-suggest: 7-day pace below threshold")
        events = _parse_log_suggest_events(text)
        assert len(events) == 1
        assert events[0].event_type == "suppressed"

    def test_no_suggestions_event(self):
        from analytics import _parse_log_suggest_events
        text = self._get_log_text("usage-suggest: no suggestions found")
        events = _parse_log_suggest_events(text)
        assert len(events) == 1
        assert events[0].event_type == "no_suggestions"

    def test_result_event(self):
        from analytics import _parse_log_suggest_events
        text = self._get_log_text("usage-suggest: result: task queued")
        events = _parse_log_suggest_events(text)
        assert len(events) == 1
        assert events[0].event_type == "result"

    def test_unknown_falls_back_to_info(self):
        from analytics import _parse_log_suggest_events
        text = self._get_log_text("usage-suggest: some unknown message")
        events = _parse_log_suggest_events(text)
        assert len(events) == 1
        assert events[0].event_type == "info"

    def test_heartbeat_logger_also_matched(self):
        from analytics import _parse_log_suggest_events
        text = self._get_log_text("usage-suggest: timeout", logger_name="heartbeat")
        events = _parse_log_suggest_events(text)
        assert len(events) == 1
        assert events[0].event_type == "timeout"

    def test_empty_text(self):
        from analytics import _parse_log_suggest_events
        assert _parse_log_suggest_events("") == []

    def test_suggest_events_in_recent_events(self, tmp_path):
        """suggest events appear in recent_events with type='suggest' from get_dashboard_data()."""
        from analytics import get_dashboard_data, _cache
        _cache["data"] = None
        _cache["ts"] = 0.0
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        (log_dir / "orchestrator.log").write_text(
            f"{ts_str},000 [usage_suggester] INFO usage-suggest: user declined\n",
            encoding="utf-8",
        )
        with patch("analytics.VAULT_PATH", tmp_path), \
             patch("analytics.LOG_FILE", log_dir / "orchestrator.log"), \
             patch("analytics.QUEUE_FILE", tmp_path / "queue.md"), \
             patch("analytics.QUEUE_EVENTS_LOG_FILE", tmp_path / "queue-events.log"), \
             patch("analytics.CAPACITY_LOG_FILE", tmp_path / "capacity-log.md"):
            d = get_dashboard_data()
        suggest_items = [e for e in d["recent_events"] if e["type"] == "suggest"]
        assert len(suggest_items) == 1
        assert "declined" in suggest_items[0]["msg"]
        assert d["usage_suggest_today"] >= 1


class TestGetCurrentLimits:
    def test_returns_empty_when_log_missing(self, tmp_path):
        from analytics import _get_current_limits
        with patch("analytics.CAPACITY_LOG_FILE", tmp_path / "nonexistent.md"):
            assert _get_current_limits() == {}

    def test_aggregates_windows_per_provider(self, tmp_path):
        from analytics import _get_current_limits
        log = tmp_path / "capacity-log.md"
        log.write_text(
            "2026-03-18 12:00:00 | claude_five_hour | 80.0 | true\n"
            "2026-03-18 12:00:00 | claude_seven_day | 30.0 | true\n"
            "2026-03-18 12:00:00 | gemini | 60.0 | true\n",
            encoding="utf-8",
        )
        with patch("analytics.CAPACITY_LOG_FILE", log):
            result = _get_current_limits()
        assert result["claude"]["remaining_pct"] == pytest.approx(30.0)
        assert result["claude"]["available"] is True
        assert result["gemini"]["remaining_pct"] == pytest.approx(60.0)

    def test_only_latest_snapshot_per_provider(self, tmp_path):
        from analytics import _get_current_limits
        log = tmp_path / "capacity-log.md"
        log.write_text(
            "2026-03-18 11:00:00 | claude_five_hour | 90.0 | true\n"
            "2026-03-18 12:00:00 | claude_five_hour | 50.0 | true\n",
            encoding="utf-8",
        )
        with patch("analytics.CAPACITY_LOG_FILE", log):
            result = _get_current_limits()
        assert result["claude"]["remaining_pct"] == pytest.approx(50.0)

    def test_available_false_when_any_window_unavailable(self, tmp_path):
        from analytics import _get_current_limits
        log = tmp_path / "capacity-log.md"
        log.write_text(
            "2026-03-18 12:00:00 | claude_five_hour | 80.0 | true\n"
            "2026-03-18 12:00:00 | claude_seven_day | 5.0 | false\n",
            encoding="utf-8",
        )
        with patch("analytics.CAPACITY_LOG_FILE", log):
            result = _get_current_limits()
        assert result["claude"]["available"] is False
        assert result["claude"]["remaining_pct"] == pytest.approx(5.0)


class TestCache:
    def test_cache_returns_same_object(self, tmp_path):
        from analytics import get_dashboard_data, _cache
        # Reset cache
        _cache["data"] = None
        _cache["ts"] = 0.0
        with patch("analytics.VAULT_PATH", tmp_path), \
             patch("analytics.LOG_FILE", tmp_path / "logs" / "orchestrator.log"), \
             patch("analytics.QUEUE_FILE", tmp_path / "queue.md"), \
             patch("analytics.QUEUE_EVENTS_LOG_FILE", tmp_path / "queue-events.log"), \
             patch("analytics.CAPACITY_LOG_FILE", tmp_path / "capacity-log.md"):
            (tmp_path / "logs").mkdir(exist_ok=True)
            d1 = get_dashboard_data()
            d2 = get_dashboard_data()
            assert d1 is d2  # same cached object
