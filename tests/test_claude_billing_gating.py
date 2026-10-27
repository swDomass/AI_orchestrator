"""Gating + live-estimate behaviour of Claude's credit mode (Scheibe 2a).

While ``claude_billing.api_active()`` is True, Claude bills the API credits and
not the subscription, so:

* an exhausted subscription window must not bar Claude (dispatcher, cached
  availability, watch loop);
* credit-billed runs must not feed ``report_estimated_usage`` (that would show
  phantom subscription usage).

``api_active`` is steered via monkeypatch in every test, so a real
ORCH_CLAUDE_API_KEY in the developer's ``.env`` cannot leak in.
"""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_dispatcher_routing import _make_limits
from test_orchestrator_tool_tasks import _stub_single_shot_env

import claude_billing
import dispatcher
import doctor
import heartbeat
import limits
import orchestrator
import parallel_runner as parallel_runner_module
import queue_manager
from dispatcher import _limits_ok, select_provider
from limits import AllLimits, ProviderLimits, WindowData
from parallel_runner import SubTask


def _api(monkeypatch, active: bool) -> None:
    monkeypatch.setattr(claude_billing, "api_active", lambda *a, **kw: active)


# --------------------------------------------------------------- dispatcher


def test_limits_ok_claude_exhausted_but_api_active_is_true(monkeypatch):
    _api(monkeypatch, True)
    assert _limits_ok("claude", _make_limits(claude_avail=False)) is True


def test_limits_ok_claude_exhausted_and_api_inactive_is_false(monkeypatch):
    _api(monkeypatch, False)
    assert _limits_ok("claude", _make_limits(claude_avail=False)) is False


def test_limits_ok_codex_unaffected_by_api_mode(monkeypatch):
    _api(monkeypatch, True)
    assert _limits_ok("codex", _make_limits(codex_avail=False)) is False
    assert _limits_ok("codex", _make_limits(codex_avail=True)) is True


def test_select_provider_picks_claude_despite_exhausted_subscription(monkeypatch):
    _api(monkeypatch, True)
    provider = select_provider("Fix a bug", _make_limits(claude_avail=False))
    assert provider is not None
    assert provider.name == "claude"


def test_select_provider_falls_back_to_codex_when_api_blocked(monkeypatch):
    _api(monkeypatch, False)
    provider = select_provider("Fix a bug", _make_limits(claude_avail=False))
    assert provider is not None
    assert provider.name == "codex"


# --------------------------------------------------------------- limits cache


def _exhausted_claude_cache() -> AllLimits:
    return AllLimits(
        claude=ProviderLimits(
            available=False, remaining_pct=3.0, resets_in_sec=3600,
            windows={
                "five_hour": WindowData(remaining_pct=3.0, resets_in_sec=1200),
                "seven_day": WindowData(remaining_pct=60.0, resets_in_sec=3600),
            },
        ),
        codex=ProviderLimits(available=False, remaining_pct=2.0),
    )


@pytest.fixture
def exhausted_cache(monkeypatch):
    monkeypatch.setattr(limits, "_limits_cache", (_exhausted_claude_cache(), time.monotonic()))
    monkeypatch.setattr(limits, "_live_estimated_usage", {})
    monkeypatch.setattr(limits, "_429_snapshots", {})
    monkeypatch.setattr(limits, "_429_estimated_usage", {})
    return monkeypatch


def test_cached_availability_true_in_api_mode_despite_exhausted_cache(exhausted_cache):
    _api(exhausted_cache, True)
    assert limits.is_cached_provider_available("claude") is True


def test_cached_availability_false_without_api_mode(exhausted_cache):
    _api(exhausted_cache, False)
    assert limits.is_cached_provider_available("claude") is False


def test_cached_availability_codex_unaffected_by_api_mode(exhausted_cache):
    _api(exhausted_cache, True)
    assert limits.is_cached_provider_available("codex") is False


