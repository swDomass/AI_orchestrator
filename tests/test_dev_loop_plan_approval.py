"""dev-loop plan approval (`tool_phases.dev-loop.plan_approval`) — fail-closed.

Until 2026-10-06 the switch was dead twice over: `_get_plan_approval_mode` imported
a `policy.load_policy` that never existed (ImportError → always "auto"), and the
approval itself imported a `notifier.request_approval` that never existed either
(ImportError → "Plan-Approval uebersprungen" → the run went on unapproved). On top
of that the plan was cached BEFORE the approval, so a rejected or interrupted
approval left a plan behind that the next run executed without asking.

What is real here and what is not: the PolicyEngine is a real one, reading a real
policy.yaml in tmp_path and installed as the process singleton, exactly where
dev-loop finds it at runtime. The only fake is the outermost boundary,
`notifier._send` — and the "user" answers from inside it through `engine._respond`,
the same call the TelegramListener makes for /approve, /deny and /skip. The pending
slot is set before the message goes out, so the answer arrives synchronously.
Neither the import, nor `request_approval`, nor `get_tool_phase` is mocked — except
where a test names exactly that component as the fault: three cases of (c)
(`get_engine` raising or returning None, `request_approval` raising) and the mode
reader's engine-failure case.
"""

import json
import os
import threading
import time
from collections.abc import Callable

import pytest

import notifier
import policy as policy_module
import taxonomy
import tools.dev_loop as dev_loop_module
from providers.base import RunResult
from tools.base_tool import TokenCounter
from tools.dev_loop import (
    _PARK_CAPACITY,
    DevLoopTool,
    _resume_checkpoint,
    _run_dir,
    _task_hash,
    _write_checkpoint,
)

_TASK = "Fix login bug in auth.py"
_PLAN = (
    "## Problem Analysis\nToken check skipped.\n"
    "## Implementation Plan\n1. PLAN-MARKER-7f3: validate the token in auth.py."
)
_CLEAN_QUALITY = "No P1/P2/P3 findings."
_RESOLVED = "RESOLVED: Bug is fixed."

_RESEARCH_AND_PLAN_MARK = "You are a Research+Planning Agent"
_RESEARCH_ONLY_MARK = "You are a Research Agent."
_EXEC_MARK = "You are an Execution Agent"


# ── Helpers ──────────────────────────────────────────────────────────────────

class _Scripted:
    """Pre-scripted provider (after tests/test_dev_loop.py::_ScriptedProvider).

    `fail_at` makes that 1-based call fail with `fail_error` — "rate_limit" is a
    capacity end, so dev-loop PARKS instead of failing (used for the resume case).
    """
    name = "claude"
    supports_sessions = False

    def __init__(self, outputs, fail_at=None, fail_error="rate_limit") -> None:
        self._outputs = list(outputs)
        self.prompts: list[str] = []
        self._fail_at = fail_at
        self._fail_error = fail_error

    def run(self, task, cwd=None, timeout=0, **kwargs):
        self.prompts.append(task)
        if self._fail_at is not None and len(self.prompts) == self._fail_at:
            return RunResult(success=False, error=self._fail_error)
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))

    def count(self, mark: str) -> int:
        return sum(1 for p in self.prompts if mark in p)


def _full_run():
    return _Scripted([_PLAN, "Fixed auth.py.", _CLEAN_QUALITY, _RESOLVED])


def _patch(monkeypatch, tmp_path):
    """tests/test_dev_loop.py::_patch, plus a git ceiling for the approval text.

    notify_approval_required runs `git` in the cwd for its repo block; without the
    ceiling it would walk up out of tmp_path into whatever repo contains it.
    """
    monkeypatch.setattr("tools.dev_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.dev_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.dev_loop.time.sleep", lambda _: None)
    monkeypatch.setattr("tools.dev_loop.is_cached_provider_available", lambda _name: True)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def _install_engine(tmp_path, monkeypatch, policy_yaml: str | None) -> policy_module.PolicyEngine:
    """A real PolicyEngine on tmp_path, installed as the process singleton.

    `policy_yaml=None` writes no file at all (the "policy.yaml missing" case).
    """
    ai = tmp_path / "vault" / "99_System" / "AI"
    ai.mkdir(parents=True)
    if policy_yaml is not None:
        (ai / "policy.yaml").write_text(policy_yaml, encoding="utf-8")
    engine = policy_module.PolicyEngine(vault_path=tmp_path / "vault")
    monkeypatch.setattr(policy_module, "_engine", engine)
    return engine


