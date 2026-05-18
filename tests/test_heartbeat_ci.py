"""Tests for the CI-Failure-Watcher heartbeat handler (P4)."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import ci_watcher
import heartbeat


@pytest.fixture(autouse=True)
def _mock_dotenv():
    with patch("config._load_dotenv"):
        yield


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Redirect the on-disk state file to a tmp path."""
    monkeypatch.setattr(ci_watcher, "_STATE_FILE", tmp_path / "ci-watcher-state.json")


# ── state persistence ────────────────────────────────────────────────────────

class TestState:
    def test_load_missing_returns_empty(self):
        assert ci_watcher._load_state() == {"version": 1, "repos": {}}

    def test_roundtrip(self):
        ci_watcher._save_state({"version": 1, "repos": {"u/r": {"x": 1}}})
        loaded = ci_watcher._load_state()
        assert loaded["repos"] == {"u/r": {"x": 1}}

    def test_corrupt_recovered(self):
        ci_watcher._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ci_watcher._STATE_FILE.write_text("garbage", encoding="utf-8")
        assert ci_watcher._load_state() == {"version": 1, "repos": {}}


# ── _build_queue_line ─────────────────────────────────────────────────────────

class TestBuildQueueLine:
    def test_without_local_path(self, monkeypatch):
        monkeypatch.setattr(ci_watcher, "CI_WATCHER_REPO_PATHS", {})
        line = ci_watcher._build_queue_line("user/repo", {
            "headBranch": "main", "headSha": "abc1234567",
            "displayTitle": "Tests failed",
        })
        assert "user/repo" in line
        assert "@main" in line
        assert "abc1234" in line
        assert "Tests failed" in line
        assert "#tool:dev-loop" in line
        assert "cwd:" not in line

    def test_with_local_path(self, monkeypatch):
        monkeypatch.setattr(
            ci_watcher, "CI_WATCHER_REPO_PATHS",
            {"user/repo": "D:/proj/repo"},
        )
        line = ci_watcher._build_queue_line("user/repo", {
            "headBranch": "main", "headSha": "abc1234",
            "displayTitle": "build",
        })
        assert "cwd:D:/proj/repo" in line

    def test_truncates_long_title(self, monkeypatch):
        monkeypatch.setattr(ci_watcher, "CI_WATCHER_REPO_PATHS", {})
        long_title = "x" * 200
        line = ci_watcher._build_queue_line("u/r", {
            "headBranch": "f", "headSha": "abc",
            "displayTitle": long_title,
        })
        # Title gets clipped to 80; check it's not the full 200
        assert len(line) < 200


# ── sweep_once ────────────────────────────────────────────────────────────────

