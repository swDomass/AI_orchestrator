"""Tests for gh CLI wrapper (gh_helpers.py) — shared by P2 + P4."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import gh_helpers


@pytest.fixture(autouse=True)
def _mock_dotenv():
    with patch("config._load_dotenv"):
        yield


def _fake_proc(*, returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ── _run_gh error classification ──────────────────────────────────────────────

class TestRunGh:
    def test_gh_missing_returns_typed_error(self, monkeypatch):
        monkeypatch.setattr(gh_helpers, "gh_available", lambda: False)
        out, err = gh_helpers._run_gh(["pr", "list"], timeout_sec=5)
        assert out == ""
        assert err == "gh_not_found"

    def test_success(self, monkeypatch):
        monkeypatch.setattr(gh_helpers, "gh_available", lambda: True)
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_proc(stdout="ok"))
        out, err = gh_helpers._run_gh(["pr", "list"], timeout_sec=5)
        assert out == "ok"
        assert err == ""

    def test_auth_error(self, monkeypatch):
        monkeypatch.setattr(gh_helpers, "gh_available", lambda: True)
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: _fake_proc(returncode=1, stderr="not logged into github.com"),
        )
        _, err = gh_helpers._run_gh(["pr", "list"], timeout_sec=5)
        assert err == "gh_auth"

    def test_repo_not_found(self, monkeypatch):
        monkeypatch.setattr(gh_helpers, "gh_available", lambda: True)
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: _fake_proc(returncode=1, stderr="HTTP 404: Not Found"),
        )
        _, err = gh_helpers._run_gh(["pr", "view", "1"], timeout_sec=5)
        assert err == "gh_not_found_repo"

    def test_timeout(self, monkeypatch):
        monkeypatch.setattr(gh_helpers, "gh_available", lambda: True)
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="gh", timeout=5)
        monkeypatch.setattr(subprocess, "run", boom)
        _, err = gh_helpers._run_gh(["pr", "list"], timeout_sec=5)
        assert err == "gh_timeout"

    def test_generic_error_preserves_stderr(self, monkeypatch):
        monkeypatch.setattr(gh_helpers, "gh_available", lambda: True)
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: _fake_proc(returncode=1, stderr="weird gh failure XYZ"),
        )
        _, err = gh_helpers._run_gh(["pr", "list"], timeout_sec=5)
        assert err.startswith("gh_error:")
        assert "weird gh failure XYZ" in err


# ── list_open_prs ─────────────────────────────────────────────────────────────

class TestListOpenPrs:
    def test_filters_by_label(self, monkeypatch):
        payload = json.dumps([
            {"number": 1, "title": "fix", "headRefName": "f1",
             "labels": [{"name": "auto-fix"}]},
            {"number": 2, "title": "other", "headRefName": "f2",
             "labels": [{"name": "draft"}]},
            {"number": 3, "title": "another", "headRefName": "f3",
             "labels": [{"name": "auto-fix"}, {"name": "blocked"}]},
        ])
        monkeypatch.setattr(gh_helpers, "_run_gh", lambda *a, **k: (payload, ""))
        prs, err = gh_helpers.list_open_prs("u/r", timeout_sec=5, labels=["auto-fix"])
        assert err == ""
        assert [p["number"] for p in prs] == [1, 3]

    def test_no_filter_returns_all(self, monkeypatch):
        monkeypatch.setattr(gh_helpers, "_run_gh",
                            lambda *a, **k: (json.dumps([{"number": 1, "labels": []}]), ""))
        prs, err = gh_helpers.list_open_prs("u/r", timeout_sec=5)
        assert err == ""
        assert len(prs) == 1

    def test_propagates_gh_error(self, monkeypatch):
        monkeypatch.setattr(gh_helpers, "_run_gh", lambda *a, **k: ("", "gh_auth"))
        prs, err = gh_helpers.list_open_prs("u/r", timeout_sec=5)
        assert prs == []
        assert err == "gh_auth"

    def test_bad_json(self, monkeypatch):
        monkeypatch.setattr(gh_helpers, "_run_gh", lambda *a, **k: ("not json", ""))
        prs, err = gh_helpers.list_open_prs("u/r", timeout_sec=5)
        assert prs == []
        assert err.startswith("gh_bad_json")


# ── view_pr ───────────────────────────────────────────────────────────────────

class TestViewPr:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            gh_helpers, "_run_gh",
            lambda *a, **k: (json.dumps({"number": 42, "comments": []}), ""),
        )
        data, err = gh_helpers.view_pr("u/r", 42, timeout_sec=5)
        assert err == ""
        assert data["number"] == 42


# ── list_failed_runs (P4) ─────────────────────────────────────────────────────

class TestListFailedRuns:
    def test_returns_runs(self, monkeypatch):
        payload = json.dumps([
            {"databaseId": 11, "headBranch": "main", "headSha": "abc"},
            {"databaseId": 12, "headBranch": "main", "headSha": "def"},
        ])
        monkeypatch.setattr(gh_helpers, "_run_gh", lambda *a, **k: (payload, ""))
        runs, err = gh_helpers.list_failed_runs("u/r", timeout_sec=5)
        assert err == ""
        assert [r["databaseId"] for r in runs] == [11, 12]
