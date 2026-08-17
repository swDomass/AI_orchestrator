from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import limits
import orchestrator
import policy as policy_module
from tools.base_tool import ToolResult



def test_execute_tool_task_does_not_mark_done_on_retryable_failure(monkeypatch):
    provider = SimpleNamespace(name="codex")
    tool = Mock()
    tool.name = "test-loop"
    tool.description = "Test loop"
    tool.read_only = False
    tool.run.return_value = ToolResult(
        success=False,
        error="Tests konnten nicht ausgeführt werden: timeout",
        error_code="timeout",
        retryable=True,
    )

    mark_done = Mock(return_value=True)

    monkeypatch.setattr(orchestrator, "get_tool", lambda _name: tool)
    monkeypatch.setattr(orchestrator, "mark_done", mark_done)
    monkeypatch.setattr(orchestrator, "append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_task_done", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_get_change_summary", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(orchestrator, "_git_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "TRACK_FILE_CHANGES", False)
    monkeypatch.setattr(orchestrator, "strip_metadata_tags", lambda task: task)

    monkeypatch.setattr(orchestrator, "load_skill", lambda *_args, **_kwargs: None)

    outcome = orchestrator._execute_tool_task(
        "Run tests #tool:test-loop",
        "test-loop",
        provider,
        cwd=None,
        timeout=77,
    )

    assert outcome.success is False
    assert outcome.finalized is False
    assert outcome.retryable is True
    assert outcome.error_code == "timeout"
    mark_done.assert_not_called()
    assert tool.run.call_args.kwargs["timeout"] == 77


def test_run_once_marks_tool_task_for_retry_on_timeout_without_provider_fallback(monkeypatch):
    """A tool-task timeout must mark the task for retry and NOT fall back to a
    second provider.  Falling back risks the next provider failing non-retryably,
    which would finalize the task as [-] and incorrectly satisfy #needs: deps."""
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())
    p2 = SimpleNamespace(name="codex", set_cooldown=Mock())

    exec_mock = Mock(return_value=orchestrator.ToolTaskExecutionOutcome(
        success=False,
        finalized=False,
        retryable=True,
        error="timeout",
        error_code="timeout",
    ))
    mark_retry_mock = Mock(return_value=True)

    def fake_select_provider(_task, _limits, exclude=None, **_kwargs):
        exclude = exclude or set()
        if "claude" not in exclude:
            return p1
        if "codex" not in exclude:
            return p2
        return None

    def fake_extract_timeout(_task, default=0):
        return 77 if default == 0 else default

    monkeypatch.setattr(
        orchestrator,
        "read_queue_items",
        lambda: [SimpleNamespace(task_text="Task #tool:test-loop", line_no=1)],
    )
    # Simulate task still pending after retry mark (retry comment keeps it in queue)
    monkeypatch.setattr(orchestrator, "read_queue", lambda: ["Task #tool:test-loop"])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", fake_extract_timeout)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "test-loop")
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", fake_select_provider)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", lambda _limits: 3600)
    monkeypatch.setattr(orchestrator, "append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *_args, **_kwargs: None)

    result = orchestrator.run_once()

    # Task is still pending (retry-marked) so run_once reports incomplete
    assert result is False
    # Only Claude should be tried — no fallback to Codex
    assert exec_mock.call_count == 1
    assert exec_mock.call_args_list[0].args[2].name == "claude"
    assert exec_mock.call_args_list[0].kwargs["timeout"] == 77
    # Task must be marked for retry so #needs: deps stay blocked
    mark_retry_mock.assert_called_once()
    p1.set_cooldown.assert_not_called()
    p2.set_cooldown.assert_not_called()


def test_run_once_tool_task_pins_and_restores_forced_effort(monkeypatch):
    """#effort: must reach the provider on the TOOL-task path and be restored after.

    Regression guard: until 2026-07-30 deleting the `_forced_effort` setattr in
    run_once() left the whole suite green — the flag was only covered at provider level
    and on the parallel path, so the normal execution path was untested.
    """
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())
    seen: list[str | None] = []

    def capture_exec(*args, **kwargs):
        seen.append(getattr(p1, "_forced_effort", None))
        return orchestrator.ToolTaskExecutionOutcome(
            success=True, finalized=False, retryable=False, error="", error_code="",
        )

    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(task_text="Task #tool:test-loop #effort:low", line_no=1)],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: [])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "test-loop")
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: p1)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", capture_exec)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)

    orchestrator.run_once()

    assert seen == ["low"], "effort was not pinned on the provider during the tool call"
    assert getattr(p1, "_forced_effort", None) is None, "effort was not restored afterwards"


def test_run_once_tool_task_without_effort_tag_pins_nothing(monkeypatch):
    """No tag → no pin, so the CLI keeps its own session default."""
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())
    seen: list[str | None] = []

    def capture_exec(*args, **kwargs):
        seen.append(getattr(p1, "_forced_effort", None))
        return orchestrator.ToolTaskExecutionOutcome(
            success=True, finalized=False, retryable=False, error="", error_code="",
        )

    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(task_text="Task #tool:test-loop", line_no=1)],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: [])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "test-loop")
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: p1)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", capture_exec)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)

    orchestrator.run_once()

    assert seen == [None]


