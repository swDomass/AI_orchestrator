"""Traceback logging + process-crash circuit breaker (2026-09-10).

Reference case, measured 2026-09-09/10: a ``ValueError`` out of
``orchestrator._snapshot_dir`` escaped ``run_once``/``main``, the process exited
1, ``run_orchestrator.ps1`` restarted it into the SAME first queue task, and
that repeated 96 times between 20:09 and 07:38 — 91 of them in an unbroken
5-minute cadence, zero tasks executed. ``logs/orchestrator.log`` knew nothing
about it (``grep -c "path is on mount" logs/orchestrator.log`` == 0), so the
only copy of the traceback was the terminal the user happened to have open.

Two guarantees are pinned here:

1. an unexpected exception in the main run reaches the LOG FILE with its
traceback and the process exits 1 — and ``KeyboardInterrupt``/``SystemExit``
keep passing through untouched;
2. the same queue line cannot kill the process indefinitely: after
``MAX_HANG_RETRIES`` attributable crashes it is quarantined as
``- [x] … ❌ …``.

**A crash is not simulated here.** The attribution point IS a Python ``except``,
so a crash IS a ``raise``: these tests throw a real exception through the real
``run_once`` over a real queue file and let the real ``main()`` wrapper catch
it. No faked state file, no ``taskkill``, no ``sleep``.
"""
import logging
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import dispatcher
import doctor as doctor_module
import orchestrator
import parallel_runner
import queue_linter
import queue_manager

# ---------------------------------------------------------------------------
# Isolation — the suite is order-sensitive (`-p no:randomly` is load-bearing)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_process_globals():
    """Restore everything process-wide these tests touch.

    ``main()`` installs a thread excepthook and the breaker keeps module state;
    leaking either would make an unrelated test fail later and look like a bug
    in that test. Log handlers are NOT diffed here on purpose — pytest's own
    caplog handler is attached and detached around the test, and removing it
    from underneath the plugin is worse than the leak it would prevent. The one
    test that needs a real file handler installs and removes exactly its own.
    """
    saved_hook = threading.excepthook
    saved_in_flight = orchestrator._in_flight_task
    saved_level = logging.getLogger().level
    try:
        yield
    finally:
        threading.excepthook = saved_hook
        orchestrator._in_flight_task = saved_in_flight
        logging.getLogger().setLevel(saved_level)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

_CRASH_MESSAGE = "path is on mount 'D:', start on mount '\\\\.\\nul'"


def _real_queue_env(tmp_path, monkeypatch, queue_content, *, max_hang_retries=2):
    """Drive run_once()/main() over a REAL queue file and the real queue_manager.

    Local copy of ``tests/test_orchestrator_tool_tasks.py::_real_queue_run`` —
    the counter this feature writes lives IN the queue line, so it only shows
    against a real file and the real ``mark_retry``; and a cross-import between
    test modules would be a new pattern for this repo.
    """
    q_file = tmp_path / "agent-queue.md"
    q_file.write_text(queue_content, encoding="utf-8")
    monkeypatch.setattr(queue_manager, "QUEUE_FILE", q_file)

    monkeypatch.setattr(orchestrator, "_worktree_gate_violation", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "MAX_HANG_RETRIES", max_hang_retries)
    # Negative backoff → the retry marker lands in the past, so the next run
    # picks the task up again instead of the test having to sleep.
    monkeypatch.setattr(orchestrator, "HANG_RETRY_BACKOFF_SEC", -120)
    monkeypatch.setattr(orchestrator, "_get_next_retry_sec", lambda _limits: -120)
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(
        orchestrator, "select_provider",
        lambda *a, **kw: SimpleNamespace(name="claude", set_cooldown=Mock()),
    )
    monkeypatch.setattr(orchestrator, "cleanup_done_tasks", lambda *a, **kw: 0)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_task_started", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_task_done", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_providers_exhausted", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator.memory_module, "get_context_for_task", lambda *a, **kw: "")
    return q_file


def _crash_in_single_shot(tmp_path, monkeypatch, exc_factory=None):
    """Make the single-shot path raise at the ORIGINAL spot of the reference case.

    ``_snapshot_dir`` runs before provider selection, which is exactly why the
    real incident never executed a task and never produced a replay record.
    """
    def boom(_cwd):
        raise (exc_factory or (lambda: ValueError(_CRASH_MESSAGE)))()

    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _t: str(tmp_path))
    monkeypatch.setattr(orchestrator, "TRACK_FILE_CHANGES", True)
    monkeypatch.setattr(orchestrator, "_is_git_repo", lambda *a, **kw: False)
    monkeypatch.setattr(orchestrator, "_git_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_snapshot_dir", boom)


def _main_env(monkeypatch, argv=("orchestrator.py",)):
    """Neutralise everything main() does around _main(), except the crash net.

    ``setup_logging`` MUST be a no-op here: the real one hangs a
    RotatingFileHandler on the production ``logs/orchestrator.log``.
    """
    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.setattr(orchestrator, "setup_logging", lambda: None)
    monkeypatch.setattr(orchestrator, "install_thread_excepthook", lambda: None)
    monkeypatch.setattr(orchestrator, "ensure_queue_file", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "start_session", lambda *a, **kw: None)


