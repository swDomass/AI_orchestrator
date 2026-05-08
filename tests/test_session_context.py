"""Tests for SessionContext helper in tools/base_tool.py (Phase B5/B6)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_dotenv():
    with patch("config._load_dotenv"):
        yield


def _no_session_provider():
    return SimpleNamespace(supports_sessions=False, name="codex")


def _claude_provider():
    return SimpleNamespace(supports_sessions=True, name="claude")


def test_session_context_disabled_when_provider_unsupported(monkeypatch):
    from tools.base_tool import SessionContext
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)
    sess = SessionContext.create(_no_session_provider(), tool_name="dev-loop", cwd="/p")
    assert sess.enabled is False
    assert sess.uuid is None
    assert sess.first_call_kwargs() == {}
    assert sess.resume_kwargs() == {}


def test_session_context_disabled_when_feature_flag_off(monkeypatch):
    from tools.base_tool import SessionContext
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", False)
    sess = SessionContext.create(_claude_provider(), tool_name="dev-loop", cwd="/p")
    assert sess.enabled is False


def test_session_context_active_when_both_conditions_met(monkeypatch, tmp_path):
    from tools.base_tool import SessionContext
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)
    monkeypatch.setattr("session_registry.ORCH_SESSION_REGISTRY",
                        tmp_path / "orch-sessions.jsonl")
    sess = SessionContext.create(_claude_provider(), tool_name="dev-loop", cwd="/p")
    assert sess.enabled is True
    assert sess.uuid is not None
    # First call uses --session-id (creates), subsequent --resume
    fc = sess.first_call_kwargs()
    assert fc["session_id"] == sess.uuid
    assert fc["resume"] is False
    rk = sess.resume_kwargs()
    assert rk["session_id"] == sess.uuid
    assert rk["resume"] is True


def test_session_context_registers_uuid_in_sidecar(monkeypatch, tmp_path):
    """When enabled, SessionContext.create registers the UUID so heartbeat
    cleanup can recognize it as orchestrator-created."""
    registry = tmp_path / "orch-sessions.jsonl"
    from tools.base_tool import SessionContext
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)
    monkeypatch.setattr("session_registry.ORCH_SESSION_REGISTRY", registry)

    sess = SessionContext.create(_claude_provider(), tool_name="dev-loop", cwd="/p")

    from session_registry import is_orchestrator_session
    assert is_orchestrator_session(sess.uuid) is True


def test_session_context_bump_and_rollover(monkeypatch, tmp_path):
    from tools.base_tool import SessionContext
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)
    monkeypatch.setattr("session_registry.ORCH_SESSION_REGISTRY",
                        tmp_path / "orch-sessions.jsonl")

    sess = SessionContext.create(_claude_provider(), tool_name="dev-loop", cwd="/p", cap=3)
    original_uuid = sess.uuid

    sess.bump(); assert sess.needs_rollover() is False  # 1
    sess.bump(); assert sess.needs_rollover() is False  # 2
    sess.bump(); assert sess.needs_rollover() is True   # 3 → cap reached

    sess.rollover(tool_name="dev-loop", cwd="/p")
    assert sess.uuid != original_uuid  # fresh UUID
    assert sess.iteration_count == 0   # counter reset
    assert sess.needs_rollover() is False


def test_session_context_disabled_never_rolls_over(monkeypatch):
    from tools.base_tool import SessionContext
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", False)
    sess = SessionContext.create(_claude_provider(), tool_name="dev-loop", cwd="/p", cap=1)
    sess.bump(); sess.bump(); sess.bump()
    # cap-based rollover only applies when sessions are enabled
    assert sess.needs_rollover() is False


def test_session_context_rollover_no_op_when_disabled(monkeypatch):
    from tools.base_tool import SessionContext
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", False)
    sess = SessionContext.create(_claude_provider(), tool_name="dev-loop", cwd="/p")
    # Should not raise even though not enabled
    sess.rollover(tool_name="dev-loop", cwd="/p")
    assert sess.uuid is None