def test_run_once_tool_task_recovers_from_token_refresh_race(monkeypatch):
    """Tool-path boot race: on the FIRST selection a tool task whose provider is only
    blocked by an in-flight OAuth token refresh must force-refresh limits ONCE and then
    dispatch — not fast-fail as provider_unreachable (mirrors the single-shot fix)."""
    provider = SimpleNamespace(name="claude", set_cooldown=Mock())

    expired = limits.AllLimits(
        claude=limits.ProviderLimits(available=False, error="token expired"),
    )
    healthy = limits.AllLimits(
        claude=limits.ProviderLimits(available=True, remaining_pct=100.0),
    )
    get_limits_calls = []

    def fake_get_limits(force_refresh=False):
        get_limits_calls.append(force_refresh)
        return healthy if force_refresh else expired

    # None on the first selection (limits show expired), provider once refreshed.
    select_results = [None, provider]

    def fake_select_provider(_task, _limits, exclude=None, **_kw):
        return select_results.pop(0)

    exec_mock = Mock(return_value=orchestrator.ToolTaskExecutionOutcome(
        success=True, finalized=True, retryable=False, error="", error_code="",
    ))
    mark_retry_mock = Mock(return_value=True)

    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(task_text="Task #tool:test-loop", line_no=1)],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: ["Task #tool:test-loop"])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "test-loop")
    monkeypatch.setattr(orchestrator, "get_limits", fake_get_limits)
    monkeypatch.setattr(orchestrator, "select_provider", fake_select_provider)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_mark_retry_checked", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", lambda _limits: 3600)
    monkeypatch.setattr(orchestrator, "append_log", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator, "notify_task_started", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *_a, **_k: None)

    orchestrator.run_once()

    # Exactly one force_refresh after the transient miss, then the tool executes.
    assert get_limits_calls == [False, True]
    exec_mock.assert_called_once()
    assert exec_mock.call_args.args[2].name == "claude"
    mark_retry_mock.assert_not_called()


def test_run_once_tool_task_parks_after_single_force_refresh_when_token_stays_expired(monkeypatch):
    """Tool-path endless-loop guard: if the token is STILL expired after the one
    force_refresh, the task is parked after EXACTLY ONE refresh — never loops."""
    provider = SimpleNamespace(name="claude", set_cooldown=Mock())

    expired = limits.AllLimits(
        claude=limits.ProviderLimits(available=False, error="token expired"),
    )
    get_limits_calls = []

    def fake_get_limits(force_refresh=False):
        get_limits_calls.append(force_refresh)
        return expired

    exec_mock = Mock()
    mark_retry_mock = Mock(return_value=True)

    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(task_text="Task #tool:test-loop", line_no=1)],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: ["Task #tool:test-loop"])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "test-loop")
    monkeypatch.setattr(orchestrator, "get_limits", fake_get_limits)
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_mark_retry_checked", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", lambda _limits: 3600)
    monkeypatch.setattr(orchestrator, "append_log", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *_a, **_k: None)

    orchestrator.run_once()

    # Exactly one force_refresh, then parked — no endless force-refresh loop.
    assert get_limits_calls == [False, True]
    exec_mock.assert_not_called()
    mark_retry_mock.assert_called_once()


def test_run_once_tool_runtime_exceeded_is_terminal_no_fallback_no_fresh_deadline(monkeypatch):
    """tool_runtime_exceeded must be terminal: the task is finalized with its
    partial result, NOT retried on the next provider (which would restart from
    iteration 1 with a fresh max_runtime_sec deadline → 3× the budget) and NOT
    mark_retry'd (the next poll would also restart with a fresh deadline)."""
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())
    p2 = SimpleNamespace(name="codex", set_cooldown=Mock())

    exec_mock = Mock(return_value=orchestrator.ToolTaskExecutionOutcome(
        success=False, finalized=False, retryable=True,
        error="Gesamt-Laufzeit-Limit erreicht nach Iteration 7",
        error_code="tool_runtime_exceeded",
        output="partial work so far",
    ))
    mark_retry_mock = Mock(return_value=True)
    finalize_mock = Mock(return_value=True)

    def fake_select_provider(_task, _limits, exclude=None, **_kwargs):
        exclude = exclude or set()
        if "claude" not in exclude:
            return p1
        if "codex" not in exclude:
            return p2
        return None

    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(task_text="Task #tool:review-loop", line_no=1)],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: ["Task #tool:review-loop"])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "review-loop")
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", fake_select_provider)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_finalize_task_with_result_checked", finalize_mock)
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", lambda _limits: 3600)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)

    orchestrator.run_once()

    # Only ONE provider invocation — no fallback to the next provider, which
    # would restart the loop from iteration 1 with a fresh deadline.
    assert exec_mock.call_count == 1
    assert exec_mock.call_args_list[0].args[2].name == "claude"
    # Task finalized with the partial result, NOT requeued for a fresh-deadline re-run.
    finalize_mock.assert_called_once()
    assert finalize_mock.call_args.args[1] == "partial work so far"
    mark_retry_mock.assert_not_called()
    p1.set_cooldown.assert_not_called()
    p2.set_cooldown.assert_not_called()


def test_run_once_hang_requeues_with_backoff_not_quota_reset(monkeypatch):
    """A tool-task hang (idle-kill) must requeue with a short backoff via
    mark_retry(hang_count=...) — NOT take the quota-reset retry path."""
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())

    exec_mock = Mock(return_value=orchestrator.ToolTaskExecutionOutcome(
        success=False, finalized=False, retryable=True,
        error="hang", error_code="hang",
    ))
    mark_retry_mock = Mock(return_value=True)
    next_retry_mock = Mock(return_value=99999)  # must NOT be used for hang

    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(task_text="Task #tool:test-loop", line_no=1, raw_line="- [ ] Task #tool:test-loop")],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: ["Task #tool:test-loop"])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "test-loop")
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: p1)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", next_retry_mock)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)

    result = orchestrator.run_once()

    assert result is False
    mark_retry_mock.assert_called_once()
    assert mark_retry_mock.call_args.kwargs["hang_count"] == 1
    next_retry_mock.assert_not_called()  # NOT quota-reset retried
    p1.set_cooldown.assert_not_called()


def test_run_once_hang_blocks_task_after_max_retries(monkeypatch):
    """After MAX_HANG_RETRIES the task is BLOCKED (finalized), not requeued."""
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())

    exec_mock = Mock(return_value=orchestrator.ToolTaskExecutionOutcome(
        success=False, finalized=False, retryable=True,
        error="hang", error_code="hang",
    ))
    mark_retry_mock = Mock(return_value=True)
    finalize_mock = Mock(return_value=True)

    monkeypatch.setattr(orchestrator, "MAX_HANG_RETRIES", 2)
    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(
            task_text="Task #tool:test-loop", line_no=1,
            raw_line="- [ ] Task #tool:test-loop <!-- retry: 2026-01-01 00:00 --> <!-- hang: 2 -->",
        )],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: ["Task #tool:test-loop"])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "test-loop")
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: p1)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_finalize_task_with_result_checked", finalize_mock)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)

    orchestrator.run_once()

    # hang_count was 2 → +1 = 3 > MAX_HANG_RETRIES(2) → blocked (finalized), no requeue
    finalize_mock.assert_called_once()
    mark_retry_mock.assert_not_called()


