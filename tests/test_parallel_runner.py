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
    
    text = f"Review project X #claude_haiku #tool:review-loop cwd:{cwd_str} #timeout:300s"
    st = _parse_subtask(text)
    assert st.text == text
    assert st.provider_forced == "claude"
    assert st.tool_name == "review-loop"
    assert Path(st.cwd).resolve() == d.resolve()
    assert st.timeout == 300
    assert st.model_tag == "claude_haiku"

def test_parse_subtask_defaults():
    text = "Review project X"
    st = _parse_subtask(text)
    assert st.text == text
    assert st.provider_forced is None
    assert st.tool_name is None
    assert st.cwd is None
    assert st.timeout == 5400  # TASK_TIMEOUT_SEC default (90 min hard backstop)

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


def test_run_parallel_inherits_parent_forced_model_for_subtasks_without_model(monkeypatch):
    monkeypatch.setattr(queue_manager, "ALLOWED_CWD_ROOTS", [])

    monkeypatch.setattr(
        parallel_runner_module,
        "_parse_subtask",
        lambda text: SubTask(
            text=text,
            provider_forced=None,
            cwd=None,
            tool_name=None,
            timeout=5,
        ),
    )

    seen_models = []
    monkeypatch.setattr(
        parallel_runner_module,
        "_run_single_subtask",
        lambda subtask, idx, limits, memory_context, pause_event, profile=None: (
            seen_models.append(subtask.model_tag),
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
        "Parent task #parallel #claude_haiku",
        ("subtask a", "subtask b"),
        AllLimits(),
    )

    assert seen_models == ["claude_haiku", "claude_haiku"]


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


def test_run_single_subtask_applies_forced_claude_model(monkeypatch):
    import dispatcher
    import orchestrator

    class DummyProvider:
        name = "claude"

        def __init__(self):
            self._forced_model = None

    provider = DummyProvider()
    subtask = SubTask(
        text="Do the thing",
        provider_forced="claude",
        cwd=None,
        tool_name=None,
        timeout=30,
        model_tag="claude_haiku",
    )

    seen_models = []

    monkeypatch.setattr(dispatcher, "select_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(queue_manager, "strip_metadata_tags", lambda text: text)
    monkeypatch.setattr(orchestrator, "_build_prompt", lambda *_args, **_kwargs: "prompt")
    monkeypatch.setattr(
        orchestrator,
        "_run_with_retry",
        lambda provider, *_args, **_kwargs: (
            seen_models.append(provider._forced_model),
            (SimpleNamespace(success=True, output="ok", error=""), 0.0)
        )[1],
    )

    result = parallel_runner_module._run_single_subtask(
        subtask,
        idx=0,
        limits=AllLimits(),
        memory_context="",
        pause_event=None,
    )

    assert seen_models == ["claude-haiku-4-5-20251001"]
    assert provider._forced_model is None
    assert result.success is True
    assert result.provider_name == "claude"


def test_run_single_subtask_reports_estimated_usage_for_plain_tasks(monkeypatch):
    import dispatcher
    import orchestrator

    provider = SimpleNamespace(name="codex")
    subtask = SubTask(
        text="Do the thing",
        provider_forced=None,
        cwd=None,
        tool_name=None,
        timeout=30,
    )

    reported = []

    monkeypatch.setattr(dispatcher, "select_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(queue_manager, "strip_metadata_tags", lambda text: text)
    monkeypatch.setattr(orchestrator, "_build_prompt", lambda *_args, **_kwargs: "prompt")
    monkeypatch.setattr(
        orchestrator,
        "_run_with_retry",
        lambda *_args, **_kwargs: (
            SimpleNamespace(
                success=True,
                output="ok",
                error="",
                input_tokens=123,
                output_tokens=45,
            ),
            0.0,
        ),
    )
    monkeypatch.setattr(parallel_runner_module, "estimate_task_usage_pct", lambda *a, **kw: 7.5)
    monkeypatch.setattr(
        parallel_runner_module,
        "report_estimated_usage",
        lambda provider_name, estimated_pct: reported.append((provider_name, estimated_pct)),
    )

    result = parallel_runner_module._run_single_subtask(
        subtask,
        idx=0,
        limits=AllLimits(),
        memory_context="",
        pause_event=None,
    )

    assert result.success is True
    assert reported == [("codex", 7.5)]


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


def test_run_single_subtask_notifies_once_on_auth_expired_plain_task(monkeypatch):
    """P2 (Runde 2): a #parallel subtask with an expired OAuth login must get
    the same one-time actionable notice as the non-parallel paths, via the
    shared dedup helper (no second mechanism). Deliberately NOT asserting a
    cooldown call — #parallel subtasks set no cooldown for ANY error code,
    a pre-existing gap this fix does not close (see components.md/ROADMAP.md).
    `provider` carries no `set_cooldown` attribute on purpose: a code path that
    tried to call it would fail loudly here instead of the gap being silently
    reintroduced."""
    import dispatcher
    import orchestrator

    provider = SimpleNamespace(name="claude")
    subtask = SubTask(
        text="Do the thing", provider_forced=None, cwd=None, tool_name=None, timeout=30,
    )
    notified = []

    monkeypatch.setattr(orchestrator, "_AUTH_EXPIRED_NOTIFIED", set())
    monkeypatch.setattr(orchestrator, "_notify_auth_expired_once", lambda name: notified.append(name))
    monkeypatch.setattr(dispatcher, "select_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(queue_manager, "strip_metadata_tags", lambda text: text)
    monkeypatch.setattr(orchestrator, "_build_prompt", lambda *_args, **_kwargs: "prompt")
    monkeypatch.setattr(
        orchestrator, "_run_with_retry",
        lambda *_args, **_kwargs: (SimpleNamespace(success=False, output="", error="auth_expired"), True),
    )

    result = parallel_runner_module._run_single_subtask(
        subtask, idx=0, limits=AllLimits(), memory_context="", pause_event=None,
    )

    assert result.success is False
    assert notified == ["claude"]


def test_run_single_subtask_notifies_once_on_auth_expired_tool_task(monkeypatch):
    """Same guard, tool-based subtask branch (`_execute_tool_task`)."""
    import dispatcher
    import orchestrator

    provider = SimpleNamespace(name="claude")
    subtask = SubTask(
        text="Run tool #tool:review-loop", provider_forced=None, cwd=None,
        tool_name="review-loop", timeout=30,
    )
    notified = []

    monkeypatch.setattr(orchestrator, "_AUTH_EXPIRED_NOTIFIED", set())
    monkeypatch.setattr(orchestrator, "_notify_auth_expired_once", lambda name: notified.append(name))
    monkeypatch.setattr(dispatcher, "select_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(queue_manager, "strip_metadata_tags", lambda text: text)
    monkeypatch.setattr(
        orchestrator, "_execute_tool_task",
        lambda *_args, **_kwargs: orchestrator.ToolTaskExecutionOutcome(
            success=False, finalized=False, retryable=True,
            error="auth_expired", error_code="auth_expired",
        ),
    )

    result = parallel_runner_module._run_single_subtask(
        subtask, idx=0, limits=AllLimits(), memory_context="", pause_event=None,
    )

    assert result.success is False
    assert notified == ["claude"]


def test_run_single_subtask_success_rearms_auth_expired_notice_plain_task(monkeypatch):
    """P2-1 (oc r1): a successful #parallel subtask must re-arm the one-time
    auth_expired notice exactly like both run_once() paths do
    (orchestrator.py:2703/:3054) — otherwise the outage after a fresh
    `claude login` stays silent because _AUTH_EXPIRED_NOTIFIED was never
    cleared on the #parallel path. Uses the REAL _clear_auth_expired_notice/
    _notify_auth_expired_once (only the Telegram send at the bottom is
    mocked) against a real, pre-seeded _AUTH_EXPIRED_NOTIFIED set, to prove
    the full cycle: already-notified -> success clears it -> the next outage
    notifies again."""
    import dispatcher
    import orchestrator

    provider = SimpleNamespace(name="claude")
    subtask = SubTask(
        text="Do the thing", provider_forced=None, cwd=None, tool_name=None, timeout=30,
    )
    notified = []

    monkeypatch.setattr(orchestrator, "_AUTH_EXPIRED_NOTIFIED", {"claude"})
    monkeypatch.setattr(orchestrator, "notify_auth_expired", lambda name: notified.append(name))
    monkeypatch.setattr(dispatcher, "select_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(queue_manager, "strip_metadata_tags", lambda text: text)
    monkeypatch.setattr(orchestrator, "_build_prompt", lambda *_args, **_kwargs: "prompt")
    monkeypatch.setattr(
        orchestrator, "_run_with_retry",
        lambda *_args, **_kwargs: (SimpleNamespace(success=True, output="done", error=""), True),
    )

    result = parallel_runner_module._run_single_subtask(
        subtask, idx=0, limits=AllLimits(), memory_context="", pause_event=None,
    )

    assert result.success is True
    assert "claude" not in orchestrator._AUTH_EXPIRED_NOTIFIED  # re-armed
    assert notified == []  # success itself never notifies

    # A later outage must notify again — without the fix it would stay silent
    # because the set was never cleared above.
    monkeypatch.setattr(
        orchestrator, "_run_with_retry",
        lambda *_args, **_kwargs: (SimpleNamespace(success=False, output="", error="auth_expired"), True),
    )
    result2 = parallel_runner_module._run_single_subtask(
        subtask, idx=0, limits=AllLimits(), memory_context="", pause_event=None,
    )

    assert result2.success is False
    assert notified == ["claude"]


def test_run_single_subtask_success_rearms_auth_expired_notice_tool_task(monkeypatch):
    """Same re-arm proof as above, tool-based subtask branch (_execute_tool_task)."""
    import dispatcher
    import orchestrator

    provider = SimpleNamespace(name="claude")
    subtask = SubTask(
        text="Run tool #tool:review-loop", provider_forced=None, cwd=None,
        tool_name="review-loop", timeout=30,
    )
    notified = []

    monkeypatch.setattr(orchestrator, "_AUTH_EXPIRED_NOTIFIED", {"claude"})
    monkeypatch.setattr(orchestrator, "notify_auth_expired", lambda name: notified.append(name))
    monkeypatch.setattr(dispatcher, "select_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(queue_manager, "strip_metadata_tags", lambda text: text)
    monkeypatch.setattr(
        orchestrator, "_execute_tool_task",
        lambda *_args, **_kwargs: orchestrator.ToolTaskExecutionOutcome(
            success=True, finalized=True, retryable=False,
            error="", error_code="", output="done", output_tokens=10,
        ),
    )

    result = parallel_runner_module._run_single_subtask(
        subtask, idx=0, limits=AllLimits(), memory_context="", pause_event=None,
    )

    assert result.success is True
    assert "claude" not in orchestrator._AUTH_EXPIRED_NOTIFIED  # re-armed
    assert notified == []

    monkeypatch.setattr(
        orchestrator, "_execute_tool_task",
        lambda *_args, **_kwargs: orchestrator.ToolTaskExecutionOutcome(
            success=False, finalized=False, retryable=True,
            error="auth_expired", error_code="auth_expired",
        ),
    )
    result2 = parallel_runner_module._run_single_subtask(
        subtask, idx=0, limits=AllLimits(), memory_context="", pause_event=None,
    )

    assert result2.success is False
    assert notified == ["claude"]


def test_run_single_subtask_does_not_notify_on_other_errors(monkeypatch):
    """Gegenprobe: an unrelated failure (e.g. rate_limit) must not fire the
    auth-expired notice — the check is exact-code, not "any failure"."""
    import dispatcher
    import orchestrator

    provider = SimpleNamespace(name="claude")
    subtask = SubTask(
        text="Do the thing", provider_forced=None, cwd=None, tool_name=None, timeout=30,
    )
    notified = []

    monkeypatch.setattr(orchestrator, "_notify_auth_expired_once", lambda name: notified.append(name))
    monkeypatch.setattr(dispatcher, "select_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(queue_manager, "strip_metadata_tags", lambda text: text)
    monkeypatch.setattr(orchestrator, "_build_prompt", lambda *_args, **_kwargs: "prompt")
    monkeypatch.setattr(
        orchestrator, "_run_with_retry",
        lambda *_args, **_kwargs: (SimpleNamespace(success=False, output="", error="rate_limit"), True),
    )

    result = parallel_runner_module._run_single_subtask(
        subtask, idx=0, limits=AllLimits(), memory_context="", pause_event=None,
    )

    assert result.success is False
    assert notified == []