def _run_main_expecting_crash() -> int:
    with pytest.raises(SystemExit) as excinfo:
        orchestrator.main()
    return excinfo.value.code


def _hang_count(q_file) -> int:
    return queue_manager.extract_hang_count(q_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1-2 — the crash is attributed on every execution path (KERN 3 / A4)
# ---------------------------------------------------------------------------

def test_single_shot_crash_is_charged_to_the_running_task(tmp_path, monkeypatch):
    """Plan test 1 — the reference case, at its original crash site.

    The single-shot path is where the 96 crashes happened. One crash must leave
    the task open with `<!-- hang: 1 -->`, not with a reset or absent counter.
    """
    q_file = _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task\n")
    _crash_in_single_shot(tmp_path, monkeypatch)
    _main_env(monkeypatch)

    assert _run_main_expecting_crash() == 1

    content = q_file.read_text(encoding="utf-8")
    assert "<!-- hang: 1 -->" in content, content
    assert content.lstrip().startswith("## Queue")
    assert "- [ ] Absturz-Task" in content, "task must stay open after crash 1"


def test_tool_task_crash_is_charged_too(tmp_path, monkeypatch):
    """Plan test 2 — the `#tool:` route.

    `_execute_tool_task` is also the entry point `parallel_runner` calls for
    every subtask, so the register has to be armed for it as well — that is the
    same blind spot the worktree gate had to close afterwards.
    """
    q_file = _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Tool-Task #tool:dev-loop\n")

    def boom(*_a, **_kw):
        raise RuntimeError("tool exploded")

    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _t: None)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", boom)
    _main_env(monkeypatch)

    assert _run_main_expecting_crash() == 1
    assert _hang_count(q_file) == 1


# ---------------------------------------------------------------------------
# 3-5 — the bound (DONE 2 / A3, A5, A6)
# ---------------------------------------------------------------------------

def test_third_crash_quarantines_the_task(tmp_path, monkeypatch, caplog):
    """Plan test 3 — MAX_HANG_RETRIES=2 means the THIRD dead attempt is terminal.

    "Quarantined" is the queue state that already exists: `- [x] … ❌ …`. It
    leaves OPEN_TASK_RE (so the watchdog cannot restart into it), keeps its line
    for the 48 h archiver, and stays reopenable via `/retry`.
    """
    q_file = _real_queue_env(
        tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task #id:crashy\n",
        max_hang_retries=2,
    )
    _crash_in_single_shot(tmp_path, monkeypatch)
    _main_env(monkeypatch)

    with caplog.at_level(logging.INFO):
        assert _run_main_expecting_crash() == 1          # hang: 1
        assert _hang_count(q_file) == 1
        assert _run_main_expecting_crash() == 1          # hang: 2
        assert _hang_count(q_file) == 2
        assert _run_main_expecting_crash() == 1          # 3 > 2 → quarantine

    line = next(
        ln for ln in q_file.read_text(encoding="utf-8").splitlines()
        if "Absturz-Task" in ln
    )
    assert queue_manager.line_is_failed(line), line
    assert line.startswith("- [x] "), line
    assert not any("Absturz-Task" in t.task_text for t in queue_manager.read_queue_items())

    # A3: the log line has to NAME the task and say why.
    quarantine = [
        r.getMessage() for r in caplog.records
        if "quarantäniert" in r.getMessage()
    ]
    assert quarantine, [r.getMessage() for r in caplog.records]
    assert "Absturz-Task" in quarantine[-1]
    assert "#id:crashy" in quarantine[-1]
    assert "3. erfolgloser Versuch" in quarantine[-1]


def test_quarantined_task_satisfies_no_dependency(tmp_path, monkeypatch):
    """Plan test 4 (A5) — ❌ must not release a `#needs:` dependent.

    This is the failure mode of 2026-09-04 in a new costume: a task that ended
    badly but was stamped ✅ released a dependent `#shutdown` task and the
    machine powered off with the fix unlanded.
    """
    q_file = _real_queue_env(
        tmp_path, monkeypatch,
        "## Queue\n- [ ] Absturz-Task #id:crashy\n- [ ] Folge-Task #needs:crashy\n",
        max_hang_retries=2,
    )
    _crash_in_single_shot(tmp_path, monkeypatch)
    _main_env(monkeypatch)

    for _ in range(3):
        assert _run_main_expecting_crash() == 1

    # Both halves, or the assertion below is satisfied by the task merely still
    # being open: the line must actually carry the terminal ❌ stamp …
    crash_line = next(
        ln for ln in q_file.read_text(encoding="utf-8").splitlines()
        if "Absturz-Task" in ln
    )
    assert queue_manager.line_is_failed(crash_line), crash_line

    # … and the dependent must still be blocked by it.
    items = queue_manager.read_queue_items()
    follow = next(t for t in items if "Folge-Task" in t.task_text)
    assert "crashy" in follow.blocked_reason, follow.blocked_reason