def test_run_once_format_error_requeues_under_the_hang_cap(monkeypatch):
    """A tool that returns unparseable output must be retried, not ticked off.

    Before, the three format-break paths returned ToolResult without error_code
    and without retryable → the orchestrator skipped the retry and finalized the
    queue item as if the work were done. The retry has to be capped, and the
    persistent `<!-- hang: N -->` counter is the only per-task counter the queue
    has, so format_error shares it.
    """
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())

    exec_mock = Mock(return_value=orchestrator.ToolTaskExecutionOutcome(
        success=False, finalized=False, retryable=True,
        error="Review-Output entspricht nicht dem erwarteten Format",
        error_code="format_error",
    ))
    mark_retry_mock = Mock(return_value=True)
    next_retry_mock = Mock(return_value=99999)  # quota-reset path must NOT be used

    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(task_text="Task #tool:review-loop", line_no=1,
                                 raw_line="- [ ] Task #tool:review-loop")],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: ["Task #tool:review-loop"])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "review-loop")
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: p1)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", next_retry_mock)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)

    assert orchestrator.run_once() is False
    mark_retry_mock.assert_called_once()
    assert mark_retry_mock.call_args.kwargs["hang_count"] == 1
    next_retry_mock.assert_not_called()
    # A format break says nothing about provider health → no cooldown.
    p1.set_cooldown.assert_not_called()


def test_run_once_format_error_blocks_task_after_max_retries(monkeypatch):
    """A model that keeps breaking format gets blocked, not looped forever."""
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())

    exec_mock = Mock(return_value=orchestrator.ToolTaskExecutionOutcome(
        success=False, finalized=False, retryable=True,
        error="Quality-Review-Output entspricht nicht dem erwarteten Format",
        error_code="format_error",
    ))
    mark_retry_mock = Mock(return_value=True)
    finalize_mock = Mock(return_value=True)

    monkeypatch.setattr(orchestrator, "MAX_HANG_RETRIES", 2)
    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(
            task_text="Task #tool:dev-loop", line_no=1,
            raw_line="- [ ] Task #tool:dev-loop <!-- retry: 2026-01-01 00:00 --> <!-- hang: 2 -->",
        )],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: ["Task #tool:dev-loop"])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "dev-loop")
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: p1)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_finalize_task_with_result_checked", finalize_mock)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)

    orchestrator.run_once()

    finalize_mock.assert_called_once()
    mark_retry_mock.assert_not_called()
    assert "Format" in finalize_mock.call_args.args[1]


def test_run_once_tool_task_stops_on_policy_barred_provider_tag(monkeypatch):
    """A #provider tag the policy bars is terminal — no endless quota-reset park.

    select_provider() returns None both for "policy said no" and "everything is
    capacity-exhausted"; only the latter is worth waiting for, so the orchestrator
    has to distinguish them or the task parks and re-parks forever.
    """
    finalize_mock = Mock(return_value=True)
    mark_retry_mock = Mock(return_value=True)

    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(task_text="Task #tool:dev-loop #vibe", line_no=1,
                                 raw_line="- [ ] Task #tool:dev-loop #vibe")],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: ["Task #tool:dev-loop #vibe"])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "dev-loop")
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: None)
    monkeypatch.setattr(
        orchestrator, "forced_provider_policy_violation",
        lambda *a, **kw: ("vibe", ["claude", "codex"]),
    )
    monkeypatch.setattr(orchestrator, "_finalize_task_with_result_checked", finalize_mock)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_mark_retry_checked", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)

    orchestrator.run_once()

    finalize_mock.assert_called_once()
    msg = finalize_mock.call_args.args[1]
    assert "vibe" in msg and "nicht zugelassen" in msg
    mark_retry_mock.assert_not_called()  # not parked for a quota reset


