"""Tests for the daily status-recap heartbeat (P6)."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_dotenv():
    """Avoid loading the user's actual .env into tests."""
    with patch("config._load_dotenv"):
        yield


# ── _format_recap (pure function) ─────────────────────────────────────────────

class TestFormatRecap:
    def test_idle_day_returns_one_liner(self):
        from heartbeat import _format_recap
        summary = {
            "done": 0, "failed": 0, "provider_counts": {},
            "top_successes": [], "top_failures": [],
            "pending_approval": False, "blocked_count": 0, "idle": True,
        }
        assert _format_recap(summary) == "Keine Aktivität in 24h."

    def test_counts_and_providers(self):
        from heartbeat import _format_recap
        summary = {
            "done": 5, "failed": 2,
            "provider_counts": {"claude": 4, "codex": 2, "gemini": 1},
            "top_successes": [], "top_failures": [],
            "pending_approval": False, "blocked_count": 0, "idle": False,
        }
        out = _format_recap(summary)
        assert "✅ 5 erledigt" in out
        assert "❌ 2 fehlgeschlagen" in out
        # Provider line sorted by count desc
        assert "claude 4" in out
        assert "codex 2" in out
        assert out.index("claude 4") < out.index("codex 2")

    def test_pending_approval_and_blocked(self):
        from heartbeat import _format_recap
        summary = {
            "done": 1, "failed": 0, "provider_counts": {"claude": 1},
            "top_successes": [], "top_failures": [],
            "pending_approval": True, "blocked_count": 3, "idle": False,
        }
        out = _format_recap(summary)
        assert "⏸️ 1 ausstehende Approval" in out
        assert "🚧 3 blockierte Task(s)" in out

    def test_top_successes_and_failures(self):
        from heartbeat import _format_recap
        summary = {
            "done": 2, "failed": 1, "provider_counts": {"claude": 3},
            "top_successes": ["Refactor X (claude)", "Fix Y (claude)"],
            "top_failures":  ["Migration Z (claude+dev-loop)"],
            "pending_approval": False, "blocked_count": 0, "idle": False,
        }
        out = _format_recap(summary)
        assert "Top:" in out
        assert "✅ Refactor X (claude)" in out
        assert "✅ Fix Y (claude)" in out
        assert "❌ Migration Z (claude+dev-loop)" in out

    def test_no_status_line_when_clean(self):
        """When no approval/blocked, the status bits line is omitted entirely."""
        from heartbeat import _format_recap
        summary = {
            "done": 1, "failed": 0, "provider_counts": {"claude": 1},
            "top_successes": [], "top_failures": [],
            "pending_approval": False, "blocked_count": 0, "idle": False,
        }
        out = _format_recap(summary)
        assert "⏸" not in out
        assert "🚧" not in out


# ── _check_status_recap (handler entry) ───────────────────────────────────────

class TestStatusRecapHandler:
    def test_returns_string_on_success(self):
        from heartbeat import _check_status_recap
        fake_summary = {
            "done": 3, "failed": 1, "provider_counts": {"claude": 4},
            "top_successes": ["Task A (claude)"], "top_failures": [],
            "pending_approval": False, "blocked_count": 0, "idle": False,
        }
        with patch("analytics.last_24h_summary", return_value=fake_summary):
            out = _check_status_recap()
        assert out is not None
        assert "✅ 3 erledigt" in out
        assert "Task A (claude)" in out

    def test_returns_idle_message_when_empty(self):
        from heartbeat import _check_status_recap
        fake_summary = {
            "done": 0, "failed": 0, "provider_counts": {},
            "top_successes": [], "top_failures": [],
            "pending_approval": False, "blocked_count": 0, "idle": True,
        }
        with patch("analytics.last_24h_summary", return_value=fake_summary):
            out = _check_status_recap()
        assert out == "Keine Aktivität in 24h."

    def test_returns_none_on_internal_failure(self):
        """analytics raises → handler swallows + returns None (no crash)."""
        from heartbeat import _check_status_recap
        with patch("analytics.last_24h_summary", side_effect=RuntimeError("boom")):
            out = _check_status_recap()
        assert out is None


# ── HEARTBEAT.md parsing: status-recap is a recognized handler ───────────────

def test_status_recap_label_maps_to_handler():
    from heartbeat import _match_handler_key
    assert _match_handler_key("status-recap: 24h-Übersicht") == "_check_status_recap"
    assert _match_handler_key("STATUS-RECAP all caps") == "_check_status_recap"


# ── analytics.last_24h_summary (aggregation) ──────────────────────────────────