def test_crashes_share_the_hang_counter_with_format_errors(tmp_path, monkeypatch):
    """Plan test 5 (A6) — one format error + two crashes = quarantine, not 5 tries.

    The counter is deliberately SHARED with hang/format_error rather than being
    a second persistent marker. The consequence has to be visible: the
    unattended budget stays at three dead attempts on one task, it does not
    become 3+N.
    """
    q_file = _real_queue_env(
        tmp_path, monkeypatch, "## Queue\n- [ ] Tool-Task #tool:dev-loop\n",
        max_hang_retries=2,
    )
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _t: None)

    calls = {"n": 0}

    def flaky(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return orchestrator.ToolTaskExecutionOutcome(
                success=False, finalized=False, retryable=True,
                error="format", error_code="format_error",
            )
        raise ValueError(_CRASH_MESSAGE)

    monkeypatch.setattr(orchestrator, "_execute_tool_task", flaky)
    _main_env(monkeypatch)

    orchestrator.main()                       # attempt 1: format_error → hang: 1
    assert _hang_count(q_file) == 1
    assert _run_main_expecting_crash() == 1   # attempt 2: crash → hang: 2
    assert _hang_count(q_file) == 2
    assert _run_main_expecting_crash() == 1   # attempt 3: crash → quarantine

    line = next(
        ln for ln in q_file.read_text(encoding="utf-8").splitlines()
        if "Tool-Task" in ln
    )
    assert queue_manager.line_is_failed(line), line
    assert calls["n"] == 3, "budget grew beyond three dead attempts"


# ---------------------------------------------------------------------------
# 6-11 — what must NOT be charged (A2, A7)
# ---------------------------------------------------------------------------

def test_keyboard_interrupt_charges_nothing(tmp_path, monkeypatch):
    """Plan test 6 (A2/A7) — Ctrl+C is a user decision, never a failed attempt.

    Structural, not heuristic: the `except (KeyboardInterrupt, SystemExit)`
    clause sits AHEAD of the BaseException clause, so the charging code is never
    reached. A persistent breadcrumb file could not make this distinction at
    all — 1 of the 97 process ends that night was exactly this.
    """
    q_file = _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task\n")
    _crash_in_single_shot(tmp_path, monkeypatch, exc_factory=KeyboardInterrupt)
    _main_env(monkeypatch)

    with pytest.raises(KeyboardInterrupt):
        orchestrator.main()

    content = q_file.read_text(encoding="utf-8")
    assert "<!-- hang:" not in content, content
    assert content == "## Queue\n- [ ] Absturz-Task\n"


def test_system_exit_passes_its_code_through(tmp_path, monkeypatch):
    """Plan test 7 (A2) — sys.exit() inside a task keeps its own exit code.

    Turning a deliberate `sys.exit(3)` into the breaker's `sys.exit(1)` would
    silently rewrite every exit contract in the process.
    """
    q_file = _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task\n")
    _crash_in_single_shot(tmp_path, monkeypatch, exc_factory=lambda: SystemExit(3))
    _main_env(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        orchestrator.main()

    assert excinfo.value.code == 3
    assert "<!-- hang:" not in q_file.read_text(encoding="utf-8")


def test_doctor_and_lint_queue_exit_codes_survive_the_wrapper(monkeypatch):
    """Plan test 7 / risk 5 — the main() split must not eat CLI exit codes.

    `--doctor` and `--lint-queue` report through `sys.exit(...)`, which now
    passes through a wrapper that also catches exceptions. Pinned rather than
    assumed.
    """
    _main_env(monkeypatch, argv=("orchestrator.py", "--lint-queue"))
    monkeypatch.setattr(queue_linter, "run_lint", lambda *a, **kw: 2)
    with pytest.raises(SystemExit) as excinfo:
        orchestrator.main()
    assert excinfo.value.code == 2

    _main_env(monkeypatch, argv=("orchestrator.py", "--doctor"))
    monkeypatch.setattr(doctor_module, "run_doctor", lambda *a, **kw: False)
    with pytest.raises(SystemExit) as excinfo:
        orchestrator.main()
    assert excinfo.value.code == 1


def test_crash_outside_a_task_charges_nothing(tmp_path, monkeypatch, caplog):
    """Plan test 8 (A7) — a crash before/after the task loop hits no queue line.

    `read_queue_items()` itself raising is the shape here: nothing was in
    flight, so nothing may be counted, and the log has to say so instead of
    staying silent.
    """
    q_file = _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task\n")

    def boom():
        raise ValueError("vault offline")

    monkeypatch.setattr(orchestrator, "read_queue_items", boom)
    _main_env(monkeypatch)

    with caplog.at_level(logging.INFO):
        assert _run_main_expecting_crash() == 1

    assert q_file.read_text(encoding="utf-8") == "## Queue\n- [ ] Absturz-Task\n"
    assert any("außerhalb eines Queue-Tasks" in r.getMessage() for r in caplog.records)


def test_crash_after_finalization_is_not_charged_to_the_closed_line(tmp_path, monkeypatch, caplog):
    """Plan test 9 — a line that is already `[x]` cannot be re-opened by a crash.

    The queue write is the guard: `mark_retry` matches `- [ ] <text>`, finds
    nothing, returns False, and the handler logs a warning instead of inventing
    an open task. Driven directly against the register because the alternative
    is a contrived run whose task both succeeds and crashes.
    """
    q_file = _real_queue_env(
        tmp_path, monkeypatch,
        "## Queue\n- [x] Fertiger Task ✅ 2026-09-10 03:00 (claude)\n",
    )
    before = q_file.read_text(encoding="utf-8")

    orchestrator._set_in_flight(SimpleNamespace(
        task_text="Fertiger Task", line_no=2,
        raw_line="- [x] Fertiger Task ✅ 2026-09-10 03:00 (claude)",
    ))
    with caplog.at_level(logging.INFO):
        orchestrator._charge_process_crash(ValueError("boom"))

    assert q_file.read_text(encoding="utf-8") == before
    # CRITICAL, not WARNING: `msg` claims an outcome ("→ Requeue um …") that did
    # NOT happen, so the line that says so has to carry the same weight as the one
    # that would have claimed it — otherwise a 03:00 grep for CRITICAL reads the
    # false version and the correction hides one level down (review round 3).
    not_charged = [
        r for r in caplog.records
        if "nicht angerechnet" in r.getMessage().lower()
    ]
    assert not_charged, [(r.levelname, r.getMessage()) for r in caplog.records]
    assert all(r.levelno >= logging.CRITICAL for r in not_charged), (
        [(r.levelname, r.getMessage()) for r in not_charged]
    )
    assert orchestrator._in_flight_task is None


def test_register_is_empty_after_run_once(tmp_path, monkeypatch):
    """Plan test 10 — no task stays armed once run_once has returned normally.

    The `#tool:` branch leaves its loop iteration via `continue`, so the disarm
    at the end of the loop body is not the one that fires here — the one after
    the loop is. Without it a `read_queue()` failure in run_once's tail would be
    charged to a task that had already finished.
    """
    _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Tool-Task #tool:dev-loop\n")
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _t: None)
    monkeypatch.setattr(
        orchestrator, "_execute_tool_task",
        lambda *a, **kw: orchestrator.ToolTaskExecutionOutcome(success=True, finalized=True),
    )
    monkeypatch.setattr(orchestrator, "_in_flight_task", None, raising=False)

    orchestrator.run_once()

    assert orchestrator._in_flight_task is None


def test_edited_queue_line_is_not_charged(tmp_path, monkeypatch, caplog):
    """Plan test 11 (F3) — the task text IS the identity, and it may change.

    Editing the line while the task runs means `mark_retry` finds nothing. That
    is the conservative direction on purpose: NOTHING is counted, with a
    warning, rather than a counter landing on the wrong line.
    """
    q_file = _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task\n")

    def boom_after_edit(_cwd):
        q_file.write_text("## Queue\n- [ ] Ganz anderer Task\n", encoding="utf-8")
        raise ValueError(_CRASH_MESSAGE)

    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _t: str(tmp_path))
    monkeypatch.setattr(orchestrator, "TRACK_FILE_CHANGES", True)
    monkeypatch.setattr(orchestrator, "_is_git_repo", lambda *a, **kw: False)
    monkeypatch.setattr(orchestrator, "_git_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_snapshot_dir", boom_after_edit)
    _main_env(monkeypatch)

    with caplog.at_level(logging.INFO):
        assert _run_main_expecting_crash() == 1

    content = q_file.read_text(encoding="utf-8")
    assert "<!-- hang:" not in content, content
    assert content == "## Queue\n- [ ] Ganz anderer Task\n"
    # CRITICAL, not WARNING: `msg` claims an outcome ("→ Requeue um …") that did
    # NOT happen, so the line that says so has to carry the same weight as the one
    # that would have claimed it — otherwise a 03:00 grep for CRITICAL reads the
    # false version and the correction hides one level down (review round 3).
    not_charged = [
        r for r in caplog.records
        if "nicht angerechnet" in r.getMessage().lower()
    ]
    assert not_charged, [(r.levelname, r.getMessage()) for r in caplog.records]
    assert all(r.levelno >= logging.CRITICAL for r in not_charged), (
        [(r.levelname, r.getMessage()) for r in not_charged]
    )