def _stub_single_shot_env(monkeypatch, *, raw_line, provider):
    """Common monkeypatching so run_once reaches the single-shot provider loop
    for a plain (non-tool) task without hitting policy/memory/limits I/O."""
    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(
            task_text="Plain claude task", line_no=1, raw_line=raw_line,
            subtasks=None,
        )],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: ["Plain claude task"])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: None)  # NON-tool
    # Real AllLimits() so the single-shot None-path can call
    # limits.has_transient_token_refresh() (default → False = no transient state).
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: limits.AllLimits())
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: provider)
    monkeypatch.setattr(orchestrator, "_build_prompt", lambda *a, **kw: "prompt")
    monkeypatch.setattr(orchestrator, "report_estimated_usage", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "estimate_task_usage_pct", lambda *a, **kw: 0.0)
    monkeypatch.setattr(orchestrator, "model_id_for_provider", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_is_git_repo", lambda *a, **kw: False)
    monkeypatch.setattr(orchestrator, "TRACK_FILE_CHANGES", False)
    monkeypatch.setattr(orchestrator, "_git_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator.memory_module, "get_context_for_task", lambda *a, **kw: "")
    monkeypatch.setattr(orchestrator.memory_module, "archive_old_memories", lambda *a, **kw: 0)
    monkeypatch.setattr(orchestrator, "extract_profile_tag", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_model_tag", lambda _task: None)
    monkeypatch.setattr(orchestrator, "is_known_model_tag", lambda _tag: True)
    monkeypatch.setattr(orchestrator, "has_cwd_tag", lambda _task: False)
    monkeypatch.setattr(orchestrator, "has_explicit_provider_tag", lambda _task: False)
    monkeypatch.setattr(orchestrator, "strip_metadata_tags", lambda task: task)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_task_started", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)


def test_run_once_single_shot_hang_requeues_with_backoff(monkeypatch):
    """A plain (non-tool) #claude task that idle-hangs must requeue with a
    short backoff via mark_retry(hang_count=...) — NOT loop/rotate forever."""
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())

    _stub_single_shot_env(
        monkeypatch,
        raw_line="- [ ] Plain claude task",
        provider=p1,
    )
    monkeypatch.setattr(
        orchestrator, "_run_with_retry",
        lambda *a, **kw: (orchestrator.RunResult(success=False, error="hang"), False),
    )
    mark_retry_mock = Mock(return_value=True)
    next_retry_mock = Mock(return_value=99999)  # must NOT be used for hang
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", next_retry_mock)

    orchestrator.run_once()

    mark_retry_mock.assert_called_once()
    assert mark_retry_mock.call_args.kwargs["hang_count"] == 1
    next_retry_mock.assert_not_called()  # NOT quota-reset retried
    p1.set_cooldown.assert_not_called()  # hang is not a capacity/health issue


def test_run_once_single_shot_pins_and_restores_forced_effort(monkeypatch):
    """Same guard as the tool path, for the plain (non-tool) single-shot path."""
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())
    seen: list[str | None] = []

    _stub_single_shot_env(
        monkeypatch,
        raw_line="- [ ] Plain claude task #effort:xhigh",
        provider=p1,
    )
    # The stub maps the queue text; effort is read from the task text via the real
    # extractor, so point it at a tagged string explicitly.
    monkeypatch.setattr(orchestrator, "extract_effort_tag", lambda _task: "xhigh")
    monkeypatch.setattr(orchestrator, "extract_effort_tag_raw", lambda _task: "xhigh")

    def capture_run(*a, **kw):
        seen.append(getattr(p1, "_forced_effort", None))
        return (orchestrator.RunResult(success=True, output="ok"), False)

    monkeypatch.setattr(orchestrator, "_run_with_retry", capture_run)

    orchestrator.run_once()

    assert seen == ["xhigh"], "effort was not pinned during the single-shot run"
    assert getattr(p1, "_forced_effort", None) is None, "effort was not restored"


def test_run_once_single_shot_restores_forced_effort_on_exception(monkeypatch):
    """A throwing run must not leak the pinned level onto the shared provider
    singleton — the restore lives in a `finally`, and this proves it."""
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())

    _stub_single_shot_env(
        monkeypatch,
        raw_line="- [ ] Plain claude task #effort:max",
        provider=p1,
    )
    monkeypatch.setattr(orchestrator, "extract_effort_tag", lambda _task: "max")
    monkeypatch.setattr(orchestrator, "extract_effort_tag_raw", lambda _task: "max")

    def boom(*a, **kw):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(orchestrator, "_run_with_retry", boom)

    with pytest.raises(RuntimeError):
        orchestrator.run_once()

    assert getattr(p1, "_forced_effort", None) is None, "effort leaked after exception"


def test_run_once_single_shot_hang_blocks_after_max_retries(monkeypatch):
    """A non-tool task that has already hung MAX_HANG_RETRIES times is BLOCKED
    (finalized), not requeued — proving it stops looping silently (spec §4.1)."""
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())

    monkeypatch.setattr(orchestrator, "MAX_HANG_RETRIES", 2)
    _stub_single_shot_env(
        monkeypatch,
        raw_line="- [ ] Plain claude task <!-- hang: 2 -->",
        provider=p1,
    )
    monkeypatch.setattr(
        orchestrator, "_run_with_retry",
        lambda *a, **kw: (orchestrator.RunResult(success=False, error="hang"), False),
    )
    mark_retry_mock = Mock(return_value=True)
    finalize_mock = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(
        orchestrator, "_finalize_task_with_result_checked", finalize_mock,
    )

    orchestrator.run_once()

    # hang_count 2 → +1 = 3 > MAX_HANG_RETRIES(2) → blocked (finalized), no requeue
    finalize_mock.assert_called_once()
    mark_retry_mock.assert_not_called()


def test_run_once_single_shot_recovers_from_token_refresh_race(monkeypatch):
    """Boot race: a single-shot task whose provider is briefly unavailable because
    its OAuth token is mid-refresh (preliminary "expired" snapshot) must force-refresh
    the limits ONCE and then dispatch — NOT fast-fail as provider_unreachable."""
    provider = SimpleNamespace(name="claude", set_cooldown=Mock())

    _stub_single_shot_env(
        monkeypatch,
        raw_line="- [ ] Plain claude task",
        provider=provider,
    )

    # First snapshot mimics the boot preliminary: claude token expired (transient).
    # The synchronous force_refresh then returns healthy limits.
    expired = limits.AllLimits(
        claude=limits.ProviderLimits(available=False, error="token expired"),
    )
    healthy = limits.AllLimits(
        claude=limits.ProviderLimits(available=True, remaining_pct=100.0),
    )
    get_limits_calls = []

    def fake_get_limits(force_refresh=False):
        get_limits_calls.append(force_refresh)
        return healthy if force_refresh else expired

    # select_provider: None while limits show expired, provider once refreshed.
    select_results = [None, provider]

    def fake_select_provider(_task, _limits, exclude=None, **_kw):
        return select_results.pop(0)

    monkeypatch.setattr(orchestrator, "get_limits", fake_get_limits)
    monkeypatch.setattr(orchestrator, "select_provider", fake_select_provider)
    monkeypatch.setattr(
        orchestrator, "_run_with_retry",
        lambda *a, **kw: (orchestrator.RunResult(success=True, output="ok"), False),
    )
    mark_retry_mock = Mock(return_value=True)
    finalize_mock = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_mark_retry_checked", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_finalize_task_with_result_checked", finalize_mock)
    monkeypatch.setattr(orchestrator, "_get_change_summary", lambda *a, **kw: "")
    monkeypatch.setattr(orchestrator, "notify_task_done", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator.memory_module, "store_result", lambda *a, **kw: None)

    orchestrator.run_once()

    # Exactly one force_refresh after the transient miss, then dispatch succeeds.
    assert get_limits_calls == [False, True]
    finalize_mock.assert_called_once()      # task dispatched + finalized
    mark_retry_mock.assert_not_called()     # NOT parked as provider_unreachable


