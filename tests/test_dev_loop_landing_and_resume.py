"""Dev-Loop: landing round, budget clamp, capacity park and resume.

Covers the three things that were broken when #id:orchtrio died on 2026-09-09 with
two hours of finished work in the tree and a terminal failure stamp on its queue line:

1. the total-runtime deadline cut the loop off between iterations (no landing),
2. a quota exhaustion mid-phase was not recognised as one, so the run was finalised
   as failed instead of parked, and
3. nothing was checkpointed, so even a park would have restarted at iteration 1.

Every gate here is written so that breaking the corresponding production line turns
it red — checked by mutation, one at a time. That discipline is not decoration: in
the previous package in this repo four of nine gates were trivially green in their
first version.
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import memory
import orchestrator
from providers import claude as claude_provider
from providers.base import RunResult, error_code_of, is_transient
from queue_manager import strip_metadata_tags
from tools.base_tool import TokenCounter
from tools.dev_loop import (
    _MAX_CAPACITY_PARKS,
    _PARK_CAPACITY,
    DevLoopTool,
    _dirty_paths,
    _is_capacity_error,
    _resume_checkpoint,
    _run_dir,
    _task_hash,
    _write_checkpoint,
)
from tools.registry import get_tool

# ── Helpers ──────────────────────────────────────────────────────────────────

_PLAN = "## Problem Analysis\nResearch.\n## Implementation Plan\n1. Do it."
_CLEAN_QUALITY = "No P1/P2/P3 findings."
_DIRTY_QUALITY = "- [P2] Something is still wrong in utils.py"
_RESOLVED = "RESOLVED: done."
_UNRESOLVED = "UNRESOLVED: not finished."


class _Scripted:
    """Scripted provider that also records the timeout each call was given."""
    name = "claude"
    supports_sessions = False

    def __init__(self, outputs, fail_at=None, fail_error="rate_limit"):
        self._outputs = list(outputs)
        self.prompts: list[str] = []
        self.timeouts: list[int] = []
        self._fail_at = fail_at          # 1-based index of the call that fails
        self._fail_error = fail_error
        self.calls = 0

    def run(self, task, cwd=None, timeout=0, **kwargs):
        self.calls += 1
        self.prompts.append(task)
        self.timeouts.append(timeout)
        if self._fail_at is not None and self.calls == self._fail_at:
            return RunResult(success=False, error=self._fail_error)
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))


def _patch(monkeypatch):
    monkeypatch.setattr("tools.dev_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.dev_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.dev_loop.time.sleep", lambda _: None)
    # See the long note in tests/test_dev_loop.py: unmocked, this reads the real
    # process-global quota cache and the result depends on the machine.
    monkeypatch.setattr("tools.dev_loop.is_cached_provider_available", lambda _n: True)


def _budget(monkeypatch, seconds):
    """Pin the tool's total wall-clock budget, bypassing policy.yaml."""
    monkeypatch.setattr(DevLoopTool, "_max_runtime_sec", lambda self: seconds)


def _git_repo(tmp_path: Path) -> Path:
    """A real git repo with one commit, so _is_clean_git_repo reports clean.

    Deliberately a SUBDIRECTORY of tmp_path, not tmp_path itself: conftest's autouse
    `_isolate_active_runs_dir` points ACTIVE_RUNS_DIR at `tmp_path / "active_runs"`,
    so a repo rooted at tmp_path picks up the tool's own run registry as untracked
    dirt and every dirty-path assertion becomes a coin flip.
    """
    tmp_path = tmp_path / "repo"
    tmp_path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)
    return tmp_path


# ── G1/G2: the keyword that cost a finished run ──────────────────────────────

class TestSessionLimitIsACapacityError:
    """G1/G2 — providers/claude.py must read "session limit" as a quota end."""

    REAL = "You've hit your session limit · resets 1:30am (Europe/Vienna)"

    def _run_cli(self, monkeypatch, stdout, stderr="", rc=1):
        monkeypatch.setattr(
            claude_provider, "run_with_watchdog",
            lambda *a, **k: SimpleNamespace(
                returncode=rc, stdout=stdout, stderr=stderr, stdin_error=None),
        )
        return claude_provider.ClaudeProvider().run("prompt", cwd=None, timeout=60)

    def test_error_result_carrying_the_real_message_is_rate_limit(self, monkeypatch):
        """G1: the exact string from logs/queue-events.log:292."""
        evt = json.dumps({"type": "result", "subtype": "error_during_execution",
                          "result": self.REAL})
        assert self._run_cli(monkeypatch, evt).error == "rate_limit"

    def test_stderr_carrying_it_is_rate_limit(self, monkeypatch):
        assert self._run_cli(monkeypatch, "", stderr=self.REAL).error == "rate_limit"

    def test_a_successful_answer_discussing_limits_is_not_a_rate_limit(self, monkeypatch):
        """G2 — the counter-test, and the reason the keyword can be widened at all.

        The scan surface is restricted to signal-bearing lines, so an answer whose
        PROSE mentions session/rate limits must not trigger a bogus cooldown. Widen
        `scan_parts` to the whole stdout and this goes red.
        """
        evt = json.dumps({
            "type": "result", "subtype": "success",
            "result": "Note: Claude has a session limit and a rate limit and a quota.",
        })
        res = self._run_cli(monkeypatch, evt, rc=0)
        assert res.success is True
        assert res.error == ""

    def test_assistant_prose_in_a_failed_run_is_not_scanned(self, monkeypatch):
        """G2 proper — this is what the RESTRICTED scan surface actually buys.

        The test above is carried by the early success return, not by `scan_parts`,
        so it stays green even if the surface is widened — measured. Here the run
        FAILS (so the early return does not fire) and the failure has nothing to do
        with quota, but an assistant message earlier in the stream discusses session
        limits. Widen `scan_parts` to the whole stdout and this goes red, booking a
        bogus cooldown on a provider that is not rate limited at all.
        """
        stdout = "\n".join([
            json.dumps({"type": "assistant", "message": {"content":
                        "Your code should handle the session limit and quota errors."}}),
            json.dumps({"type": "result", "subtype": "error_during_execution",
                        "result": "TypeError: cannot serialise object"}),
        ])
        res = self._run_cli(monkeypatch, stdout)
        assert res.error != "rate_limit", (
            "an assistant message must never trigger a rate-limit classification"
        )

    def test_it_is_transient_so_the_tool_layer_can_act_on_it(self):
        assert error_code_of("rate_limit") == "rate_limit"
        assert is_transient("rate_limit") is True

    def test_capacity_predicate_is_narrow(self):
        assert _is_capacity_error("rate_limit") is True
        assert _is_capacity_error("rate_limit: 429 upstream") is True
        assert _is_capacity_error("hang") is False
        assert _is_capacity_error("timeout") is False
        assert _is_capacity_error("") is False


# ── G3-G7: the landing round ─────────────────────────────────────────────────

