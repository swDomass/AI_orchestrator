from types import SimpleNamespace
from unittest.mock import Mock

import pytest

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


def test_run_once_retries_tool_task_with_next_provider_and_passes_timeout(monkeypatch):
    p1 = SimpleNamespace(name="claude", set_cooldown=Mock())
    p2 = SimpleNamespace(name="codex", set_cooldown=Mock())
    read_queue_calls = iter([[]])

    exec_mock = Mock(side_effect=[
        orchestrator.ToolTaskExecutionOutcome(
            success=False,
            finalized=False,
            retryable=True,
            error="timeout",
            error_code="timeout",
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

    def fake_extract_timeout(_task, default=0):
        return 77 if default == 0 else default

    monkeypatch.setattr(
        orchestrator,
        "read_queue_items",
        lambda: [SimpleNamespace(task_text="Task #tool:test-loop", line_no=1)],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: next(read_queue_calls))
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", fake_extract_timeout)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: "test-loop")
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", fake_select_provider)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr(orchestrator, "append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *_args, **_kwargs: None)

    result = orchestrator.run_once()

    assert result is True
    assert exec_mock.call_count == 2
    first_call = exec_mock.call_args_list[0]
    second_call = exec_mock.call_args_list[1]
    assert first_call.args[2].name == "claude"
    assert second_call.args[2].name == "codex"
    assert first_call.kwargs["timeout"] == 77
    assert second_call.kwargs["timeout"] == 77
    p1.set_cooldown.assert_not_called()
    p2.set_cooldown.assert_not_called()


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
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
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
