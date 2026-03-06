from pathlib import Path

import pytest

from providers.base import RunResult
from tools.dev_loop import DevLoopTool, _parse_resolution


# ── Helpers ──────────────────────────────────────────────────────────────────

class _ScriptedProvider:
    """Returns pre-scripted outputs in order."""
    name = "claude"

    def __init__(self, outputs: list[str]):
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    def run(self, task: str, cwd: str | None = None, timeout: int = 0) -> RunResult:
        self.prompts.append(task)
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))



def _patch(monkeypatch):
    monkeypatch.setattr("tools.dev_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.dev_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.dev_loop.time.sleep", lambda _: None)


# ── _parse_resolution ─────────────────────────────────────────────────────────

def test_parse_resolution_resolved():
    assert _parse_resolution("RESOLVED: everything works") == "RESOLVED"

def test_parse_resolution_partial():
    assert _parse_resolution("PARTIAL: login works, logout missing") == "PARTIAL"

def test_parse_resolution_unresolved():
    assert _parse_resolution("UNRESOLVED: nothing was changed") == "UNRESOLVED"

def test_parse_resolution_case_insensitive():
    assert _parse_resolution("resolved: done") == "RESOLVED"

def test_parse_resolution_unknown():
    assert _parse_resolution("looks good to me") == "UNKNOWN"

def test_parse_resolution_earliest_match_wins():
    # PARTIAL on line 1 should win over RESOLVED on line 2
    assert _parse_resolution("PARTIAL: login done\nRESOLVED: edge case also fixed") == "PARTIAL"
    # UNRESOLVED should win when it appears first
    assert _parse_resolution("UNRESOLVED: nothing changed\nRESOLVED: nope") == "UNRESOLVED"


# ── Happy path ───────────────────────────────────────────────────────────────

def test_dev_loop_succeeds_in_one_iteration(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "## Problem Analysis\nBug found.\n## Relevant Files\nauth.py\n## Implementation Plan\nFix it.",  # research
        "Fixed the bug in auth.py.",                                                                      # execution
        "No P1/P2/P3 findings.",                                                                          # quality review
        "RESOLVED: Bug is fixed.",                                                                        # resolution review
    ])
    tool = DevLoopTool()
    result = tool.run("Fix login bug", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 1
    assert len(provider.prompts) == 4


def test_dev_loop_writes_research_file(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "## Problem Analysis\nFound it.",
        "Fixed.",
        "No P1/P2/P3 findings.",
        "RESOLVED: done.",
    ])
    DevLoopTool().run("Fix bug", provider, cwd=str(tmp_path))

    research_file = tmp_path / ".dev-loop" / "research.md"
    assert research_file.exists()
    assert "Found it." in research_file.read_text(encoding="utf-8")


def test_dev_loop_writes_round_file(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "Research output.",
        "Execution output.",
        "No P1/P2/P3 findings.",
        "RESOLVED: task solved.",
    ])
    DevLoopTool().run("Add feature", provider, cwd=str(tmp_path))

    round_file = tmp_path / ".dev-loop" / "round-001.md"
    assert round_file.exists()
    content = round_file.read_text(encoding="utf-8")
    assert "Execution output." in content
    assert "RESOLVED" in content


def test_dev_loop_writes_summary_on_success(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "Research.", "Execution.", "No P1/P2/P3 findings.", "RESOLVED: done.",
    ])
    DevLoopTool().run("Fix bug", provider, cwd=str(tmp_path))

    summary = tmp_path / ".dev-loop" / "summary.md"
    assert summary.exists()
    assert "DONE" in summary.read_text(encoding="utf-8")


# ── Retry on review failure ───────────────────────────────────────────────────

def test_dev_loop_retries_on_quality_failure(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "Research.",                       # research
        "First attempt.",                  # execution iter 1
        "- [P1] Null pointer in auth.py",  # quality review iter 1 — fail
        "RESOLVED: task done.",            # resolution review iter 1
        "Fixed null pointer.",             # execution iter 2
        "No P1/P2/P3 findings.",           # quality review iter 2 — pass
        "RESOLVED: task done.",            # resolution review iter 2
    ])
    tool = DevLoopTool()
    result = tool.run("Fix bug", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 2
    # round-002.md should exist
    assert (tmp_path / ".dev-loop" / "round-002.md").exists()


def test_dev_loop_retries_on_resolution_partial(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "Research.",
        "Partial fix.",
        "No P1/P2/P3 findings.",           # quality ok
        "PARTIAL: logout not fixed yet.",  # resolution fail
        "Full fix.",
        "No P1/P2/P3 findings.",
        "RESOLVED: all done.",
    ])
    result = DevLoopTool().run("Fix login+logout", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 2


def test_dev_loop_previous_reviews_passed_to_execution(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "Research.",
        "Bad impl.",
        "- [P2] Missing error handling",
        "PARTIAL: logout not fixed.",
        "Better impl.",
        "No P1/P2/P3 findings.",
        "RESOLVED: done.",
    ])
    tool = DevLoopTool()
    tool.run("Fix bug", provider, cwd=str(tmp_path))

    # Second execution prompt must contain both previous reviews
    exec_prompt_iter2 = provider.prompts[4]  # research, exec1, qual1, res1, exec2
    assert "QUALITY REVIEW" in exec_prompt_iter2
    assert "Missing error handling" in exec_prompt_iter2
    assert "RESOLUTION REVIEW" in exec_prompt_iter2
    assert "logout not fixed" in exec_prompt_iter2