class TestLandingRound:

    def test_budget_below_reserve_makes_the_first_iteration_the_last(self, monkeypatch, tmp_path):
        """G3 + G4 — crossing the soft threshold marks the round AND tells the executor."""
        _patch(monkeypatch)
        _budget(monkeypatch, config.TOOL_LANDING_RESERVE_SEC)
        prov = _Scripted([_PLAN, "Implementation.", _DIRTY_QUALITY, _UNRESOLVED])

        result = DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        exec_prompts = [p for p in prov.prompts
                        if "Implement the solution exactly as laid out" in p]
        assert len(exec_prompts) == 1
        assert "LETZTE ITERATION" in exec_prompts[0], (
            "the executor must be told this is the final round"
        )
        assert "Offen geblieben" in exec_prompts[0]
        assert result.iterations == 1

    def test_no_further_iteration_after_the_landing_round(self, monkeypatch, tmp_path):
        """G5 — an unresolved landing round stops; it does not iterate again."""
        _patch(monkeypatch)
        _budget(monkeypatch, config.TOOL_LANDING_RESERVE_SEC)
        # Enough script for THREE full rounds — if the loop continued it would use them.
        prov = _Scripted([_PLAN, "Impl.", _DIRTY_QUALITY, _UNRESOLVED,
                          "Impl2.", _DIRTY_QUALITY, _UNRESOLVED,
                          "Impl3.", _CLEAN_QUALITY, _RESOLVED])

        result = DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        exec_prompts = [p for p in prov.prompts
                        if "Implement the solution exactly as laid out" in p]
        assert len(exec_prompts) == 1, "the landing round must be the only one"
        assert result.success is False
        assert result.error_code == "tool_runtime_exceeded"

    def test_a_clean_landing_round_is_a_success_not_a_failure(self, monkeypatch, tmp_path):
        """G6 — THE point of K2: landing cleanly under the wire is done, not failed.

        Before 2026-09-10 the deadline returned tool_runtime_exceeded regardless of
        what the reviews said, and orchestrator.py stamped the queue line ❌.
        """
        _patch(monkeypatch)
        _budget(monkeypatch, config.TOOL_LANDING_RESERVE_SEC)
        prov = _Scripted([_PLAN, "Implementation.", _CLEAN_QUALITY, _RESOLVED])

        result = DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        assert result.success is True, "a clean landing round must not be a failure"
        assert result.error_code in (None, "")
        assert result.iterations == 1

    def test_an_unresolved_landing_round_stays_terminal(self, monkeypatch, tmp_path):
        """G7 — the other half: not clean means the old terminal outcome is kept."""
        _patch(monkeypatch)
        _budget(monkeypatch, config.TOOL_LANDING_RESERVE_SEC)
        prov = _Scripted([_PLAN, "Implementation.", _DIRTY_QUALITY, _UNRESOLVED])

        result = DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        assert result.success is False
        assert result.error_code == "tool_runtime_exceeded"
        assert result.retryable is True

    def test_phase_timeouts_are_clamped_to_the_remaining_budget(self, monkeypatch, tmp_path):
        """G8 — the reserve is enforced, not merely scheduled.

        A single exec phase may ask for TOOL_DEV_EXEC_TIMEOUT_SEC (7200 s). Without
        the clamp it would blow straight through a 600 s budget.
        """
        _patch(monkeypatch)
        _budget(monkeypatch, 600)
        prov = _Scripted([_PLAN, "Implementation.", _CLEAN_QUALITY, _RESOLVED])

        DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        # Call 1 is Research+Plan (before the loop, not clamped). Calls 2..4 are the
        # landing round's exec / quality / resolution.
        landing = prov.timeouts[1:]
        assert landing, "expected the landing round to run"
        assert all(t <= 600 for t in landing), (
            f"phase timeouts must be clamped to the remaining budget, got {landing}"
        )
        assert all(t >= config.TOOL_LANDING_MIN_PHASE_SEC for t in landing), (
            "the clamp must never hand a provider a sub-floor timeout"
        )

    def test_a_budget_too_small_for_a_landing_round_does_not_start_one(self, monkeypatch, tmp_path):
        """G9 — three calls that cannot finish would cost real quota for nothing."""
        _patch(monkeypatch)
        _budget(monkeypatch, config.TOOL_LANDING_MIN_PHASE_SEC * 3 - 1)
        prov = _Scripted([_PLAN, "Implementation.", _CLEAN_QUALITY, _RESOLVED])

        result = DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        exec_prompts = [p for p in prov.prompts
                        if "Implement the solution exactly as laid out" in p]
        assert exec_prompts == [], "no round may be started when none can finish"
        assert result.success is False
        assert result.error_code == "tool_runtime_exceeded"

    def test_an_exhausted_budget_is_still_terminal(self, monkeypatch, tmp_path):
        """The pre-existing contract, unchanged: nothing left means nothing runs."""
        _patch(monkeypatch)
        _budget(monkeypatch, 0)
        prov = _Scripted([_PLAN, "Implementation.", _CLEAN_QUALITY, _RESOLVED])

        result = DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        assert result.success is False
        assert result.error_code == "tool_runtime_exceeded"
        assert result.iterations == 0

    def test_a_normal_budget_runs_normal_rounds_without_the_note(self, monkeypatch, tmp_path):
        """Guard against the landing note leaking into every ordinary run."""
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        prov = _Scripted([_PLAN, "Impl.", _DIRTY_QUALITY, _UNRESOLVED,
                          "Impl2.", _CLEAN_QUALITY, _RESOLVED])

        result = DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        assert result.success is True
        assert result.iterations == 2
        assert not any("LETZTE ITERATION" in p for p in prov.prompts), (
            "an ordinary run must never see the landing instruction"
        )


# ── G10: capacity mid-phase parks instead of rotating ────────────────────────

class TestCapacityParksTheTask:
    """G10 — one test per provider call site, because each is its own code path."""

    @pytest.mark.parametrize("fail_at,phase", [
        (2, "Execution"),
        (3, "Quality-Review"),
        (4, "Resolution-Review"),
    ])
    def test_capacity_in_any_phase_yields_capacity_exhausted(
            self, monkeypatch, tmp_path, fail_at, phase):
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        prov = _Scripted([_PLAN, "Impl.", _CLEAN_QUALITY, _RESOLVED],
                         fail_at=fail_at, fail_error="rate_limit")

        result = DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        assert result.error_code == "capacity_exhausted", (
            f"{phase}: a quota end must PARK the task, not rotate it to the next "
            f"provider (which would restart at iteration 1 with a fresh deadline)"
        )
        assert result.retryable is True
        assert result.success is False

    def test_capacity_in_research_phase_parks_too(self, monkeypatch, tmp_path):
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        prov = _Scripted([], fail_at=1, fail_error="rate_limit")

        result = DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        assert result.error_code == "capacity_exhausted"

    def test_a_non_capacity_failure_still_takes_the_generic_path(self, monkeypatch, tmp_path):
        """The redirect must be narrow: a hang is not a quota problem."""
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        prov = _Scripted([_PLAN, "Impl.", _CLEAN_QUALITY, _RESOLVED],
                         fail_at=2, fail_error="hang")

        result = DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        assert result.error_code == "hang"
        assert result.error_code != "capacity_exhausted"


# ── G11-G13, G16: the checkpoint and the resume ──────────────────────────────

