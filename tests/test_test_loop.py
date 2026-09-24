
from providers.base import RunResult
from tools.base_tool import ToolResult
from tools.test_loop import TestLoopTool, _tests_passed


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


# ── Rundenreflexion / BEKANNTE GRENZE ───────────────────────────────────────

class _MultiScriptedProvider:
    """Returns pre-scripted outputs in order; records every prompt."""
    name = "claude"
    supports_sessions = False

    def __init__(self, outputs: list[str]):
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    def run(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))


def _patch_test_loop(monkeypatch):
    monkeypatch.setattr("tools.test_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.test_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.test_loop.time.sleep", lambda _s: None)


def test_test_loop_iteration1_fix_prompt_has_no_reflection_iteration2_does(monkeypatch, tmp_path):
    _patch_test_loop(monkeypatch)
    provider = _MultiScriptedProvider([
        "2 failed, 3 passed",   # test run iter 1
        "Fixed one.",           # fix iter 1 — no reflection instruction yet
        "1 failed, 4 passed",   # test run iter 2 (different text avoids repeat-detect)
        "Fixed another.",       # fix iter 2 — reflection instruction present
        "5 passed in 0.2s",     # test run iter 3 — all green
    ])
    result = TestLoopTool().run("Run tests", provider, cwd=str(tmp_path))

    assert result.success is True
    # prompts: test1(0), fix1(1), test2(2), fix2(3), test3(4)
    fix1_prompt, fix2_prompt = provider.prompts[1], provider.prompts[3]
    assert "Rundenreflexion" not in fix1_prompt
    assert "BEKANNTE GRENZE" not in fix1_prompt
    assert "Rundenreflexion" in fix2_prompt
    assert "BEKANNTE GRENZE" in fix2_prompt


def test_test_loop_known_limit_appears_in_repeat_detected_failure_message(monkeypatch, tmp_path):
    """A test deferred as BEKANNTE GRENZE stays red — the run cannot end green while
    it is outstanding, and the deferral is listed under 'Bekannte Grenzen'.

    The deferral happens in round 2 on purpose (not round 1): a marker in the very
    first fix output must be ignored (see
    test_test_loop_iteration1_known_limit_marker_is_ignored)."""
    _patch_test_loop(monkeypatch)
    same_failure = "1 failed, 6 passed -- tests/test_x.py::test_edge_case"
    provider = _MultiScriptedProvider([
        "2 failed, 5 passed -- tests/test_x.py::test_edge_case, tests/test_y.py::test_other",
        "Fixed test_other, investigating test_edge_case further.",  # fix iter 1 — no defer
        same_failure,  # test run iter 2
        (
            "## Rundenreflexion\nThis is an edge case beyond the task.\n"
            "- [BEKANNTE GRENZE] tests/test_x.py::test_edge_case — "
            "pre-existing flake unrelated to this task\n"
        ),             # fix iter 2 — defers instead of fixing
        same_failure,  # test run iter 3 — identical to iter 2's output → repeat-detect
    ])
    result = TestLoopTool().run("Run tests", provider, cwd=str(tmp_path))

    assert result.success is False, "a known limit must never turn the run green"
    assert "bekannte Grenze" in result.error
    assert "--- Bekannte Grenzen" in result.output
    block = result.output.split("--- Bekannte Grenzen")[1]
    assert "tests/test_x.py::test_edge_case" in block
    assert "pre-existing flake unrelated to this task" in block


def test_test_loop_known_limit_appears_in_max_iterations_failure_message(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.test_loop.TOOL_MAX_ITERATIONS", 2)
    _patch_test_loop(monkeypatch)
    provider = _MultiScriptedProvider([
        "1 failed, 4 passed -- iter1",
        "Attempted fix, no change yet.",  # fix iter 1 — no defer (would be ignored anyway)
        "1 failed, 5 passed -- iter2",    # different text avoids repeat-detect, still red
        (
            "## Rundenreflexion\nEdge case beyond scope.\n"
            "- [BEKANNTE GRENZE] tests/test_x.py::test_edge_case — flaky, unrelated\n"
        ),                                 # fix iter 2 — defers instead of fixing
    ])
    result = TestLoopTool().run("Run tests", provider, cwd=str(tmp_path))

    assert result.success is False
    assert result.iterations == 2
    assert "Max Iterationen" in result.error
    assert "bekannte Grenze" in result.error
    assert "--- Bekannte Grenzen" in result.output


def test_test_loop_iteration1_known_limit_marker_is_ignored(monkeypatch, tmp_path):
    """A BEKANNTE GRENZE marker in the iteration-1 fix output has no effect —
    deferrals are only accepted from iteration 2 on ("ab Runde 2")."""
    _patch_test_loop(monkeypatch)
    same_failure = "1 failed, 4 passed -- tests/test_x.py::test_edge_case"
    provider = _MultiScriptedProvider([
        same_failure,  # test run iter 1
        (
            "- [BEKANNTE GRENZE] tests/test_x.py::test_edge_case — "
            "pre-existing flake unrelated to this task\n"
        ),             # fix iter 1 — deferral attempt, must be ignored (iteration 1)
        same_failure,  # test run iter 2 — identical output → repeat-detect
    ])
    result = TestLoopTool().run("Run tests", provider, cwd=str(tmp_path))

    assert result.success is False
    assert "bekannte Grenze" not in result.error
    assert "--- Bekannte Grenzen" not in result.output