class TestLast24hSummary:
    @pytest.fixture(autouse=True)
    def _isolate_vault(self, tmp_path, monkeypatch):
        """Point analytics at an empty vault dir so tests stay hermetic."""
        memory_root = tmp_path / "99_System" / "AI" / "memory"
        (memory_root / "task_results").mkdir(parents=True)
        (memory_root / "archive").mkdir(parents=True)
        monkeypatch.setattr("analytics.VAULT_PATH", tmp_path)
        # Stub policy / queue_manager so default tests don't hit real state.
        import policy as _policy
        import queue_manager as _qm
        monkeypatch.setattr(
            _policy, "get_engine",
            lambda: type("E", (), {"has_pending_approval": staticmethod(lambda: False)})()
        )
        monkeypatch.setattr(_qm, "read_queue_items", lambda: [])
        self.task_results_dir = memory_root / "task_results"

    def _write(self, name, *, task="t", provider="claude", success=True, ts=None):
        ts = (ts or datetime.now()).isoformat(timespec="seconds")
        (self.task_results_dir / name).write_text(
            f"---\n"
            f'task: "{task}"\n'
            f"provider: {provider}\n"
            f"cwd: /d/test\n"
            f"duration_sec: 1.0\n"
            f"timestamp: {ts}\n"
            f"success: {str(success).lower()}\n"
            f"---\n\nresult\n",
            encoding="utf-8",
        )

    def test_empty_returns_idle(self):
        from analytics import last_24h_summary
        out = last_24h_summary()
        assert out["idle"] is True
        assert out["done"] == 0
        assert out["failed"] == 0
        assert out["pending_approval"] is False
        assert out["blocked_count"] == 0

    def test_excludes_records_older_than_24h(self):
        from analytics import last_24h_summary
        now = datetime.now()
        self._write("old.md", ts=now - timedelta(hours=30))
        self._write("recent.md", ts=now - timedelta(hours=1))
        out = last_24h_summary(now=now)
        assert out["done"] == 1
        assert out["idle"] is False

    def test_counts_successes_and_failures(self):
        from analytics import last_24h_summary
        now = datetime.now()
        self._write("a.md", success=True, ts=now - timedelta(minutes=10))
        self._write("b.md", success=True, ts=now - timedelta(minutes=20))
        self._write("c.md", success=False, ts=now - timedelta(minutes=30))
        out = last_24h_summary(now=now)
        assert out["done"] == 2
        assert out["failed"] == 1

    def test_provider_counts_normalize_tool_suffix(self):
        """provider 'claude+dev-loop' counts under 'claude' (matches dashboard)."""
        from analytics import last_24h_summary
        now = datetime.now()
        self._write("a.md", provider="claude+dev-loop", ts=now - timedelta(minutes=5))
        self._write("b.md", provider="claude", ts=now - timedelta(minutes=10))
        self._write("c.md", provider="codex", ts=now - timedelta(minutes=15))
        out = last_24h_summary(now=now)
        assert out["provider_counts"] == {"claude": 2, "codex": 1}

    def test_top_successes_newest_first_max_3(self):
        from analytics import last_24h_summary
        now = datetime.now()
        for i in range(5):
            self._write(f"s{i}.md", task=f"task-{i}", success=True,
                        ts=now - timedelta(minutes=i + 1))
        out = last_24h_summary(now=now)
        # task-0 is newest, task-4 is oldest
        assert len(out["top_successes"]) == 3
        assert "task-0" in out["top_successes"][0]
        assert "task-2" in out["top_successes"][2]

    def test_pending_approval_surfaces(self, monkeypatch):
        import policy
        monkeypatch.setattr(
            policy, "get_engine",
            lambda: type("E", (), {"has_pending_approval": staticmethod(lambda: True)})()
        )
        from analytics import last_24h_summary
        out = last_24h_summary()
        assert out["pending_approval"] is True
        assert out["idle"] is False  # pending approval breaks the idle bit

    def test_blocked_count_from_queue(self, monkeypatch):
        from queue_manager import QueueTask
        import queue_manager
        blocked = [
            QueueTask(task_text="open #needs:foo", line_no=1, blocked_reason="needs foo"),
            QueueTask(task_text="other", line_no=2, blocked_reason=""),  # not blocked
            QueueTask(task_text="another #needs:bar", line_no=3, blocked_reason="needs bar"),
        ]
        monkeypatch.setattr(queue_manager, "read_queue_items", lambda: blocked)
        from analytics import last_24h_summary
        out = last_24h_summary()
        assert out["blocked_count"] == 2
        assert out["idle"] is False