class TestCheckpointAndResume:

    def test_only_a_capacity_park_licenses_a_resume(self, monkeypatch, tmp_path):
        """G11 — the resume must NOT become a general retry mechanism.

        The Auftrag rules out "ein allgemeiner Retry-/Quarantaene-Mechanismus fuer
        andere Fehlerklassen" under KÜR. An earlier version checkpointed at the end
        of every completed iteration, which quietly granted exactly that: any later
        interruption — hang, format error, process crash, a hand-reopened task —
        would have resumed mid-loop with the reduced budget AND the dirty-tree
        waiver. Here iteration 1 completes and the run then dies of a `hang`, which
        is NOT a capacity end, so nothing resumable may be left behind.
        """
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        task = "Fix bug"
        prov = _Scripted([_PLAN, "Impl.", _DIRTY_QUALITY, _UNRESOLVED],
                         fail_at=5, fail_error="hang")

        DevLoopTool().run(task, prov, cwd=str(tmp_path))

        assert _resume_checkpoint(str(tmp_path), _task_hash(task)) is None, (
            "a completed iteration must not license a resume — only a capacity park does"
        )
        # The research cache is a different thing and must survive, so the next
        # attempt still skips Research+Plan.
        state = json.loads(
            (_run_dir(str(tmp_path), _task_hash(task)) / "state.json").read_text("utf-8"))
        assert state.get("park_reason") is None
        assert state["research_and_plan"], "the research cache is not the resume licence"

    def test_a_capacity_park_carries_the_review_context(self, monkeypatch, tmp_path):
        """The other half: what the park DOES have to preserve.

        Without `previous_quality_findings` a resumed iteration executes against an
        empty review context, i.e. blind to what the last reviewer found.
        """
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        task = "Fix bug"
        prov = _Scripted([_PLAN, "Impl.", _DIRTY_QUALITY, _UNRESOLVED],
                         fail_at=5, fail_error="rate_limit")

        DevLoopTool().run(task, prov, cwd=str(tmp_path))

        resume = _resume_checkpoint(str(tmp_path), _task_hash(task))
        assert resume is not None
        assert resume["next_iteration"] == 2, "iteration 1 completed, so resume at 2"
        assert resume["previous_quality_findings"], (
            "the review context is the whole point of the checkpoint"
        )

    def test_capacity_park_checkpoints_the_current_iteration(self, monkeypatch, tmp_path):
        """A phase that did not complete must be REDONE, not skipped."""
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        task = "Fix bug"
        prov = _Scripted([_PLAN, "Impl.", _CLEAN_QUALITY], fail_at=4,
                         fail_error="rate_limit")

        DevLoopTool().run(task, prov, cwd=str(tmp_path))

        state = json.loads(
            (_run_dir(str(tmp_path), _task_hash(task)) / "state.json").read_text("utf-8"))
        assert state["next_iteration"] == 1, (
            "iteration 1 was interrupted mid-way, so it must be repeated"
        )

    def test_a_resumed_run_starts_at_the_checkpointed_iteration(self, monkeypatch, tmp_path):
        """G12 — the user-visible half of "weitermachen wenn capa wieder da"."""
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        task = "Fix bug"
        _write_checkpoint(
            str(tmp_path), _task_hash(task),
            cache_phase="research_and_plan_done", research_and_plan=_PLAN,
            next_iteration=4, elapsed_budget_sec=100.0,
            previous_quality_findings=["- [P2] carried over"],
            previous_resolution_output="PARTIAL: half done.",
            deferred_p3={"- [P3] earlier nit": None},
            seen_quality_signatures=set(), seen_review_signatures=set(),
            tokens=TokenCounter(), park_reason=_PARK_CAPACITY,
        )
        prov = _Scripted(["Impl.", _CLEAN_QUALITY, _RESOLVED])

        result = DevLoopTool().run(task, prov, cwd=str(tmp_path))

        assert result.success is True
        assert result.iterations == 4, "the resumed run must continue the numbering"
        exec_prompts = [p for p in prov.prompts
                        if "Implement the solution exactly as laid out" in p]
        assert len(exec_prompts) == 1
        assert "carried over" in exec_prompts[0], (
            "a resumed executor must still see what the last reviewer found"
        )
        assert "earlier nit" in result.output, "P3 offers must survive the interruption"

    def test_a_resumed_run_does_not_get_a_fresh_full_budget(self, monkeypatch, tmp_path):
        """G13 — otherwise every park hands out another 3 h and the bound is fiction."""
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        task = "Fix bug"
        # 10800 - 8500 = 2300 left, which is below the 2400 reserve → landing round.
        _write_checkpoint(
            str(tmp_path), _task_hash(task),
            cache_phase="research_and_plan_done", research_and_plan=_PLAN,
            next_iteration=2, elapsed_budget_sec=8500.0,
            previous_quality_findings=[], previous_resolution_output="",
            deferred_p3={}, seen_quality_signatures=set(),
            seen_review_signatures=set(), tokens=TokenCounter(),
            park_reason=_PARK_CAPACITY,
        )
        prov = _Scripted(["Impl.", _DIRTY_QUALITY, _UNRESOLVED,
                          "Impl2.", _CLEAN_QUALITY, _RESOLVED])

        result = DevLoopTool().run(task, prov, cwd=str(tmp_path))

        exec_prompts = [p for p in prov.prompts
                        if "Implement the solution exactly as laid out" in p]
        assert len(exec_prompts) == 1, (
            "consumed budget must be subtracted, so only a landing round is left"
        )
        assert "LETZTE ITERATION" in exec_prompts[0]
        assert result.error_code == "tool_runtime_exceeded"

    def test_restored_token_counts_are_cumulative(self, monkeypatch, tmp_path):
        task = "Fix bug"
        tc = TokenCounter()
        tc.input_tokens = 1234
        _write_checkpoint(
            str(tmp_path), _task_hash(task),
            cache_phase="research_and_plan_done", research_and_plan=_PLAN,
            next_iteration=2, elapsed_budget_sec=0.0,
            previous_quality_findings=[], previous_resolution_output="",
            deferred_p3={}, seen_quality_signatures=set(),
            seen_review_signatures=set(), tokens=tc, park_reason=_PARK_CAPACITY,
        )
        resume = _resume_checkpoint(str(tmp_path), _task_hash(task))
        assert resume["tokens"]["input_tokens"] == 1234

    def test_known_limits_round_trips_through_checkpoint(self, tmp_path):
        """Rundenreflexion: known_limits must survive a park exactly like deferred_p3,
        or a capacity park silently loses every accepted BEKANNTE GRENZE deferral."""
        task = "Fix bug"
        _write_checkpoint(
            str(tmp_path), _task_hash(task),
            cache_phase="research_and_plan_done", research_and_plan=_PLAN,
            next_iteration=3, elapsed_budget_sec=50.0,
            previous_quality_findings=["- [P2] carried over"],
            previous_resolution_output="",
            deferred_p3={}, seen_quality_signatures=set(),
            seen_review_signatures=set(), tokens=TokenCounter(),
            park_reason=_PARK_CAPACITY,
            known_limits={"- [P2] flaky heuristic": "needs a constructed input"},
        )
        resume = _resume_checkpoint(str(tmp_path), _task_hash(task))
        assert resume is not None
        assert resume["known_limits"] == {
            "- [P2] flaky heuristic": "needs a constructed input"
        }

    def test_known_limits_defaults_to_empty_when_absent_from_an_older_checkpoint(
        self, tmp_path
    ):
        """A checkpoint written before this feature carries no `known_limits` key at
        all — must read back as {}, not crash, same defensive coercion as every
        other field in _resume_checkpoint."""
        task = "Fix bug"
        _write_checkpoint(
            str(tmp_path), _task_hash(task),
            cache_phase="research_and_plan_done", research_and_plan=_PLAN,
            next_iteration=2, elapsed_budget_sec=1.0,
            previous_quality_findings=[], previous_resolution_output="",
            deferred_p3={}, seen_quality_signatures=set(),
            seen_review_signatures=set(), tokens=TokenCounter(),
            park_reason=_PARK_CAPACITY,
            # known_limits deliberately omitted — exercises the default.
        )
        resume = _resume_checkpoint(str(tmp_path), _task_hash(task))
        assert resume is not None
        assert resume["known_limits"] == {}

    def test_a_version_one_state_is_still_only_a_research_cache(self, tmp_path):
        """G16 — a file written by the orchestrator BEFORE this upgrade must not
        be mistaken for a checkpoint, and must still skip Research+Plan."""
        task = "Fix bug"
        d = _run_dir(str(tmp_path), _task_hash(task))
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({
            "tool": "dev-loop", "task_hash": _task_hash(task),
            "phase": "research_and_plan_done", "research_and_plan": _PLAN,
        }), encoding="utf-8")

        assert _resume_checkpoint(str(tmp_path), _task_hash(task)) is None

    def test_a_version_one_state_still_skips_the_research_phase(self, monkeypatch, tmp_path):
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        task = "Fix bug"
        d = _run_dir(str(tmp_path), _task_hash(task))
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({
            "tool": "dev-loop", "task_hash": _task_hash(task),
            "phase": "research_and_plan_done", "research_and_plan": _PLAN,
        }), encoding="utf-8")
        prov = _Scripted(["Impl.", _CLEAN_QUALITY, _RESOLVED])

        result = DevLoopTool().run(task, prov, cwd=str(tmp_path))

        assert result.success is True
        assert result.iterations == 1, "starts at iteration 1, plan comes from cache"

    @pytest.mark.parametrize("payload", [
        {"state_version": 2, "next_iteration": 0},           # below the first iteration
        {"state_version": 2, "next_iteration": -3},          # nonsense
        {"state_version": 2, "next_iteration": 999},         # past the loop
        {"state_version": 2, "next_iteration": "zwei"},      # wrong type
        {"state_version": 2},                                 # missing
        {"state_version": 3, "next_iteration": 4},           # from the future
    ])
    def test_an_unusable_checkpoint_degrades_to_a_fresh_start(self, tmp_path, payload):
        """Falling back to iteration 1 repeats work. Trusting a half-understood
        checkpoint would SKIP work that may never have happened."""
        task = "Fix bug"
        d = _run_dir(str(tmp_path), _task_hash(task))
        d.mkdir(parents=True, exist_ok=True)
        base = {"tool": "dev-loop", "task_hash": _task_hash(task),
                "phase": "research_and_plan_done", "research_and_plan": _PLAN}
        (d / "state.json").write_text(json.dumps({**base, **payload}), encoding="utf-8")

        assert _resume_checkpoint(str(tmp_path), _task_hash(task)) is None

    def test_state_is_written_atomically(self, tmp_path):
        """No .tmp left behind, and the target is complete JSON."""
        task = "Fix bug"
        _write_checkpoint(
            str(tmp_path), _task_hash(task),
            cache_phase="research_and_plan_done", research_and_plan=_PLAN,
            next_iteration=2, elapsed_budget_sec=1.0,
            previous_quality_findings=[], previous_resolution_output="",
            deferred_p3={}, seen_quality_signatures=set(),
            seen_review_signatures=set(), tokens=TokenCounter(),
            park_reason=_PARK_CAPACITY,
        )
        d = _run_dir(str(tmp_path), _task_hash(task))
        assert (d / "state.json").exists()
        assert not (d / "state.json.tmp").exists()
        json.loads((d / "state.json").read_text("utf-8"))


