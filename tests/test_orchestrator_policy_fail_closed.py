"""run_once(): a fault in the policy check holds the task — it never runs (2026-10-06).

Until then the whole policy block ended in `except ImportError: pass` and
`except Exception: _log.warning("policy check failed")`, and both fell through
into execution: the task ran without the approval AND without the DENY check.
That covered every call in the block — get_engine, check_task, request_approval,
the notification, and the re-stamp in the refusal branches (the user answers
/deny, the re-stamp raises, the task runs anyway).

Now any such fault holds the task like the `timeout` branch: it stays open, is
retried in 10 minutes, and the execution part (select_provider onwards) is never
reached in that pass. Set-up after the run_once tests in
tests/test_orchestrator_tool_tasks.py; select_provider is a plain Mock that
returns None, so a regression reaches it and shows up as a call, rather than as an
AssertionError some outer handler might swallow.
"""

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import limits
import orchestrator
import policy as policy_module
import replay

_TASK = "Deploy release and git push origin main"


class _Engine:
    """Fake engine. Each method can be made to raise; the answer is configurable."""

    def __init__(self, *, verdict=policy_module.TIER_APPROVE, answer="approved",
                 check_raises=None, approval_raises=None):
        self.verdict = verdict
        self.answer = answer
        self.check_raises = check_raises
        self.approval_raises = approval_raises
        self.approval_calls = 0

    def check_task(self, _text, profile_rules=None):
        if self.check_raises is not None:
            raise self.check_raises
        return self.verdict, ["git push to remote"] if self.verdict != policy_module.TIER_AUTO else []

    def is_preapproved(self, _category):
        return False

    def request_approval(self, _text, _reasons, _timeout_sec=0, **_context):
        self.approval_calls += 1
        if self.approval_raises is not None:
            raise self.approval_raises
        return self.answer


@pytest.fixture
def world(monkeypatch):
    """One open single-shot task; everything around the policy block stubbed."""
    queue_item = SimpleNamespace(task_text=_TASK, line_no=7)
    w = SimpleNamespace(
        mark_retry=Mock(return_value=True),
        mark_done=Mock(return_value=True),
        select_provider=Mock(return_value=None),
        notify_error=Mock(),
        log_lines=[],
    )
    monkeypatch.setattr(orchestrator, "read_queue_items", lambda: [queue_item])
    monkeypatch.setattr(orchestrator, "read_queue", lambda: [_TASK])
    monkeypatch.setattr(orchestrator, "has_cwd_tag", lambda _task: False)
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_shutdown_tag", lambda _task: False)
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: limits.AllLimits())
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", lambda _limits: 1)
    monkeypatch.setattr(orchestrator.memory_module, "archive_old_memories", lambda: 0)
    monkeypatch.setattr(orchestrator.memory_module, "get_context_for_task", lambda *_a, **_kw: "")
    monkeypatch.setattr(orchestrator, "mark_retry", w.mark_retry)
    monkeypatch.setattr(orchestrator, "mark_done", w.mark_done)
    monkeypatch.setattr(orchestrator, "select_provider", w.select_provider)
    monkeypatch.setattr(orchestrator, "append_log", lambda msg, *_a, **_kw: w.log_lines.append(msg))
    monkeypatch.setattr(orchestrator, "notify_error", w.notify_error)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *_a, **_kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *_a, **_kw: None)
    return w


def _held(world, caplog):
    """The common verdict for every fault: not run, still open, the fault on record."""
    world.select_provider.assert_not_called()
    world.mark_done.assert_not_called()          # no ✅/❌ — the line stays open
    assert any("Policy-Prüfung gestört" in line for line in world.log_lines), world.log_lines
    assert any("Policy-Prüfung gestört" in r.getMessage() for r in caplog.records)
    (record,) = replay.read_runs()
    assert record["error_code"] == "approval_unavailable"


def _requeued_in_ten_minutes(world):
    world.mark_retry.assert_called_once()
    args, kwargs = world.mark_retry.call_args
    assert args[0] == _TASK
    retry_at = datetime.strptime(args[1], "%Y-%m-%d %H:%M")
    assert timedelta(minutes=8) < retry_at - datetime.now() <= timedelta(minutes=10)
    assert kwargs["line_no"] == 7


def test_check_task_raising_holds_the_task(world, monkeypatch, caplog):
    engine = _Engine(check_raises=RuntimeError("regex engine exploded"))
    monkeypatch.setattr(policy_module, "get_engine", lambda: engine)

    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        assert orchestrator.run_once() is False

    _held(world, caplog)
    _requeued_in_ten_minutes(world)
    assert engine.approval_calls == 0
    (record,) = replay.read_runs()
    assert record["exit_status"] == replay.EXIT_RETRY
    world.notify_error.assert_called_once()
    assert "regex engine exploded" in world.notify_error.call_args.args[2]


