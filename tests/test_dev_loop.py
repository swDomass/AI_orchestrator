from pathlib import Path

import pytest

from providers.base import RunResult
from tools.dev_loop import DevLoopTool, _parse_resolution


# ── Helpers ──────────────────────────────────────────────────────────────────

class _ScriptedProvider:
    """Returns pre-scripted outputs in order."""
    name = "claude"
    supports_sessions = False

    def __init__(self, outputs: list[str]):
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    def run(self, task: str, cwd: str | None = None, timeout: int = 0, **kwargs) -> RunResult:
        self.prompts.append(task)
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))



def _patch(monkeypatch):
    monkeypatch.setattr("tools.dev_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.dev_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.dev_loop.time.sleep", lambda _: None)
    # The capacity gate reads a process-global cache of the REAL provider quota, filled by
    # limits.py's background refresh thread. Unmocked, 17 of these tests return
    # `capacity_exhausted` instead of running whenever the actual Claude quota is spent —
    # so the suite went red for reasons that had nothing to do with the code under test.
    # `pytest-randomly` only decided whether a test ran before or after the first refresh,
    # which made a live-state dependency look like test-order dependence. The other five
    # tools with this gate mock it in their tests; test_dev_loop.py was the only one left.
    monkeypatch.setattr("tools.dev_loop.is_cached_provider_available", lambda _name: True)


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
        "## Problem Analysis\nBug found.\n## Relevant Files\nauth.py\n## Implementation Plan\n1. Fix the auth bug.",  # research+plan merged
        "Fixed the bug in auth.py.",                                                                                    # execution
        "No P1/P2/P3 findings.",                                                                                        # quality review
        "RESOLVED: Bug is fixed.",                                                                                      # resolution review
    ])
    tool = DevLoopTool()
    result = tool.run("Fix login bug", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 1
    assert len(provider.prompts) == 4


def test_dev_loop_writes_research_and_plan_file(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "## Problem Analysis\nFound it.\n## Implementation Plan\n1. Fix it.",
        "Fixed.",
        "No P1/P2/P3 findings.",
        "RESOLVED: done.",
    ])
    DevLoopTool().run("Fix bug", provider, cwd=str(tmp_path))

    rp_file = tmp_path / ".dev-loop" / "research-and-plan.md"
    assert rp_file.exists()
    content = rp_file.read_text(encoding="utf-8")
    assert "Found it." in content
    assert "Fix it." in content


def test_dev_loop_writes_round_file(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "## Problem Analysis\nResearch output.\n## Implementation Plan\n1. Do it.",
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
        "## Problem Analysis\nResearch.\n## Implementation Plan\n1. Fix it.",
        "Execution.",
        "No P1/P2/P3 findings.",
        "RESOLVED: done.",
    ])
    DevLoopTool().run("Fix bug", provider, cwd=str(tmp_path))

    summary = tmp_path / ".dev-loop" / "summary.md"
    assert summary.exists()
    assert "DONE" in summary.read_text(encoding="utf-8")


# ── Retry on review failure ───────────────────────────────────────────────────