# ── G14/G15: the worktree gate ───────────────────────────────────────────────

class TestWorktreeGateResumeException:

    def _checkpoint_with(self, repo: Path, task: str, dirty: list[str]):
        # The TOOL is invoked with strip_metadata_tags(task) by _execute_tool_task,
        # so it checkpoints under the hash of the CLEAN text — while the gate is
        # handed the raw queue line. Writing under the raw hash here (as the first
        # version of these tests did) makes both sides agree by accident and hides
        # exactly the production defect that only the review caught.
        clean_hash = _task_hash(strip_metadata_tags(task))
        d = _run_dir(str(repo), clean_hash)
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({
            # Both the directory AND the payload key: _read_state_file validates
            # state["task_hash"], so getting only the directory right still yields None.
            "tool": "dev-loop", "task_hash": clean_hash, "state_version": 2,
            "phase": "research_and_plan_done", "research_and_plan": _PLAN,
            "next_iteration": 3, "elapsed_budget_sec": 100.0,
            "previous_quality_findings": [], "previous_resolution_output": "",
            "deferred_p3": [], "seen_quality_signatures": [],
            "seen_review_signatures": [], "tokens": {},
            "park_reason": _PARK_CAPACITY, "dirty_snapshot_ok": True,
            "dirty_paths": dirty,
        }), encoding="utf-8")

    def test_gate_lets_a_resumed_task_through(self, tmp_path):
        """G14 — without this the capacity park is a trap: the task is parked and
        then refused TERMINALLY on the next poll for the mess it made itself."""
        repo = _git_repo(tmp_path)
        task = "Fix bug #tool:dev-loop"
        (repo / "work.py").write_text("changed\n", encoding="utf-8")
        self._checkpoint_with(repo, task, _dirty_paths(str(repo)))

        assert orchestrator._worktree_gate_violation(task, "dev-loop", str(repo)) is None

    def test_a_foreign_change_invalidates_the_exception(self, tmp_path):
        """G15 — the whole reason this is a path SET and not a boolean flag."""
        repo = _git_repo(tmp_path)
        task = "Fix bug #tool:dev-loop"
        (repo / "work.py").write_text("changed\n", encoding="utf-8")
        self._checkpoint_with(repo, task, _dirty_paths(str(repo)))
        # Somebody else edits the same repo between park and resume.
        (repo / "somebody_elses.py").write_text("not mine\n", encoding="utf-8")

        msg = orchestrator._worktree_gate_violation(task, "dev-loop", str(repo))
        assert msg is not None, "foreign work must restore the normal refusal"
        assert "uncommitted changes present" in msg

    def test_fewer_dirty_paths_than_recorded_is_still_ours(self, tmp_path):
        """Somebody committed part of it. Still our tree, still resumable."""
        repo = _git_repo(tmp_path)
        task = "Fix bug #tool:dev-loop"
        (repo / "a.py").write_text("a\n", encoding="utf-8")
        (repo / "b.py").write_text("b\n", encoding="utf-8")
        self._checkpoint_with(repo, task, _dirty_paths(str(repo)))
        (repo / "b.py").unlink()

        assert orchestrator._worktree_gate_violation(task, "dev-loop", str(repo)) is None

    def test_without_a_checkpoint_the_gate_still_refuses(self, tmp_path):
        """The exemption must not fire for an ordinary dirty repo."""
        repo = _git_repo(tmp_path)
        (repo / "work.py").write_text("changed\n", encoding="utf-8")

        msg = orchestrator._worktree_gate_violation("Fix bug #tool:dev-loop",
                                                    "dev-loop", str(repo))
        assert msg is not None

    def test_a_checkpoint_without_recorded_paths_proves_nothing(self, tmp_path):
        """git was unreadable when the run parked → no ownership proof → refuse."""
        repo = _git_repo(tmp_path)
        task = "Fix bug #tool:dev-loop"
        (repo / "work.py").write_text("changed\n", encoding="utf-8")
        self._checkpoint_with(repo, task, [])

        assert orchestrator._worktree_gate_violation(task, "dev-loop", str(repo)) is not None

    def test_a_broken_resume_check_does_not_take_the_run_down(self, tmp_path, monkeypatch):
        """The gate must degrade to its normal refusal, not raise into run_once."""
        repo = _git_repo(tmp_path)
        (repo / "work.py").write_text("changed\n", encoding="utf-8")

        def _boom(self, cwd, task):
            raise RuntimeError("resume check exploded")

        monkeypatch.setattr(type(get_tool("dev-loop")), "resume_permits_dirty", _boom)
        msg = orchestrator._worktree_gate_violation("Fix bug #tool:dev-loop",
                                                    "dev-loop", str(repo))
        assert msg is not None and "uncommitted changes present" in msg

    def test_other_tools_are_unaffected(self, tmp_path):
        """resume_permits_dirty defaults to False, and review-loop has no gate anyway."""
        repo = _git_repo(tmp_path)
        (repo / "work.py").write_text("changed\n", encoding="utf-8")

        assert orchestrator._worktree_gate_violation("t #tool:review-loop",
                                                     "review-loop", str(repo)) is None
        # The BaseTool default, exercised through a real tool that does not override
        # it — even with a checkpoint-shaped directory present it stays False.
        assert get_tool("review-loop").resume_permits_dirty(str(repo), "t") == (False, "")

    def test_the_tools_own_run_directory_never_counts_as_foreign(self, tmp_path):
        """`.dev-loop/` is gitignored HERE but not necessarily in a target repo.

        Without the exclusion the checkpoint's own state.json looks like a path that
        appeared after the park, so every resume would be refused in such a repo.
        """
        repo = _git_repo(tmp_path)
        (repo / ".dev-loop" / "abc123").mkdir(parents=True)
        (repo / ".dev-loop" / "abc123" / "state.json").write_text("{}", encoding="utf-8")
        assert _dirty_paths(str(repo)) == []


# ── _dirty_paths ─────────────────────────────────────────────────────────────