def test_request_approval_raising_holds_the_task(world, monkeypatch, caplog):
    engine = _Engine(approval_raises=OSError("telegram socket gone"))
    monkeypatch.setattr(policy_module, "get_engine", lambda: engine)

    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        assert orchestrator.run_once() is False

    _held(world, caplog)
    _requeued_in_ten_minutes(world)
    assert engine.approval_calls == 1


def test_get_engine_raising_holds_the_task(world, monkeypatch, caplog):
    def broken():
        raise ImportError("policy engine not loadable")

    monkeypatch.setattr(policy_module, "get_engine", broken)

    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        assert orchestrator.run_once() is False

    _held(world, caplog)
    _requeued_in_ten_minutes(world)


class _PolicyBoomError(Exception):
    """A project-specific exception class no narrowed `except` tuple would list."""


def _raise(exc_type):
    def raiser(*_a, **_kw):
        raise exc_type("injected policy fault")
    return raiser


@pytest.mark.parametrize("site", ["get_engine", "check_task", "request_approval"])
@pytest.mark.parametrize("exc_type", [
    AttributeError,   # get_engine() handing back None, a fake missing a method
    TypeError,        # an engine with a different signature (the **kwargs case)
    KeyError,
    ValueError,
    _PolicyBoomError,
])
def test_any_exception_class_at_any_fault_site_holds_the_task(
    world, monkeypatch, caplog, site, exc_type,
):
    """The three tests above use RuntimeError, OSError and ImportError. A hold
    narrowed to exactly those (`except (ImportError, OSError, RuntimeError)`) passed
    all of them while a task with any other fault ran unapproved and unchecked
    again. The hold has to be `except Exception`, and this pins it."""
    if site == "get_engine":
        monkeypatch.setattr(policy_module, "get_engine", _raise(exc_type))
    else:
        engine = _Engine()
        monkeypatch.setattr(engine, site, _raise(exc_type))
        monkeypatch.setattr(policy_module, "get_engine", lambda: engine)

    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        assert orchestrator.run_once() is False

    _held(world, caplog)
    _requeued_in_ten_minutes(world)
    assert exc_type.__name__ in world.notify_error.call_args.args[2]


@pytest.mark.parametrize("answer", ["", None, "bogus", "Approved", "approved "],
                         ids=["empty", "none", "bogus", "capitalised", "trailing_space"])
def test_an_answer_that_is_not_an_approval_holds_the_task(world, monkeypatch, caplog, answer):
    """Only the exact word "approved" runs. "" is reachable, not hypothetical: the
    engine's one pending slot can be reset to "" by a second request between
    `_respond()` setting the event and the waiter reading the answer — and before
    2026-10-06 such an answer fell through into execution as if approved."""
    engine = _Engine(answer=answer)
    monkeypatch.setattr(policy_module, "get_engine", lambda: engine)

    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        assert orchestrator.run_once() is False

    assert engine.approval_calls == 1
    _held(world, caplog)
    _requeued_in_ten_minutes(world)


def test_denied_with_a_failing_restamp_still_never_runs(world, monkeypatch, caplog):
    """The user answered /deny; the re-stamp raises (queue file locked). The old
    handler logged a warning and ran the task anyway."""
    engine = _Engine(answer="denied")
    monkeypatch.setattr(policy_module, "get_engine", lambda: engine)
    world.mark_retry.side_effect = OSError("queue file locked")

    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        assert orchestrator.run_once() is False

    _held(world, caplog)
    assert world.mark_retry.call_count == 2, "denied re-stamp, then the hold's own requeue"
    (record,) = replay.read_runs()
    assert record["exit_status"] == replay.EXIT_ERROR, "requeue failed → error, line still open"


def test_a_failing_notification_does_not_keep_the_requeue_from_happening(
    world, monkeypatch, caplog,
):
    engine = _Engine(check_raises=RuntimeError("boom"))
    monkeypatch.setattr(policy_module, "get_engine", lambda: engine)
    world.notify_error.side_effect = OSError("telegram down too")

    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        assert orchestrator.run_once() is False

    _held(world, caplog)
    _requeued_in_ten_minutes(world)


# ── Controls: the undisturbed paths behave as before ─────────────────────────

def test_control_approved_task_reaches_execution(world, monkeypatch):
    engine = _Engine(answer="approved")
    monkeypatch.setattr(policy_module, "get_engine", lambda: engine)

    orchestrator.run_once()

    assert engine.approval_calls == 1
    world.select_provider.assert_called_once()
    assert not any("Policy-Prüfung gestört" in line for line in world.log_lines)


def test_control_auto_task_reaches_execution_without_asking(world, monkeypatch):
    engine = _Engine(verdict=policy_module.TIER_AUTO)
    monkeypatch.setattr(policy_module, "get_engine", lambda: engine)

    orchestrator.run_once()

    assert engine.approval_calls == 0
    world.select_provider.assert_called_once()