def test_dev_loop_retries_on_quality_failure(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "## Problem Analysis\nResearch.\n## Implementation Plan\n1. Fix it.",  # research+plan merged
        "First attempt.",                               # execution iter 1
        "- [P1] Null pointer in auth.py",               # quality review iter 1 — fail
        "RESOLVED: task done.",                         # resolution review iter 1
        "Fixed null pointer.",                          # execution iter 2
        "No P1/P2/P3 findings.",                        # quality review iter 2 — pass
        "RESOLVED: task done.",                         # resolution review iter 2
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
        "## Problem Analysis\nResearch.\n## Implementation Plan\n1. Fix it.",
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
        "## Problem Analysis\nResearch.\n## Implementation Plan\n1. Fix it.",
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
    # prompts: research+plan(0), exec1(1), qual1(2), res1(3), exec2(4)
    exec_prompt_iter2 = provider.prompts[4]
    assert "QUALITY REVIEW" in exec_prompt_iter2
    assert "Missing error handling" in exec_prompt_iter2
    assert "RESOLUTION REVIEW" in exec_prompt_iter2
    assert "logout not fixed" in exec_prompt_iter2


# ── P3-only is non-blocking ───────────────────────────────────────────────────

def test_dev_loop_p3_only_quality_does_not_block(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "## Problem Analysis\nResearch.\n## Implementation Plan\n1. Do it.",
        "Implementation.",
        "- [P3] Minor naming issue in utils.py",  # P3 only → non-blocking
        "RESOLVED: done.",
    ])
    result = DevLoopTool().run("Add feature", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 1
    # Non-blocking is not the same as invisible — the P3 comes back as a closing offer.
    assert "P3 offen" in result.output
    assert "Minor naming issue in utils.py" in result.output


def test_dev_loop_deferred_p3_survives_a_later_clean_round(monkeypatch, tmp_path):
    """Same contract as review-loop: a P3 from round 1 must still be offered when the
    round-2 review is clean. The executor never sees P3 at all (only `blocking_findings`
    are passed on), so the closing offer is the only place it can surface."""
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "## Problem Analysis\nResearch.\n## Implementation Plan\n1. Do it.",
        "Implementation.",
        "- [P2] Real bug in utils.py\n- [P3] round one nit",  # quality iter 1
        "RESOLVED: done.",
        "Fix applied.",                                        # execute iter 2
        "No P1/P2/P3 findings.",                               # quality iter 2 — clean
        "RESOLVED: done.",
    ])
    result = DevLoopTool().run("Add feature", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 2
    # Assert on the OFFER BLOCK, not on the whole output: the iteration-1 quality review
    # text is part of result.output either way, so `"round one nit" in result.output`
    # passes even with the accumulation removed and proves nothing.
    assert "--- P3 offen" in result.output
    offer = result.output.split("--- P3 offen")[1]
    assert "round one nit" in offer, (
        "a P3 reported in an earlier round must still be in the closing offer"
    )
    # ...and the P3 was never handed to the executor. Only the EXECUTION prompts matter
    # here — the resolution reviewer and the lesson summarizer legitimately receive the
    # full review text, so a blanket check over every later prompt proves nothing.
    execution_prompts = [
        p for p in provider.prompts if "Implement the solution exactly as laid out" in p
    ]
    assert len(execution_prompts) == 2, "expected one execution prompt per iteration"
    assert "Real bug in utils.py" in execution_prompts[1], (
        "the blocking P2 must reach the executor"
    )
    assert not any("round one nit" in p for p in execution_prompts), (
        "P3 must not reach the execution prompt"
    )


def test_dev_loop_p3_in_resolution_feedback_is_not_requested(monkeypatch, tmp_path):
    """A P3 listed by the RESOLUTION reviewer must not reach the execution prompt.

    Regression: `previous_resolution_output` was stored verbatim and pasted into the next
    execution prompt under "fix every finding listed above" — so a P3 bullet the resolution
    reviewer happened to include was explicitly *requested*, making the whole "no P3
    reaches the executor" contract false through a second door that has nothing to do with
    session history. The RESOLVED/PARTIAL verdict and every non-P3 line must survive.
    """
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "## Problem Analysis\nResearch.\n## Implementation Plan\n1. Do it.",
        "Implementation.",
        "No P1/P2/P3 findings.",                                    # quality iter 1 — clean
        "PARTIAL: logout missing\n- [P3] rename the helper",        # resolution blocks
        "Fix applied.",                                             # execute iter 2
        "No P1/P2/P3 findings.",
        "RESOLVED: done.",
    ])
    result = DevLoopTool().run("Add feature", provider, cwd=str(tmp_path))

    assert result.success is True
    execution_prompts = [
        p for p in provider.prompts if "Implement the solution exactly as laid out" in p
    ]
    assert len(execution_prompts) == 2
    second = execution_prompts[1]
    assert "rename the helper" not in second, "a resolution-review P3 must not be requested"
    # ...but the verdict and the functional gap still have to reach the executor.
    assert "logout missing" in second
    assert "PARTIAL" in second
    # ...and the P3 is offered at the end rather than silently dropped.
    assert "--- P3 offen" in result.output
    assert "rename the helper" in result.output.split("--- P3 offen")[1]


def test_strip_p3_lines_keeps_everything_else():
    from tools.review_loop import strip_p3_lines

    assert strip_p3_lines("PARTIAL: x\n- [P3] nit\n- [P2] real") == "PARTIAL: x\n- [P2] real"
    # Alternative provider format must be caught too.
    assert strip_p3_lines("PARTIAL: x\n1. `P3` nit") == "PARTIAL: x"
    assert strip_p3_lines("PARTIAL: nothing to drop") == "PARTIAL: nothing to drop"


