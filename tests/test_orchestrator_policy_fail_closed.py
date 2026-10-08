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
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import limits
import notifier
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


# ── A broken policy.yaml, through a REAL engine (2026-10-08) ─────────────────
#
# Until 2026-10-08 such a file reached run_once() as `('auto', [])` (broken at
# process start) or as the stale last-good rules (broken while running), and the
# task ran. Now check_task raises PolicyUnreadableError and the hold above applies.
# Nothing in the policy path is faked here: a real PolicyEngine on a real file in
# tmp_path, installed as the singleton run_once() asks for.

_GOOD_AUTO = "auto:\n  - \"Deploy release\"\n"          # matches _TASK, decides AUTO
_UNPARSEABLE = "approve:\n  - pattern: [unclosed\n"
_UNPARSEABLE_OTHER = "deny: {oops\n"
_BROKEN_SECTION = _GOOD_AUTO + "tool_contracts: {dev-loop: {stop_conditions: 1}}\n"


def _install_real_engine(monkeypatch, tmp_path, text: str) -> policy_module.PolicyEngine:
    ai = tmp_path / "vault" / "99_System" / "AI"
    ai.mkdir(parents=True, exist_ok=True)
    (ai / "policy.yaml").write_text(text, encoding="utf-8")
    engine = policy_module.PolicyEngine(vault_path=tmp_path / "vault")
    monkeypatch.setattr(policy_module, "_engine", engine)
    return engine