def _mode_yaml(mode: str) -> str:
    return f"tool_phases:\n  dev-loop:\n    plan_approval: {mode}\n"


class _Telegram:
    """Fake `notifier._send`. Records every message; answers like the user would.

    `answers` is consumed one entry per message; None (or an empty list) means
    nobody answers, so the request runs into its timeout.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.answers: list[str | None] = []
        self.raises: BaseException | None = None
        self.probe: Callable[[], object] | None = None   # sampled at each send
        self.probed: list[object] = []

    def __call__(self, text: str) -> bool:
        self.sent.append(text)
        if self.probe is not None:
            self.probed.append(self.probe())
        if self.raises is not None:
            raise self.raises
        answer = self.answers.pop(0) if self.answers else None
        if answer is not None:
            policy_module.get_engine()._respond(answer)
        return True


@pytest.fixture
def telegram(monkeypatch):
    fake = _Telegram()
    monkeypatch.setattr(notifier, "_send", fake)
    return fake


@pytest.fixture
def cwd(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return str(work)


def _state_path(cwd: str, task: str = _TASK):
    return _run_dir(cwd, _task_hash(task)) / "state.json"


# ── (a) approve: the real path runs ──────────────────────────────────────────

def test_approve_sends_one_request_carrying_the_plan(monkeypatch, tmp_path, telegram, cwd):
    _patch(monkeypatch, tmp_path)
    engine = _install_engine(tmp_path, monkeypatch, _mode_yaml("approve"))
    telegram.answers = ["approved"]

    DevLoopTool().run(_TASK, _full_run(), cwd=cwd)

    assert len(telegram.sent) == 1, telegram.sent
    (msg,) = telegram.sent
    assert "PLAN-MARKER-7f3" in msg, "the request must show the plan it asks about"
    assert "Fix login bug" in msg
    assert "plan" in msg.lower() and "approval" in msg.lower()
    # The engine consumed the answer: nothing pending any more.
    assert engine.has_pending_approval() is False


# ── (b) only "approved" executes ─────────────────────────────────────────────

def test_approved_plan_is_executed(monkeypatch, tmp_path, telegram, cwd):
    _patch(monkeypatch, tmp_path)
    _install_engine(tmp_path, monkeypatch, _mode_yaml("approve"))
    telegram.answers = ["approved"]
    provider = _full_run()
    telegram.probe = lambda: len(provider.prompts)

    result = DevLoopTool().run(_TASK, provider, cwd=cwd)

    assert result.success is True
    assert len(provider.prompts) == 4
    assert provider.count(_EXEC_MARK) == 1, "approval granted → execution must run"
    assert telegram.probed == [1], "asked exactly once, after the plan and before execution"


@pytest.mark.parametrize("case", [
    ("denied", "approval_denied"),
    ("skipped", "approval_skipped"),
    (None, "approval_timeout"),
], ids=["denied", "skipped", "timeout"])
def test_anything_but_approved_halts_before_execution(monkeypatch, tmp_path, telegram, cwd, case):
    answer, code = case
    _patch(monkeypatch, tmp_path)
    _install_engine(tmp_path, monkeypatch, _mode_yaml("approve"))
    monkeypatch.setattr(dev_loop_module, "_PLAN_APPROVAL_TIMEOUT_SEC", 0)
    telegram.answers = [answer]
    provider = _full_run()

    result = DevLoopTool().run(_TASK, provider, cwd=cwd)

    assert len(provider.prompts) == 1, "only the plan phase may have run"
    assert provider.count(_EXEC_MARK) == 0
    assert result.success is False
    assert result.error_code == code
    assert result.retryable is False, "a retry would re-plan and wait again — ❌ instead"
    assert result.iterations == 0
    assert "angehalten" in result.error
    assert "PLAN-MARKER-7f3" in result.output, "the plan stays visible in the output"


def test_telegram_unreachable_ends_in_timeout_not_in_execution(monkeypatch, tmp_path, cwd):
    """No fake at all: the real `_send` with Telegram switched off returns False,
    nothing answers, and the wait runs out. That has to halt, not continue."""
    _patch(monkeypatch, tmp_path)
    _install_engine(tmp_path, monkeypatch, _mode_yaml("approve"))
    monkeypatch.setattr(notifier, "TELEGRAM_ENABLED", False)
    monkeypatch.setattr(dev_loop_module, "_PLAN_APPROVAL_TIMEOUT_SEC", 0)
    provider = _full_run()

    result = DevLoopTool().run(_TASK, provider, cwd=cwd)

    assert provider.count(_EXEC_MARK) == 0
    assert (result.success, result.error_code, result.retryable) == (
        False, "approval_timeout", False,
    )


# ── (c) a broken approval path halts (fail-closed) ───────────────────────────

def _boom(*_a, **_kw):
    raise RuntimeError("approval path broken")


@pytest.mark.parametrize("fault", [
    "send_raises",
    "engine_not_loadable",
    "no_engine",
    "request_approval_raises",
    "unknown_answer",
])
def test_a_broken_approval_path_halts_the_run(monkeypatch, tmp_path, telegram, cwd, fault):
    _patch(monkeypatch, tmp_path)
    engine = _install_engine(tmp_path, monkeypatch, _mode_yaml("approve"))
    if fault == "send_raises":
        telegram.raises = OSError("telegram socket gone")
    elif fault == "engine_not_loadable":
        monkeypatch.setattr(policy_module, "get_engine", _boom)
    elif fault == "no_engine":
        monkeypatch.setattr(policy_module, "get_engine", lambda: None)
    elif fault == "request_approval_raises":
        monkeypatch.setattr(engine, "request_approval", _boom)
    elif fault == "unknown_answer":
        telegram.answers = ["maybe later"]
    provider = _full_run()

    result = DevLoopTool().run(_TASK, provider, cwd=cwd)

    assert len(provider.prompts) == 1, "nothing beyond the plan phase may run"
    assert provider.count(_EXEC_MARK) == 0
    assert result.success is False
    assert result.error_code == "approval_unavailable"
    assert result.retryable is False
    assert "angehalten" in result.error
    assert not _state_path(cwd).exists(), "a plan nobody approved must not stay cached"


def test_a_broken_approval_path_is_logged(monkeypatch, tmp_path, telegram, cwd, caplog):
    _patch(monkeypatch, tmp_path)
    _install_engine(tmp_path, monkeypatch, _mode_yaml("approve"))
    telegram.raises = OSError("telegram socket gone")

    with caplog.at_level("WARNING", logger="tools.dev_loop"):
        DevLoopTool().run(_TASK, _full_run(), cwd=cwd)

    assert any("telegram socket gone" in r.getMessage() and "angehalten" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# ── (d) auto / skip / no section: nothing asked, flow unchanged ──────────────

@pytest.mark.parametrize("policy_yaml", [
    _mode_yaml("auto"),
    "tool_providers:\n  default: [claude]\n",   # file there, no tool_phases block
    None,                                        # no policy.yaml at all
], ids=["auto", "no_tool_phases", "no_file"])
def test_auto_and_absent_switch_never_ask(monkeypatch, tmp_path, telegram, cwd, policy_yaml):
    _patch(monkeypatch, tmp_path)
    _install_engine(tmp_path, monkeypatch, policy_yaml)
    provider = _full_run()

    result = DevLoopTool().run(_TASK, provider, cwd=cwd)

    assert telegram.sent == []
    assert result.success is True
    assert len(provider.prompts) == 4
    assert _RESEARCH_AND_PLAN_MARK in provider.prompts[0]
    assert _EXEC_MARK in provider.prompts[1]


def test_skip_never_asks_and_runs_research_only(monkeypatch, tmp_path, telegram, cwd):
    """`skip` takes effect for the first time with this change (Defekt 2)."""
    _patch(monkeypatch, tmp_path)
    _install_engine(tmp_path, monkeypatch, _mode_yaml("skip"))
    provider = _Scripted(["## Problem Analysis\nFound.", "Fixed.", _CLEAN_QUALITY, _RESOLVED])

    result = DevLoopTool().run(_TASK, provider, cwd=cwd)

    assert telegram.sent == []
    assert result.success is True
    assert _RESEARCH_ONLY_MARK in provider.prompts[0]
    assert "Do NOT produce an implementation plan" in provider.prompts[0]
    assert _RESEARCH_AND_PLAN_MARK not in provider.prompts[0]


@pytest.mark.parametrize("policy_yaml, expected", [
    (_mode_yaml("approve"), "approve"),
    (_mode_yaml("auto"), "auto"),
    (_mode_yaml("skip"), "skip"),
    (_mode_yaml("aprove"), "approve"),                         # typo → the safe side
    (_mode_yaml("Approve"), "approve"),                        # case is not folded
    (_mode_yaml("yes"), "approve"),                            # yaml bool → "True"
    ("tool_phases:\n  dev-loop:\n    plan_approval:\n", "approve"),  # null value
    ("tool_providers:\n  default: [claude]\n", "auto"),       # block missing
    ("tool_phases:\n  review-loop:\n    verification: skip\n", "auto"),  # no dev-loop entry
    ("tool_phases:\n  dev-loop:\n    other_key: x\n", "auto"),            # key missing
    ("{}\n", "auto"),
    ("", "auto"),                                              # empty file = nothing configured
    (None, "auto"),                                            # file missing
    ("tool_phases: [dev-loop]\n", "auto"),                     # section not a mapping: ignored
    ("tool_phases:\n  dev-loop: approve\n", "approve"),        # entry not a mapping
    ("tool_phases:\n  dev-loop:\n    plan_approval: [approve\n", "approve"),  # unparseable
    ("- just\n- a list\n", "approve"),                         # top level not a mapping
], ids=[
    "approve", "auto", "skip", "typo", "capitalised", "yaml_bool", "null",
    "block_missing", "tool_missing", "key_missing", "empty_mapping", "empty_file",
    "file_missing", "section_not_mapping", "entry_not_mapping", "unparseable",
    "top_level_list",
])
def test_mode_reader(monkeypatch, tmp_path, policy_yaml, expected):
    _install_engine(tmp_path, monkeypatch, policy_yaml)
    assert DevLoopTool()._get_plan_approval_mode() == expected


def test_mode_reader_engine_failure_means_approve(monkeypatch, tmp_path):
    monkeypatch.setattr(policy_module, "get_engine", _boom)
    assert DevLoopTool()._get_plan_approval_mode() == "approve"


def test_mode_reader_follows_a_policy_edit(monkeypatch, tmp_path):
    """The reader goes through the engine's mtime reload, not a one-off parse."""
    engine = _install_engine(tmp_path, monkeypatch, _mode_yaml("auto"))
    assert DevLoopTool()._get_plan_approval_mode() == "auto"
    path = engine.config_path
    path.write_text(_mode_yaml("approve"), encoding="utf-8")
    later = path.stat().st_mtime + 5   # a same-second rewrite would keep the mtime
    os.utime(path, (later, later))
    assert DevLoopTool()._get_plan_approval_mode() == "approve"