def test_dev_loop_contradictory_quality_output_does_not_pass(monkeypatch, tmp_path):
    """Blocking finding + clean sentinel in one output: the findings win, the loop keeps
    going. Regression on `quality_ok = no_quality_findings or not blocking_findings`."""
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "## Problem Analysis\nResearch.\n## Implementation Plan\n1. Do it.",
        "Implementation.",
        "- [P2] Real bug\nNo P1/P2/P3 findings.",  # contradictory
        "RESOLVED: done.",
        "Fix applied.",
        "No P1/P2/P3 findings.",                   # genuinely clean
        "RESOLVED: done.",
    ])
    result = DevLoopTool().run("Add feature", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 2, "the contradictory round must not count as clean"


# ── Failure cases ─────────────────────────────────────────────────────────────

def test_dev_loop_fails_on_research_error(monkeypatch, tmp_path):
    _patch(monkeypatch)

    class _FailResearch:
        name = "claude"
        supports_sessions = False
        def run(self, task, cwd=None, timeout=0, **kwargs):
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
        supports_sessions = False
        _calls = 0
        def run(self, task, cwd=None, timeout=0, **kwargs):
            self._calls += 1
            if self._calls == 1:  # research+plan merged
                return RunResult(
                    success=True,
                    output="## Problem Analysis\nResearch.\n## Implementation Plan\n1. Fix it.",
                )
            return RunResult(success=False, error="exec error")

    result = DevLoopTool().run("Fix bug", _FailExec(), cwd=str(tmp_path))

    assert result.success is False
    assert "Execution" in result.error
    # RunResult.error is an unconstrained string. Free-form prose ("exec error",
    # a raw stderr dump) is NOT a taxonomy code and must not be handed on as one —
    # it used to land verbatim in orchestrator.py's error_code branch and fall into
    # the generic 5-minute cooldown instead of a real classification.
    assert result.error_code == ""
    assert result.retryable is False


def test_dev_loop_classifies_transient_execution_error(monkeypatch, tmp_path):
    """A provider error in "code: detail" form keeps its code and stays retryable."""
    _patch(monkeypatch)

    class _RateLimited:
        name = "claude"
        supports_sessions = False
        _calls = 0
        def run(self, task, cwd=None, timeout=0, **kwargs):
            self._calls += 1
            if self._calls == 1:
                return RunResult(
                    success=True,
                    output="## Problem Analysis\nResearch.\n## Implementation Plan\n1. Fix it.",
                )
            return RunResult(success=False, error="rate_limit: 429 from upstream")

    result = DevLoopTool().run("Fix bug", _RateLimited(), cwd=str(tmp_path))

    assert result.success is False
    assert result.error_code == "rate_limit"
    assert result.retryable is True


def test_dev_loop_detects_infinite_loop(monkeypatch, tmp_path):
    _patch(monkeypatch)
    # Same P1 finding twice → infinite loop detection
    provider = _ScriptedProvider([
        "## Problem Analysis\nResearch.\n## Implementation Plan\n1. Fix it.",
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
        "## Problem Analysis\nResearch.\n## Implementation Plan\n1. Fix it.",
        "Implementation.",
        "Looks good overall.",  # invalid quality format
    ])

    result = DevLoopTool().run("Fix bug", provider, cwd=str(tmp_path))

    assert result.success is False
    assert "Quality-Review-Output" in result.error


def test_dev_loop_fails_on_invalid_resolution_review_output(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([
        "## Problem Analysis\nResearch.\n## Implementation Plan\n1. Fix it.",
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
        "## Problem Analysis\nResearch.\n## Implementation Plan\n1. Fix it.",
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
    call_count = [0]

    class _AlwaysFailing:
        name = "claude"
        supports_sessions = False
        def run(self, task, cwd=None, timeout=0, **kwargs):
            call_count[0] += 1
            n = call_count[0]
            if n == 1:
                return RunResult(
                    success=True,
                    output="## Problem Analysis\nResearch.\n## Implementation Plan\n1. Fix it.",
                )
            adj = n - 1  # 1-based iteration cycle index (research+plan eaten)
            phase = (adj - 1) % 3  # 0=exec, 1=quality, 2=resolution
            if phase == 0:  # execution
                return RunResult(success=True, output=f"Attempt {n}.")
            if phase == 1:  # quality
                return RunResult(success=True, output=f"- [P1] Unique finding #{n}")
            return RunResult(success=True, output="UNRESOLVED: still broken.")

    result = DevLoopTool().run("Fix bug", _AlwaysFailing(), cwd=str(tmp_path))

    assert result.success is False
    assert result.iterations == 2