def test_cached_pct_stays_subscription_truth_in_api_mode(exhausted_cache):
    _api(exhausted_cache, False)
    before = limits.get_cached_provider_pct("claude")
    _api(exhausted_cache, True)
    assert limits.get_cached_provider_pct("claude") == before == 3.0


# --------------------------------------------------------------- single run


@pytest.mark.parametrize(
    ("billing", "expected_calls"),
    [("api", 0), ("abo", 1), ("", 1)],
)
def test_run_once_single_shot_reports_usage_only_for_non_api(
    monkeypatch, billing, expected_calls
):
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())
    _stub_single_shot_env(monkeypatch, raw_line="- [ ] Plain claude task", provider=p1)
    reported = Mock()
    monkeypatch.setattr(orchestrator, "report_estimated_usage", reported)
    monkeypatch.setattr(
        orchestrator, "_run_with_retry",
        lambda *a, **kw: (
            orchestrator.RunResult(success=True, output="ok", billing=billing), False,
        ),
    )

    orchestrator.run_once()

    assert reported.call_count == expected_calls


# --------------------------------------------------------------- sub-tasks


@pytest.mark.parametrize(
    ("billing", "expected"),
    [("api", []), ("abo", [("claude", 7.5)]), ("", [("claude", 7.5)])],
)
def test_parallel_subtask_reports_usage_only_for_non_api(monkeypatch, billing, expected):
    provider = SimpleNamespace(name="claude")
    subtask = SubTask(
        text="Do the thing", provider_forced=None, cwd=None, tool_name=None, timeout=30,
    )
    reported = []

    monkeypatch.setattr(dispatcher, "select_provider", lambda *_a, **_kw: provider)
    monkeypatch.setattr(queue_manager, "strip_metadata_tags", lambda text: text)
    monkeypatch.setattr(orchestrator, "_build_prompt", lambda *_a, **_kw: "prompt")
    monkeypatch.setattr(
        orchestrator, "_run_with_retry",
        lambda *_a, **_kw: (
            SimpleNamespace(
                success=True, output="ok", error="", input_tokens=1, output_tokens=1,
                billing=billing,
            ),
            0.0,
        ),
    )
    monkeypatch.setattr(parallel_runner_module, "estimate_task_usage_pct", lambda *a, **kw: 7.5)
    monkeypatch.setattr(
        parallel_runner_module, "report_estimated_usage",
        lambda provider_name, estimated_pct: reported.append((provider_name, estimated_pct)),
    )

    result = parallel_runner_module._run_single_subtask(
        subtask, idx=0, limits=AllLimits(), memory_context="", pause_event=None,
    )

    assert result.success is True
    assert reported == expected


# --------------------------------------------------------------- tool path