def test_unreadable_policy_halts_unless_approved(monkeypatch, tmp_path, telegram, cwd):
    """End to end: a policy.yaml that exists but cannot be parsed asks for approval."""
    _patch(monkeypatch, tmp_path)
    _install_engine(tmp_path, monkeypatch, "tool_phases: {dev-loop: [\n")
    telegram.answers = ["denied"]
    provider = _full_run()

    result = DevLoopTool().run(_TASK, provider, cwd=cwd)

    assert len(telegram.sent) == 1
    assert provider.count(_EXEC_MARK) == 0
    assert result.error_code == "approval_denied"


# ── (e) the plan cache cannot carry a run past the approval ──────────────────

def test_a_rejected_plan_is_not_cached_and_the_next_run_asks_again(
    monkeypatch, tmp_path, telegram, cwd,
):
    _patch(monkeypatch, tmp_path)
    _install_engine(tmp_path, monkeypatch, _mode_yaml("approve"))
    telegram.answers = ["denied", "denied"]

    first, second = _full_run(), _full_run()
    r1 = DevLoopTool().run(_TASK, first, cwd=cwd)
    assert r1.error_code == "approval_denied"
    assert not _state_path(cwd).exists(), "a rejected plan must not stay a cache entry"

    r2 = DevLoopTool().run(_TASK, second, cwd=cwd)

    assert len(telegram.sent) == 2, "the second run must ask again"
    assert second.count(_RESEARCH_AND_PLAN_MARK) == 1, "…and plan again, not reuse the cache"
    assert first.count(_EXEC_MARK) == 0 and second.count(_EXEC_MARK) == 0
    assert r2.error_code == "approval_denied"


