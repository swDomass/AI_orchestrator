"""Tests for PR-Babysitter slash-command helpers (P5)."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.pr_babysitter import (
    _load_state,
    _parse_pr_key,
    _save_state,
    cmd_pr_fix,
    cmd_pr_ignore,
    sweep,
)


@pytest.fixture(autouse=True)
def _mock_dotenv():
    with patch("config._load_dotenv"):
        yield


# ── _parse_pr_key ─────────────────────────────────────────────────────────────

class TestParsePrKey:
    def test_valid(self):
        assert _parse_pr_key("user/repo#42") == ("user/repo", 42)

    def test_strips_whitespace(self):
        assert _parse_pr_key("  org/r1#7  ") == ("org/r1", 7)

    def test_invalid_returns_none(self):
        assert _parse_pr_key("") is None
        assert _parse_pr_key("just-text") is None
        assert _parse_pr_key("user/repo") is None  # missing #N
        assert _parse_pr_key("#42") is None        # missing repo

    def test_repo_with_dots_and_dashes(self):
        assert _parse_pr_key("my.org/my-repo#1") == ("my.org/my-repo", 1)


# ── cmd_pr_fix ────────────────────────────────────────────────────────────────

class TestCmdPrFix:
    def test_appends_task(self, tmp_path):
        queued: list[str] = []
        ok, msg = cmd_pr_fix(
            "user/repo#42", tmp_path,
            append_task_fn=lambda line: queued.append(line) or True,
        )
        assert ok is True
        assert len(queued) == 1
        assert "PR #42" in queued[0]
        assert "user/repo" in queued[0]
        assert "#tool:dev-loop" in queued[0]
        assert f"cwd:{tmp_path}" in queued[0]
        assert "queued:" in msg

    def test_marks_cooldown_in_state(self, tmp_path):
        cmd_pr_fix(
            "user/repo#42", tmp_path,
            append_task_fn=lambda line: True,
            now=datetime(2026, 5, 18, 12, 0, 0),
        )
        state = _load_state(tmp_path)
        entry = state["prs"]["user/repo#42"]
        assert entry["last_queued_at"] == "2026-05-18T12:00:00"
        assert entry["last_queue_reason"] == "manual /pr-fix"

    def test_bad_key_fails_clean(self, tmp_path):
        queued: list[str] = []
        ok, msg = cmd_pr_fix(
            "garbage", tmp_path,
            append_task_fn=lambda line: queued.append(line) or True,
        )
        assert ok is False
        assert queued == []
        assert "unrecognized" in msg

    def test_append_failure_returns_false(self, tmp_path):
        ok, msg = cmd_pr_fix(
            "user/repo#1", tmp_path,
            append_task_fn=lambda line: False,
        )
        assert ok is False
        assert "queue append failed" in msg


# ── cmd_pr_ignore ─────────────────────────────────────────────────────────────

class TestCmdPrIgnore:
    def test_sets_ignore_marker(self, tmp_path):
        ok, msg = cmd_pr_ignore(
            "user/repo#7", tmp_path,
            now=datetime(2026, 5, 18, 12, 0, 0),
        )
        assert ok is True
        assert "ignored user/repo#7" in msg
        state = _load_state(tmp_path)
        entry = state["prs"]["user/repo#7"]
        assert entry["last_ignored_at"] == "2026-05-18T12:00:00"
        assert entry["last_queued_at"] == "2026-05-18T12:00:00"

    def test_bad_key_fails_clean(self, tmp_path):
        ok, msg = cmd_pr_ignore("not-a-key", tmp_path)
        assert ok is False
        assert "unrecognized" in msg

    def test_ignore_then_sweep_skips_due_to_cooldown(self, tmp_path):
        """Ignored PR within cooldown window must not queue on next sweep."""
        cmd_pr_ignore("user/repo#1", tmp_path,
                      now=datetime.now() - timedelta(minutes=10))

        prs = [{"number": 1, "labels": []}]
        # Provide a PR detail that WOULD trigger (failing CI) — sweep should still skip.
        detail = {"number": 1, "comments": [{"id": 99}],
                  "statusCheckRollup": [{"conclusion": "failure"}],
                  "commits": [{"oid": "newsha"}]}
        queued: list[str] = []
        summary = sweep(
            ["user/repo"], cwd=tmp_path,
            cooldown_hours=1,
            list_open_prs_fn=lambda r, **k: (prs, ""),
            view_pr_fn=lambda r, n, **k: (detail, ""),
            append_task_fn=lambda line: queued.append(line) or True,
            send_message_fn=lambda m: None,
        )
        assert queued == []
        assert any("cooldown" in s for s in summary["skipped"])


# ── report-only mode emits action commands in telegram message ───────────────

def test_report_only_message_contains_slash_commands(tmp_path):
    prs = [{"number": 5, "labels": []}]
    detail = {"number": 5, "comments": [{}, {}],
              "statusCheckRollup": [], "commits": [{"oid": "abc"}]}
    captured: list[str] = []
    sweep(
        ["user/repo"], cwd=tmp_path, mode="report-only",
        list_open_prs_fn=lambda r, **k: (prs, ""),
        view_pr_fn=lambda r, n, **k: (detail, ""),
        append_task_fn=lambda line: True,
        send_message_fn=lambda m: captured.append(m),
    )
    assert len(captured) == 1
    assert "/pr-fix user/repo#5" in captured[0]
    assert "/pr-ignore user/repo#5" in captured[0]