class TestDirtyPaths:

    def test_clean_repo_has_none(self, tmp_path):
        assert _dirty_paths(str(_git_repo(tmp_path))) == []

    def test_untracked_and_modified_both_count(self, tmp_path):
        repo = _git_repo(tmp_path)
        (repo / "seed.txt").write_text("changed\n", encoding="utf-8")
        (repo / "new.txt").write_text("new\n", encoding="utf-8")
        assert _dirty_paths(str(repo)) == ["new.txt", "seed.txt"]

    def test_no_cwd_and_no_repo_are_empty_not_an_error(self, tmp_path):
        assert _dirty_paths(None) == []
        assert _dirty_paths(str(tmp_path / "nope")) == []


# ── Regressions found by the Phase-3 review, not by the first test pass ──────

class TestReviewFoundRegressions:
    """Five defects the first version of this file was structurally unable to see.

    Kept as their own class on purpose: each one is a case where the earlier gate
    looked right and proved nothing, which is the failure mode worth naming.
    """

    def test_the_gate_finds_the_checkpoint_the_tool_really_wrote(self, monkeypatch, tmp_path):
        """P1 — the gate hashed the raw queue line, the tool the stripped one.

        This is the end-to-end version, and the only shape that could have caught it:
        the tool is invoked the way `_execute_tool_task` invokes it (with
        `strip_metadata_tags(task)`), and the gate is then asked with the RAW line,
        exactly as `run_once` asks it. The earlier gate tests handed the same raw
        string to both sides, so they agreed by construction and stayed green while
        the production path could never match.
        """
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        repo = _git_repo(tmp_path)
        raw = "Fix the login bug #tool:dev-loop #claude_opus cwd:" + str(repo)
        clean = strip_metadata_tags(raw)
        assert _task_hash(raw) != _task_hash(clean), "precondition: the hashes differ"

        # The tool parks in iteration 1 (quota gone during the quality review).
        prov = _Scripted([_PLAN, "Impl."], fail_at=3, fail_error="rate_limit")
        (repo / "work.py").write_text("half-done\n", encoding="utf-8")
        result = DevLoopTool().run(clean, prov, cwd=str(repo))
        assert result.error_code == "capacity_exhausted"

        # ...and the gate, asked with the RAW line, must recognise the continuation.
        assert orchestrator._worktree_gate_violation(raw, "dev-loop", str(repo)) is None, (
            "the parked task must be allowed to resume; refusing it here is terminal"
        )

    def test_a_park_in_iteration_one_is_a_usable_checkpoint(self, monkeypatch, tmp_path):
        """P1 — `next_iteration == 1` used to be rejected by its own reader.

        The record carries five other things besides the iteration number, and all
        five were thrown away with it: the ownership proof, the consumed budget, the
        P3 offers and both loop detectors.
        """
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        repo = _git_repo(tmp_path)
        task = "Fix bug"
        (repo / "work.py").write_text("half-done\n", encoding="utf-8")
        prov = _Scripted([_PLAN, "Impl."], fail_at=3, fail_error="rate_limit")
        DevLoopTool().run(task, prov, cwd=str(repo))

        resume = _resume_checkpoint(str(repo), _task_hash(task))
        assert resume is not None, "a park in iteration 1 must still be resumable"
        assert resume["next_iteration"] == 1
        assert resume["elapsed_budget_sec"] >= 0.0
        assert resume["dirty_paths"] == ["work.py"], (
            "without the ownership proof the worktree gate refuses the resume"
        )

    def test_each_landing_phase_is_clamped_to_the_time_left_at_that_moment(
            self, monkeypatch, tmp_path):
        """P2 — the clamp was computed ONCE and spread over three sequential phases.

        A mocked provider returns instantly, so with a still clock all three phases
        legitimately see the full remaining budget and the defect is invisible — the
        first version of this gate asserted `all(t <= budget)` and would have stayed
        green either way. The clamp can only be observed when wall-clock actually
        passes, so this drives a fake clock that a phase advances by exactly the
        timeout it was granted (a phase that uses its whole budget).

        Expected with a 600 s budget: exec gets 600 and burns it, after which
        NOTHING is left — so quality and resolution drop to the floor. Before the
        fix the three were [600, 600, 600] regardless of what exec consumed.
        """
        _patch(monkeypatch)

        clock = {"t": 1000.0}

        class _Clock:
            @staticmethod
            def monotonic():
                return clock["t"]

            @staticmethod
            def sleep(_seconds):
                pass

        monkeypatch.setattr("tools.dev_loop.time", _Clock)
        # The deadline itself is built in base_tool from the REAL clock, so it has to
        # be anchored to the fake one too — otherwise `remaining` is astronomically
        # large and no landing round happens at all.
        monkeypatch.setattr(DevLoopTool, "_runtime_deadline",
                            lambda self, consumed_sec=0.0: clock["t"] + 600 - consumed_sec)

        class _TimeConsuming(_Scripted):
            def run(self, task, cwd=None, timeout=0, **kwargs):
                res = super().run(task, cwd=cwd, timeout=timeout, **kwargs)
                # Research+Plan is not part of the landing round and is granted 5400 s
                # by default; letting it burn that would exhaust the 600 s budget
                # before the loop even starts. Only the round's own phases consume.
                if self.calls > 1:
                    clock["t"] += timeout      # the phase used its whole allowance
                return res

        prov = _TimeConsuming([_PLAN, "Implementation.", _CLEAN_QUALITY, _RESOLVED])
        DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        landing = prov.timeouts[1:]          # call 1 is Research+Plan, before the loop
        assert len(landing) == 3, f"expected one landing round of three phases, got {landing}"
        assert landing[0] > landing[1], (
            f"a phase that consumed the budget must shrink the next one, got {landing}"
        )
        assert landing[1] == landing[2] == config.TOOL_LANDING_MIN_PHASE_SEC, (
            f"with nothing left, later phases fall to the floor, got {landing}"
        )
        # The floor is the ONE deliberate way past the budget, and it is bounded:
        # at most two later phases can each be granted the floor when the budget is
        # already spent. Anything beyond that is the defect this test exists for.
        assert sum(landing) <= 600 + 2 * config.TOOL_LANDING_MIN_PHASE_SEC, (
            f"overrun must be bounded by the floor, got {landing} = {sum(landing)}s"
        )

    def test_a_terminal_runtime_end_drops_the_resume_point(self, monkeypatch, tmp_path):
        """P2 — otherwise /retry is silently dead for that task forever.

        A terminal `tool_runtime_exceeded` used to leave a checkpoint carrying
        `elapsed_budget_sec ~= max_runtime`, so every later attempt at the same queue
        line started with zero budget and returned without a single provider call.
        """
        _patch(monkeypatch)
        _budget(monkeypatch, config.TOOL_LANDING_RESERVE_SEC)
        task = "Fix bug"
        prov = _Scripted([_PLAN, "Impl.", _DIRTY_QUALITY, _UNRESOLVED])
        first = DevLoopTool().run(task, prov, cwd=str(tmp_path))
        assert first.error_code == "tool_runtime_exceeded"
        assert _resume_checkpoint(str(tmp_path), _task_hash(task)) is None, (
            "a terminal end is not a park — the next attempt is a NEW attempt"
        )

        # The retry really runs again instead of returning on an exhausted budget.
        _budget(monkeypatch, 10800)
        prov2 = _Scripted([_PLAN, "Impl.", _CLEAN_QUALITY, _RESOLVED])
        second = DevLoopTool().run(task, prov2, cwd=str(tmp_path))
        assert second.success is True
        assert prov2.calls > 0, "the retry must reach the provider at all"

    def test_a_park_during_research_still_records_budget_and_ownership(
            self, monkeypatch, tmp_path):
        """P2 — the earliest park point wrote no checkpoint, so repeated capacity
        parks there never converged: every retry got a fresh full budget."""
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        repo = _git_repo(tmp_path)
        task = "Fix bug"
        (repo / "work.py").write_text("touched\n", encoding="utf-8")
        prov = _Scripted([], fail_at=1, fail_error="rate_limit")

        result = DevLoopTool().run(task, prov, cwd=str(repo))

        assert result.error_code == "capacity_exhausted"
        resume = _resume_checkpoint(str(repo), _task_hash(task))
        assert resume is not None, "the Research+Plan park must leave a record"
        assert resume["next_iteration"] == 1
        assert resume["dirty_paths"] == ["work.py"]

    def test_the_research_checkpoint_does_not_downgrade_an_existing_one(
            self, monkeypatch, tmp_path):
        """P2 follow-on — the post-Research+Plan save wrote the old four-key dict.

        Path created by the fix above: park during Research+Plan (writes v2 with a
        consumed budget) → next run redoes Research+Plan → the old save would drop
        back to v1 and hand the run a fresh full budget.
        """
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        repo = _git_repo(tmp_path)
        task = "Fix bug"
        (repo / "work.py").write_text("touched\n", encoding="utf-8")

        DevLoopTool().run(task, _Scripted([], fail_at=1, fail_error="rate_limit"), cwd=str(repo))
        # Second attempt: research succeeds, then quota dies again in execution.
        DevLoopTool().run(task, _Scripted([_PLAN], fail_at=2, fail_error="rate_limit"),
                          cwd=str(repo))

        resume = _resume_checkpoint(str(repo), _task_hash(task))
        assert resume is not None, "the record must survive a successful Research+Plan"
        assert resume["elapsed_budget_sec"] >= 0.0
        assert resume["dirty_paths"] == ["work.py"]