def test_an_interrupted_approval_does_not_leave_an_executable_plan(
    monkeypatch, tmp_path, telegram, cwd,
):
    """Ctrl+C (or a crash) DURING the wait: the run never returns, so no refusal
    branch runs — but the plan checkpoint was already written. The next attempt
    must not take the cache branch straight into execution."""
    _patch(monkeypatch, tmp_path)
    _install_engine(tmp_path, monkeypatch, _mode_yaml("approve"))
    telegram.raises = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        DevLoopTool().run(_TASK, _full_run(), cwd=cwd)
    assert _state_path(cwd).exists(), "precondition: the plan checkpoint survived"

    telegram.raises = None
    telegram.answers = ["denied"]
    second = _full_run()
    result = DevLoopTool().run(_TASK, second, cwd=cwd)

    assert len(telegram.sent) == 2, "the cached plan must be asked for again"
    assert second.count(_RESEARCH_AND_PLAN_MARK) == 0, "precondition: the cache WAS used"
    assert second.count(_EXEC_MARK) == 0
    assert result.error_code == "approval_denied"


def test_a_plan_cached_without_approval_is_asked_for_and_then_runs(
    monkeypatch, tmp_path, telegram, cwd,
):
    """A plan cached while the mode was still `auto` (or by the old, dead approval
    code) carries no approval mark. Switching to `approve` must cover it."""
    _patch(monkeypatch, tmp_path)
    _install_engine(tmp_path, monkeypatch, _mode_yaml("approve"))
    _write_checkpoint(
        cwd, _task_hash(_TASK), cache_phase="research_and_plan_done",
        research_and_plan=_PLAN, next_iteration=1, elapsed_budget_sec=0.0,
        previous_quality_findings=[], previous_resolution_output="", deferred_p3={},
        seen_quality_signatures=set(), seen_review_signatures=set(),
        tokens=TokenCounter(),
    )
    telegram.answers = ["approved"]
    provider = _Scripted(["Fixed auth.py.", _CLEAN_QUALITY, _RESOLVED])

    result = DevLoopTool().run(_TASK, provider, cwd=cwd)

    assert len(telegram.sent) == 1
    assert "PLAN-MARKER-7f3" in telegram.sent[0]
    assert provider.count(_RESEARCH_AND_PLAN_MARK) == 0
    assert result.success is True