class TestSweepOnce:
    def test_no_repos_no_work(self):
        summary = ci_watcher.sweep_once(repos=[])
        assert summary["checked_repos"] == 0
        assert summary["queued"] == []

    def test_first_failure_queues(self):
        runs = [{"databaseId": 1, "headBranch": "main", "headSha": "abc1234",
                 "displayTitle": "build failed"}]
        queued: list[str] = []
        summary = ci_watcher.sweep_once(
            repos=["u/r"],
            list_failed_runs_fn=lambda repo, **k: (runs, ""),
            append_task_fn=lambda line: queued.append(line) or True,
        )
        assert summary["checked_repos"] == 1
        assert len(summary["queued"]) == 1
        assert "u/r" in summary["queued"][0]
        assert queued == summary["queued"]

    def test_dedup_same_commit(self):
        """Two failing runs on the same SHA → only 1 queue item."""
        runs = [
            {"databaseId": 1, "headBranch": "main", "headSha": "abc",
             "displayTitle": "test"},
            {"databaseId": 2, "headBranch": "main", "headSha": "abc",
             "displayTitle": "lint"},
        ]
        queued: list[str] = []
        summary = ci_watcher.sweep_once(
            repos=["u/r"],
            list_failed_runs_fn=lambda repo, **k: (runs, ""),
            append_task_fn=lambda line: queued.append(line) or True,
        )
        assert len(queued) == 1
        # Second run is suppressed — exact reason is implementation detail
        # (in-same-sweep cooldown OR already-queued marker), both prove dedup.
        assert any(("cooldown" in s) or ("already queued" in s)
                   for s in summary["skipped"])

    def test_second_sweep_skips_seen_sha(self):
        runs = [{"databaseId": 1, "headBranch": "main", "headSha": "abc",
                 "displayTitle": "ci"}]
        queued: list[str] = []
        for _ in range(2):
            ci_watcher.sweep_once(
                repos=["u/r"],
                list_failed_runs_fn=lambda repo, **k: (runs, ""),
                append_task_fn=lambda line: queued.append(line) or True,
            )
        assert len(queued) == 1

    def test_cooldown_blocks_requeue(self):
        # Seed state with a SHA queued 10 minutes ago
        now = datetime.now()
        ci_watcher._save_state({
            "version": 1,
            "repos": {"u/r": {"seen_shas": {
                "abc": {"queued_at": (now - timedelta(minutes=10)).isoformat(timespec="seconds")}
            }}},
        })
        # Pretend we forgot the queued_at marker but the SHA was previously seen
        # → cooldown path should still kick in.
        runs = [{"databaseId": 1, "headBranch": "main", "headSha": "abc",
                 "displayTitle": "ci"}]
        queued: list[str] = []
        summary = ci_watcher.sweep_once(
            repos=["u/r"],
            cooldown_hours=1,
            list_failed_runs_fn=lambda repo, **k: (runs, ""),
            append_task_fn=lambda line: queued.append(line) or True,
            now=now,
        )
        assert queued == []
        assert any("cooldown" in s for s in summary["skipped"])

    def test_gh_error_propagates(self):
        summary = ci_watcher.sweep_once(
            repos=["u/r"],
            list_failed_runs_fn=lambda repo, **k: ([], "gh_auth"),
            append_task_fn=lambda line: True,
        )
        assert summary["errors"] == ["u/r: gh_auth"]
        assert summary["queued"] == []

    def test_runs_without_sha_skipped(self):
        runs = [{"databaseId": 1, "headBranch": "main", "headSha": "",
                 "displayTitle": "no-sha"}]
        summary = ci_watcher.sweep_once(
            repos=["u/r"],
            list_failed_runs_fn=lambda repo, **k: (runs, ""),
            append_task_fn=lambda line: True,
        )
        assert summary["queued"] == []


# ── Heartbeat integration ─────────────────────────────────────────────────────

class TestHeartbeatHandler:
    def test_label_maps_to_handler(self):
        assert heartbeat._match_handler_key("check-ci-failures: gh runs") == "_check_ci_failures"

    def test_returns_none_when_no_repos(self, monkeypatch):
        monkeypatch.setattr("config.CI_WATCHER_REPOS", [])
        assert heartbeat._check_ci_failures() is None

    def test_returns_none_when_quiet(self, monkeypatch):
        monkeypatch.setattr("config.CI_WATCHER_REPOS", ["u/r"])
        monkeypatch.setattr(
            ci_watcher, "sweep_once",
            lambda **k: {"checked_repos": 1, "queued": [], "skipped": [], "errors": []},
        )
        assert heartbeat._check_ci_failures() is None

    def test_formats_summary_when_queued(self, monkeypatch):
        monkeypatch.setattr("config.CI_WATCHER_REPOS", ["u/r"])
        monkeypatch.setattr(
            ci_watcher, "sweep_once",
            lambda **k: {
                "checked_repos": 1,
                "queued": ["CI failure (u/r@main, abc): build #tool:dev-loop"],
                "skipped": [],
                "errors": [],
            },
        )
        out = heartbeat._check_ci_failures()
        assert out is not None
        assert "CI-Watcher queued 1 fix task" in out
        assert "u/r" in out

    def test_includes_errors_in_summary(self, monkeypatch):
        monkeypatch.setattr("config.CI_WATCHER_REPOS", ["u/r"])
        monkeypatch.setattr(
            ci_watcher, "sweep_once",
            lambda **k: {
                "checked_repos": 1, "queued": [], "skipped": [],
                "errors": ["u/r: gh_auth"],
            },
        )
        out = heartbeat._check_ci_failures()
        assert out is not None
        assert "gh_auth" in out

    def test_handler_swallows_exceptions(self, monkeypatch):
        monkeypatch.setattr("config.CI_WATCHER_REPOS", ["u/r"])
        monkeypatch.setattr(
            ci_watcher, "sweep_once",
            lambda **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        # Even when sweep raises, the handler returns None (no crash).
        assert heartbeat._check_ci_failures() is None