# ---------------------------------------------------------------------------
# 12-15 — the traceback itself (DONE 1 / A1) and the watch path (A2)
# ---------------------------------------------------------------------------

def test_main_logs_the_traceback_and_exits_one(tmp_path, monkeypatch, caplog):
    """Plan test 12 (A1) — CRITICAL record, with exc_info, exit code 1."""
    _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task\n")
    _crash_in_single_shot(tmp_path, monkeypatch)
    _main_env(monkeypatch)

    with caplog.at_level(logging.INFO):
        assert _run_main_expecting_crash() == 1

    critical = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    with_tb = [r for r in critical if r.exc_info is not None]
    assert with_tb, [(r.levelname, r.getMessage()) for r in caplog.records]
    assert with_tb[0].exc_info[0] is ValueError
    assert "Unerwarteter Fehler im Hauptlauf" in with_tb[0].getMessage()
    assert "ValueError" in with_tb[0].getMessage()


def test_traceback_reaches_a_real_log_file(tmp_path, monkeypatch):
    """Plan test 13 (DONE 1) — the whole point: it has to be IN THE FILE.

    `run_orchestrator.ps1` starts `--watch` with a hidden window and no stdout
    redirection, so a console-only traceback does not exist at 03:00. Counter-
    check on the morning of 2026-09-10: 96 crashes, 0 hits in
    `logs/orchestrator.log`.

    The handler is installed and removed by this test alone — see the isolation
    fixture for why the autouse one does not manage handlers.
    """
    log_file = tmp_path / "orchestrator.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))

    _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task\n")
    _crash_in_single_shot(tmp_path, monkeypatch)
    _main_env(monkeypatch)

    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        assert _run_main_expecting_crash() == 1
    finally:
        root.removeHandler(handler)
        handler.close()

    written = log_file.read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in written, written
    assert "ValueError" in written, written
    assert "path is on mount" in written, written