def test_an_approved_and_parked_run_resumes_without_a_second_question(
    monkeypatch, tmp_path, telegram, cwd,
):
    """Regression guard for step 3: the fix must not turn every capacity resume
    into a second Telegram question for a plan that was already approved."""
    _patch(monkeypatch, tmp_path)
    _install_engine(tmp_path, monkeypatch, _mode_yaml("approve"))
    telegram.answers = ["approved"]
    parked = _Scripted([_PLAN], fail_at=2, fail_error="rate_limit")  # quota dies in exec

    r1 = DevLoopTool().run(_TASK, parked, cwd=cwd)
    assert r1.error_code == "capacity_exhausted"
    assert _resume_checkpoint(cwd, _task_hash(_TASK)) is not None, "precondition: parked"
    state = json.loads(_state_path(cwd).read_text(encoding="utf-8"))
    assert state["plan_approved"] is True

    resumed = _Scripted(["Fixed auth.py.", _CLEAN_QUALITY, _RESOLVED])
    r2 = DevLoopTool().run(_TASK, resumed, cwd=cwd)

    assert len(telegram.sent) == 1, "an approved plan must not be asked for twice"
    assert resumed.count(_RESEARCH_AND_PLAN_MARK) == 0
    assert r2.success is True


def test_an_approval_is_recorded_only_for_the_plan_that_was_shown(cwd):
    """_record_plan_approval binds to the plan text: a cache holding a different
    plan (an older one, a rewritten file) is not marked approved."""
    _write_checkpoint(
        cwd, _task_hash(_TASK), cache_phase="research_and_plan_done",
        research_and_plan="some other plan", next_iteration=1, elapsed_budget_sec=0.0,
        previous_quality_findings=[], previous_resolution_output="", deferred_p3={},
        seen_quality_signatures=set(), seen_review_signatures=set(),
        tokens=TokenCounter(), park_reason=_PARK_CAPACITY,
    )

    dev_loop_module._record_plan_approval(cwd, _task_hash(_TASK), _PLAN)

    state = json.loads(_state_path(cwd).read_text(encoding="utf-8"))
    assert state["plan_approved"] is False