def test_run_once_single_shot_no_force_refresh_on_genuine_exhaustion(monkeypatch):
    """Genuine capacity exhaustion (a known reset window, no "expired" token) must
    NOT trigger a force_refresh — it falls straight through to the retry path."""
    provider = SimpleNamespace(name="claude", set_cooldown=Mock())

    _stub_single_shot_env(
        monkeypatch,
        raw_line="- [ ] Plain claude task",
        provider=provider,
    )

    exhausted = limits.AllLimits(
        claude=limits.ProviderLimits(available=False, remaining_pct=0.0, resets_in_sec=3600),
    )
    get_limits_calls = []

    def fake_get_limits(force_refresh=False):
        get_limits_calls.append(force_refresh)
        return exhausted

    monkeypatch.setattr(orchestrator, "get_limits", fake_get_limits)
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", lambda _limits: 3600)
    mark_retry_mock = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_mark_retry_checked", mark_retry_mock)

    orchestrator.run_once()

    # No force_refresh (transient marker is False for genuine exhaustion); parked.
    assert get_limits_calls == [False]
    mark_retry_mock.assert_called_once()


def test_run_once_single_shot_parks_after_single_force_refresh_when_token_stays_expired(monkeypatch):
    """If the OAuth token is STILL expired after the one synchronous force_refresh
    (persistent re-auth needed — the _refresh_failed_until backoff case), the task is
    parked as provider_unreachable after EXACTLY ONE force_refresh — it must NEVER
    force-refresh on every loop iteration (endless-loop guard)."""
    provider = SimpleNamespace(name="claude", set_cooldown=Mock())

    _stub_single_shot_env(
        monkeypatch,
        raw_line="- [ ] Plain claude task",
        provider=provider,
    )

    # The refresh never clears the expired state (persistent re-auth required).
    expired = limits.AllLimits(
        claude=limits.ProviderLimits(available=False, error="token expired"),
    )
    get_limits_calls = []

    def fake_get_limits(force_refresh=False):
        get_limits_calls.append(force_refresh)
        return expired

    monkeypatch.setattr(orchestrator, "get_limits", fake_get_limits)
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", lambda _limits: 900)
    mark_retry_mock = Mock(return_value=True)
    monkeypatch.setattr(orchestrator, "_mark_retry_checked", mark_retry_mock)

    orchestrator.run_once()

    # Exactly one force_refresh (guard fires once), then parked — no endless loop.
    assert get_limits_calls == [False, True]
    mark_retry_mock.assert_called_once()


def test_run_once_single_shot_passes_strict_flag_to_force_refresh_check(monkeypatch):
    """run_once must scope the token-refresh recovery to the routable provider by
    passing strict=provider_is_forced into force_refresh_can_unblock (so a forced
    task's unrelated expired provider can't trigger a wasteful refresh)."""
    provider = SimpleNamespace(name="claude", set_cooldown=Mock())
    _stub_single_shot_env(monkeypatch, raw_line="- [ ] Plain claude task", provider=provider)
    monkeypatch.setattr(orchestrator, "has_explicit_provider_tag", lambda _task: True)  # forced

    all_limits = limits.AllLimits(
        claude=limits.ProviderLimits(available=False, error="token expired"),
    )
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: all_limits)
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", lambda _limits: 900)
    monkeypatch.setattr(orchestrator, "_mark_retry_checked", Mock(return_value=True))

    seen = {}

    def spy(task, lim, *, strict=False, force_name=None):
        seen["strict"] = strict
        return False  # short-circuit: skip the force_refresh, park the task

    monkeypatch.setattr(orchestrator, "force_refresh_can_unblock", spy)

    orchestrator.run_once()

    assert seen["strict"] is True