def _run_tool_task(monkeypatch, tmp_path, *, provider_name, api_at_start, api_at_end=None):
    """Drive the real _execute_tool_task; return the report_estimated_usage mock.

    ``api_at_end`` flips api_active() *during* tool.run() to model a fallback to
    the subscription mid-tool (only the start state counts).
    """
    state = {"active": api_at_start}
    monkeypatch.setattr(claude_billing, "api_active", lambda *a, **kw: state["active"])

    def _tool_run(*a, **kw):
        if api_at_end is not None:
            state["active"] = api_at_end
        return SimpleNamespace(
            success=True, output="out", iterations=1, error="", error_code="",
            retryable=False, input_tokens=1, output_tokens=1,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )

    tool = SimpleNamespace(name="dummy", description="d", read_only=True, run=_tool_run)
    reported = Mock()
    monkeypatch.setattr(orchestrator, "get_tool", lambda name: tool)
    monkeypatch.setattr(orchestrator, "load_skill", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_snapshot_dir", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_get_change_summary", lambda *a, **kw: "")
    monkeypatch.setattr(orchestrator, "report_estimated_usage", reported)
    monkeypatch.setattr(orchestrator, "estimate_task_usage_pct", lambda *a, **kw: 1.0)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_finalize_task_with_result_checked", lambda *a, **kw: True)
    monkeypatch.setattr(
        orchestrator, "_verify_task_result", lambda *a, **kw: orchestrator.VerifyOutcome(),
    )
    monkeypatch.setattr(orchestrator.memory_module, "store_result", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_task_done", lambda *a, **kw: None)

    orchestrator._execute_tool_task(
        "Task", "dummy", SimpleNamespace(name=provider_name), str(tmp_path),
    )
    return reported


def test_tool_path_no_usage_report_when_api_active_at_start(monkeypatch, tmp_path):
    reported = _run_tool_task(
        monkeypatch, tmp_path, provider_name="claude", api_at_start=True,
    )
    assert reported.call_count == 0


def test_tool_path_reports_usage_when_api_inactive(monkeypatch, tmp_path):
    reported = _run_tool_task(
        monkeypatch, tmp_path, provider_name="claude", api_at_start=False,
    )
    assert reported.call_count == 1


def test_tool_path_non_claude_provider_reports_even_if_api_active(monkeypatch, tmp_path):
    reported = _run_tool_task(
        monkeypatch, tmp_path, provider_name="codex", api_at_start=True,
    )
    assert reported.call_count == 1


def test_tool_path_start_state_decides_not_end_state(monkeypatch, tmp_path):
    """Fallback to the subscription mid-tool: still not reported (under-estimate
    is harmless, over-estimate is the forbidden error)."""
    reported = _run_tool_task(
        monkeypatch, tmp_path, provider_name="claude", api_at_start=True, api_at_end=False,
    )
    assert reported.call_count == 0


# --------------------------------------------------------------- watch loop


class _EndWatchError(Exception):
    """Raised by the second run_once: ends run_watch after one failed round."""


@pytest.mark.parametrize(
    ("api_active", "expected_sleep"),
    [(True, 30), (False, min(1234, orchestrator.SLEEP_POLL_INTERVAL * 10))],
)
def test_watch_loop_retry_pause_after_failed_round(monkeypatch, api_active, expected_sleep):
    """Exhausted subscription + failed round: credits keep the short 30 s pause,
    without credits the loop waits for the next window reset."""
    _api(monkeypatch, api_active)
    monkeypatch.setattr(doctor, "run_startup_checks", lambda: True)
    monkeypatch.setattr(heartbeat, "HeartbeatRunner", lambda: SimpleNamespace(run_due=lambda *_a: None))
    monkeypatch.setattr(heartbeat, "_log_capacity", lambda: None)
    monkeypatch.setattr(heartbeat, "start_heartbeat_thread", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "TelegramListener", lambda *_a: Mock())
    monkeypatch.setattr(orchestrator, "STARTUP_DELAY_SEC", 0)
    monkeypatch.setattr(orchestrator, "start_session", lambda: None)
    monkeypatch.setattr(orchestrator, "set_queue_idle", lambda *_a: None)
    monkeypatch.setattr(orchestrator, "_start_dashboard_autostart", lambda: None)
    monkeypatch.setattr(orchestrator, "append_log", lambda *_a, **_kw: None)
    monkeypatch.setattr(orchestrator, "read_queue", lambda: ["a task"])
    monkeypatch.setattr(
        orchestrator, "get_limits",
        lambda **_kw: SimpleNamespace(any_available=lambda: False),
    )
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", lambda _limits: 1234)
    rounds = []

    def run_once(**_kw):
        rounds.append(1)
        if len(rounds) > 1:
            raise _EndWatchError
        return False

    monkeypatch.setattr(orchestrator, "run_once", run_once)
    sleeps: list[float] = []
    monkeypatch.setattr(orchestrator, "time", SimpleNamespace(sleep=sleeps.append))

    with pytest.raises(_EndWatchError):
        orchestrator.run_watch()

    assert sum(sleeps) == expected_sleep