def _rewrite(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    later = path.stat().st_mtime + 5
    os.utime(path, (later, later))


def test_broken_policy_yaml_holds_the_task_and_no_provider_runs(world, monkeypatch, tmp_path, caplog):
    _install_real_engine(monkeypatch, tmp_path, _UNPARSEABLE)

    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        assert orchestrator.run_once() is False

    _held(world, caplog)                                  # select_provider never called
    _requeued_in_ten_minutes(world)
    (record,) = replay.read_runs()
    assert record["exit_status"] == replay.EXIT_RETRY
    assert any("policy.yaml unreadable (" in line for line in world.log_lines), world.log_lines


def test_broken_policy_yaml_holds_on_every_cycle(world, monkeypatch, tmp_path, caplog):
    """The second cycle reads no new mtime — it must hold all the same."""
    _install_real_engine(monkeypatch, tmp_path, _UNPARSEABLE)

    for _ in range(3):
        assert orchestrator.run_once() is False

    world.select_provider.assert_not_called()
    world.mark_done.assert_not_called()
    assert world.mark_retry.call_count == 3
    assert [r["error_code"] for r in replay.read_runs()] == ["approval_unavailable"] * 3


def test_policy_yaml_broken_while_running_holds_instead_of_using_the_old_rules(
    world, monkeypatch, tmp_path,
):
    engine = _install_real_engine(monkeypatch, tmp_path, _GOOD_AUTO)
    orchestrator.run_once()
    assert world.select_provider.call_count == 1     # good file: execution is reached
    requeues_before = world.mark_retry.call_count     # (the stub has no provider → parked)

    _rewrite(engine.config_path, _UNPARSEABLE)
    assert orchestrator.run_once() is False

    assert world.select_provider.call_count == 1, "the broken file must not reach execution"
    assert world.mark_retry.call_count == requeues_before + 1
    assert any("policy.yaml unreadable (" in line for line in world.log_lines)


def test_control_a_readable_policy_yaml_reaches_execution(world, monkeypatch, tmp_path):
    """Without this the tests above could pass on a set-up that never gets that far."""
    _install_real_engine(monkeypatch, tmp_path, _GOOD_AUTO)

    orchestrator.run_once()

    world.select_provider.assert_called_once()
    assert not any("Policy-Prüfung gestört" in line for line in world.log_lines)


# ── Alert throttle on the hold (2026-10-08) ──────────────────────────────────
#
# Only the Telegram alert is throttled — one per cause and
# POLICY_HOLD_NOTIFY_WINDOW_SEC. Log, append_log and the requeue run on every hold.
# The real notify_error runs; the fake is the outermost boundary, notifier._send.

class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(orchestrator.time, "monotonic", fake)
    return fake


@pytest.fixture
def telegram(world, monkeypatch):
    sent: list[str] = []

    def _send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(orchestrator, "notify_error", notifier.notify_error)
    monkeypatch.setattr(notifier, "NOTIFY_ON_ERROR", True)
    monkeypatch.setattr(notifier, "_send", _send)
    return sent


def _alerts(sent):
    return [m for m in sent if "Policy-Prüfung gestört" in m]


def _all_clears(sent):
    return [m for m in sent if "wieder in Ordnung" in m]


_WINDOW = orchestrator.POLICY_HOLD_NOTIFY_WINDOW_SEC


def test_same_cause_alerts_once_while_every_hold_still_logs_and_requeues(
    world, monkeypatch, tmp_path, clock, telegram,
):
    _install_real_engine(monkeypatch, tmp_path, _UNPARSEABLE)

    for _ in range(4):                           # four cycles, 10 minutes apart
        assert orchestrator.run_once() is False
        clock.now += 600

    assert len(_alerts(telegram)) == 1, telegram
    assert "policy.yaml unreadable" in _alerts(telegram)[0]
    assert "frühestens in 6 h" in _alerts(telegram)[0]
    # ...and nothing else is throttled:
    assert world.mark_retry.call_count == 4
    assert sum("Policy-Prüfung gestört" in line for line in world.log_lines) == 4
    world.select_provider.assert_not_called()


def test_the_log_warning_is_not_throttled(world, monkeypatch, tmp_path, caplog, telegram):
    _install_real_engine(monkeypatch, tmp_path, _UNPARSEABLE)

    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        for _ in range(3):
            orchestrator.run_once()

    assert len(_alerts(telegram)) == 1
    assert sum("Policy-Prüfung gestört" in r.getMessage() for r in caplog.records) == 3


def test_the_same_cause_alerts_again_once_the_window_is_over(
    world, monkeypatch, tmp_path, clock, telegram,
):
    _install_real_engine(monkeypatch, tmp_path, _UNPARSEABLE)

    orchestrator.run_once()
    clock.now += _WINDOW - 1
    orchestrator.run_once()
    assert len(_alerts(telegram)) == 1, "inside the window: throttled"
    clock.now += 1
    orchestrator.run_once()

    assert len(_alerts(telegram)) == 2


def test_a_different_cause_alerts_at_once(world, monkeypatch, tmp_path, clock, telegram):
    engine = _install_real_engine(monkeypatch, tmp_path, _UNPARSEABLE)
    orchestrator.run_once()

    _rewrite(engine.config_path, _UNPARSEABLE_OTHER)
    orchestrator.run_once()

    assert len(_alerts(telegram)) == 2


def test_different_tasks_held_for_the_same_cause_alert_once(
    world, monkeypatch, tmp_path, clock, telegram,
):
    _install_real_engine(monkeypatch, tmp_path, _UNPARSEABLE)
    other = "Release notes schreiben und git push"
    for text, line_no in ((_TASK, 7), (other, 8)):
        item = SimpleNamespace(task_text=text, line_no=line_no)
        monkeypatch.setattr(orchestrator, "read_queue_items", lambda item=item: [item])
        monkeypatch.setattr(orchestrator, "read_queue", lambda text=text: [text])
        orchestrator.run_once()

    assert [c.args[0] for c in world.mark_retry.call_args_list] == [_TASK, other]
    assert len(_alerts(telegram)) == 1, telegram


def test_a_failing_section_parser_is_one_cause_from_the_first_cycle_on(
    world, monkeypatch, tmp_path, clock, telegram,
):
    """Prüffrage 1: the reload that hits the parser used to surface its raw TypeError,
    every later one the stored error — two texts, two alerts for one fault."""
    engine = _install_real_engine(monkeypatch, tmp_path, _GOOD_AUTO)
    _rewrite(engine.config_path, _BROKEN_SECTION)

    for _ in range(3):
        assert orchestrator.run_once() is False

    assert len(_alerts(telegram)) == 1, telegram
    held = [line for line in world.log_lines if "Policy-Prüfung gestört" in line]
    assert len(held) == 3 and len(set(held)) == 1, held


def test_recovery_sends_one_all_clear_and_rearms_the_alert(
    world, monkeypatch, tmp_path, clock, telegram,
):
    engine = _install_real_engine(monkeypatch, tmp_path, _UNPARSEABLE)
    orchestrator.run_once()
    orchestrator.run_once()
    assert (len(_alerts(telegram)), len(_all_clears(telegram))) == (1, 0)

    _rewrite(engine.config_path, _GOOD_AUTO)
    orchestrator.run_once()
    orchestrator.run_once()
    assert len(_all_clears(telegram)) == 1, "exactly one all-clear"
    assert world.select_provider.call_count == 2, "the repaired policy lets tasks run"

    # Broken again, same text, well inside the window: alerted at once, because the
    # all-clear has forgotten the cause.
    _rewrite(engine.config_path, _UNPARSEABLE)
    orchestrator.run_once()
    assert len(_alerts(telegram)) == 2


def test_no_all_clear_without_an_alert_before(world, monkeypatch, tmp_path, clock, telegram):
    _install_real_engine(monkeypatch, tmp_path, _GOOD_AUTO)

    orchestrator.run_once()

    assert telegram == []


def test_a_raising_notify_error_still_requeues_and_is_retried_on_the_next_hold(
    world, monkeypatch, tmp_path, clock, telegram,
):
    _install_real_engine(monkeypatch, tmp_path, _UNPARSEABLE)
    attempts: list[str] = []

    def _send_raises(text):
        attempts.append(text)
        raise OSError("telegram socket gone")

    monkeypatch.setattr(notifier, "_send", _send_raises)

    orchestrator.run_once()
    orchestrator.run_once()

    assert world.mark_retry.call_count == 2, "the requeue never depends on the alert"
    assert len(_alerts(attempts)) == 2, "an alert that raised is not recorded as sent"


def test_a_broken_throttle_state_alerts_anyway_and_requeues(
    world, monkeypatch, tmp_path, clock, telegram,
):
    """Prüffrage 4: the bookkeeping itself raising must neither swallow the alert nor
    stop the requeue."""
    class _Broken(dict):
        def get(self, *_a, **_kw):
            raise RuntimeError("state gone")

        def __setitem__(self, *_a):
            raise RuntimeError("state gone")

    monkeypatch.setattr(orchestrator, "_POLICY_HOLD_NOTICES", _Broken())
    _install_real_engine(monkeypatch, tmp_path, _UNPARSEABLE)

    orchestrator.run_once()

    world.mark_retry.assert_called_once()
    assert len(_alerts(telegram)) == 1


def test_an_approval_path_fault_gets_no_all_clear_and_stays_throttled(
    world, monkeypatch, clock, telegram,
):
    """A passing check_task says nothing about the approval path behind it. Treating it
    as a recovery would mean an all-clear plus a fresh alert on every cycle of a
    lasting approval fault — the flood the throttle exists to stop."""
    engine = _Engine(approval_raises=OSError("approval path down"))
    monkeypatch.setattr(policy_module, "get_engine", lambda: engine)

    for _ in range(3):
        orchestrator.run_once()
        clock.now += 600

    assert engine.approval_calls == 3
    assert len(_alerts(telegram)) == 1, telegram
    assert _all_clears(telegram) == []


def test_a_clock_running_backwards_alerts_rather_than_staying_silent(clock, telegram):
    """Prüffrage 2: suppression needs 0 <= elapsed < window, nothing else."""
    exc = RuntimeError("policy fault")
    orchestrator._notify_policy_hold(_TASK, exc, "Policy-Prüfung gestört — x", classifying=True)
    clock.now -= 10
    orchestrator._notify_policy_hold(_TASK, exc, "Policy-Prüfung gestört — x", classifying=True)

    assert len(_alerts(telegram)) == 2


def test_the_throttle_keeps_at_most_twenty_causes(clock, telegram):
    for n in range(25):
        clock.now += 1
        orchestrator._notify_policy_hold(
            _TASK, RuntimeError(f"fault {n}"), "Policy-Prüfung gestört — x", classifying=True,
        )

    notices = orchestrator._POLICY_HOLD_NOTICES
    assert len(notices) == orchestrator._POLICY_HOLD_MAX_CAUSES == 20
    assert "RuntimeError: fault 0" not in notices, "the oldest is dropped first"
    assert "RuntimeError: fault 24" in notices


def _boom(*_a, **_kw):
    raise RuntimeError("all-clear broken")


@pytest.mark.parametrize("site", ["send_message", "_notify_policy_recovered"])
def test_a_failing_all_clear_never_holds_the_task(world, monkeypatch, tmp_path, clock, site):
    """The all-clear runs INSIDE the policy block, whose except holds the task. Its
    first version logged through a name that does not exist at module level, so its
    own error handler raised — and every task after a recovery was held."""
    _install_real_engine(monkeypatch, tmp_path, _GOOD_AUTO)
    orchestrator._POLICY_HOLD_NOTICES["PolicyUnreadableError: x"] = (clock.now, True)
    monkeypatch.setattr(orchestrator, site, _boom)

    orchestrator.run_once()

    world.select_provider.assert_called_once()
    assert not any("Policy-Prüfung gestört" in line for line in world.log_lines)
    if site == "send_message":
        assert "PolicyUnreadableError: x" in orchestrator._POLICY_HOLD_NOTICES, (
            "an all-clear that raised is tried again by the next task that passes"
        )
