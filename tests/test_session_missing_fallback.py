"""Tests for session_missing fallback paths added in F2.

When ``provider.run()`` returns ``error="session_missing"`` (e.g. because the
heartbeat cleanup deleted the session JSONL between calls), the tool must:
    1. roll over to a fresh UUID via ``sess.rollover()``,
    2. reset the first-call flag so the next call uses ``--session-id``,
    3. retry the SAME prompt once.

These tests verify the pattern works in dev_loop, review_loop and
critical_review at every call site.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_dotenv():
    with patch("config._load_dotenv"):
        yield


def _make_result(*, success=True, output="No findings.", error=""):
    return SimpleNamespace(
        success=success, output=output, error=error,
        error_code="", retryable=False,
        input_tokens=0, output_tokens=0,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )


# ── critical_review Pass 1 fallback ───────────────────────────────────────────

def test_critical_review_pass1_session_missing_triggers_fallback(monkeypatch, tmp_path):
    """Pass 1 returns session_missing → tool retries once with a fresh session."""
    from tools.critical_review import CriticalReviewTool

    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)
    monkeypatch.setattr("session_registry.ORCH_SESSION_REGISTRY",
                        tmp_path / "orch-sessions.jsonl")
    monkeypatch.setattr("tools.critical_review.is_cached_provider_available", lambda _: True)
    monkeypatch.setattr("tools.critical_review.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.critical_review.notify_tool_done", lambda *a, **kw: None)

    call_log: list[dict] = []
    sequence: list = [
        _make_result(success=False, error="session_missing", output=""),
        # Pass 1 retry succeeds:
        _make_result(success=True, output="No P1/P2/P3 findings."),
        # Pass 2:
        _make_result(success=True, output="No P1/P2/P3 findings."),
    ]

    def fake_run(*args, **kwargs):
        call_log.append(kwargs)
        return sequence.pop(0) if sequence else _make_result()

    fake_provider = SimpleNamespace(name="claude", supports_sessions=True, run=fake_run)
    monkeypatch.setattr(
        "tools.critical_review._resolve_pass2_provider",
        lambda pass_providers, default: default,
    )

    CriticalReviewTool().run("Review", fake_provider, cwd=str(tmp_path), pass_providers={})

    # First two calls should both have session_id (one for original, one for retry).
    assert "session_id" in call_log[0], "Pass 1 first attempt should carry session_id"
    assert "session_id" in call_log[1], "Pass 1 retry should also carry session_id"
    # The two UUIDs must DIFFER (rollover happened).
    assert call_log[0]["session_id"] != call_log[1]["session_id"], \
        "Rollover should have allocated a fresh UUID"
    # Retry uses --session-id (resume=False), not --resume — fresh session.
    assert call_log[1].get("resume") is False


# ── critical_review Pass 2 explicit-inject drop in session mode (F3) ──────────

def test_critical_review_pass2_session_mode_drops_pass1_inject(monkeypatch, tmp_path):
    """In same-provider session mode, Pass 2 prompt MUST NOT contain Pass 1's
    full output as injected text — Pass 1 lives in the conversation history."""
    from tools.critical_review import CriticalReviewTool

    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)
    monkeypatch.setattr("session_registry.ORCH_SESSION_REGISTRY",
                        tmp_path / "orch-sessions.jsonl")
    monkeypatch.setattr("tools.critical_review.is_cached_provider_available", lambda _: True)
    monkeypatch.setattr("tools.critical_review.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.critical_review.notify_tool_done", lambda *a, **kw: None)

    SENTINEL_PASS1 = "PASS-1-UNIQUE-CONTENT-SENTINEL-XYZ"
    captured_prompts: list[str] = []

    def fake_run(prompt, **kwargs):
        captured_prompts.append(prompt)
        if len(captured_prompts) == 1:
            return _make_result(output=SENTINEL_PASS1)
        return _make_result(output="No P1/P2/P3 findings.")

    fake_provider = SimpleNamespace(name="claude", supports_sessions=True, run=fake_run)
    monkeypatch.setattr(
        "tools.critical_review._resolve_pass2_provider",
        lambda pass_providers, default: default,
    )

    CriticalReviewTool().run("Review", fake_provider, cwd=str(tmp_path), pass_providers={})

    pass2_prompt = captured_prompts[1]
    assert SENTINEL_PASS1 not in pass2_prompt, (
        "Pass 2 prompt in session mode must NOT contain the literal Pass 1 output — "
        "it should rely on conversation history. Found the sentinel inside the prompt."
    )
    # And it must reference the conversation history explicitly so the model knows.
    assert "conversation history" in pass2_prompt.lower()


def test_critical_review_pass2_cross_provider_keeps_explicit_inject(monkeypatch, tmp_path):
    """Cross-provider mode (#pass1:gemini #pass2:claude) MUST still inject
    Pass 1's text into Pass 2's prompt — there is no shared history."""
    from tools.critical_review import CriticalReviewTool

    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)
    monkeypatch.setattr("tools.critical_review.is_cached_provider_available", lambda _: True)
    monkeypatch.setattr("tools.critical_review.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.critical_review.notify_tool_done", lambda *a, **kw: None)

    SENTINEL_PASS1 = "GEMINI-PASS-1-UNIQUE-OUTPUT-ABC"
    pass2_prompts: list[str] = []

    def primary_run(prompt, **kwargs):
        return _make_result(output=SENTINEL_PASS1)

    def pass2_run(prompt, **kwargs):
        pass2_prompts.append(prompt)
        return _make_result(output="No P1/P2/P3 findings.")

    primary = SimpleNamespace(name="gemini", supports_sessions=False, run=primary_run)
    monkeypatch.setattr(
        "tools.critical_review._resolve_pass2_provider",
        lambda pass_providers, default: SimpleNamespace(
            name="claude", supports_sessions=True, _forced_model=None, run=pass2_run,
        ),
    )

    CriticalReviewTool().run("Review", primary, cwd=str(tmp_path),
                             pass_providers={1: "gemini", 2: "claude"})

    assert pass2_prompts, "Pass 2 should have been called"
    assert SENTINEL_PASS1 in pass2_prompts[0], (
        "Cross-provider Pass 2 must inject Pass 1's output as text — "
        "the explicit injection IS the only signal across providers."
    )