# ── P3-only is non-blocking ───────────────────────────────────────────────────

def test_dev_loop_p3_only_quality_does_not_block(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "Research.",
        "Implementation.",
        "- [P3] Minor naming issue in utils.py",  # P3 only → non-blocking
        "RESOLVED: done.",
    ])
    result = DevLoopTool().run("Add feature", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 1


# ── Failure cases ─────────────────────────────────────────────────────────────

def test_dev_loop_fails_on_research_error(monkeypatch, tmp_path):
    _patch(monkeypatch)

    class _FailResearch:
        name = "claude"
        def run(self, task, cwd=None, timeout=0):
            return RunResult(success=False, error="timeout")

    result = DevLoopTool().run("Fix bug", _FailResearch(), cwd=str(tmp_path))

    assert result.success is False
    assert result.iterations == 0
    assert "Research" in result.error
    assert result.error_code == "timeout"
    assert result.retryable is True


def test_dev_loop_fails_on_execution_error(monkeypatch, tmp_path):
    _patch(monkeypatch)

    class _FailExec:
        name = "claude"
        _calls = 0
        def run(self, task, cwd=None, timeout=0):
            self._calls += 1
            if self._calls == 1:
                return RunResult(success=True, output="Research output.")
            return RunResult(success=False, error="exec error")

    result = DevLoopTool().run("Fix bug", _FailExec(), cwd=str(tmp_path))

    assert result.success is False
    assert "Execution" in result.error
    assert result.error_code == "exec error"
    assert result.retryable is True


def test_dev_loop_detects_infinite_loop(monkeypatch, tmp_path):
    _patch(monkeypatch)
    # Same P1 finding twice → infinite loop detection
    provider = _ScriptedProvider([
        "Research.",
        "Attempt 1.",
        "- [P1] Missing auth check",  # quality iter 1
        "RESOLVED: done.",
        "Attempt 2.",
        "- [P1] Missing auth check",  # same finding → abort
        "RESOLVED: done.",
    ])
    result = DevLoopTool().run("Fix auth", provider, cwd=str(tmp_path))

    assert result.success is False
    assert "wiederholen" in result.error


def test_dev_loop_fails_on_invalid_quality_review_output(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "Research.",
        "Implementation.",
        "Looks good overall.",  # invalid quality format
    ])

    result = DevLoopTool().run("Fix bug", provider, cwd=str(tmp_path))

    assert result.success is False
    assert "Quality-Review-Output" in result.error


def test_dev_loop_fails_on_invalid_resolution_review_output(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "Research.",
        "Implementation.",
        "No P1/P2/P3 findings.",
        "Looks solved to me.",  # invalid resolution format
    ])

    result = DevLoopTool().run("Fix bug", provider, cwd=str(tmp_path))

    assert result.success is False
    assert "Resolution-Review-Output" in result.error


def test_dev_loop_detects_repeated_resolution_feedback(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "Research.",
        "Attempt 1.",
        "No P1/P2/P3 findings.",
        "PARTIAL: logout flow is still missing.",
        "Attempt 2.",
        "No P1/P2/P3 findings.",
        "PARTIAL: logout flow is still missing.",
    ])

    result = DevLoopTool().run("Fix auth flow", provider, cwd=str(tmp_path))

    assert result.success is False
    assert "Review-Ergebnis wiederholt sich" in result.error


def test_dev_loop_respects_max_iterations(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.dev_loop.TOOL_MAX_ITERATIONS", 2)
    _patch(monkeypatch)
    # Always returns different P1 findings (no infinite loop detection) but never resolves
    call_count = [0]

    class _AlwaysFailing:
        name = "claude"
        def run(self, task, cwd=None, timeout=0):
            call_count[0] += 1
            n = call_count[0]
            if n == 1:
                return RunResult(success=True, output="Research.")
            if n % 3 == 2:  # execution
                return RunResult(success=True, output=f"Attempt {n}.")
            if n % 3 == 0:  # quality
                return RunResult(success=True, output=f"- [P1] Unique finding #{n}")
            return RunResult(success=True, output="UNRESOLVED: still broken.")

    result = DevLoopTool().run("Fix bug", _AlwaysFailing(), cwd=str(tmp_path))

    assert result.success is False
    assert result.iterations == 2