def test_run_once_sets_rate_limit_cooldown_for_tool_task(monkeypatch):
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())
    p2 = SimpleNamespace(name="codex", set_cooldown=Mock())
    read_queue_calls = iter([[]])

    exec_mock = Mock(side_effect=[
        orchestrator.ToolTaskExecutionOutcome(
            success=False,
            finalized=False,
            retryable=True,
            error="rate limited",
            error_code="rate_limit",
        ),
        orchestrator.ToolTaskExecutionOutcome(success=True, finalized=True),
    ])

    def fake_select_provider(_task, _limits, exclude=None, **_kwargs):
        exclude = exclude or set()
        if "claude" not in exclude:
            return p1
        if "codex" not in exclude:
            return p2
        return None

    monkeypatch.setattr(
        orchestrator,
        "read_queue_items",
        lambda: [SimpleNamespace(task_text="Task #tool:test-loop", line_no=1)],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: next(read_queue_calls))
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "test-loop")
    fake_limits = SimpleNamespace(
        claude=SimpleNamespace(resets_in_sec=45),
        codex=SimpleNamespace(resets_in_sec=0),
        gemini=SimpleNamespace(resets_in_sec=0),
        earliest_reset_sec=lambda: 300,
    )
    monkeypatch.setattr(
        orchestrator,
        "get_limits",
        lambda force_refresh=False: fake_limits,
    )
    monkeypatch.setattr(orchestrator, "select_provider", fake_select_provider)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr(orchestrator, "append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *_args, **_kwargs: None)

    result = orchestrator.run_once()

    assert result is True
    p1.set_cooldown.assert_called_once_with(60)
    p2.set_cooldown.assert_not_called()


def test_execute_tool_task_does_not_finalize_when_atomic_queue_update_fails(monkeypatch):
    provider = SimpleNamespace(name="codex")
    tool = Mock()
    tool.name = "test-loop"
    tool.description = "Test loop"
    tool.read_only = False
    tool.run.return_value = ToolResult(
        success=True,
        output="ALL TESTS PASSED",
        iterations=1,
    )

    finalize_task = Mock(return_value=False)

    monkeypatch.setattr(orchestrator, "get_tool", lambda _name: tool)
    monkeypatch.setattr(orchestrator, "finalize_task_with_result", finalize_task)
    monkeypatch.setattr(orchestrator, "append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_task_done", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_get_change_summary", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(orchestrator, "_git_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "TRACK_FILE_CHANGES", False)
    monkeypatch.setattr(orchestrator, "strip_metadata_tags", lambda task: task)

    monkeypatch.setattr(orchestrator, "load_skill", lambda *_args, **_kwargs: None)

    outcome = orchestrator._execute_tool_task(
        "Run tests #tool:test-loop",
        "test-loop",
        provider,
        cwd=None,
        timeout=77,
        queue_line_no=42,
    )

    assert outcome.success is False
    assert outcome.finalized is False
    assert outcome.error == "queue_update_failed"
    finalize_task.assert_called_once_with(
        "Run tests #tool:test-loop",
        "ALL TESTS PASSED",
        "codex+test-loop",
        line_no=42,
        subtasks=None,
    )


def test_execute_read_only_tool_skips_git_snapshot(monkeypatch, tmp_path):
    provider = SimpleNamespace(name="codex")
    tool = Mock()
    tool.name = "research-qa"
    tool.description = "Research"
    tool.read_only = True
    tool.run.return_value = ToolResult(
        success=True,
        output="analysis",
        iterations=1,
    )

    git_snapshot = Mock()

    monkeypatch.setattr(orchestrator, "get_tool", lambda _name: tool)
    monkeypatch.setattr(orchestrator, "_is_git_repo", lambda _cwd: True)
    monkeypatch.setattr(orchestrator, "_git_snapshot", git_snapshot)
    monkeypatch.setattr(orchestrator, "_get_change_summary", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(orchestrator, "append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_task_done", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "finalize_task_with_result", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(orchestrator.memory_module, "store_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "strip_metadata_tags", lambda task: task)
    monkeypatch.setattr(orchestrator, "TRACK_FILE_CHANGES", False)

    monkeypatch.setattr(orchestrator, "load_skill", lambda *_args, **_kwargs: None)

    outcome = orchestrator._execute_tool_task(
        "Research task #tool:research-qa",
        "research-qa",
        provider,
        cwd=str(tmp_path),
    )

    assert outcome.success is True
    git_snapshot.assert_not_called()


def test_run_once_stops_when_atomic_queue_finalization_fails(monkeypatch):
    provider = SimpleNamespace(name="codex", set_cooldown=Mock())
    queue_item = SimpleNamespace(task_text="Task A", line_no=7)

    finalize_task = Mock(return_value=False)

    monkeypatch.setattr(orchestrator, "read_queue_items", lambda: [queue_item])
    monkeypatch.setattr(orchestrator, "read_queue", lambda: [queue_item.task_text])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: None)
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(orchestrator, "_build_prompt", lambda *_args, **_kwargs: "prompt")
    monkeypatch.setattr(
        orchestrator,
        "_run_with_retry",
        lambda *_args, **_kwargs: (orchestrator.RunResult(success=True, output="ok", error=""), False),
    )
    monkeypatch.setattr(orchestrator, "_is_git_repo", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(orchestrator, "_snapshot_dir", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(orchestrator, "_git_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_get_change_summary", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(orchestrator, "finalize_task_with_result", finalize_task)
    monkeypatch.setattr(orchestrator, "append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_task_done", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *_args, **_kwargs: None)

    result = orchestrator.run_once()

    assert result is False
    finalize_task.assert_called_once_with("Task A", "ok", "codex", line_no=7, subtasks=None)


def test_run_once_aborts_task_when_cwd_tag_is_invalid(monkeypatch):
    queue_item = SimpleNamespace(task_text="Fix bug cwd:/missing/project #codex", line_no=9)
    mark_done = Mock(return_value=True)
    select_provider = Mock(side_effect=AssertionError("provider must not be selected"))

    monkeypatch.setattr(orchestrator, "read_queue_items", lambda: [queue_item])
    monkeypatch.setattr(orchestrator, "read_queue", lambda: [])
    monkeypatch.setattr(orchestrator, "has_cwd_tag", lambda _task: True)
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: None)
    monkeypatch.setattr(orchestrator, "mark_done", mark_done)
    monkeypatch.setattr(orchestrator, "select_provider", select_provider)
    monkeypatch.setattr(orchestrator, "append_log", lambda *_args, **_kwargs: None)
    notify_error = Mock()
    monkeypatch.setattr(orchestrator, "notify_error", notify_error)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *_args, **_kwargs: None)

    result = orchestrator.run_once()

    assert result is True
    mark_done.assert_called_once_with(
        "Fix bug cwd:/missing/project #codex",
        "invalid-cwd",
        line_no=9,
        subtasks=None,
    )
    notify_error.assert_called_once()
    select_provider.assert_not_called()


def test_run_once_policy_skip_marks_retry_and_does_not_execute(monkeypatch):
    queue_item = SimpleNamespace(task_text="Risky task", line_no=11)
    mark_retry = Mock(return_value=True)
    select_provider = Mock(side_effect=AssertionError("provider must not be selected after /skip"))

    class FakeEngine:
        def check_task(self, _task_text, profile_rules=None):
            return policy_module.TIER_APPROVE, ["git push to remote"]

        def is_preapproved(self, _category):
            return False

        def request_approval(self, _task_text, _reasons):
            return "skipped"

    monkeypatch.setattr(policy_module, "get_engine", lambda: FakeEngine())
    monkeypatch.setattr(orchestrator, "read_queue_items", lambda: [queue_item])
    monkeypatch.setattr(orchestrator, "read_queue", lambda: [queue_item.task_text])
    monkeypatch.setattr(orchestrator, "has_cwd_tag", lambda _task: False)
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_shutdown_tag", lambda _task: False)
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator.memory_module, "archive_old_memories", lambda: 0)
    monkeypatch.setattr(orchestrator.memory_module, "get_context_for_task", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry)
    monkeypatch.setattr(orchestrator, "select_provider", select_provider)
    monkeypatch.setattr(orchestrator, "append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *_args, **_kwargs: None)

    result = orchestrator.run_once()

    assert result is False
    mark_retry.assert_called_once()
    select_provider.assert_not_called()


def test_run_once_inline_preapproval_tag_matches_policy_reason(monkeypatch):
    queue_item = SimpleNamespace(task_text="Deploy release #approve:push", line_no=12)
    mark_retry = Mock(return_value=True)
    engine = Mock()
    engine.check_task.return_value = (policy_module.TIER_APPROVE, ["git push to remote"])
    engine.is_preapproved.return_value = False
    engine.request_approval = Mock(side_effect=AssertionError("approval prompt should be skipped"))

    monkeypatch.setattr(policy_module, "get_engine", lambda: engine)
    monkeypatch.setattr(orchestrator, "read_queue_items", lambda: [queue_item])
    monkeypatch.setattr(orchestrator, "read_queue", lambda: [queue_item.task_text])
    monkeypatch.setattr(orchestrator, "has_cwd_tag", lambda _task: False)
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_shutdown_tag", lambda _task: False)
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: limits.AllLimits())
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", lambda _limits: 1)
    monkeypatch.setattr(orchestrator.memory_module, "archive_old_memories", lambda: 0)
    monkeypatch.setattr(orchestrator.memory_module, "get_context_for_task", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry)
    monkeypatch.setattr(orchestrator, "select_provider", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *_args, **_kwargs: None)

    result = orchestrator.run_once()

    assert result is False
    engine.request_approval.assert_not_called()
    mark_retry.assert_called_once()


def test_run_once_parallel_exception_marks_retry_instead_of_done(monkeypatch):
    queue_item = SimpleNamespace(task_text="Parent task #parallel", line_no=21, subtasks=("sub a",))
    mark_retry = Mock(return_value=True)
    mark_done = Mock(return_value=True)

    class FakeEngine:
        def check_task(self, _task_text, profile_rules=None):
            return policy_module.TIER_AUTO, []

        def is_preapproved(self, _category):
            return False

    monkeypatch.setattr(policy_module, "get_engine", lambda: FakeEngine())
    monkeypatch.setattr(orchestrator, "read_queue_items", lambda: [queue_item])
    monkeypatch.setattr(orchestrator, "has_cwd_tag", lambda _task: False)
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_shutdown_tag", lambda _task: False)
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry)
    monkeypatch.setattr(orchestrator, "mark_done", mark_done)
    monkeypatch.setattr(orchestrator, "append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_task_done", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "select_provider", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator.memory_module, "archive_old_memories", lambda: 0)
    monkeypatch.setattr(orchestrator.memory_module, "get_context_for_task", lambda *_args, **_kwargs: "")

    import parallel_runner as parallel_runner_module
    monkeypatch.setattr(parallel_runner_module, "run_parallel", Mock(side_effect=RuntimeError("boom")))

    result = orchestrator.run_once()

    assert result is False
    mark_retry.assert_called_once()
    mark_done.assert_not_called()


# ---------------------------------------------------------------------------
# stdin_incomplete: prompt not fully delivered to the CLI
# (providers/process_runner._feed_stdin). MAX_RETRIES_PER_PROVIDER is 2, so
# without the bail-out the same oversized prompt would be pushed down the same
# broken pipe a second time after a 10 s backoff — pure token burn.
# ---------------------------------------------------------------------------

def test_run_with_retry_does_not_retry_stdin_incomplete_in_run(monkeypatch):
    # Patched so a REGRESSION fails fast instead of sitting through the backoff.
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _s: None)
    calls = []

    class _Provider:
        name = "claude"

        def run(self, *a, **kw):
            calls.append(1)
            return orchestrator.RunResult(success=False, error="stdin_incomplete")

    result, exhausted = orchestrator._run_with_retry(
        _Provider(), task="t", prompt="p", cwd=None, timeout=60,
    )
    assert result.error == "stdin_incomplete"
    assert exhausted is True
    assert len(calls) == 1, f"expected no in-run retry, got {len(calls)} attempts"


def test_run_with_retry_still_retries_generic_errors(monkeypatch):
    """Guard the bail-out list: ordinary failures keep their in-run retry."""
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _s: None)  # skip backoff
    calls = []

    class _Provider:
        name = "claude"

        def run(self, *a, **kw):
            calls.append(1)
            return orchestrator.RunResult(success=False, error="something odd")

    orchestrator._run_with_retry(
        _Provider(), task="t", prompt="p", cwd=None, timeout=60,
    )
    assert len(calls) > 1, "generic errors must still be retried in-run"


