"""Clean-worktree precondition for tools that produce the diff they review.

Measured 2026-09-03/04: `nightstash` (22:02-22:45, `#tool:dev-loop`) finished and
left the repo on its own branch. Two hours later `nightfloor` started in the SAME
repo, and its Quality reviewer refused the output format with "Task und Working
Tree passen nicht zusammen ... Branch: night/stash-pruning" — which tipped the run
into `format_error` and, three attempts later, burned the whole retry budget. The
runs did not overlap: this is a missing reset between sequential tasks, not a race.

Every task order said "start with `git switch -c night/xyz master`, abort on an
unclean tree" — but that lived in the PROMPT, i.e. a fail-open guard that nothing
enforced.

These tests use REAL git repositories rather than a mocked `subprocess.run`: the
guard exists to notice a real-world repo state, and a mock would only prove that
the code calls the functions it calls.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import orchestrator
import taxonomy

# ---------------------------------------------------------------------------
# Real git fixtures
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo), check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "clean")


@pytest.fixture
def dirty_repo(tmp_path: Path) -> Path:
    repo = _make_repo(tmp_path / "dirty")
    (repo / "README.md").write_text("hello, uncommitted\n", encoding="utf-8")
    return repo


class _FakeTool:
    def __init__(self, name: str, requires_clean_worktree: bool):
        self.name = name
        self.requires_clean_worktree = requires_clean_worktree


# ---------------------------------------------------------------------------
# _worktree_gate_violation — scope and verdict
# ---------------------------------------------------------------------------

def test_dev_loop_declares_the_requirement():
    """Scope hangs on the TOOL, not on a tag the user has to remember."""
    from tools.dev_loop import DevLoopTool
    from tools.review_loop import ReviewLoopTool

    assert DevLoopTool.requires_clean_worktree is True
    # review-loop CONSUMES an existing diff — demanding a clean tree there would
    # be the exact opposite of what the tool is for.
    assert ReviewLoopTool.requires_clean_worktree is False


def test_gate_blocks_a_dirty_repo(monkeypatch, dirty_repo):
    monkeypatch.setattr(orchestrator, "get_tool", lambda _n: _FakeTool("dev-loop", True))
    msg = orchestrator._worktree_gate_violation("Task #tool:dev-loop", "dev-loop", str(dirty_repo))
    assert msg is not None
    assert "uncommitted changes" in msg
    assert str(dirty_repo) in msg
    # The branch is part of the message: the measured symptom was a foreign branch,
    # and the reader needs to see which state the repo was actually left in.
    assert "Branch:" in msg


def test_gate_passes_a_clean_repo(monkeypatch, clean_repo):
    monkeypatch.setattr(orchestrator, "get_tool", lambda _n: _FakeTool("dev-loop", True))
    assert orchestrator._worktree_gate_violation(
        "Task #tool:dev-loop", "dev-loop", str(clean_repo)
    ) is None


def test_gate_blocks_a_directory_that_is_no_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "get_tool", lambda _n: _FakeTool("dev-loop", True))
    plain = tmp_path / "plain"
    plain.mkdir()
    msg = orchestrator._worktree_gate_violation("Task #tool:dev-loop", "dev-loop", str(plain))
    assert msg is not None and "not a git repository" in msg


def test_gate_ignores_a_tool_that_does_not_require_it(monkeypatch, dirty_repo):
    monkeypatch.setattr(orchestrator, "get_tool", lambda _n: _FakeTool("review-loop", False))
    assert orchestrator._worktree_gate_violation(
        "Task #tool:review-loop", "review-loop", str(dirty_repo)
    ) is None


def test_gate_ignores_a_task_without_a_tool(dirty_repo):
    """The `nightlovelace` regression guard.

    That task ran plain (no `#tool:`) in D:\\programmieren\\privat_python\\haus — a
    repo that permanently carries uncommitted changes from parallel sessions — and
    was explicitly ordered NOT to commit or write. It ran correctly and must keep
    running.
    """
    assert orchestrator._worktree_gate_violation("Plain task", None, str(dirty_repo)) is None


def test_allow_dirty_waives_the_gate(monkeypatch, dirty_repo):
    monkeypatch.setattr(orchestrator, "get_tool", lambda _n: _FakeTool("dev-loop", True))
    assert orchestrator._worktree_gate_violation(
        "Task #tool:dev-loop #allow-dirty", "dev-loop", str(dirty_repo)
    ) is None


def test_gate_blocks_when_there_is_no_cwd(monkeypatch):
    """"No cwd" is not "no repo" — it is an UNVERIFIABLE repo, so it is refused.

    providers/process_runner._spawn() hands cwd=None straight to Popen, which
    inherits the orchestrator's own working directory: a dev-loop task without a
    cwd: tag runs against whatever repo the orchestrator was started in. A guard
    that cannot check must not pass.
    """
    monkeypatch.setattr(orchestrator, "get_tool", lambda _n: _FakeTool("dev-loop", True))
    msg = orchestrator._worktree_gate_violation("Task #tool:dev-loop", "dev-loop", None)
    assert msg is not None and "cwd" in msg


def test_no_cwd_block_is_still_waivable(monkeypatch):
    monkeypatch.setattr(orchestrator, "get_tool", lambda _n: _FakeTool("dev-loop", True))
    assert orchestrator._worktree_gate_violation(
        "Task #tool:dev-loop #allow-dirty", "dev-loop", None
    ) is None


def test_spawn_inherits_the_orchestrator_cwd_when_none_is_given():
    """The evidence behind the rule above, pinned so it cannot rot silently."""
    import inspect

    from providers import process_runner

    src = inspect.getsource(process_runner._spawn)
    # cwd is passed through to Popen untouched; Popen with cwd=None inherits.
    assert "cwd=cwd" in src


def test_allow_dirty_tag_is_stripped_from_the_prompt():
    from queue_manager import has_allow_dirty_tag, strip_metadata_tags

    assert has_allow_dirty_tag("Task #tool:dev-loop #allow-dirty") is True
    assert has_allow_dirty_tag("Task #tool:dev-loop") is False
    assert strip_metadata_tags("Do it #tool:dev-loop #allow-dirty") == "Do it"


def test_worktree_dirty_has_its_own_taxonomy_category():
    assert taxonomy._ERROR_CODE_MAP["worktree_dirty"] == taxonomy.CAT_WORKTREE
    assert taxonomy.CAT_WORKTREE in taxonomy.ALL_CATEGORIES


# ---------------------------------------------------------------------------
# run_once — the task must not START, and must not release its dependents
# ---------------------------------------------------------------------------

def _queue_run(tmp_path, monkeypatch, repo: Path, task_line: str):
    """Drive run_once() over a REAL queue file against a REAL repo."""
    import queue_manager

    q_file = tmp_path / "agent-queue.md"
    q_file.write_text("## Queue\n" + task_line + "\n", encoding="utf-8")
    monkeypatch.setattr(queue_manager, "QUEUE_FILE", q_file)

    exec_mock = Mock(return_value=orchestrator.ToolTaskExecutionOutcome(
        success=True, finalized=True,
    ))
    select_mock = Mock(return_value=SimpleNamespace(name="claude", set_cooldown=Mock()))

    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: str(repo))
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", select_mock)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr(orchestrator, "cleanup_done_tasks", lambda *a, **kw: 0)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_task_started", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_task_done", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)
    return q_file, exec_mock, select_mock


def test_run_once_does_not_start_dev_loop_in_a_dirty_repo(tmp_path, monkeypatch, dirty_repo):
    q_file, exec_mock, select_mock = _queue_run(
        tmp_path, monkeypatch, dirty_repo, "- [ ] Fix it #id:nightfloor #tool:dev-loop",
    )

    orchestrator.run_once()

    exec_mock.assert_not_called()
    # Gated BEFORE provider selection — an unattended run must not spend a token
    # on a task whose result cannot be trusted.
    select_mock.assert_not_called()

    line = q_file.read_text(encoding="utf-8").splitlines()[1]
    assert line.startswith("- [x]"), "must leave the queue, not be requeued forever: " + line
    assert "\u274c" in line, "must be stamped as failed: " + line

    # ... and therefore does not release a dependent.
    import queue_manager
    assert queue_manager._collect_completed_ids(
        q_file.read_text(encoding="utf-8")
    ) == set()


def test_run_once_runs_dev_loop_in_a_clean_repo(tmp_path, monkeypatch, clean_repo):
    """The gate must not become a blanket block on dev-loop."""
    _q_file, exec_mock, _select = _queue_run(
        tmp_path, monkeypatch, clean_repo, "- [ ] Fix it #tool:dev-loop",
    )

    orchestrator.run_once()

    exec_mock.assert_called_once()


def test_run_once_runs_a_plain_task_in_a_dirty_repo(tmp_path, monkeypatch, dirty_repo):
    """`nightlovelace` end to end: no tool tag, permanently dirty repo, still runs."""
    q_file, _exec, select_mock = _queue_run(
        tmp_path, monkeypatch, dirty_repo, "- [ ] Read the dashboards, write nothing",
    )
    run_mock = Mock(return_value=SimpleNamespace(
        success=True, output="done", error=None, input_tokens=0, output_tokens=0,
        cache_creation_input_tokens=0, cache_read_input_tokens=0, session_id=None,
    ))
    monkeypatch.setattr(
        orchestrator, "_run_with_retry", lambda *a, **kw: (run_mock(), False),
    )
    monkeypatch.setattr(orchestrator, "_git_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "TRACK_FILE_CHANGES", False)

    orchestrator.run_once()

    select_mock.assert_called()          # it got as far as picking a provider
    assert "\u274c" not in q_file.read_text(encoding="utf-8")


def test_run_once_honours_allow_dirty(tmp_path, monkeypatch, dirty_repo):
    _q_file, exec_mock, _select = _queue_run(
        tmp_path, monkeypatch, dirty_repo, "- [ ] Fix it #tool:dev-loop #allow-dirty",
    )

    orchestrator.run_once()

    exec_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Parallel subtasks are the one route around run_once() — they must be gated too
#
# run_once() exempts the #parallel PARENT because the parent's own #tool: tag is
# not what runs. parallel_runner._run_single_subtask() calls _execute_tool_task()
# directly (parallel_runner.py, "Tool-based subtask" branch), so without a check
# there a dev-loop subtask in a dirty repo would start unchecked.
# ---------------------------------------------------------------------------

def test_parallel_subtask_with_dev_loop_is_blocked_on_a_dirty_repo(monkeypatch, dirty_repo):
    import parallel_runner
    from limits import AllLimits

    select_mock = Mock(side_effect=AssertionError("provider must not be selected"))
    exec_mock = Mock(side_effect=AssertionError("subtask must not execute"))
    monkeypatch.setattr("dispatcher.select_provider", select_mock)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)

    subtask = parallel_runner.SubTask(
        text="Fix it #tool:dev-loop", provider_forced=None, cwd=str(dirty_repo), tool_name="dev-loop",
        timeout=60,
    )
    result = parallel_runner._run_single_subtask(
        subtask, 0, AllLimits(), "", None,
    )

    assert result.success is False
    assert result.error.startswith("worktree_dirty")


def test_parallel_subtask_with_dev_loop_runs_on_a_clean_repo(monkeypatch, clean_repo):
    """The gate must not make every parallel dev-loop subtask fail."""
    import parallel_runner
    from limits import AllLimits

    provider = SimpleNamespace(name="claude", set_cooldown=Mock())
    exec_mock = Mock(return_value=orchestrator.ToolTaskExecutionOutcome(
        success=True, finalized=False, output="done",
    ))
    monkeypatch.setattr("dispatcher.select_provider", lambda *a, **kw: provider)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", exec_mock)
    monkeypatch.setattr("config.model_id_for_provider", lambda *a, **kw: None)

    subtask = parallel_runner.SubTask(
        text="Fix it #tool:dev-loop", provider_forced=None, cwd=str(clean_repo), tool_name="dev-loop",
        timeout=60,
    )
    result = parallel_runner._run_single_subtask(subtask, 0, AllLimits(), "", None)

    assert result.success is True
    exec_mock.assert_called_once()


def test_parallel_plain_subtask_in_a_dirty_repo_is_untouched(monkeypatch, dirty_repo):
    """No tool tag → no gate, exactly like the single-task path."""
    import parallel_runner
    from limits import AllLimits

    provider = SimpleNamespace(name="claude", set_cooldown=Mock())
    monkeypatch.setattr("dispatcher.select_provider", lambda *a, **kw: provider)
    monkeypatch.setattr("config.model_id_for_provider", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_build_prompt", lambda *a, **kw: "prompt")
    monkeypatch.setattr(orchestrator, "_run_with_retry", lambda *a, **kw: (
        SimpleNamespace(
            success=True, output="done", error=None, input_tokens=0, output_tokens=0,
            cache_creation_input_tokens=0, cache_read_input_tokens=0, session_id=None,
        ),
        False,
    ))

    subtask = parallel_runner.SubTask(
        text="Read the dashboards", provider_forced=None, cwd=str(dirty_repo), tool_name=None, timeout=60,
    )
    result = parallel_runner._run_single_subtask(subtask, 0, AllLimits(), "", None)

    assert result.success is True