def test_ctrl_c_still_stops_watch_cleanly(monkeypatch):
    """Plan test 14 (A2) — the documented way to stop `--watch` is untouched.

    The `except KeyboardInterrupt` inside `_main()` stays where it always was;
    the wrapper must not turn an orderly stop into an exit-1 crash, and must not
    charge it.
    """
    def stop(*_a, **_kw):
        raise KeyboardInterrupt

    _main_env(monkeypatch, argv=("orchestrator.py", "--watch"))
    monkeypatch.setattr(orchestrator, "run_watch", stop)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "read_queue", lambda: [])
    notify = Mock()
    monkeypatch.setattr(orchestrator, "notify_queue_complete", notify)
    monkeypatch.setattr(orchestrator, "_in_flight_task", None, raising=False)

    orchestrator.main()  # returns normally — no SystemExit, no re-raise

    notify.assert_called_once_with(0)
    assert orchestrator._in_flight_task is None


def test_parallel_parent_failure_logs_a_traceback(monkeypatch, caplog):
    """Plan test 15 (A4) — the `#parallel` parent used to log only `str(e)`.

    The subtask side already logs with exc_info (`parallel_runner._run_group`);
    this is the parent half. Without it an aggregation failure at 03:00 names a
    symptom and no location.
    """
    def boom(*_a, **_kw):
        raise RuntimeError("subtask allocation failed")

    monkeypatch.setattr(orchestrator, "_worktree_gate_violation", lambda *a, **kw: None)
    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(
            task_text="Eltern-Task #parallel", line_no=1,
            subtasks=("Sub A", "Sub B"),
            raw_line="- [ ] Eltern-Task #parallel",
        )],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: [])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _t: None)
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "cleanup_done_tasks", lambda *a, **kw: 0)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_task_started", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_queue_complete", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "mark_retry", lambda *a, **kw: True)
    monkeypatch.setattr(orchestrator.memory_module, "get_context_for_task", lambda *a, **kw: "")
    monkeypatch.setattr(parallel_runner, "run_parallel", boom)

    with caplog.at_level(logging.INFO):
        assert orchestrator.run_once() is False

    failed = [
        r for r in caplog.records
        if "Parallel-Ausführung fehlgeschlagen" in r.getMessage() and r.exc_info
    ]
    assert failed, [(r.levelname, r.getMessage(), r.exc_info) for r in caplog.records]
    assert failed[0].exc_info[0] is RuntimeError
    assert failed[0].levelno >= logging.ERROR


# ---------------------------------------------------------------------------
# Round 2 — gates the first review round found missing
# ---------------------------------------------------------------------------

def test_dry_run_arms_nothing_and_writes_nothing(tmp_path, monkeypatch):
    """The `if not dry_run:` guard is a switching branch and needs a real gate.

    The first version of this test asserted the right things but was trivially
    green: nothing threw during a dry run, so the register ended up empty via the
    post-loop clear whether the guard existed or not — proven by mutation in
    review round 2 (guard removed, all three assertions still held). It now makes
    the dry-run iteration RAISE, past where the arming point sits, so removing
    the guard turns the run into a charge and the test red.

    What the guard protects: a `--dry-run` must never stamp `<!-- hang: N -->`
    into the live queue, let alone quarantine a task the user only wanted to
    inspect.
    """
    q_file = _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Trockenlauf-Task\n")
    _main_env(monkeypatch, argv=("orchestrator.py", "--dry-run"))

    def boom(_task):
        raise ValueError(_CRASH_MESSAGE)

    # extract_cwd runs inside the iteration, AFTER the arming point — a dry run
    # parses metadata just like a real one.
    monkeypatch.setattr(orchestrator, "extract_cwd", boom)

    assert _run_main_expecting_crash() == 1

    content = q_file.read_text(encoding="utf-8")
    assert "<!-- hang:" not in content, content
    assert "- [ ] Trockenlauf-Task" in content
    assert orchestrator._in_flight_task is None


def test_both_finalizers_disarm_the_register(tmp_path, monkeypatch):
    """A persisted fate ends the task's flight — measured defect, review round 2.

    Without this, the register stayed armed through the whole post-run tail
    (verify script, memory store, notify, replay emit). Harmless for a normal
    task — its line is `[x]` and mark_retry finds nothing — but a `#every:` line
    has already been rewritten as `- [ ] … <!-- retry: … -->`, so the charge
    lands: a daily task that had just SUCCEEDED came back 5 minutes later and
    carried a fruitless attempt.
    """
    _real_queue_env(
        tmp_path, monkeypatch,
        "## Queue\n- [ ] Task A\n- [ ] Task B\n",
    )
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)

    # One open line per finalizer: the first call closes its line, so reusing it
    # would make the second call fail for the wrong reason and prove nothing.
    cases = (
        ("Task A", 2, lambda: orchestrator._finalize_task_with_result_checked(
            "Task A", "ok", "claude", queue_line_no=2)),
        ("Task B", 3, lambda: orchestrator._mark_done_checked(
            "Task B", "claude", queue_line_no=3)),
    )
    for text, line_no, finalize in cases:
        orchestrator._set_in_flight(SimpleNamespace(
            task_text=text, line_no=line_no, subtasks=None,
            raw_line=f"- [ ] {text}",
        ))
        assert orchestrator._in_flight_task is not None
        assert finalize() is True, f"{text}: Finalisierung selbst muss gelingen"
        assert orchestrator._in_flight_task is None