# ── Regressions found by the external pass (Codex), round 2 ──────────────────

class TestExternalReviewRegressions:
    """Defects the internal review and the first mutation round both missed."""

    @pytest.mark.parametrize("bad", [
        {"previous_resolution_output": {"nicht": "string"}},
        {"previous_resolution_output": ["auch", "nicht"]},
        {"seen_review_signatures": [[[["verschachtelt"]], "UNRESOLVED", "x"]]},
        {"seen_quality_signatures": [[["verschachtelt"]]]},
        {"previous_quality_findings": [{"kein": "string"}, 42]},
        {"deferred_p3": "kein array"},
        {"dirty_paths": [1, 2, 3]},
        {"tokens": "kein dict"},
    ])
    def test_a_hostile_state_file_cannot_crash_the_run(self, tmp_path, bad):
        """P1 - the docstring promised defensive coercion of every field; two fields
        were passed through raw and both produce a real TypeError.

        previous_resolution_output lands in an f-string, and the signature tuples
        land in set.update(), where a nested list is unhashable. The file sits in a
        directory the provider under review can write, so this is a reachable crash
        path, not a thought experiment.
        """
        task = "Fix bug"
        h = _task_hash(task)
        d = _run_dir(str(tmp_path), h)
        d.mkdir(parents=True, exist_ok=True)
        base = {
            "tool": "dev-loop", "task_hash": h, "state_version": 2,
            "park_reason": _PARK_CAPACITY, "phase": "research_and_plan_done",
            "research_and_plan": _PLAN, "next_iteration": 2,
            "elapsed_budget_sec": 1.0, "previous_quality_findings": [],
            "previous_resolution_output": "", "deferred_p3": [],
            "seen_quality_signatures": [], "seen_review_signatures": [],
            "tokens": {}, "dirty_paths": [], "dirty_snapshot_ok": True,
        }
        (d / "state.json").write_text(json.dumps({**base, **bad}), encoding="utf-8")

        resume = _resume_checkpoint(str(tmp_path), h)
        assert resume is not None, "a coercible file must still be usable"
        assert isinstance(resume["previous_resolution_output"], str)
        assert all(isinstance(s, str) for s in resume["previous_quality_findings"])
        assert all(isinstance(s, str) for s in resume["deferred_p3"])
        assert all(isinstance(s, str) for s in resume["dirty_paths"])
        assert isinstance(resume["tokens"], dict)
        # The operations that actually blew up must now be safe.
        set().update(resume["seen_quality_signatures"])
        set().update(resume["seen_review_signatures"])
        assert "x" + resume["previous_resolution_output"] is not None

    def test_a_late_capacity_park_still_leaves_room_to_resume(self, monkeypatch, tmp_path):
        """P2 - a park that recorded a fully spent budget promised a continuation it
        could not deliver: the next run hit remaining <= 0 and stamped the task
        terminally without one provider call. Worse than never parking."""
        _patch(monkeypatch)
        _budget(monkeypatch, config.TOOL_LANDING_RESERVE_SEC)
        task = "Fix bug"

        # The clock ADVANCES on every read. A frozen clock made the earlier version
        # of this test trivially green: `int(deadline - now)` came out exactly at the
        # go/no-go threshold, so the resumed run appeared to proceed — while under a
        # real clock any elapsed time at all truncated it one below the threshold and
        # the run went terminal without a single provider call. That off-by-one was
        # the actual production defect this test is supposed to hold.
        clock = {"t": 5000.0}

        class _Clock:
            @staticmethod
            def monotonic():
                clock["t"] += 0.01      # startup, policy load, state read, prints
                return clock["t"]

            @staticmethod
            def sleep(_s):
                pass

        monkeypatch.setattr("tools.dev_loop.time", _Clock)
        monkeypatch.setattr(
            DevLoopTool, "_runtime_deadline",
            lambda self, consumed_sec=0.0: (
                clock["t"] + config.TOOL_LANDING_RESERVE_SEC - consumed_sec))

        class _Slow(_Scripted):
            def run(self, task, cwd=None, timeout=0, **kwargs):
                res = super().run(task, cwd=cwd, timeout=timeout, **kwargs)
                if self.calls > 1:
                    clock["t"] += timeout      # burn the whole allowance
                return res

        # Landing round: exec burns everything, then the quota dies in the review.
        first = DevLoopTool().run(task, _Slow([_PLAN, "Impl."], fail_at=3,
                                              fail_error="rate_limit"), cwd=str(tmp_path))
        assert first.error_code == "capacity_exhausted"

        resume = _resume_checkpoint(str(tmp_path), _task_hash(task))
        assert resume is not None
        left = config.TOOL_LANDING_RESERVE_SEC - resume["elapsed_budget_sec"]
        assert left > config.TOOL_LANDING_MIN_PHASE_SEC * 3, (
            f"a park must leave STRICTLY more than the go/no-go threshold, left {left}s "
            f"— leaving exactly the threshold means any startup cost at all truncates "
            f"below it and the resumed run ends terminally without a provider call"
        )

        # And the resumed run really reaches the provider instead of stamping.
        prov2 = _Slow(["Impl2.", _CLEAN_QUALITY, _RESOLVED])
        DevLoopTool().run(task, prov2, cwd=str(tmp_path))
        assert prov2.calls > 0, "the resumed run must not end without a provider call"

    def test_an_ordinary_round_is_clamped_too(self, monkeypatch, tmp_path):
        """P2 - the clamp only applied inside the landing round, so a normal round
        starting one second above the reserve was still granted 7200+3600+1800 s:
        up to ~10200 s past the deadline, while the docs claimed a 120 s cap."""
        _patch(monkeypatch)
        budget = config.TOOL_LANDING_RESERVE_SEC + 1     # one second too much to land
        _budget(monkeypatch, budget)
        prov = _Scripted([_PLAN, "Impl.", _DIRTY_QUALITY, _UNRESOLVED,
                          "Impl2.", _CLEAN_QUALITY, _RESOLVED])

        DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        round_one = prov.timeouts[1:4]
        assert round_one, "expected a normal (non-landing) round to run"
        assert all(t <= budget for t in round_one), (
            f"an ordinary round must not be granted more than the whole budget, "
            f"got {round_one} against {budget}s"
        )

    def test_an_empty_dirty_snapshot_is_not_a_failed_one(self, tmp_path):
        """P2 - a park that changed no business files recorded dirty_paths: [],
        and the gate treated the empty set as "no proof" and refused terminally.
        In a repo where .dev-loop/ is not gitignored the outer check still sees
        the tree as dirty, so this was the whole feature failing in exactly the
        repos the path filter exists for."""
        repo = _git_repo(tmp_path)
        task = "Fix bug #tool:dev-loop"
        h = _task_hash(strip_metadata_tags(task))
        d = _run_dir(str(repo), h)
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({
            "tool": "dev-loop", "task_hash": h, "state_version": 2,
            "park_reason": _PARK_CAPACITY, "phase": "research_and_plan_done",
            "research_and_plan": _PLAN, "next_iteration": 2, "elapsed_budget_sec": 1.0,
            "previous_quality_findings": [], "previous_resolution_output": "",
            "deferred_p3": [], "seen_quality_signatures": [],
            "seen_review_signatures": [], "tokens": {},
            "dirty_paths": [], "dirty_snapshot_ok": True,
        }), encoding="utf-8")
        # Only the tool's own run directory is dirty - nothing else.
        assert _dirty_paths(str(repo)) == []

        assert orchestrator._worktree_gate_violation(task, "dev-loop", str(repo)) is None, (
            "an honest empty snapshot must still allow the resume"
        )

    def test_a_failed_dirty_snapshot_still_refuses(self, tmp_path):
        """The other half: the flag, not the emptiness, is what proves nothing."""
        repo = _git_repo(tmp_path)
        task = "Fix bug #tool:dev-loop"
        h = _task_hash(strip_metadata_tags(task))
        d = _run_dir(str(repo), h)
        d.mkdir(parents=True, exist_ok=True)
        (repo / "work.py").write_text("changed\n", encoding="utf-8")
        (d / "state.json").write_text(json.dumps({
            "tool": "dev-loop", "task_hash": h, "state_version": 2,
            "park_reason": _PARK_CAPACITY, "phase": "research_and_plan_done",
            "research_and_plan": _PLAN, "next_iteration": 2, "elapsed_budget_sec": 1.0,
            "previous_quality_findings": [], "previous_resolution_output": "",
            "deferred_p3": [], "seen_quality_signatures": [],
            "seen_review_signatures": [], "tokens": {},
            "dirty_paths": ["work.py"], "dirty_snapshot_ok": False,
        }), encoding="utf-8")

        assert orchestrator._worktree_gate_violation(task, "dev-loop", str(repo)) is not None

    def test_a_park_that_cannot_persist_says_so(self, monkeypatch, tmp_path):
        """P2 - a capacity park reported "suspended until reset" even when the
        checkpoint write failed, so the next run found nothing and could be refused
        terminally for the dirt this run left behind."""
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr("tools.dev_loop._save_state", _boom)
        prov = _Scripted([_PLAN, "Impl."], fail_at=3, fail_error="rate_limit")

        result = DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        assert result.error_code == "capacity_exhausted", "the task must still park"
        assert "OHNE Fortsetzungspunkt" in (result.error or ""), (
            "a park without a durable checkpoint must not claim a continuation"
        )

    def test_error_ndjson_is_recognised_with_either_json_spacing(self, monkeypatch):
        """P3 - the substring test only matched compact JSON, so an event written by
        a plain json.dumps went unscanned and the raw prose escaped classification."""
        monkeypatch.setattr(
            claude_provider, "run_with_watchdog",
            lambda *a, **k: SimpleNamespace(
                returncode=1,
                stdout=json.dumps({"type": "error",
                                   "error": "You have hit your session limit"}),
                stderr="", stdin_error=None),
        )
        res = claude_provider.ClaudeProvider().run("p", cwd=None, timeout=60)
        assert res.error == "rate_limit"

    def test_landing_round_skips_the_lesson_call(self, monkeypatch, tmp_path):
        """P3 - the lesson summary is an unclamped extra provider call (120 s) that
        would push a run which just landed exactly on its deadline back past it.

        Patched on the `memory` module itself, because dev_loop imports it lazily
        inside run() (`import memory as memory_module`), so there is no attribute on
        tools.dev_loop to replace.
        """
        _patch(monkeypatch)
        called = []
        monkeypatch.setattr(memory, "create_lesson_from_loop",
                            lambda *a, **k: called.append(1))
        monkeypatch.setattr(memory, "search_lessons", lambda *a, **k: "")

        # The landing round must be iteration 2, not 1 — at iteration 1 the guard
        # `iteration > 1` already suppresses the lesson and the test would pass with
        # the new condition removed. A fake clock makes round 1 consume enough for
        # round 2 to fall under the reserve.
        clock = {"t": 3000.0}

        class _Clock:
            @staticmethod
            def monotonic():
                return clock["t"]

            @staticmethod
            def sleep(_s):
                pass

        budget = config.TOOL_LANDING_RESERVE_SEC + 1000
        monkeypatch.setattr("tools.dev_loop.time", _Clock)
        monkeypatch.setattr(DevLoopTool, "_runtime_deadline",
                            lambda self, consumed_sec=0.0: clock["t"] + budget - consumed_sec)

        class _Burn(_Scripted):
            def run(self, task, cwd=None, timeout=0, **kwargs):
                res = super().run(task, cwd=cwd, timeout=timeout, **kwargs)
                if self.calls == 2:          # execution of round 1
                    clock["t"] += 1000
                return res

        prov = _Burn([_PLAN, "Impl.", _DIRTY_QUALITY, _UNRESOLVED,
                      "Impl2.", _CLEAN_QUALITY, _RESOLVED])
        DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))
        exec_prompts = [q for q in prov.prompts
                        if "Implement the solution exactly as laid out" in q]
        assert len(exec_prompts) == 2, "precondition: the landing round is iteration 2"
        assert "LETZTE ITERATION" in exec_prompts[1]
        assert called == [], "no lesson call inside a landing round"

        # Control: an ordinary two-iteration run still writes its lesson.
        _budget(monkeypatch, 10800)
        prov2 = _Scripted([_PLAN, "Impl.", _DIRTY_QUALITY, _UNRESOLVED,
                           "Impl2.", _CLEAN_QUALITY, _RESOLVED])
        DevLoopTool().run("Anderer Task", prov2, cwd=str(tmp_path))
        assert called == [1], "an ordinary run must still produce its lesson"

    def test_a_v1_file_claiming_a_park_reason_is_still_not_resumable(self, tmp_path):
        """The version check must stand on its own.

        `park_reason` gating made it redundant in the happy path, so removing the
        version check left every test green — measured by mutation. A file that
        carries the newer key but the older schema has fields this reader would
        misinterpret, so the version, not the reason, has to decide first.
        """
        task = "Fix bug"
        h = _task_hash(task)
        d = _run_dir(str(tmp_path), h)
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({
            "tool": "dev-loop", "task_hash": h,
            "park_reason": _PARK_CAPACITY,          # newer key...
            "state_version": 1,                      # ...older schema
            "phase": "research_and_plan_done", "research_and_plan": _PLAN,
            "next_iteration": 4, "elapsed_budget_sec": 99.0,
            "dirty_paths": [], "dirty_snapshot_ok": True,
        }), encoding="utf-8")

        assert _resume_checkpoint(str(tmp_path), h) is None

    def test_research_and_plan_is_clamped_too(self, monkeypatch, tmp_path):
        """The same hole one phase earlier, and reachable through the new R+P park.

        Research+Plan asks for TOOL_DEV_RESEARCH+PLAN (5400 s) and ran outside the
        clamp. A run resumed after a park DURING Research+Plan redoes that phase with
        as little as one minimal landing round left, so an unclamped 5400 s request
        would overrun the whole budget at the first provider call.
        """
        _patch(monkeypatch)
        _budget(monkeypatch, 600)
        prov = _Scripted([_PLAN, "Impl.", _CLEAN_QUALITY, _RESOLVED])

        DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        assert prov.timeouts, "expected Research+Plan to run"
        assert prov.timeouts[0] <= 600, (
            f"Research+Plan must be clamped to the budget, got {prov.timeouts[0]}s"
        )

    def test_parks_are_counted_and_the_budget_cap_is_eventually_dropped(
            self, monkeypatch, tmp_path):
        """The bound the off-by-one fix made necessary.

        `next_iteration` does not advance on a park (the interrupted iteration is
        repeated), so a task whose quota dies in the same iteration every time is a
        fixed point TOOL_MAX_ITERATIONS never reaches. Capping the recorded budget
        forever would make that cycle endless; `_MAX_CAPACITY_PARKS` ends it by
        recording the truth, after which the run reaches its deadline and stops.
        """
        _patch(monkeypatch)
        task = "Fix bug"
        budget = 3000
        cap = budget - config.TOOL_LANDING_MIN_PHASE_SEC * 4      # what a park may record
        _budget(monkeypatch, budget)

        # The cap is a MAXIMUM, so it only shows when the run really burns budget:
        # with a mocked provider and a still clock the spend is ~0 and both branches
        # record the same thing. This clock jumps well past the cap.
        clock = {"t": 1000.0}

        class _Clock:
            @staticmethod
            def monotonic():
                return clock["t"]

            @staticmethod
            def sleep(_s):
                pass

        monkeypatch.setattr("tools.dev_loop.time", _Clock)
        monkeypatch.setattr(DevLoopTool, "_runtime_deadline",
                            lambda self, consumed_sec=0.0: clock["t"] + budget - consumed_sec)

        class _Burn(_Scripted):
            def run(self, task, cwd=None, timeout=0, **kwargs):
                res = super().run(task, cwd=cwd, timeout=timeout, **kwargs)
                clock["t"] += 950          # every call eats real wall-clock
                return res

        # First park: counted, and the recorded budget is CAPPED below the real spend.
        DevLoopTool().run(task, _Burn([_PLAN, "Impl."], fail_at=3,
                                      fail_error="rate_limit"), cwd=str(tmp_path))
        first = _resume_checkpoint(str(tmp_path), _task_hash(task))
        assert first["park_count"] == 1
        assert first["elapsed_budget_sec"] == cap, (
            f"under the limit a park records at most the cap ({cap}s), so the next "
            f"run keeps a landing round; got {first['elapsed_budget_sec']}"
        )

        # Pretend the task has already burnt its allowance of parks.
        d = _run_dir(str(tmp_path), _task_hash(task))
        state = json.loads((d / "state.json").read_text("utf-8"))
        state["park_count"] = _MAX_CAPACITY_PARKS
        state["park_reason"] = _PARK_CAPACITY
        (d / "state.json").write_text(json.dumps(state), encoding="utf-8")

        # The resumed run takes the plan from cache, so there is NO Research+Plan
        # call and the phase numbering shifts by one: call 1 is the execution.
        clock["t"] = 1000.0
        DevLoopTool().run(task, _Burn(["Impl."], fail_at=2,
                                      fail_error="rate_limit"), cwd=str(tmp_path))
        last = _resume_checkpoint(str(tmp_path), _task_hash(task))
        assert last["park_count"] == _MAX_CAPACITY_PARKS + 1
        assert last["elapsed_budget_sec"] > cap, (
            f"past the limit the cap is dropped and the TRUE spend is recorded, so the "
            f"next run runs out and ends instead of being handed another landing round; "
            f"got {last['elapsed_budget_sec']} against cap {cap}"
        )

    def test_a_resume_licence_is_single_use(self, monkeypatch, tmp_path):
        """A checkpoint permits ONE continuation, not a standing property.

        Only success and a terminal runtime end ever removed the file, so a licence
        survived every other ending — hang, format_error, both loop detectors, max
        iterations, a rejected plan — and kept granting the reduced budget and the
        dirty-tree waiver indefinitely, while the wall-clock those runs burned was
        never counted.
        """
        _patch(monkeypatch)
        _budget(monkeypatch, 10800)
        task = "Fix bug"

        DevLoopTool().run(task, _Scripted([_PLAN, "Impl."], fail_at=3,
                                          fail_error="rate_limit"), cwd=str(tmp_path))
        assert _resume_checkpoint(str(tmp_path), _task_hash(task)) is not None

        # A resumed run that ends in something OTHER than a park must leave no licence.
        DevLoopTool().run(task, _Scripted([_PLAN, "Impl."], fail_at=2,
                                          fail_error="hang"), cwd=str(tmp_path))
        assert _resume_checkpoint(str(tmp_path), _task_hash(task)) is None, (
            "the licence was consumed on use and a hang does not write a new one"
        )
        # ...but the research cache survives, so the next attempt still skips it.
        state = json.loads(
            (_run_dir(str(tmp_path), _task_hash(task)) / "state.json").read_text("utf-8"))
        assert state["research_and_plan"], "consuming the licence must not drop the plan"

    def test_a_clean_landing_round_says_it_was_one(self, monkeypatch, tmp_path):
        """A landing round that passes its reviews is a success and gets the normal
        stamp — but its executor was told to STOP starting things and list the rest.
        Without saying so, the recipient reads a clean finish where work was
        deliberately left on the table."""
        _patch(monkeypatch)
        _budget(monkeypatch, config.TOOL_LANDING_RESERVE_SEC)
        prov = _Scripted([_PLAN, "Implementation.", _CLEAN_QUALITY, _RESOLVED])

        result = DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        assert result.success is True
        summary = (_run_dir(str(tmp_path), _task_hash("Fix bug")) / "summary.md").read_text("utf-8")
        assert "Offen geblieben" in summary, (
            "the summary must point at the list the landing round was told to produce"
        )

    def test_lesson_call_is_skipped_when_the_budget_cannot_carry_it(
            self, monkeypatch, tmp_path):
        """The lesson call is 120 s in another module, outside `_landing_cap`.

        Skipping it only inside a landing round is not enough: an ORDINARY round can
        start one second above the reserve, consume the whole rest through the clamp,
        and then still make this call — the unaccounted consumer that made the
        documented overrun bound wrong. Here iteration 2 is a normal round (it starts
        above the reserve) that ends with less than the call needs.
        """
        _patch(monkeypatch)
        called = []
        monkeypatch.setattr(memory, "create_lesson_from_loop",
                            lambda *a, **k: called.append(1))
        monkeypatch.setattr(memory, "search_lessons", lambda *a, **k: "")

        budget = 5000
        clock = {"t": 2000.0}

        class _Clock:
            @staticmethod
            def monotonic():
                return clock["t"]

            @staticmethod
            def sleep(_s):
                pass

        monkeypatch.setattr("tools.dev_loop.time", _Clock)
        monkeypatch.setattr(DevLoopTool, "_max_runtime_sec", lambda self: budget)
        monkeypatch.setattr(DevLoopTool, "_runtime_deadline",
                            lambda self, consumed_sec=0.0: clock["t"] + budget - consumed_sec)

        class _Burn(_Scripted):
            def run(self, task, cwd=None, timeout=0, **kwargs):
                res = super().run(task, cwd=cwd, timeout=timeout, **kwargs)
                if self.calls in (2, 5):      # the execution phase of each round
                    clock["t"] += 2450
                return res

        prov = _Burn([_PLAN, "Impl.", _DIRTY_QUALITY, _UNRESOLVED,
                      "Impl2.", _CLEAN_QUALITY, _RESOLVED])
        result = DevLoopTool().run("Fix bug", prov, cwd=str(tmp_path))

        exec_prompts = [q for q in prov.prompts
                        if "Implement the solution exactly as laid out" in q]
        assert result.success is True and result.iterations == 2
        assert not any("LETZTE ITERATION" in q for q in exec_prompts), (
            "precondition: BOTH rounds must be ordinary, so only the budget check "
            "can suppress the lesson call"
        )
        assert called == [], (
            "with less than the call needs left, the lesson must be skipped even in "
            "an ordinary round"
        )
