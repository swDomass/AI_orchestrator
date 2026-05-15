"""Unit tests for the ToolTraceEvent parser + _tool_trace_stats aggregator
added to analytics.py.

Covers:
- _parse_tool_traces returns empty list when no allowed roots configured
- Parser globs nested .{tool}/traces/*.jsonl files correctly
- Old files (older than max_age_days) are skipped
- Invalid JSON lines are tolerated (skipped, not raised)
- _tool_trace_stats aggregates runs / completed / success / avg_duration
- get_dashboard_data exposes "tool_trace_stats" key
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    from analytics import (
        ToolTraceEvent,
        _parse_tool_traces,
        _tool_trace_stats,
    )


def _write_trace(file: Path, lines: list[dict]) -> None:
    """Write a JSONL trace file."""
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )


def _now_iso() -> str:
    return datetime.now().isoformat()


# ── _parse_tool_traces ───────────────────────────────────────────────


class TestParseToolTraces:

    def test_empty_when_no_roots(self) -> None:
        events = _parse_tool_traces([])
        assert events == []

    def test_empty_when_root_missing(self, tmp_path: Path) -> None:
        events = _parse_tool_traces([tmp_path / "does-not-exist"])
        assert events == []

    def test_parses_single_trace_file(self, tmp_path: Path) -> None:
        trace = tmp_path / ".dev-loop" / "traces" / "abc.jsonl"
        _write_trace(trace, [
            {"ts": _now_iso(), "elapsed_sec": 0.1, "run_id": "abc",
             "tool": "dev-loop", "action": "run_start", "details": {}},
            {"ts": _now_iso(), "elapsed_sec": 1.5, "run_id": "abc",
             "tool": "dev-loop", "action": "run_end", "details": {"success": True}},
        ])

        events = _parse_tool_traces([tmp_path])
        assert len(events) == 2
        assert events[0].action == "run_start"
        assert events[1].action == "run_end"
        # details encoded as tuple of (k, v) pairs
        success_kv = dict(events[1].details).get("success")
        assert success_kv is True

    def test_skips_old_files(self, tmp_path: Path) -> None:
        trace = tmp_path / ".dev-loop" / "traces" / "old.jsonl"
        _write_trace(trace, [{"ts": _now_iso(), "elapsed_sec": 0, "run_id": "r",
                              "tool": "dev-loop", "action": "run_start", "details": {}}])
        # Backdate mtime by 60 days
        import os
        old_time = (datetime.now() - timedelta(days=60)).timestamp()
        os.utime(trace, (old_time, old_time))

        events = _parse_tool_traces([tmp_path], max_age_days=30)
        assert events == []

    def test_tolerates_invalid_json_lines(self, tmp_path: Path) -> None:
        trace = tmp_path / ".dev-loop" / "traces" / "mixed.jsonl"
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text(
            json.dumps({"ts": _now_iso(), "elapsed_sec": 0, "run_id": "r",
                        "tool": "dev-loop", "action": "run_start", "details": {}}) + "\n"
            + "not json at all\n"
            + json.dumps({"ts": _now_iso(), "elapsed_sec": 0, "run_id": "r",
                          "tool": "dev-loop", "action": "run_end", "details": {"success": True}}) + "\n",
            encoding="utf-8",
        )

        events = _parse_tool_traces([tmp_path])
        # 2 valid + 1 garbage → 2 events
        assert len(events) == 2

    def test_parses_multiple_tool_dirs(self, tmp_path: Path) -> None:
        _write_trace(
            tmp_path / ".dev-loop" / "traces" / "a.jsonl",
            [{"ts": _now_iso(), "elapsed_sec": 0, "run_id": "a",
              "tool": "dev-loop", "action": "run_start", "details": {}}],
        )
        _write_trace(
            tmp_path / ".review-loop" / "traces" / "b.jsonl",
            [{"ts": _now_iso(), "elapsed_sec": 0, "run_id": "b",
              "tool": "review-loop", "action": "run_start", "details": {}}],
        )

        events = _parse_tool_traces([tmp_path])
        tools = {e.tool for e in events}
        assert tools == {"dev-loop", "review-loop"}

    def test_returns_sorted_by_timestamp(self, tmp_path: Path) -> None:
        t1 = datetime.now() - timedelta(seconds=10)
        t2 = datetime.now() - timedelta(seconds=5)
        t3 = datetime.now()
        _write_trace(
            tmp_path / ".dev-loop" / "traces" / "out-of-order.jsonl",
            [
                {"ts": t3.isoformat(), "elapsed_sec": 0, "run_id": "r3",
                 "tool": "dev-loop", "action": "run_end", "details": {}},
                {"ts": t1.isoformat(), "elapsed_sec": 0, "run_id": "r1",
                 "tool": "dev-loop", "action": "run_start", "details": {}},
                {"ts": t2.isoformat(), "elapsed_sec": 0, "run_id": "r2",
                 "tool": "dev-loop", "action": "phase_start", "details": {}},
            ],
        )

        events = _parse_tool_traces([tmp_path])
        assert [e.run_id for e in events] == ["r1", "r2", "r3"]

    def test_caps_events_per_file(self, tmp_path: Path) -> None:
        trace = tmp_path / ".dev-loop" / "traces" / "huge.jsonl"
        lots = [
            {"ts": _now_iso(), "elapsed_sec": i, "run_id": "r",
             "tool": "dev-loop", "action": "tick", "details": {"i": i}}
            for i in range(2000)
        ]
        _write_trace(trace, lots)

        events = _parse_tool_traces([tmp_path], max_events_per_file=500)
        assert len(events) == 500


# ── _tool_trace_stats ────────────────────────────────────────────────


class TestToolTraceStats:

    def test_empty_events_returns_empty_dict(self) -> None:
        assert _tool_trace_stats([]) == {}

    def test_counts_runs_per_tool(self) -> None:
        events = [
            ToolTraceEvent(
                timestamp=datetime.now(), elapsed_sec=0, run_id=f"r{i}",
                tool="dev-loop", action="run_start", details=(),
            )
            for i in range(3)
        ]
        stats = _tool_trace_stats(events)
        assert stats["dev-loop"]["runs"] == 3
        assert stats["dev-loop"]["completed_runs"] == 0

    def test_completed_run_with_success(self) -> None:
        t0 = datetime.now()
        events = [
            ToolTraceEvent(timestamp=t0, elapsed_sec=0, run_id="x",
                           tool="dev-loop", action="run_start", details=()),
            ToolTraceEvent(timestamp=t0 + timedelta(seconds=12), elapsed_sec=12, run_id="x",
                           tool="dev-loop", action="run_end",
                           details=(("success", True),)),
        ]
        stats = _tool_trace_stats(events)
        assert stats["dev-loop"]["runs"] == 1
        assert stats["dev-loop"]["completed_runs"] == 1
        assert stats["dev-loop"]["success_runs"] == 1
        assert stats["dev-loop"]["avg_duration_sec"] == 12.0

    def test_completed_run_with_failure(self) -> None:
        t0 = datetime.now()
        events = [
            ToolTraceEvent(timestamp=t0, elapsed_sec=0, run_id="x",
                           tool="review-loop", action="run_start", details=()),
            ToolTraceEvent(timestamp=t0 + timedelta(seconds=5), elapsed_sec=5, run_id="x",
                           tool="review-loop", action="run_end",
                           details=(("success", False), ("reason", "max_iterations"))),
        ]
        stats = _tool_trace_stats(events)
        assert stats["review-loop"]["completed_runs"] == 1
        assert stats["review-loop"]["success_runs"] == 0

    def test_separates_tools(self) -> None:
        t0 = datetime.now()
        events = [
            ToolTraceEvent(timestamp=t0, elapsed_sec=0, run_id="a",
                           tool="dev-loop", action="run_start", details=()),
            ToolTraceEvent(timestamp=t0, elapsed_sec=0, run_id="b",
                           tool="review-loop", action="run_start", details=()),
            ToolTraceEvent(timestamp=t0, elapsed_sec=0, run_id="c",
                           tool="dev-loop", action="run_start", details=()),
        ]
        stats = _tool_trace_stats(events)
        assert stats["dev-loop"]["runs"] == 2
        assert stats["review-loop"]["runs"] == 1


# ── get_dashboard_data integration ───────────────────────────────────


class TestDashboardIntegration:

    def test_dashboard_exposes_tool_trace_stats_key(self, monkeypatch):
        """get_dashboard_data must return a dict with 'tool_trace_stats'."""
        # Avoid scanning the real filesystem
        monkeypatch.setattr("analytics.ALLOWED_CWD_ROOTS", [])
        # Invalidate the module-level cache so the new value sticks
        monkeypatch.setattr("analytics._cache", {})

        from analytics import get_dashboard_data
        data = get_dashboard_data(days=7)
        assert "tool_trace_stats" in data
        assert isinstance(data["tool_trace_stats"], dict)