def test_a_successful_every_task_is_not_charged_by_a_crash_in_the_tail(tmp_path, monkeypatch):
    """The integration form of the defect above, on the line shape that bites.

    `_completion_replacement()` does not stamp a `#every:` line, it reschedules
    it — so unlike a normal finished task its line is OPEN again and a late
    charge would really land, re-running a task that had just done its work.
    """
    q_file = _real_queue_env(
        tmp_path, monkeypatch,
        "## Queue\n- [ ] Daily brief #id:brief #every:24h\n",
    )
    _main_env(monkeypatch)
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _t: None)
    monkeypatch.setattr(orchestrator, "_is_git_repo", lambda *a, **kw: False)
    monkeypatch.setattr(orchestrator, "_git_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "TRACK_FILE_CHANGES", False)
    monkeypatch.setattr(
        orchestrator, "_run_with_retry",
        lambda *a, **kw: (SimpleNamespace(
            success=True, output="fertig", error=None, error_code=None,
            input_tokens=0, output_tokens=0, cache_creation_tokens=0,
            cache_read_tokens=0, session_id=None, cost=0.0,
        ), False),
    )

    # The crash happens AFTER the queue line has been rewritten as open again.
    def boom(*_a, **_kw):
        raise ValueError(_CRASH_MESSAGE)

    monkeypatch.setattr(orchestrator.memory_module, "store_result", boom)

    assert _run_main_expecting_crash() == 1

    content = q_file.read_text(encoding="utf-8")
    assert "<!-- hang:" not in content, (
        "a task whose success was already persisted must not be charged: " + content
    )


def test_the_queue_write_survives_a_broken_notifier(tmp_path, monkeypatch):
    """Reporting must never cost the write — review round 2, P2-1.

    `append_log` touches a second file and `notify_error` does an HTTPS round
    trip whose own failure path calls print(), which can raise on the broken
    stdout of a hidden-window `--watch` process. While those ran BEFORE the
    write, one throw meant the counter stood still and the unbounded crash loop
    simply continued — the exact failure this function exists to stop.
    """
    q_file = _real_queue_env(
        tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task <!-- hang: 2 -->\n")
    _crash_in_single_shot(tmp_path, monkeypatch)
    _main_env(monkeypatch)

    def boom(*_a, **_kw):
        raise OSError("[Errno 9] Bad file descriptor")

    monkeypatch.setattr(orchestrator, "append_log", boom)
    monkeypatch.setattr(orchestrator, "notify_error", boom)

    assert _run_main_expecting_crash() == 1

    content = q_file.read_text(encoding="utf-8")
    assert queue_manager.line_is_failed(content), (
        "the quarantine must be written even when reporting is broken: " + content
    )


def test_a_failing_queue_write_still_leaves_the_exit_code_intact(tmp_path, monkeypatch):
    """The inner `except` exists so a dead queue cannot eat the traceback.

    This is the path a OneDrive lock on `agent-queue.md` really lands on: the
    crash is logged, the charge fails, and the process must still end with 1
    rather than dying a second time inside its own crash handler.
    """
    _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task\n")
    _crash_in_single_shot(tmp_path, monkeypatch)
    _main_env(monkeypatch)

    def boom(*_a, **_kw):
        raise OSError("[Errno 13] agent-queue.md is locked by another process")

    monkeypatch.setattr(orchestrator, "mark_retry", boom)

    assert _run_main_expecting_crash() == 1


def test_main_actually_installs_the_thread_excepthook(monkeypatch):
    """Pin the WIRING, not just the function.

    Every other test in this file stubs `install_thread_excepthook` out, and
    test_logging_setup.py exercises the function in isolation — so without this
    the one line in main() that connects them is covered by nothing.
    """
    calls: list[int] = []
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "--list-tools"])
    monkeypatch.setattr(orchestrator, "setup_logging", lambda: None)
    monkeypatch.setattr(orchestrator, "install_thread_excepthook", lambda: calls.append(1))
    monkeypatch.setattr(orchestrator, "list_tools", lambda: {})

    orchestrator.main()

    assert calls == [1]


