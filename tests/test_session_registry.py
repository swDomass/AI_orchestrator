"""Tests for the orchestrator session sidecar registry (Phase B3)."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_dotenv():
    with patch("config._load_dotenv"):
        yield


@pytest.fixture()
def registry_path(tmp_path, monkeypatch):
    """Redirect ORCH_SESSION_REGISTRY to a temp file for the duration of the test."""
    p = tmp_path / "orchestrator-sessions.jsonl"
    monkeypatch.setattr("session_registry.ORCH_SESSION_REGISTRY", p)
    return p


def test_register_session_appends_jsonl(registry_path):
    from session_registry import register_session, list_sessions
    register_session("uuid-1", "dev-loop", "/d/proj")
    register_session("uuid-2", "review-loop", "/d/proj2")

    sessions = list_sessions()
    assert len(sessions) == 2
    assert sessions[0]["uuid"] == "uuid-1"
    assert sessions[0]["tool"] == "dev-loop"
    assert sessions[1]["cwd"] == "/d/proj2"


def test_list_sessions_empty_when_no_file(registry_path):
    from session_registry import list_sessions
    assert list_sessions() == []


def test_is_orchestrator_session(registry_path):
    from session_registry import register_session, is_orchestrator_session
    register_session("known-uuid", "dev-loop", "/d/proj")

    assert is_orchestrator_session("known-uuid") is True
    assert is_orchestrator_session("unknown-uuid") is False


def test_list_sessions_skips_malformed_lines(registry_path):
    """A garbled JSONL line must not break the parser — skip it."""
    registry_path.write_text(
        '{"uuid":"good-1","tool":"x","cwd":"/p","created_at":"2026-05-01T10:00:00"}\n'
        '{"broken json missing brace\n'
        '{"uuid":"good-2","tool":"y","cwd":"/q","created_at":"2026-05-02T10:00:00"}\n',
        encoding="utf-8",
    )
    from session_registry import list_sessions
    sessions = list_sessions()
    assert len(sessions) == 2
    assert {s["uuid"] for s in sessions} == {"good-1", "good-2"}


def test_prune_old_separates_by_age(registry_path):
    from session_registry import prune_old
    fresh = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
    expired = (datetime.now() - timedelta(days=20)).isoformat(timespec="seconds")
    registry_path.write_text(
        f'{{"uuid":"fresh-1","tool":"x","cwd":"/p","created_at":"{fresh}"}}\n'
        f'{{"uuid":"expired-1","tool":"y","cwd":"/q","created_at":"{expired}"}}\n',
        encoding="utf-8",
    )

    kept, expired_entries = prune_old(retention_days=14)
    assert len(kept) == 1
    assert len(expired_entries) == 1
    assert kept[0]["uuid"] == "fresh-1"
    assert expired_entries[0]["uuid"] == "expired-1"

    # Registry was rewritten with kept-only entries
    from session_registry import list_sessions
    remaining = list_sessions()
    assert len(remaining) == 1
    assert remaining[0]["uuid"] == "fresh-1"


def test_prune_old_no_op_when_all_fresh(registry_path):
    from session_registry import prune_old, register_session
    register_session("u1", "dev-loop", "/p")
    kept, expired = prune_old(retention_days=14)
    assert len(kept) == 1
    assert expired == []


def test_prune_old_handles_malformed_timestamp(registry_path):
    """An entry with a bogus timestamp gets treated as expired (so we don't
    accumulate broken entries forever)."""
    registry_path.write_text(
        '{"uuid":"bad-ts","tool":"x","cwd":"/p","created_at":"not-a-date"}\n',
        encoding="utf-8",
    )
    from session_registry import prune_old
    kept, expired = prune_old(retention_days=14)
    assert kept == []
    assert len(expired) == 1
    assert expired[0]["uuid"] == "bad-ts"