# ── concurrency: one question on screen at a time ────────────────────────────

def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.01)


def test_concurrent_plan_questions_are_asked_one_after_the_other(monkeypatch, tmp_path, telegram):
    """`#parallel` runs CWD groups in threads, so two dev-loops can ask at once. The
    engine has ONE pending slot and /approve answers whatever is in it: unserialised,
    B's request replaced A's, the user's answer to message A released plan B, and A
    ran into its timeout. Here B must wait until A's question is settled."""
    _install_engine(tmp_path, monkeypatch, _mode_yaml("approve"))
    monkeypatch.setattr(dev_loop_module, "_PLAN_APPROVAL_TIMEOUT_SEC", 5)
    telegram.answers = [None, "denied"]   # message 1: not answered yet; message 2: /deny
    results = {}
    b_started = threading.Event()

    def ask(name, started=None):
        if started is not None:
            started.set()
        results[name] = dev_loop_module._ask_plan_approval(f"Task {name}", f"plan {name}", None)

    a = threading.Thread(target=ask, args=("A",), daemon=True)
    a.start()
    _wait_until(lambda: len(telegram.sent) == 1)
    b = threading.Thread(target=ask, args=("B", b_started), daemon=True)
    b.start()
    assert b_started.wait(5)
    time.sleep(0.3)   # ample time for B to overwrite A's slot — it must not get to
    assert len(telegram.sent) == 1, "B asked while A's question was still open"

    policy_module.get_engine()._respond("approved")    # the user answers message 1
    a.join(5)
    b.join(5)

    assert results["A"] is None, "the answer to A's message must release plan A"
    assert results["B"][0] == "approval_denied"
    assert "plan A" in telegram.sent[0] and "plan B" in telegram.sent[1]


# ── taxonomy ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code", [
    "approval_denied", "approval_skipped", "approval_timeout", "approval_unavailable",
])
def test_every_plan_approval_code_is_a_mapped_approval_category(code):
    """dev_loop passes these through a table, which the source scanner in
    tests/test_taxonomy.py cannot see — so they are pinned here."""
    assert taxonomy._ERROR_CODE_MAP[code] == taxonomy.CAT_APPROVAL
    assert taxonomy.classify({"exit_status": "error", "error_code": code}) == taxonomy.CAT_APPROVAL