def test_all_three_counter_messages_name_the_shared_count():
    """Guard against the honesty defect re-opening a third time.

    `<!-- hang: N -->` is written by hang, format_error AND an attributable
    process crash. On 2026-08-15 the tool path was made to say so; the crash path
    made the sharing three-way and left the other two behind, so a first genuine
    hang after two crashes reported "zum 3. Mal". The wording now lives in ONE
    constant — this asserts that the constant exists, that neither retired
    spelling comes back, and that the current number of readers does not shrink.

    What it does NOT catch, stated because the docstring overclaimed it once: a
    NEW branch with its own hard-coded wording leaves both the count and the
    assertions untouched. Catching that needs an AST rule ("every f-string that
    interpolates a hang count also references JOINT_ATTEMPT_NOTE"), which is not
    built. This is a regression lock on the two known-bad spellings, not a proof.
    """
    with open(orchestrator.__file__, encoding="utf-8") as fh:
        source = fh.read()

    assert 'JOINT_ATTEMPT_NOTE = "Hang/Format-Fehler/Absturz zusammen gezählt"' in source
    # The two spellings this replaced must not come back.
    assert '"Hang/Format-Fehler zusammen gezählt"' not in source
    assert "zum {hang_count}. Mal" not in source
    # Every branch that reports the ordinal reads the constant.
    assert source.count("JOINT_ATTEMPT_NOTE") >= 5


def test_quarantine_notifies_but_a_requeue_stays_silent(tmp_path, monkeypatch):
    """Parity with the two other terminal outcomes — and only there.

    A quarantine is the one state the user cannot discover by waiting: the line
    leaves the open queue and no replay record is written for a crashed
    iteration. The requeue branch must NOT notify — it is not terminal, the task
    comes back on its own, and a Telegram message per crash would be noise.
    """
    q_file = _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task #id:krit\n")
    _crash_in_single_shot(tmp_path, monkeypatch)
    _main_env(monkeypatch)

    sent: list[tuple] = []
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: sent.append(a))

    assert _run_main_expecting_crash() == 1
    assert sent == [], "a requeue is not terminal — no notification"

    assert _run_main_expecting_crash() == 1
    assert sent == [], "still only a requeue at attempt 2"

    assert _run_main_expecting_crash() == 1
    assert len(sent) == 1, "the quarantine must be announced"
    assert "quarantäniert" in sent[0][2]
    assert "#id:krit" in sent[0][2]
    assert queue_manager.line_is_failed(q_file.read_text(encoding="utf-8"))


def test_a_recurring_task_is_rescheduled_instead_of_quarantined(tmp_path, monkeypatch, caplog):
    """The `#every:` branch of the quarantine message is switching logic — gate it.

    `finalize_task_with_result(failed=True)` does NOT stamp a recurring line:
    `_completion_replacement()` reschedules it and the hang counter goes with it.
    So a recurring task is structurally NOT cappable, and the message has to say
    so — the first version told the user per Telegram "wird nicht erneut
    gestartet", which is measurably false. Review round 3: the branch existed
    with no test at all, so collapsing the ternary to the non-recurring text
    would have stayed green.
    """
    q_file = _real_queue_env(
        tmp_path, monkeypatch,
        "## Queue\n- [ ] Daily brief #id:brief #every:24h <!-- hang: 2 -->\n",
    )
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)

    orchestrator._set_in_flight(SimpleNamespace(
        task_text="Daily brief #id:brief #every:24h",
        line_no=2,
        subtasks=None,
        raw_line="- [ ] Daily brief #id:brief #every:24h <!-- hang: 2 -->",
    ))
    with caplog.at_level(logging.CRITICAL):
        orchestrator._charge_process_crash(ValueError(_CRASH_MESSAGE))

    content = q_file.read_text(encoding="utf-8")
    assert "- [ ] Daily brief" in content, content          # reopened, not stamped
    assert "<!-- retry:" in content, content                # rescheduled
    assert "<!-- hang:" not in content, content             # counter went with it
    assert not queue_manager.line_is_failed(content), content

    said = " ".join(r.getMessage() for r in caplog.records)
    assert "neu geplant" in said, said
    assert "wird nicht erneut gestartet" not in said, said


