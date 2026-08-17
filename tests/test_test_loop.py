from types import SimpleNamespace

from tools.test_loop import _tests_passed, TestLoopTool
from tools.base_tool import ToolResult


def test_tests_passed_accepts_zero_failed_zero_errors_summary():
    output = "==================== 12 passed, 0 failed, 0 errors in 3.21s ===================="
    assert _tests_passed(output) is True


def test_tests_passed_rejects_nonzero_failed_summary():
    output = "==================== 10 passed, 2 failed, 0 errors in 3.21s ===================="
    assert _tests_passed(output) is False


def test_tests_passed_returns_false_for_unknown_output_with_failure_keywords():
    # "failed" in output should NOT return True — that would be inverted logic
    assert _tests_passed("some error occurred during test run") is False


def test_tests_passed_returns_false_for_unknown_output_without_keywords():
    # Unknown format with no success or failure markers → assume failed
    assert _tests_passed("some unknown test runner output") is False


class _ScriptedProvider:
    """Records prompts; returns a canned ToolResult per call."""
    name = "claude"
    supports_sessions = False

    def __init__(self):
        self.prompts: list[str] = []

    def run(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return ToolResult(success=True, output="1 passed in 0.1s")


def test_test_loop_aborts_on_runtime_deadline(monkeypatch, tmp_path):
    """Total-runtime deadline already passed → abort iteration 1 with
    tool_runtime_exceeded instead of running all iterations (mirrors
    test_review_loop_aborts_on_runtime_deadline)."""
    monkeypatch.setattr("tools.test_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.test_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.test_loop.time.sleep", lambda _s: None)
    # Deadline in the past → loop must bail before any provider.run call.
    monkeypatch.setattr(TestLoopTool, "_runtime_deadline", lambda self: 0.0)

    provider = _ScriptedProvider()
    result = TestLoopTool().run("Run tests", provider, cwd=str(tmp_path))

    assert result.success is False
    assert result.error_code == "tool_runtime_exceeded"
    assert result.retryable is True
    assert provider.prompts == []  # no phase executed


def test_test_loop_phase_cap_does_not_raise_above_constant():
    """A high task #timeout: hard backstop is an upper deckel only — it never
    raises the per-step timeout above TOOL_FIX_TIMEOUT_SEC."""
    from config import TOOL_FIX_TIMEOUT_SEC
    tool = TestLoopTool()
    # 10x the constant must be clamped back to the constant.
    assert tool._phase_cap(TOOL_FIX_TIMEOUT_SEC * 10, TOOL_FIX_TIMEOUT_SEC) == TOOL_FIX_TIMEOUT_SEC
    # A smaller task timeout still wins (it is a real per-call budget).
    assert tool._phase_cap(5, TOOL_FIX_TIMEOUT_SEC) == 5
    # No task timeout → the phase default.
    assert tool._phase_cap(None, TOOL_FIX_TIMEOUT_SEC) == TOOL_FIX_TIMEOUT_SEC


class _FailingProvider:
    """Fails every call with a caller-supplied provider error string."""
    name = "claude"
    supports_sessions = False

    def __init__(self, error: str):
        self._error = error
        self.prompts: list[str] = []

    def run(self, prompt, **kwargs):
        from providers.base import RunResult
        self.prompts.append(prompt)
        return RunResult(success=False, error=self._error)


import pytest  # noqa: E402 — kept next to the parametrized test it serves


@pytest.mark.parametrize(
    "provider_error, expected_code, expected_retryable",
    [
        ("rate_limit", "rate_limit", True),
        ("rate_limit: upstream 429", "rate_limit", True),
        ("hang", "hang", True),
        ("auth_error", "auth_error", False),
        ("pytest: command not found", "", False),
    ],
)
def test_test_loop_classifies_provider_errors(
    monkeypatch, tmp_path, provider_error, expected_code, expected_retryable
):
    """RunResult.error was passed through verbatim as error_code with a hardcoded
    retryable=True — a raw stderr dump then fell into orchestrator.py's generic
    5-minute cooldown instead of a real classification."""
    monkeypatch.setattr("tools.test_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.test_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.test_loop.time.sleep", lambda _s: None)

    result = TestLoopTool().run("Run tests", _FailingProvider(provider_error), cwd=str(tmp_path))

    assert result.success is False
    assert result.error_code == expected_code
    assert result.retryable is expected_retryable