# ---------------------------------------------------------------------------
# Policy dead end: "no provider ALLOWED" is permanent and must not be parked
# as if it were "no provider AVAILABLE" (which waits for a quota reset).
# ---------------------------------------------------------------------------

def _dead_end_queue(monkeypatch, tool="dev-loop"):
    """Common run_once() wiring for the policy-dead-end tests."""
    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(task_text=f"Task #tool:{tool}", line_no=1,
                                 raw_line=f"- [ ] Task #tool:{tool}")],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: [f"Task #tool:{tool}"])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: tool)
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)


def _install_policy(monkeypatch, tmp_path, yaml_text):
    from policy import PolicyEngine
    policy_file = tmp_path / "99_System" / "AI" / "policy.yaml"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(policy_module, "_engine", PolicyEngine(vault_path=tmp_path))


def test_run_once_finalizes_when_policy_leaves_no_provider(monkeypatch, tmp_path):
    """A policy allowing only out-of-chain providers is a DEAD END, not a queue.

    select_provider() returns None for this exactly as it does for "all providers
    exhausted", and the orchestrator used to take the second reading: mark_retry
    until the quota resets, notify "providers exhausted". No quota reset can lift a
    policy restriction, so the task was re-parked on every poll, forever, with a
    message naming the wrong cause. It must be finalized with a clear reason
    instead — driven through the real dispatcher and a real policy engine, with no
    select_provider stub, so the wiring is what is under test.

    The trigger here is a misspelled provider name, which is the realistic way to
    reach this state: it matches nothing in the profile's provider order, so the
    filter empties the chain. (A first draft of this test used `[gemini]` and did
    NOT reproduce — the default profile's providers are ["claude", "gemini",
    "codex"], so gemini stays routable even though it left `_PRIORITY`. Worth
    knowing: with the shipped policy — claude/codex only — this dead end needs a
    misconfiguration to occur at all.)
    """
    _install_policy(monkeypatch, tmp_path, "tool_providers:\n  dev-loop: [claudee]\n")
    _dead_end_queue(monkeypatch)

    finalize_mock = Mock(return_value=True)
    mark_retry_mock = Mock(return_value=True)
    exhausted_mock = Mock()
    exec_mock = Mock()

    monkeypatch.setattr(orchestrator, "_finalize_task_with_result_checked", finalize_mock)
    monkeypatch.setattr(orchestrator, "_mark_retry_checked", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", exhausted_mock)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)

    orchestrator.run_once()

    finalize_mock.assert_called_once()
    mark_retry_mock.assert_not_called()      # NOT parked
    exhausted_mock.assert_not_called()       # NOT reported as a capacity problem
    exec_mock.assert_not_called()            # nothing ran

    msg = finalize_mock.call_args.args[1]
    assert "claudee" in msg                  # names what the policy DOES allow
    assert "Quota-Reset" in msg              # and says why waiting cannot help


def test_run_once_still_parks_when_providers_are_merely_exhausted(monkeypatch, tmp_path):
    """Guard the other side: capacity exhaustion keeps its wait-and-retry path.

    Without this the dead-end branch could swallow every None and turn an ordinary
    "come back after the quota resets" into a finalized (dead) task.
    """
    _install_policy(monkeypatch, tmp_path, "tool_providers:\n  dev-loop: [claude, codex]\n")
    _dead_end_queue(monkeypatch)

    finalize_mock = Mock(return_value=True)
    mark_retry_mock = Mock(return_value=True)
    exhausted_mock = Mock()

    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "force_refresh_can_unblock", lambda *a, **kw: False)
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", lambda _limits: 3600)
    monkeypatch.setattr(orchestrator, "_finalize_task_with_result_checked", finalize_mock)
    monkeypatch.setattr(orchestrator, "_mark_retry_checked", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", exhausted_mock)

    assert orchestrator.run_once() is False
    mark_retry_mock.assert_called_once()
    exhausted_mock.assert_called_once()
    finalize_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Shared hang/format counter: the cap is joint on purpose, the WORDING must say so
# ---------------------------------------------------------------------------

def _blocked_message_for(monkeypatch, error_code, raw_line):
    """Run one tool task into the blocked branch; return the finalize/retry mocks."""
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())
    exec_mock = Mock(return_value=orchestrator.ToolTaskExecutionOutcome(
        success=False, finalized=False, retryable=True,
        error="x", error_code=error_code,
    ))
    finalize_mock = Mock(return_value=True)
    mark_retry_mock = Mock(return_value=True)

    monkeypatch.setattr(orchestrator, "MAX_HANG_RETRIES", 2)
    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(task_text="Task #tool:dev-loop", line_no=1,
                                 raw_line=raw_line)],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: ["Task #tool:dev-loop"])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "dev-loop")
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: p1)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "_finalize_task_with_result_checked", finalize_mock)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)

    orchestrator.run_once()
    return finalize_mock, mark_retry_mock