def test_a_broken_append_log_does_not_swallow_the_telegram_message(tmp_path, monkeypatch):
    """The two reporters are isolated from each other, not just from the write.

    Round 2 moved the queue write ahead of the reporting; round 3 gave each
    reporter its own try. Without the second half, a throwing `append_log` still
    skipped the Telegram message — and the quarantine is exactly the state the
    user cannot discover by waiting. Merging the two try blocks back together
    turns this red; the existing broken-notifier test would not notice, because
    it breaks both reporters and only inspects the queue file.
    """
    q_file = _real_queue_env(
        tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task <!-- hang: 2 -->\n")
    _crash_in_single_shot(tmp_path, monkeypatch)
    _main_env(monkeypatch)

    sent: list[tuple] = []
    monkeypatch.setattr(orchestrator, "notify_error", lambda *a, **kw: sent.append(a))

    def boom(*_a, **_kw):
        raise OSError("[Errno 9] Bad file descriptor")

    monkeypatch.setattr(orchestrator, "append_log", boom)

    assert _run_main_expecting_crash() == 1

    assert queue_manager.line_is_failed(q_file.read_text(encoding="utf-8"))
    assert len(sent) == 1, "the Telegram message must survive a broken event log"
    assert "quarant" in sent[0][2].lower()


def test_a_subtask_crash_never_reaches_the_process(monkeypatch, caplog):
    """Pin the claim the docs now make about #parallel — by running it.

    CLAUDE.md and the arming comment both say a `#parallel` SUBTASK crash cannot
    reach `main()`, because `parallel_runner._run_group` catches it one level
    down and turns it into a failed `SubTaskResult`. That was asserted from
    reading the code; external review (Codex, 2026-09-10) pointed out that no
    test actually executes that path — `test_tool_task_crash_is_charged_too`
    drives the plain `#tool:` route and never touches parallel_runner.

    So this one really runs it: a subtask whose `_execute_tool_task` raises the
    reference exception must NOT propagate, must come back as a failed result,
    and must leave the crash register untouched.
    """
    def boom(*_a, **_kw):
        raise ValueError(_CRASH_MESSAGE)

    # Patch targets matter here and the first version got them wrong:
    # `_execute_tool_task`, `_worktree_gate_violation` and `_build_prompt` are
    # imported lazily FROM orchestrator inside _run_single_subtask, but
    # `select_provider` comes from DISPATCHER. Patching it on orchestrator left
    # the real dispatcher in the path, the subtask died on the stub limits
    # object long before `boom`, and `_run_group` labelled that foreign
    # AttributeError "subtask_crash" too — so the test passed for the wrong
    # reason and the mutation probe could not see it either (the foreign error
    # travels the same except). Caught in the closing delta review, 2026-09-10.
    monkeypatch.setattr(orchestrator, "_execute_tool_task", boom)
    monkeypatch.setattr(orchestrator, "_worktree_gate_violation", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_build_prompt", lambda *a, **kw: "prompt")
    monkeypatch.setattr(dispatcher, "select_provider",
                        lambda *a, **kw: SimpleNamespace(name="claude", set_cooldown=Mock()))
    monkeypatch.setattr(orchestrator, "_in_flight_task", None, raising=False)

    with caplog.at_level(logging.ERROR):
        results = parallel_runner.run_parallel(
            "Eltern-Task #parallel",
            ("Sub A #tool:dev-loop",),
            SimpleNamespace(),
        )

    assert len(results) == 1
    assert results[0].success is False
    assert "subtask_crash" in (results[0].error or ""), results[0].error
    # The second half is what makes the first half mean something: without it,
    # ANY exception on the way (a stubbed-out dependency, a gate firing early)
    # satisfies the assertion above, because _run_group labels them all the same.
    assert _CRASH_MESSAGE in (results[0].error or ""), results[0].error
    assert orchestrator._in_flight_task is None, (
        "a subtask crash must not arm or charge the process-crash register"
    )


def test_exit_code_survives_a_crash_inside_the_crash_handler(tmp_path, monkeypatch):
    """`sys.exit(1)` sits in a `finally` — prove that the finally is load-bearing.

    The exit code is the one thing this branch owes the watchdog, and it must not
    depend on logging or queue I/O succeeding. External review (Codex,
    2026-09-10) pointed out that a throwing logging handler or a BaseException
    out of the charge path would otherwise skip the exit entirely, letting the
    process end on the ORIGINAL exception's default excepthook — different exit
    code, duplicated traceback.

    A first version of this test moved `sys.exit` out of the `finally` and stayed
    green, because nothing in the try was made to throw. This one throws.
    """
    _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task\n")
    _crash_in_single_shot(tmp_path, monkeypatch)
    _main_env(monkeypatch)

    def boom(_exc):
        raise RuntimeError("die Zurechnung selbst ist explodiert")

    monkeypatch.setattr(orchestrator, "_charge_process_crash", boom)

    assert _run_main_expecting_crash() == 1


def test_a_throwing_logger_does_not_cost_the_charge(tmp_path, monkeypatch):
    """`main()`'s two duties owe each other nothing — and this was missed twice.

    Round 2 established the rule inside `_charge_process_crash`: the queue write
    goes first, and each reporter gets its own `try`. The same mistake was then
    made ONE LEVEL UP — in `main()` the CRITICAL line sat ahead of the charge in
    a single flow, so a throwing logging handler skipped the charge entirely, the
    counter stood still, and the unbounded crash loop continued. Measured in the
    closing delta review.

    The fix (an inner try around the logging call) then shipped WITHOUT a test:
    a batch mutation run turned `except BaseException: pass` into `raise` and
    nothing went red. This is that missing gate.
    """
    q_file = _real_queue_env(tmp_path, monkeypatch, "## Queue\n- [ ] Absturz-Task\n")
    _crash_in_single_shot(tmp_path, monkeypatch)
    _main_env(monkeypatch)

    class _FirstCriticalExplodes:
        def __init__(self):
            self.criticals = 0

        def critical(self, *_a, **_kw):
            self.criticals += 1
            if self.criticals == 1:          # the one main() emits
                raise OSError("[Errno 9] Bad file descriptor")

        def warning(self, *_a, **_kw):
            pass

    logger = _FirstCriticalExplodes()
    monkeypatch.setattr(orchestrator, "logging", SimpleNamespace(getLogger=lambda *_a: logger))

    assert _run_main_expecting_crash() == 1

    assert "<!-- hang: 1 -->" in q_file.read_text(encoding="utf-8"), (
        "the charge must survive a logging failure: " + q_file.read_text(encoding="utf-8")
    )
    assert logger.criticals >= 2, "the charge path must still have reported"
