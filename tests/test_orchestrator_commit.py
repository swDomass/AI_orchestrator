"""Wiring tests for the new post-run auto-commit (`.dev-loop/auftrag.md`).

`_commit_run_changes()` / `_with_commit_note()` live in `orchestrator.py`; the git
mechanics themselves live in `git_commit.commit_run_result()` and are covered by
`tests/test_git_commit.py`. Here `git_commit.commit_run_result` is MOCKED — the
subject under test is the WIRING (when is it called, with what arguments, in what
order relative to finalize/verify, and what happens to the task when it fails or
raises), not git behaviour.

Both success paths are exercised the way the neighbouring `tests/test_orchestrator_
tool_tasks.py` already does: the `#tool:` path via a direct `_execute_tool_task()`
call, the single-shot path via `orchestrator.run_once()` over a real (tmp_path)
queue file. Same monkeypatch-per-collaborator style, no new patterns.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import git_commit
import notifier
import orchestrator
from tools.base_tool import ToolResult

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

def _make_tool(*, name="dev-loop", description="Dev loop", read_only=False,
                success=True, output="did the work", iterations=1):
    tool = Mock()
    tool.name = name
    tool.description = description
    tool.read_only = read_only
    tool.run.return_value = ToolResult(success=success, output=output, iterations=iterations)
    return tool


#: Der Nachher-Stand, den `_two_stage_snapshot` ab dem zweiten Aufruf liefert.
AFTER_SNAPSHOT = {"f.txt": (9.0, 9), "neu.txt": (9.0, 3)}


def _two_stage_snapshot(before):
    """`_snapshot_dir`-Ersatz, der VORHER und NACHHER unterscheidbar macht.

    Der erste Aufruf (vor dem Lauf) liefert `before`, jeder weitere den
    Nachher-Stand. Ein einziges konstantes Lambda machte `snap_before` und
    `snap_after` identisch -- damit war jede Assertion ueber die beiden dieselbe
    Aussage, und eine Mutation, die `snap_after=snap_before` uebergibt (der Commit
    saehe den Vorher-Stand und committete nichts), blieb gruen. Im adversarialen
    Review gefunden.
    """
    calls = {"n": 0}

    def _snapshot(*_a, **_kw):
        calls["n"] += 1
        return before if calls["n"] == 1 else AFTER_SNAPSHOT

    return _snapshot


def _wire_tool_success_path(monkeypatch, *, tool, commit_mock, verify_outcome=None,
                             notify_done_mock=None, restamp_mock=None,
                             finalize_mock=None, track_file_changes=True,
                             snapshot=None, change_summary=""):
    """Common collaborators for a direct `_execute_tool_task()` call on the
    happy path (tool.run succeeds). Mirrors the mocking style already used in
    tests/test_orchestrator_tool_tasks.py for this same function."""
    if verify_outcome is None:
        verify_outcome = orchestrator.VerifyOutcome()
    if snapshot is None:
        snapshot = {"f.txt": (1.0, 1)}
    if finalize_mock is None:
        finalize_mock = Mock(return_value=True)

    monkeypatch.setattr(orchestrator, "get_tool", lambda _name: tool)
    monkeypatch.setattr(orchestrator, "load_skill", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_is_git_repo", lambda *a, **kw: True)
    monkeypatch.setattr(orchestrator, "_git_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "TRACK_FILE_CHANGES", track_file_changes)
    monkeypatch.setattr(orchestrator, "_snapshot_dir", _two_stage_snapshot(snapshot))
    monkeypatch.setattr(orchestrator, "_get_change_summary", lambda *a, **kw: change_summary)
    monkeypatch.setattr(orchestrator, "strip_metadata_tags", lambda task: task)
    monkeypatch.setattr(orchestrator, "finalize_task_with_result", finalize_mock)
    monkeypatch.setattr(orchestrator, "_verify_task_result", lambda *a, **kw: verify_outcome)
    monkeypatch.setattr(orchestrator.memory_module, "store_result", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_task_done", notify_done_mock or Mock())
    monkeypatch.setattr(orchestrator, "restamp_done_as_failed", restamp_mock or Mock(return_value=True))
    monkeypatch.setattr(orchestrator.git_commit, "commit_run_result", commit_mock)
    return finalize_mock


def _write_queue(tmp_path, monkeypatch, task_line):
    import queue_manager

    q_file = tmp_path / "agent-queue.md"
    q_file.write_text(f"## Queue\n- [ ] {task_line}\n", encoding="utf-8")
    monkeypatch.setattr(queue_manager, "QUEUE_FILE", q_file)
    return q_file


def _wire_single_shot_success(monkeypatch, *, cwd, commit_mock, verify_outcome=None,
                               notify_done_mock=None, restamp_mock=None,
                               finalize_mock=None, snapshot=None, change_summary="",
                               result_output="done", provider_name="claude"):
    """Common collaborators for a `run_once()` single-shot success run. Same
    shape as `_verify_run(tool=False)` in test_orchestrator_tool_tasks.py, with
    a real (non-None) cwd so the commit call args can be asserted."""
    if verify_outcome is None:
        verify_outcome = orchestrator.VerifyOutcome()
    if snapshot is None:
        snapshot = {"f.txt": (1.0, 1)}
    if finalize_mock is None:
        finalize_mock = Mock(return_value=True)

    p1 = SimpleNamespace(name=provider_name, set_cooldown=Mock())

    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _task: cwd)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _task: None)
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: p1)
    monkeypatch.setattr(orchestrator, "_build_prompt", lambda *a, **kw: "prompt")
    monkeypatch.setattr(orchestrator, "_run_with_retry", lambda *a, **kw: (
        SimpleNamespace(
            success=True, output=result_output, error=None,
            input_tokens=1, output_tokens=1,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
            session_id=None,
        ),
        False,
    ))
    monkeypatch.setattr(orchestrator, "_is_git_repo", lambda *a, **kw: True)
    monkeypatch.setattr(orchestrator, "TRACK_FILE_CHANGES", True)
    monkeypatch.setattr(orchestrator, "_snapshot_dir", _two_stage_snapshot(snapshot))
    monkeypatch.setattr(orchestrator, "_git_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_get_change_summary", lambda *a, **kw: change_summary)
    monkeypatch.setattr(orchestrator, "_verify_task_result", lambda *a, **kw: verify_outcome)
    monkeypatch.setattr(orchestrator, "_pin_verify_script", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "finalize_task_with_result", finalize_mock)
    monkeypatch.setattr(orchestrator.memory_module, "store_result", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "cleanup_done_tasks", lambda *a, **kw: 0)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_task_started", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_task_done", notify_done_mock or Mock())
    monkeypatch.setattr(orchestrator, "restamp_done_as_failed", restamp_mock or Mock(return_value=True))
    monkeypatch.setattr(orchestrator.git_commit, "commit_run_result", commit_mock)
    return p1, finalize_mock


# ---------------------------------------------------------------------------
# 1. Both success paths commit exactly once, with the right args
# ---------------------------------------------------------------------------

def test_tool_path_commits_once_with_correct_args(monkeypatch):
    task = "Refactor the widget #tool:dev-loop #id:widget"
    cwd = "D:/fake/repo"
    tool = _make_tool(name="dev-loop", output="widget refactored")
    snapshot = {"f.txt": (1.0, 1)}
    commit_mock = Mock(return_value=git_commit.CommitOutcome(
        branch="orch/widget-2026-09-10", sha="deadbeef12", files=2,
    ))
    _wire_tool_success_path(monkeypatch, tool=tool, commit_mock=commit_mock, snapshot=snapshot)

    outcome = orchestrator._execute_tool_task(
        task, "dev-loop", SimpleNamespace(name="claude"), cwd=cwd, timeout=10,
    )

    assert outcome.success is True
    commit_mock.assert_called_once()
    args, kwargs = commit_mock.call_args
    assert args[0] == cwd
    assert args[1] == task                 # raw task text, tags included
    assert args[2] == "claude+dev-loop"    # provider_tool label
    assert args[3] == snapshot, "snap_before ist der Stand VOR dem Lauf"
    assert kwargs["snap_after"] == AFTER_SNAPSHOT, "snap_after ist der Stand DANACH"
    assert kwargs["snap_after"] != args[3], "die beiden duerfen nicht dasselbe sein"


def test_single_shot_path_commits_once_with_correct_args(tmp_path, monkeypatch):
    task_line = "Write the report #id:report"
    cwd = "D:/fake/single-repo"
    _write_queue(tmp_path, monkeypatch, task_line)
    snapshot = {"g.txt": (2.0, 2)}
    commit_mock = Mock(return_value=git_commit.CommitOutcome(
        branch="orch/report-2026-09-10", sha="cafef00d", files=1,
    ))
    _wire_single_shot_success(
        monkeypatch, cwd=cwd, commit_mock=commit_mock, snapshot=snapshot,
        result_output="report written",
    )

    orchestrator.run_once()

    commit_mock.assert_called_once()
    args, kwargs = commit_mock.call_args
    assert args[0] == cwd
    assert args[1] == task_line     # raw queue task text
    assert args[2] == "claude"      # bare provider.name for the single-shot path
    assert args[3] == snapshot, "snap_before ist der Stand VOR dem Lauf"
    assert kwargs["snap_after"] == AFTER_SNAPSHOT, "snap_after ist der Stand DANACH"
    assert kwargs["snap_after"] != args[3], "die beiden duerfen nicht dasselbe sein"


# ---------------------------------------------------------------------------
# 2. #no-commit prevents the call outright
# ---------------------------------------------------------------------------

def test_no_commit_tag_prevents_commit_call(monkeypatch):
    task = "Do it #tool:dev-loop #no-commit"
    cwd = "D:/fake/repo"
    tool = _make_tool()
    commit_mock = Mock()
    _wire_tool_success_path(monkeypatch, tool=tool, commit_mock=commit_mock)

    outcome = orchestrator._execute_tool_task(
        task, "dev-loop", SimpleNamespace(name="claude"), cwd=cwd,
    )

    assert outcome.success is True
    commit_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 3. A red #verify: prevents the commit and still restamps
# ---------------------------------------------------------------------------

def test_red_verify_prevents_commit_and_restamps(monkeypatch):
    task = "Ship it #tool:dev-loop #verify:check.ps1"
    cwd = "D:/fake/repo"
    tool = _make_tool()
    commit_mock = Mock()
    restamp_mock = Mock(return_value=True)
    notify_done_mock = Mock()
    verify_outcome = orchestrator.VerifyOutcome(ok=False, note="\n\n[verify] artefact missing")
    _wire_tool_success_path(
        monkeypatch, tool=tool, commit_mock=commit_mock, verify_outcome=verify_outcome,
        restamp_mock=restamp_mock, notify_done_mock=notify_done_mock,
    )

    outcome = orchestrator._execute_tool_task(
        task, "dev-loop", SimpleNamespace(name="claude"), cwd=cwd,
    )

    assert outcome.verify_failed is True
    commit_mock.assert_not_called()
    restamp_mock.assert_called_once()
    notify_done_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 4. No #verify: tag at all -> VerifyOutcome() defaults to ok=True -> commits
# ---------------------------------------------------------------------------

def test_missing_verify_tag_still_commits(monkeypatch):
    """No `#verify:` tag means `_verify_task_result` never even looks for one and
    returns the default `VerifyOutcome()` (`ok=True`) -- which the commit gate
    treats identically to an explicit green check."""
    task = "Do it #tool:dev-loop"
    cwd = "D:/fake/repo"
    tool = _make_tool()
    commit_mock = Mock(return_value=git_commit.CommitOutcome(
        branch="orch/x-2026-09-10", sha="1234abcd", files=1,
    ))
    _wire_tool_success_path(
        monkeypatch, tool=tool, commit_mock=commit_mock,
        verify_outcome=orchestrator.VerifyOutcome(),
    )

    orchestrator._execute_tool_task(task, "dev-loop", SimpleNamespace(name="claude"), cwd=cwd)

    commit_mock.assert_called_once()


# ---------------------------------------------------------------------------
# 5. Ordering: commit happens AFTER finalize AND AFTER verify (both paths)
# ---------------------------------------------------------------------------

def test_commit_happens_after_finalize_and_verify_tool_path(monkeypatch):
    task = "Ship it #tool:dev-loop"
    cwd = "D:/fake/repo"
    tool = _make_tool()
    calls: list[str] = []

    def fake_finalize(*_a, **_kw):
        calls.append("finalize")
        return True

    def fake_verify(*_a, **_kw):
        calls.append("verify")
        return orchestrator.VerifyOutcome()

    def fake_commit(*_a, **_kw):
        calls.append("commit")
        return git_commit.CommitOutcome(branch="orch/x-2026-09-10", sha="abc123ff", files=1)

    _wire_tool_success_path(
        monkeypatch, tool=tool, commit_mock=Mock(side_effect=fake_commit),
        finalize_mock=Mock(side_effect=fake_finalize),
    )
    monkeypatch.setattr(orchestrator, "_verify_task_result", fake_verify)

    orchestrator._execute_tool_task(task, "dev-loop", SimpleNamespace(name="claude"), cwd=cwd)

    assert calls == ["finalize", "verify", "commit"], calls


def test_commit_happens_after_finalize_and_verify_single_shot(tmp_path, monkeypatch):
    task_line = "Ship the single-shot #id:sscommit"
    cwd = "D:/fake/single-repo"
    _write_queue(tmp_path, monkeypatch, task_line)
    calls: list[str] = []

    def fake_finalize(*_a, **_kw):
        calls.append("finalize")
        return True

    def fake_verify(*_a, **_kw):
        calls.append("verify")
        return orchestrator.VerifyOutcome()

    def fake_commit(*_a, **_kw):
        calls.append("commit")
        return git_commit.CommitOutcome(branch="orch/y-2026-09-10", sha="fedcba98", files=1)

    _wire_single_shot_success(
        monkeypatch, cwd=cwd, commit_mock=Mock(side_effect=fake_commit),
        finalize_mock=Mock(side_effect=fake_finalize),
    )
    monkeypatch.setattr(orchestrator, "_verify_task_result", fake_verify)

    orchestrator.run_once()

    assert calls == ["finalize", "verify", "commit"], calls


# ---------------------------------------------------------------------------
# 6. A read-only tool never commits
# ---------------------------------------------------------------------------

def test_read_only_tool_does_not_commit(monkeypatch):
    task = "Investigate #tool:research-qa"
    cwd = "D:/fake/repo"
    tool = _make_tool(name="research-qa", read_only=True)
    commit_mock = Mock()
    _wire_tool_success_path(monkeypatch, tool=tool, commit_mock=commit_mock)

    outcome = orchestrator._execute_tool_task(
        task, "research-qa", SimpleNamespace(name="claude"), cwd=cwd,
    )

    assert outcome.success is True
    commit_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 7. A #parallel subtask (skip_queue=True) never reaches the commit call
# ---------------------------------------------------------------------------

def test_parallel_subtask_with_skip_queue_never_commits(monkeypatch):
    """`skip_queue=True` (the shape `parallel_runner._run_single_subtask()` uses)
    leaves the caller responsible for finalization AND verify, so the whole
    `if not skip_queue:` block in `_execute_tool_task` -- finalize, verify,
    commit -- is skipped structurally. Nail both ends down: finalize must not
    run either, or this would silently stop being true."""
    task = "Subtask work #tool:dev-loop"
    cwd = "D:/fake/repo"
    tool = _make_tool()
    commit_mock = Mock()
    finalize_mock = Mock(return_value=True)
    _wire_tool_success_path(
        monkeypatch, tool=tool, commit_mock=commit_mock, finalize_mock=finalize_mock,
    )

    outcome = orchestrator._execute_tool_task(
        task, "dev-loop", SimpleNamespace(name="claude"), cwd=cwd, skip_queue=True,
    )

    assert outcome.success is True
    assert outcome.finalized is False
    finalize_mock.assert_not_called()
    commit_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 8. A commit failure does not turn the task red
# ---------------------------------------------------------------------------

def test_commit_failure_does_not_fail_the_task(monkeypatch):
    task = "Ship it #tool:dev-loop"
    cwd = "D:/fake/repo"
    tool = _make_tool(output="did the work")
    commit_mock = Mock(return_value=git_commit.CommitOutcome(error="boom"))
    restamp_mock = Mock(return_value=True)
    notify_done_mock = Mock()
    finalize_mock = Mock(return_value=True)
    _wire_tool_success_path(
        monkeypatch, tool=tool, commit_mock=commit_mock, restamp_mock=restamp_mock,
        notify_done_mock=notify_done_mock, finalize_mock=finalize_mock,
        change_summary="baseline changes",
    )

    outcome = orchestrator._execute_tool_task(
        task, "dev-loop", SimpleNamespace(name="claude"), cwd=cwd,
    )

    assert outcome.success is True
    assert outcome.verify_failed is False
    finalize_mock.assert_called_once()
    assert finalize_mock.call_args.kwargs["failed"] is False   # task finalized as a SUCCESS
    restamp_mock.assert_not_called()
    notify_done_mock.assert_called_once()
    change_summary = notify_done_mock.call_args.kwargs["change_summary"]
    assert "boom" in change_summary
    assert "Commit fehlgeschlagen" in change_summary


# ---------------------------------------------------------------------------
# 9. _with_commit_note puts the note in FRONT, so it survives the notifier's
#    500-char truncation of change_summary (notifier.py:124)
# ---------------------------------------------------------------------------

def test_commit_note_survives_notifier_truncation():
    note = "Commit: orch/widget-2026-09-10 (deadbeef, 3 Datei(en))"
    # Comfortably over notifier's 500-char cap, and none of it is the note text.
    long_summary = "\n".join(f"modified path/to/file_{i}.py" for i in range(60))
    assert len(long_summary) > 500

    combined = orchestrator._with_commit_note(note, long_summary)
    assert combined.startswith(note)

    truncated = notifier._truncate(combined, 500)
    assert note in truncated, truncated

    # The reason placement matters: appended at the end, the same note would be
    # cut off by the very truncation it now survives.
    appended = f"{long_summary}\n{note}"
    appended_truncated = notifier._truncate(appended, 500)
    assert note not in appended_truncated


def test_with_commit_note_falls_back_to_bare_summary_without_a_note():
    assert orchestrator._with_commit_note("", "some changes") == "some changes"


# ---------------------------------------------------------------------------
# 10. commit_run_result raising must not take the task down with it -- BEFUND:
#     as written, it does. Documented, not fixed (task instructions: report,
#     don't patch orchestrator.py).
# ---------------------------------------------------------------------------

def test_commit_run_result_raising_is_contained_and_reported(monkeypatch):
    """A raising commit must NOT take the task run down with it.

    git_commit.commit_run_result promises "never raises", but a promise is not a
    guarantee -- and the `_snapshot_dir(cwd)` call in its argument list runs
    OUTSIDE the module's own handler, so there is a second way in. Without
    containment in `_commit_run_changes`, the exception leaves `run_once()`
    entirely: on the `#tool:` path the only enclosing block is a try/FINALLY
    (restoring the forced model/effort), which does not catch.

    Auftrag KERN 3 (.dev-loop/auftrag.md): a failed commit is folgenlos for the
    task and surfaces as a WARNING. An escaping exception is the opposite -- it
    aborts the whole poll iteration and, unattended, charges the task a fruitless
    attempt on the process-crash breaker that it did not earn.

    Found by these wiring tests against the FIRST version of
    `_commit_run_changes`, which had no try/except at all.
    """
    task = "Ship it #tool:dev-loop"
    cwd = "D:/fake/repo"
    tool = _make_tool()
    commit_mock = Mock(side_effect=RuntimeError("boom"))
    notify = Mock()
    restamp = Mock(return_value=True)
    _wire_tool_success_path(
        monkeypatch, tool=tool, commit_mock=commit_mock,
        notify_done_mock=notify, restamp_mock=restamp,
    )

    outcome = orchestrator._execute_tool_task(
        task, "dev-loop", SimpleNamespace(name="claude"), cwd=cwd,
    )

    assert commit_mock.called, "der Commit-Versuch muss stattgefunden haben"
    assert outcome.success is True, "ein kaputter Commit darf den Task nicht rot machen"
    assert restamp.call_count == 0, "und darf die Zeile nicht auf ❌ umstempeln"
    assert notify.called, "die Erfolgsmeldung geht trotzdem raus"
    summary = notify.call_args.kwargs["change_summary"]
    assert "Commit fehlgeschlagen" in summary and "boom" in summary, summary


def test_commit_run_changes_lets_a_deliberate_abort_through(monkeypatch):
    """KeyboardInterrupt / SystemExit must still propagate.

    The containment above exists for BROKEN commits, not for deliberate aborts.
    A `#shutdown` task drives shutdown.py, which raises SystemExit -- swallowing
    it here would make an orderly shutdown depend on whether it happened to fire
    during a commit attempt. Same ordering rule as orchestrator.main() and
    git_commit.commit_run_result: the two abort types are re-raised BEFORE the
    catch-all, and that ordering is the whole safeguard.
    """
    monkeypatch.setattr(orchestrator, "has_no_commit_tag", lambda _t: False)
    monkeypatch.setattr(orchestrator, "_snapshot_dir", lambda *_a, **_kw: {})
    for exc_type in (KeyboardInterrupt, SystemExit):
        monkeypatch.setattr(
            orchestrator.git_commit, "commit_run_result",
            Mock(side_effect=exc_type()),
        )
        with pytest.raises(exc_type):
            orchestrator._commit_run_changes("t", "D:/fake/repo", "claude", {})


# ===========================================================================
# Ein `skipped` ist nicht immer harmlos (externer Review, Grok)
# ===========================================================================


def test_too_many_files_skip_reaches_the_morning_report(monkeypatch):
    """`too_many_files` heißt "es GAB Arbeit, sie liegt uncommittet im Baum".

    Vorher ging das nur auf `print` und `logger`: der Task war grün, die
    Telegram-Meldung ohne jeden Hinweis, und die Ursache wurde erst sichtbar,
    als der nächste `#tool:dev-loop` im selben Repo an `worktree_dirty` starb.
    """
    monkeypatch.setattr(orchestrator, "has_no_commit_tag", lambda _t: False)
    monkeypatch.setattr(orchestrator, "_snapshot_dir", lambda *_a, **_kw: {})
    monkeypatch.setattr(
        orchestrator.git_commit, "commit_run_result",
        Mock(return_value=orchestrator.git_commit.CommitOutcome(skipped="too_many_files")),
    )

    note = orchestrator._commit_run_changes("t", "D:/fake/repo", "claude", {})

    assert "Kein Commit" in note and "uncommittet" in note, note


def test_all_paths_foreign_staged_reaches_the_morning_report(monkeypatch):
    """Dasselbe, wenn ALLE Pfade fremden Index-Stand tragen.

    `nothing_git_visible` ist zweideutig: es bedeutet "kein Diff" (harmlos, still)
    ODER "es gab Kandidaten, aber jeder war gestaged" (nicht harmlos). Die Zahl in
    `skipped_paths` trennt die beiden Fälle.
    """
    monkeypatch.setattr(orchestrator, "has_no_commit_tag", lambda _t: False)
    monkeypatch.setattr(orchestrator, "_snapshot_dir", lambda *_a, **_kw: {})
    outcome = orchestrator.git_commit.CommitOutcome(
        skipped="nothing_git_visible", skipped_paths=3,
    )
    monkeypatch.setattr(
        orchestrator.git_commit, "commit_run_result", Mock(return_value=outcome),
    )
    assert "3" in orchestrator._commit_run_changes("t", "D:/fake/repo", "claude", {})

    # Gegenprobe: der harmlose Fall bleibt still.
    monkeypatch.setattr(
        orchestrator.git_commit, "commit_run_result",
        Mock(return_value=orchestrator.git_commit.CommitOutcome(skipped="nothing_git_visible")),
    )
    assert orchestrator._commit_run_changes("t", "D:/fake/repo", "claude", {}) == ""

    # Und "kein Repo" / "kein Diff" ebenfalls.
    for reason in ("not_a_repo", "no_changes", "disabled", "no_head", "no_snapshot"):
        monkeypatch.setattr(
            orchestrator.git_commit, "commit_run_result",
            Mock(return_value=orchestrator.git_commit.CommitOutcome(skipped=reason)),
        )
        assert orchestrator._commit_run_changes("t", "D:/fake/repo", "claude", {}) == "", reason