def test_blocked_message_does_not_claim_a_hang_that_never_happened(monkeypatch):
    """Two format errors + a FIRST real hang blocks at count 3 — say that honestly.

    hang and format_error share the persistent `<!-- hang: N -->` counter (the
    queue's only per-task counter). Blocking on the third dead attempt is the
    intended, safe behaviour and stays. The old message reported it as
    "Tool-Hang ... zum 3. Mal", which at 03:00 sends you hunting two earlier hangs
    that never existed. The count is joint, so the text has to say joint.
    """
    finalize_mock, mark_retry_mock = _blocked_message_for(
        monkeypatch, "hang",
        "- [ ] Task #tool:dev-loop <!-- retry: 2026-01-01 00:00 --> <!-- hang: 2 -->",
    )
    finalize_mock.assert_called_once()
    mark_retry_mock.assert_not_called()

    msg = finalize_mock.call_args.args[1]
    assert "Tool-Hang" in msg                  # what happened THIS time
    assert "zum 3. Mal" not in msg             # the false ordinal is gone
    assert "3. erfolgloser Versuch" in msg     # what the counter actually counts
    assert "Hang/Format-Fehler" in msg         # and that it is shared


def test_blocked_format_error_message_names_the_shared_counter(monkeypatch):
    """Mirror case: a format error must not be reported as a hang either."""
    finalize_mock, _ = _blocked_message_for(
        monkeypatch, "format_error",
        "- [ ] Task #tool:dev-loop <!-- retry: 2026-01-01 00:00 --> <!-- hang: 2 -->",
    )
    msg = finalize_mock.call_args.args[1]
    assert "Tool-Format-Fehler" in msg
    assert "Tool-Hang" not in msg
    assert "Hang/Format-Fehler" in msg


def test_requeue_message_shows_the_joint_attempt_budget(monkeypatch):
    """The non-blocking requeue line carries the same honesty (n/MAX)."""
    logged: list[str] = []
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())
    exec_mock = Mock(return_value=orchestrator.ToolTaskExecutionOutcome(
        success=False, finalized=False, retryable=True,
        error="x", error_code="format_error",
    ))
    mark_retry_mock = Mock(return_value=True)

    monkeypatch.setattr(orchestrator, "MAX_HANG_RETRIES", 2)
    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(task_text="Task #tool:dev-loop", line_no=1,
                                 raw_line="- [ ] Task #tool:dev-loop")],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: ["Task #tool:dev-loop"])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _task, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "dev-loop")
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: p1)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr(orchestrator, "mark_retry", mark_retry_mock)
    monkeypatch.setattr(orchestrator, "append_log", lambda m, *a, **kw: logged.append(str(m)))
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)

    orchestrator.run_once()

    assert mark_retry_mock.call_args.kwargs["hang_count"] == 1
    line = next(m for m in logged if "Tool-Format-Fehler" in m)
    assert "Versuch 1/2" in line
    assert "Hang/Format-Fehler" in line
