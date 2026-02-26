import pytest
import threading
from pathlib import Path
from types import SimpleNamespace
import queue_manager
import parallel_runner as parallel_runner_module
from parallel_runner import _parse_subtask, SubTask, run_parallel, format_parallel_result, SubTaskResult
from limits import AllLimits

def test_parse_subtask(tmp_path, monkeypatch):
    # Disable ALLOWED_CWD_ROOTS check for the test
    monkeypatch.setattr(queue_manager, "ALLOWED_CWD_ROOTS", [])
    
    # Create a dummy directory to satisfy extract_cwd's is_dir() check
    d = tmp_path / "proj"
    d.mkdir()
    cwd_str = str(d.resolve())
    
    text = f"Review project X #claude #tool:review-loop cwd:{cwd_str} #timeout:300s"
    st = _parse_subtask(text)
    assert st.text == text
    assert st.provider_forced == "claude"
    assert st.tool_name == "review-loop"
    assert Path(st.cwd).resolve() == d.resolve()
    assert st.timeout == 300

def test_parse_subtask_defaults():
    text = "Review project X"
    st = _parse_subtask(text)
    assert st.text == text
    assert st.provider_forced is None
    assert st.tool_name is None
    assert st.cwd is None
    assert st.timeout == 300 # TASK_TIMEOUT_SEC default (5 min)

def test_format_parallel_result():
    results = [
        SubTaskResult(text="Task 1", provider_name="claude", success=True, output="Output 1"),
        SubTaskResult(text="Task 2", provider_name="gemini", success=False, error="Error 2", output=""),
    ]
    formatted = format_parallel_result(results)
    assert "**Subtask 1** (claude): PASS" in formatted
    assert "Output 1" in formatted
    assert "**Subtask 2** (gemini): FAIL" in formatted
    assert "Error 2" in formatted


def test_run_parallel_uses_group_timeout_sum(monkeypatch):
    parsed = {
        "a": SubTask(text="a", provider_forced=None, cwd="C:/proj", tool_name=None, timeout=10),
        "b": SubTask(text="b", provider_forced=None, cwd="C:/proj", tool_name=None, timeout=20),
        "c": SubTask(text="c", provider_forced=None, cwd="C:/other", tool_name=None, timeout=5),
    }

    monkeypatch.setattr(parallel_runner_module, "_parse_subtask", lambda text: parsed[text])
    monkeypatch.setattr(
        parallel_runner_module,
        "_run_single_subtask",
        lambda subtask, idx, limits, memory_context, pause_event, profile=None: SubTaskResult(
            text=subtask.text,
            provider_name="mock",
            success=True,
            output=f"ok-{idx}",
        ),
    )

    created_threads = []

    class FakeThread:
        def __init__(self, target, args, daemon, name):
            self._target = target
            self._args = args
            self.daemon = daemon
            self.name = name
            self.join_timeout = None
            self._alive = False
            created_threads.append(self)

        def start(self):
            self._alive = True
            self._target(*self._args)
            self._alive = False

        def join(self, timeout=None):
            self.join_timeout = timeout

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(parallel_runner_module.threading, "Thread", FakeThread)

    results = run_parallel("parent", ("a", "b", "c"), AllLimits())
    assert [r.text for r in results] == ["a", "b", "c"]

    join_by_name = {t.name: t.join_timeout for t in created_threads}
    assert join_by_name["parallel-C:/proj"] == 150  # 10 + 20 + 120 buffer
    assert join_by_name["parallel-C:/other"] == 125  # 5 + 120 buffer


def test_run_parallel_inherits_parent_cwd_for_subtasks_without_cwd(tmp_path, monkeypatch):
    parent_dir = tmp_path / "proj"
    parent_dir.mkdir()
    monkeypatch.setattr(queue_manager, "ALLOWED_CWD_ROOTS", [])

    monkeypatch.setattr(
        parallel_runner_module,
        "_parse_subtask",
        lambda text: SubTask(text=text, provider_forced=None, cwd=None, tool_name=None, timeout=5),
    )

    seen_cwds = []
    monkeypatch.setattr(
        parallel_runner_module,
        "_run_single_subtask",
        lambda subtask, idx, limits, memory_context, pause_event, profile=None: (
            seen_cwds.append(subtask.cwd),
            SubTaskResult(text=subtask.text, provider_name="mock", success=True, output="ok")
        )[1],
    )

    class FakeThread:
        def __init__(self, target, args, daemon, name):
            self._target = target
            self._args = args
            self.name = name
            self._alive = False

        def start(self):
            self._alive = True
            self._target(*self._args)
            self._alive = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(parallel_runner_module.threading, "Thread", FakeThread)

    run_parallel(
        f"Parent task #parallel cwd:{parent_dir}",
        ("subtask a", "subtask b"),
        AllLimits(),
    )

    assert len(seen_cwds) == 2
    assert all(Path(c).resolve() == parent_dir.resolve() for c in seen_cwds)


def test_run_parallel_continues_group_after_subtask_exception(monkeypatch):
    parsed = {
        "a": SubTask(text="a", provider_forced=None, cwd="C:/proj", tool_name=None, timeout=5),
        "b": SubTask(text="b", provider_forced=None, cwd="C:/proj", tool_name=None, timeout=5),
    }
    monkeypatch.setattr(parallel_runner_module, "_parse_subtask", lambda text: parsed[text])

    calls = []

    def fake_run_single(subtask, idx, limits, memory_context, pause_event, profile=None):
        calls.append((idx, subtask.text))
        if subtask.text == "a":
            raise RuntimeError("boom")
        return SubTaskResult(text=subtask.text, provider_name="mock", success=True, output="ok")

    monkeypatch.setattr(parallel_runner_module, "_run_single_subtask", fake_run_single)

    class FakeThread:
        def __init__(self, target, args, daemon, name):
            self._target = target
            self._args = args
            self.name = name
            self._alive = False

        def start(self):
            self._alive = True
            self._target(*self._args)
            self._alive = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(parallel_runner_module.threading, "Thread", FakeThread)

    results = run_parallel("parent", ("a", "b"), AllLimits())

    assert calls == [(0, "a"), (1, "b")]
    assert len(results) == 2
    assert results[0].success is False
    assert results[0].provider_name == "internal"
    assert "subtask_crash" in results[0].error
    assert results[1].success is True
    assert results[1].output == "ok"


def test_run_single_subtask_tool_success_preserves_tool_output(monkeypatch):
    import dispatcher
    import orchestrator

    provider = SimpleNamespace(name="codex")
    subtask = SubTask(
        text="Run tool #tool:review-loop",
        provider_forced=None,
        cwd=None,
        tool_name="review-loop",
        timeout=30,
    )

    monkeypatch.setattr(dispatcher, "select_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(queue_manager, "strip_metadata_tags", lambda text: text)
    monkeypatch.setattr(
        orchestrator,
        "_execute_tool_task",
        lambda *_args, **_kwargs: orchestrator.ToolTaskExecutionOutcome(
            success=True,
            finalized=False,
            output="fixed 2 issues",
        ),
    )

    result = parallel_runner_module._run_single_subtask(
        subtask,
        idx=0,
        limits=AllLimits(),
        memory_context="",
        pause_event=None,
    )

    assert result.success is True
    assert result.provider_name == "codex+review-loop"
    assert result.output == "fixed 2 issues"
