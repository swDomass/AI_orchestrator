"""Tests for PR-Babysitter tool (P2) — sweep + state + tag parsing."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.pr_babysitter import (
    PRBabysitterTool,
    _ci_overall_status,
    _detect_change,
    _latest_commit_sha,
    _load_state,
    _parse_labels,
    _parse_mode,
    _parse_repos,
    _save_state,
    _state_path,
    sweep,
)


@pytest.fixture(autouse=True)
def _mock_dotenv():
    with patch("config._load_dotenv"):
        yield


# ── Tag parsing ──────────────────────────────────────────────────────────────

class TestTagParsing:
    def test_parse_repos_from_tag(self):
        repos = _parse_repos("sweep #repos:user/a,user/b #pr-mode:queue")
        assert repos == ["user/a", "user/b"]

    def test_parse_repos_fallback_to_env(self, monkeypatch):
        monkeypatch.setattr("tools.pr_babysitter.PR_BABYSITTER_REPOS", ["env/repo"])
        assert _parse_repos("sweep") == ["env/repo"]

    def test_parse_labels(self):
        assert _parse_labels("sweep #pr-labels:auto-fix,bot-ok") == ["auto-fix", "bot-ok"]
        assert _parse_labels("no labels here") == []

    def test_parse_mode_default(self):
        assert _parse_mode("sweep") == "queue"

    def test_parse_mode_report_only(self):
        assert _parse_mode("sweep #pr-mode:report-only") == "report-only"


# ── CI status reduction ──────────────────────────────────────────────────────

class TestCiStatus:
    def test_none_when_no_rollup(self):
        assert _ci_overall_status({}) == "none"
        assert _ci_overall_status({"statusCheckRollup": []}) == "none"

    def test_failure_beats_everything(self):
        rollup = [
            {"conclusion": "success"},
            {"conclusion": "failure"},
            {"status": "in_progress"},
        ]
        assert _ci_overall_status({"statusCheckRollup": rollup}) == "failed"

    def test_pending_when_no_failure(self):
        rollup = [{"conclusion": "success"}, {"status": "in_progress"}]
        assert _ci_overall_status({"statusCheckRollup": rollup}) == "pending"

    def test_passed_when_all_success(self):
        rollup = [{"conclusion": "success"}, {"state": "SUCCESS"}]
        assert _ci_overall_status({"statusCheckRollup": rollup}) == "passed"

    def test_status_context_state_failure(self):
        rollup = [{"state": "FAILURE"}]
        assert _ci_overall_status({"statusCheckRollup": rollup}) == "failed"


def test_latest_commit_sha():
    pr = {"commits": [{"oid": "111"}, {"oid": "222"}, {"oid": "333abc"}]}
    assert _latest_commit_sha(pr) == "333abc"


# ── Change detection ─────────────────────────────────────────────────────────

class TestDetectChange:
    def test_no_prev_no_signal(self):
        pr = {"comments": [], "statusCheckRollup": [{"conclusion": "success"}]}
        assert _detect_change({}, pr) == ""

    def test_no_prev_with_failure(self):
        pr = {"comments": [], "statusCheckRollup": [{"conclusion": "failure"}],
              "commits": [{"oid": "abc1234"}]}
        reason = _detect_change({}, pr)
        assert "CI failure" in reason

    def test_no_prev_with_existing_comments(self):
        pr = {"comments": [{"id": 1}, {"id": 2}]}
        reason = _detect_change({}, pr)
        assert "2 existing comment" in reason

    def test_new_comment_increments(self):
        prev = {"last_seen_comment_count": 2, "last_check_status": "passed",
                "last_seen_commit_sha": "abc"}
        pr = {"comments": [{}, {}, {}, {}], "statusCheckRollup": [],
              "commits": [{"oid": "abc"}]}
        reason = _detect_change(prev, pr)
        assert "2 new comment" in reason

    def test_ci_flipped_to_failure(self):
        prev = {"last_seen_comment_count": 0, "last_check_status": "passed",
                "last_seen_commit_sha": "abc"}
        pr = {"comments": [], "statusCheckRollup": [{"conclusion": "failure"}],
              "commits": [{"oid": "abc"}]}
        reason = _detect_change(prev, pr)
        assert "CI flipped to failure" in reason

    def test_no_change(self):
        prev = {"last_seen_comment_count": 2, "last_check_status": "passed",
                "last_seen_commit_sha": "abc"}
        pr = {"comments": [{}, {}], "statusCheckRollup": [{"conclusion": "success"}],
              "commits": [{"oid": "abc"}]}
        assert _detect_change(prev, pr) == ""


# ── State persistence ────────────────────────────────────────────────────────

class TestState:
    def test_load_missing_returns_empty(self, tmp_path):
        s = _load_state(tmp_path)
        assert s == {"version": 1, "prs": {}}

    def test_save_then_load(self, tmp_path):
        _save_state(tmp_path, {"version": 1, "prs": {"u/r#1": {"x": 1}}})
        loaded = _load_state(tmp_path)
        assert loaded["prs"]["u/r#1"] == {"x": 1}

    def test_corrupt_state_recovered(self, tmp_path):
        p = _state_path(tmp_path)
        p.parent.mkdir(parents=True)
        p.write_text("not json", encoding="utf-8")
        assert _load_state(tmp_path) == {"version": 1, "prs": {}}


# ── sweep() integration with mocks ────────────────────────────────────────────

def _pr_list(*items):
    """Build a list of PR dicts matching the gh shape."""
    return list(items)


class TestSweep:
    def test_first_sight_failure_queues(self, tmp_path):
        prs = _pr_list({"number": 42, "title": "fix", "headRefName": "f",
                        "labels": []})
        detail = {"number": 42, "comments": [],
                  "statusCheckRollup": [{"conclusion": "failure"}],
                  "commits": [{"oid": "abc1234"}]}
        queued: list[str] = []

        summary = sweep(
            ["user/repo"], cwd=tmp_path,
            list_open_prs_fn=lambda repo, **k: (prs, ""),
            view_pr_fn=lambda repo, n, **k: (detail, ""),
            append_task_fn=lambda line: queued.append(line) or True,
            send_message_fn=lambda msg: None,
        )
        assert summary["checked_prs"] == 1
        assert len(summary["queued"]) == 1
        assert "PR #42" in summary["queued"][0]
        assert "#tool:dev-loop" in summary["queued"][0]
        assert queued == summary["queued"]

    def test_no_change_skips(self, tmp_path):
        # Seed prior state matching the current detail
        _save_state(tmp_path, {
            "version": 1,
            "prs": {"u/r#7": {
                "last_seen_comment_count": 0,
                "last_check_status": "passed",
                "last_seen_commit_sha": "abc",
            }},
        })
        prs = _pr_list({"number": 7, "labels": []})
        detail = {"number": 7, "comments": [],
                  "statusCheckRollup": [{"conclusion": "success"}],
                  "commits": [{"oid": "abc"}]}
        queued: list[str] = []
        summary = sweep(
            ["u/r"], cwd=tmp_path,
            list_open_prs_fn=lambda r, **k: (prs, ""),
            view_pr_fn=lambda r, n, **k: (detail, ""),
            append_task_fn=lambda line: queued.append(line) or True,
            send_message_fn=lambda msg: None,
        )
        assert summary["queued"] == []
        assert any("no change" in s for s in summary["skipped"])

    def test_cooldown_suppresses_duplicate(self, tmp_path):
        recent = datetime.now() - timedelta(minutes=15)
        _save_state(tmp_path, {
            "version": 1,
            "prs": {"u/r#9": {
                "last_seen_comment_count": 0,
                "last_check_status": "passed",
                "last_seen_commit_sha": "old",
                "last_queued_at": recent.isoformat(timespec="seconds"),
            }},
        })
        prs = _pr_list({"number": 9, "labels": []})
        detail = {"number": 9, "comments": [],
                  "statusCheckRollup": [{"conclusion": "failure"}],
                  "commits": [{"oid": "new"}]}
        queued: list[str] = []
        summary = sweep(
            ["u/r"], cwd=tmp_path,
            cooldown_hours=1,
            list_open_prs_fn=lambda r, **k: (prs, ""),
            view_pr_fn=lambda r, n, **k: (detail, ""),
            append_task_fn=lambda line: queued.append(line) or True,
            send_message_fn=lambda msg: None,
        )
        assert summary["queued"] == []
        assert any("cooldown" in s for s in summary["skipped"])

    def test_report_only_mode_no_queue(self, tmp_path):
        prs = _pr_list({"number": 11, "labels": []})
        detail = {"number": 11, "comments": [{}, {}], "statusCheckRollup": [],
                  "commits": [{"oid": "abc"}]}
        queued: list[str] = []
        reported: list[str] = []
        summary = sweep(
            ["u/r"], cwd=tmp_path, mode="report-only",
            list_open_prs_fn=lambda r, **k: (prs, ""),
            view_pr_fn=lambda r, n, **k: (detail, ""),
            append_task_fn=lambda line: queued.append(line) or True,
            send_message_fn=lambda msg: reported.append(msg),
        )
        assert summary["queued"] == []
        assert len(summary["reported"]) == 1
        assert any("PR-Babysitter" in r for r in reported)

    def test_list_error_propagates(self, tmp_path):
        summary = sweep(
            ["u/r"], cwd=tmp_path,
            list_open_prs_fn=lambda r, **k: ([], "gh_auth"),
            view_pr_fn=lambda r, n, **k: ({}, ""),
            append_task_fn=lambda l: True,
            send_message_fn=lambda m: None,
        )
        assert summary["errors"] == ["u/r: gh_auth"]
        assert summary["checked_prs"] == 0

    def test_idempotent_second_run_no_duplicate(self, tmp_path):
        """Two sequential runs with the same state must produce 1 queue item only."""
        prs = _pr_list({"number": 5, "labels": []})
        detail = {"number": 5, "comments": [], "statusCheckRollup": [{"conclusion": "failure"}],
                  "commits": [{"oid": "abc1234"}]}
        queued: list[str] = []
        for _ in range(2):
            sweep(
                ["u/r"], cwd=tmp_path,
                list_open_prs_fn=lambda r, **k: (prs, ""),
                view_pr_fn=lambda r, n, **k: (detail, ""),
                append_task_fn=lambda line: queued.append(line) or True,
                send_message_fn=lambda m: None,
            )
        # Cooldown kicks in on the second run → still exactly one item queued.
        assert len(queued) == 1


# ── PRBabysitterTool.run() integration ────────────────────────────────────────

class TestPRBabysitterTool:
    def test_missing_cwd_fails_cleanly(self):
        tool = PRBabysitterTool()
        result = tool.run("sweep", provider=None, cwd=None)
        assert not result.success
        assert result.error_code == "missing_cwd"

    def test_no_repos_fails_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tools.pr_babysitter.PR_BABYSITTER_REPOS", [])
        tool = PRBabysitterTool()
        result = tool.run("sweep", provider=None, cwd=str(tmp_path))
        assert not result.success
        assert result.error_code == "no_repos"

    def test_gh_auth_failure_surfaces_in_result(self, tmp_path, monkeypatch):
        # Simulate gh_auth error from list call
        from tools import pr_babysitter as pr_mod
        monkeypatch.setattr(pr_mod, "_load_state",
                            lambda cwd: {"version": 1, "prs": {}})
        monkeypatch.setattr(pr_mod, "_save_state", lambda cwd, state: None)

        def fake_sweep(repos, **kwargs):
            return {
                "checked_prs": 0, "queued": [], "reported": [],
                "errors": ["user/repo: gh_auth"], "skipped": [],
            }
        monkeypatch.setattr(pr_mod, "sweep", fake_sweep)

        tool = PRBabysitterTool()
        result = tool.run("sweep #repos:user/repo", provider=None, cwd=str(tmp_path))
        assert not result.success
        assert result.error_code == "gh_unavailable"
        assert "gh_auth" in result.error
