"""Tests for the #verify: post-task check (orchestrator._verify_task_result).

Why this exists: a provider run can exit 0, emit a well-formed result event and
still have achieved nothing. Three consecutive morning-brief runs (20./24./25.07.2026)
were booked as successes while the daily note stayed empty — the failure was only
noticed days later, by hand. A verify script checks the OUTCOME rather than the run,
so it catches that class of failure regardless of the cause.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import orchestrator


@pytest.fixture
def calls(monkeypatch):
    """Capture the alarm side effects instead of logging/notifying for real."""
    recorded = {"log": [], "notify": []}
    monkeypatch.setattr(orchestrator, "append_log", lambda msg: recorded["log"].append(msg))
    monkeypatch.setattr(
        orchestrator, "notify_error",
        lambda task, provider, msg: recorded["notify"].append((task, provider, msg)),
    )
    return recorded


def _fake_run(returncode=0, stdout="", stderr="", record=None, raises=None):
    """Stand-in for run_with_watchdog — the system boundary _run_verify_script calls.

    Mocked at that boundary rather than at subprocess.run, so the tests exercise the
    real dispatch/tamper-gate logic above it and only fake the process launch.
    """
    def runner(cmd, **kwargs):
        if record is not None:
            record["cmd"] = cmd
            record["cwd"] = kwargs.get("cwd")
            record["timeout"] = kwargs.get("hard_timeout")
            record["idle_timeout"] = kwargs.get("idle_timeout")
        if raises is not None:
            raise raises
        return SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr, stdin_error=None
        )
    return runner


# --- _verify_task_result: the alarm behaviour -------------------------------

def test_no_verify_tag_is_a_no_op(calls, tmp_path):
    outcome = orchestrator._verify_task_result("Task ohne Tag", str(tmp_path), "claude")
    assert outcome.ok is True
    assert outcome.note == ""
    assert calls["log"] == []
    assert calls["notify"] == []


def test_passing_script_produces_no_alarm(calls, tmp_path, monkeypatch):
    # The new missing-script existence check sits BEFORE _run_verify_script — give it
    # a real (empty) file to find, or every mocked call below would short-circuit as
    # "missing" before the mock is ever reached.
    (tmp_path / "check.ps1").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        orchestrator, "_run_verify_script",
        lambda script, cwd, pin=None: (True, "Block vorhanden"),
    )
    outcome = orchestrator._verify_task_result(
        "Task #verify:check.ps1", str(tmp_path), "claude"
    )
    assert outcome.ok is True
    assert outcome.note == ""
    assert calls["notify"] == []


def test_failing_script_alarms_and_annotates_result(calls, tmp_path, monkeypatch):
    (tmp_path / "check.ps1").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        orchestrator, "_run_verify_script",
        lambda script, cwd, pin=None: (False, "Briefing-Block fehlt in 2026-07-25.md"),
    )
    outcome = orchestrator._verify_task_result(
        "Task #verify:check.ps1", str(tmp_path), "claude"
    )
    assert outcome.ok is False
    assert outcome.missing is False
    assert "Briefing-Block fehlt" in outcome.note
    assert len(calls["notify"]) == 1
    assert "Briefing-Block fehlt" in calls["notify"][0][2]
    assert len(calls["log"]) == 1


def test_failed_verify_never_touches_the_queue(calls, tmp_path, monkeypatch):
    """The no-retry-storm constraint, pinned down instead of only documented.

    A failing check must not requeue or re-mark the task: a broken check script would
    otherwise loop a working task forever — a new failure mode introduced to report an
    old one. Only the alarm is allowed.
    """
    (tmp_path / "check.ps1").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        orchestrator, "_run_verify_script",
        lambda script, cwd, pin=None: (False, "Block fehlt"),
    )

    def forbidden(name):
        def _raise(*_a, **_kw):
            raise AssertionError(f"verify failure must not call {name}()")
        return _raise

    for fn in (
        "mark_retry", "mark_done", "finalize_task_with_result",
        "_mark_retry_checked", "_mark_done_checked",
        "_finalize_task_with_result_checked",
    ):
        monkeypatch.setattr(orchestrator, fn, forbidden(fn))

    outcome = orchestrator._verify_task_result(
        "Task #verify:check.ps1", str(tmp_path), "claude"
    )

    assert outcome.ok is False
    assert "Block fehlt" in outcome.note
    assert len(calls["notify"]) == 1


def test_tool_path_orders_finalize_verify_store_notify(monkeypatch, tmp_path):
    """Behavioural counterpart to the structural guard below: drives the real
    _execute_tool_task and records the actual call sequence.

    Covers what source order cannot: that the calls happen in the SAME branch, and that
    a failed check suppresses the green 'task done' instead of following the red alarm
    with it.
    """
    order = []

    tool = SimpleNamespace(
        name="dummy", description="d", read_only=True,
        run=lambda *a, **kw: SimpleNamespace(
            success=True, output="out", iterations=1, error="", error_code="",
            retryable=False, input_tokens=1, output_tokens=1,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
    )
    monkeypatch.setattr(orchestrator, "get_tool", lambda name: tool)
    monkeypatch.setattr(orchestrator, "load_skill", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_snapshot_dir", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_get_change_summary", lambda *a, **kw: "")
    monkeypatch.setattr(orchestrator, "report_estimated_usage", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "estimate_task_usage_pct", lambda *a, **kw: 0.0)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(
        orchestrator, "_finalize_task_with_result_checked",
        lambda *a, **kw: (order.append("finalize"), True)[1],
    )
    monkeypatch.setattr(
        orchestrator, "_verify_task_result",
        lambda *a, **kw: (order.append("verify"), orchestrator.VerifyOutcome(ok=False, note="!"))[1],
    )
    monkeypatch.setattr(
        orchestrator.memory_module, "store_result",
        lambda *a, **kw: order.append(f"store(success={kw.get('success')})"),
    )
    monkeypatch.setattr(
        orchestrator, "notify_task_done", lambda *a, **kw: order.append("notify_done")
    )

    outcome = orchestrator._execute_tool_task(
        "Task #verify:check.ps1", "dummy",
        SimpleNamespace(name="claude"), str(tmp_path),
    )

    assert order[:2] == ["finalize", "verify"], f"wrong order: {order}"
    assert "store(success=False)" in order, "a failed check must not be stored as success"
    assert "notify_done" not in order, "green 'task done' must not follow the red alarm"
    assert outcome.success is True, "the tool itself succeeded — do not trigger a retry"
    assert outcome.verify_failed is True


def test_tool_path_reports_success_when_verify_passes(monkeypatch, tmp_path):
    """Counter-case, so the test above cannot pass by simply breaking notification."""
    order = []
    tool = SimpleNamespace(
        name="dummy", description="d", read_only=True,
        run=lambda *a, **kw: SimpleNamespace(
            success=True, output="out", iterations=1, error="", error_code="",
            retryable=False, input_tokens=1, output_tokens=1,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
    )
    monkeypatch.setattr(orchestrator, "get_tool", lambda name: tool)
    monkeypatch.setattr(orchestrator, "load_skill", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_snapshot_dir", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_get_change_summary", lambda *a, **kw: "")
    monkeypatch.setattr(orchestrator, "report_estimated_usage", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "estimate_task_usage_pct", lambda *a, **kw: 0.0)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_finalize_task_with_result_checked", lambda *a, **kw: True)
    monkeypatch.setattr(
        orchestrator, "_verify_task_result", lambda *a, **kw: orchestrator.VerifyOutcome()
    )
    monkeypatch.setattr(
        orchestrator.memory_module, "store_result",
        lambda *a, **kw: order.append(f"store(success={kw.get('success')})"),
    )
    monkeypatch.setattr(
        orchestrator, "notify_task_done", lambda *a, **kw: order.append("notify_done")
    )

    outcome = orchestrator._execute_tool_task(
        "Task #verify:check.ps1", "dummy",
        SimpleNamespace(name="claude"), str(tmp_path),
    )

    assert "store(success=True)" in order
    assert "notify_done" in order
    assert outcome.verify_failed is False


def test_tool_path_carries_verify_missing_through_the_outcome(monkeypatch, tmp_path):
    """The tool path's ToolTaskExecutionOutcome.verify_missing must mirror
    VerifyOutcome.missing — this is the field the caller reads to choose
    error_code='verify_missing' over 'verify_failed'."""
    tool = SimpleNamespace(
        name="dummy", description="d", read_only=True,
        run=lambda *a, **kw: SimpleNamespace(
            success=True, output="out", iterations=1, error="", error_code="",
            retryable=False, input_tokens=1, output_tokens=1,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
    )
    monkeypatch.setattr(orchestrator, "get_tool", lambda name: tool)
    monkeypatch.setattr(orchestrator, "load_skill", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_snapshot_dir", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_get_change_summary", lambda *a, **kw: "")
    monkeypatch.setattr(orchestrator, "report_estimated_usage", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "estimate_task_usage_pct", lambda *a, **kw: 0.0)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_finalize_task_with_result_checked", lambda *a, **kw: True)
    monkeypatch.setattr(
        orchestrator, "_verify_task_result",
        lambda *a, **kw: orchestrator.VerifyOutcome(ok=False, note="!", missing=True),
    )
    monkeypatch.setattr(orchestrator.memory_module, "store_result", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_task_done", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_restamp_after_failed_verify", lambda *a, **kw: None)

    outcome = orchestrator._execute_tool_task(
        "Task #verify:check.ps1", "dummy",
        SimpleNamespace(name="claude"), str(tmp_path),
    )

    assert outcome.verify_failed is True
    assert outcome.verify_missing is True


def test_tool_path_verify_missing_is_false_on_a_result_failure(monkeypatch, tmp_path):
    """Gegenprobe: a failing-but-present check must leave verify_missing False even
    though verify_failed is True — otherwise both paths would collapse into one."""
    tool = SimpleNamespace(
        name="dummy", description="d", read_only=True,
        run=lambda *a, **kw: SimpleNamespace(
            success=True, output="out", iterations=1, error="", error_code="",
            retryable=False, input_tokens=1, output_tokens=1,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
    )
    monkeypatch.setattr(orchestrator, "get_tool", lambda name: tool)
    monkeypatch.setattr(orchestrator, "load_skill", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_snapshot_dir", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_get_change_summary", lambda *a, **kw: "")
    monkeypatch.setattr(orchestrator, "report_estimated_usage", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "estimate_task_usage_pct", lambda *a, **kw: 0.0)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_finalize_task_with_result_checked", lambda *a, **kw: True)
    monkeypatch.setattr(
        orchestrator, "_verify_task_result",
        lambda *a, **kw: orchestrator.VerifyOutcome(ok=False, note="!", missing=False),
    )
    monkeypatch.setattr(orchestrator.memory_module, "store_result", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_task_done", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_restamp_after_failed_verify", lambda *a, **kw: None)

    outcome = orchestrator._execute_tool_task(
        "Task #verify:check.ps1", "dummy",
        SimpleNamespace(name="claude"), str(tmp_path),
    )

    assert outcome.verify_failed is True
    assert outcome.verify_missing is False


def test_every_verify_call_site_sits_after_a_finalize_call():
    """Structural guard over the three success paths — a source-order check, not a
    behavioural one (driving all three paths end-to-end would need the full run_once
    machinery; this catches the refactor that moves a call back up, which is the real risk).

    Why the order matters: verifying BEFORE finalization means a failed queue update
    leaves the task open, so the check runs again on the next poll — two alarms for one
    incident. Until now only comments protected this.
    """
    src = Path(orchestrator.__file__).read_text(encoding="utf-8").splitlines()

    def call_lines(marker):
        return [i for i, ln in enumerate(src) if marker in ln and not ln.lstrip().startswith("def ")]

    finalize_lines = call_lines("_finalize_task_with_result_checked(")
    verify_lines = [
        i for i in call_lines("_verify_task_result(")
        if not src[i].lstrip().startswith(("#", '"'))
    ]

    assert len(verify_lines) >= 3, "expected a verify call in all three success paths"

    for v in verify_lines:
        preceding = [f for f in finalize_lines if f < v]
        assert preceding, f"_verify_task_result at line {v + 1} has no finalize call before it"
        assert v - max(preceding) < 30, (
            f"_verify_task_result at line {v + 1} is not adjacent to its finalize call "
            f"(nearest is line {max(preceding) + 1}) — did it move before finalization?"
        )


# --- Tamper gate: the provider must not be able to swap the check (P1) ------

def test_modified_script_is_refused(calls, tmp_path, monkeypatch):
    """The provider writes in the same cwd the check lives in. A check it rewrote
    would run outside the provider sandbox with the orchestrator's environment, so a
    changed file must be refused rather than executed."""
    script = tmp_path / "check.ps1"
    script.write_text("exit 0", encoding="utf-8")
    pin = orchestrator._pin_verify_script(f"Task #verify:{script.name}", str(tmp_path))
    assert pin.digest is not None

    script.write_text("Write-Output 'pwned'; exit 0", encoding="utf-8")

    def must_not_run(*_a, **_kw):
        raise AssertionError("tampered script must not be executed")
    monkeypatch.setattr(orchestrator, "run_with_watchdog", must_not_run)

    passed, detail = orchestrator._run_verify_script(script.name, str(tmp_path), pin=pin)
    assert passed is False
    assert "verändert" in detail


def test_unchanged_script_passes_the_tamper_gate(tmp_path, monkeypatch):
    script = tmp_path / "check.ps1"
    script.write_text("exit 0", encoding="utf-8")
    pin = orchestrator._pin_verify_script(f"Task #verify:{script.name}", str(tmp_path))
    monkeypatch.setattr(orchestrator, "run_with_watchdog", _fake_run(0, "ok"))

    passed, _ = orchestrator._run_verify_script(script.name, str(tmp_path), pin=pin)
    assert passed is True


def test_pin_records_no_digest_for_missing_script(tmp_path):
    pin = orchestrator._pin_verify_script("Task #verify:gone.ps1", str(tmp_path))
    assert pin.tag_present is True
    assert pin.script == "gone.ps1"
    assert pin.digest is None


def test_pin_flags_present_but_pathless_tag(tmp_path):
    """Distinguishing this from "no tag" is what makes the runtime gate fail-closed."""
    pin = orchestrator._pin_verify_script("Task #verify:", str(tmp_path))
    assert pin.tag_present is True
    assert pin.script is None


def test_pin_is_inert_without_a_tag(tmp_path):
    pin = orchestrator._pin_verify_script("Task ohne Tag", str(tmp_path))
    assert pin.tag_present is False


def test_pathless_tag_alarms_at_runtime(calls, tmp_path):
    """Fail-closed: the linter is an offline command, not a runtime gate. A typo'd tag
    must not silently switch the check off."""
    outcome = orchestrator._verify_task_result("Task #verify:", str(tmp_path), "claude")
    assert outcome.ok is False
    assert "ohne verwertbaren Skript-Pfad" in outcome.note
    assert len(calls["notify"]) == 1


def test_no_tag_stays_silent_and_ok(calls, tmp_path):
    outcome = orchestrator._verify_task_result("Task ohne Tag", str(tmp_path), "claude")
    assert outcome.ok is True
    assert outcome.note == ""
    assert calls["notify"] == []


def test_verify_uses_the_tree_killing_watchdog(tmp_path, monkeypatch):
    """subprocess.run's timeout only kills the direct child; a lingering grandchild
    keeps the capture pipes open and the call returns late or hangs — after the queue
    line was already finalized. The project's watchdog kills the whole tree."""
    (tmp_path / "check.ps1").write_text("exit 0", encoding="utf-8")
    rec = {}
    monkeypatch.setattr(orchestrator, "run_with_watchdog", _fake_run(0, "ok", record=rec))

    def must_not_be_used(*_a, **_kw):
        raise AssertionError("_run_verify_script must not call subprocess.run directly")
    monkeypatch.setattr(subprocess, "run", must_not_be_used)

    orchestrator._run_verify_script("check.ps1", str(tmp_path))
    assert rec["timeout"] == orchestrator.VERIFY_SCRIPT_TIMEOUT_SEC
    assert rec["idle_timeout"] == orchestrator.VERIFY_SCRIPT_IDLE_TIMEOUT_SEC


def test_missing_script_fails_closed(calls, tmp_path):
    """A check that cannot run must not read as 'passed'."""
    outcome = orchestrator._verify_task_result(
        "Task #verify:does_not_exist.ps1", str(tmp_path), "claude"
    )
    assert outcome.ok is False
    assert "nicht gefunden" in outcome.note
    assert len(calls["notify"]) == 1


# --- Missing script vs. failing script: config error, not result failure ----
#
# Betriebsprüfung 2026-09-16: a relative #verify: path resolved against the wrong
# cwd: reported "Skript nicht gefunden" 8 times as `error_code=="verify_failed"` —
# indistinguishable from "the task ran and did not deliver its result", when in
# truth no check ran at all. VerifyOutcome.missing + orchestrator._verify_missing
# split the two apart.

def test_missing_script_sets_the_missing_flag(calls, tmp_path):
    outcome = orchestrator._verify_task_result(
        "Task #verify:does_not_exist.ps1", str(tmp_path), "claude"
    )
    assert outcome.ok is False
    assert outcome.missing is True
    assert "Konfigurationsfehler" in outcome.note
    assert "Prüfskript nicht gefunden" in outcome.note


def test_missing_script_alarm_names_the_resolved_path_and_cwd(calls, tmp_path):
    orchestrator._verify_task_result(
        "Task #verify:sub/does_not_exist.ps1", str(tmp_path), "claude"
    )
    msg = calls["notify"][0][2]
    assert str(tmp_path / "sub" / "does_not_exist.ps1") in msg
    assert str(tmp_path) in msg


def test_missing_script_without_cwd_names_process_cwd(calls):
    """No cwd: tag at all — the alarm must say so rather than silently printing
    a bare relative path that looks like it resolved against something."""
    outcome = orchestrator._verify_task_result(
        "Task #verify:does_not_exist.ps1", None, "claude"
    )
    assert outcome.missing is True
    assert "Prozess-cwd" in calls["notify"][0][2]


def test_verify_script_pointing_at_a_directory_is_reported_as_missing_not_failed(calls, tmp_path):
    """P3-5 (oc r1): `#verify:` resolving to an existing DIRECTORY (script never
    created inside it) must be `verify_missing` (config defect), not `verify_failed`
    (the task ran, the result is missing) — `exists()` was True for a directory too,
    so this used to fall through into `_run_verify_script()`, which fails closed
    either way but with the wrong diagnosis. `is_file()` closes the gap."""
    (tmp_path / "scripts").mkdir()  # the directory exists; no file lives inside it
    outcome = orchestrator._verify_task_result(
        "Task #verify:scripts", str(tmp_path), "claude"
    )
    assert outcome.ok is False
    assert outcome.missing is True
    assert "Konfigurationsfehler" in outcome.note
    assert "Prüfskript nicht gefunden" in outcome.note


def test_existing_script_that_fails_is_not_flagged_missing(calls, tmp_path, monkeypatch):
    """Gegenprobe: a script that EXISTS and runs but reports failure must keep
    `missing=False` — the distinction is existence, not exit code."""
    (tmp_path / "check.ps1").write_text("exit 1", encoding="utf-8")
    monkeypatch.setattr(
        orchestrator, "run_with_watchdog",
        lambda *a, **kw: __import__("types").SimpleNamespace(
            returncode=1, stdout="", stderr="Block fehlt", stdin_error=None,
        ),
    )
    outcome = orchestrator._verify_task_result(
        "Task #verify:check.ps1", str(tmp_path), "claude"
    )
    assert outcome.ok is False
    assert outcome.missing is False
    assert "Block fehlt" in outcome.note
    assert "Konfigurationsfehler" not in outcome.note


def test_missing_script_is_checked_before_running_anything(calls, tmp_path, monkeypatch):
    """The existence check must short-circuit _run_verify_script entirely — a
    missing script must never reach the tamper gate or the watchdog."""
    def must_not_run(*_a, **_kw):
        raise AssertionError("_run_verify_script must not be called for a missing script")
    monkeypatch.setattr(orchestrator, "_run_verify_script", must_not_run)

    outcome = orchestrator._verify_task_result(
        "Task #verify:does_not_exist.ps1", str(tmp_path), "claude"
    )
    assert outcome.missing is True


# --- _run_verify_script: command construction + exit handling ---------------

def test_ps1_is_launched_through_pwsh(tmp_path, monkeypatch):
    (tmp_path / "check.ps1").write_text("exit 0", encoding="utf-8")
    rec = {}
    monkeypatch.setattr(orchestrator, "run_with_watchdog", _fake_run(0, "Block vorhanden", record=rec))

    passed, detail = orchestrator._run_verify_script("check.ps1", str(tmp_path))

    assert passed is True
    assert detail == "Block vorhanden"
    assert rec["cmd"][0] == "pwsh"
    assert str(tmp_path / "check.ps1") in rec["cmd"]
    assert rec["timeout"] == orchestrator.VERIFY_SCRIPT_TIMEOUT_SEC


def test_nonzero_exit_is_a_failure(tmp_path, monkeypatch):
    (tmp_path / "check.ps1").write_text("exit 1", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "run_with_watchdog", _fake_run(1, "Briefing-Block fehlt"))

    passed, detail = orchestrator._run_verify_script("check.ps1", str(tmp_path))
    assert passed is False
    assert detail == "Briefing-Block fehlt"


def test_stderr_is_used_when_stdout_is_empty(tmp_path, monkeypatch):
    (tmp_path / "check.ps1").write_text("exit 1", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "run_with_watchdog", _fake_run(1, "", "kaputt"))

    _passed, detail = orchestrator._run_verify_script("check.ps1", str(tmp_path))
    assert detail == "kaputt"


def test_timeout_fails_closed(tmp_path, monkeypatch):
    # Body is harmless on purpose: the timeout is injected below, so a mock that ever
    # stops matching must not leave a real long-running process behind.
    (tmp_path / "check.ps1").write_text("exit 0", encoding="utf-8")
    monkeypatch.setattr(
        orchestrator, "run_with_watchdog",
        _fake_run(raises=subprocess.TimeoutExpired(cmd="pwsh", timeout=60)),
    )

    passed, detail = orchestrator._run_verify_script("check.ps1", str(tmp_path))
    assert passed is False
    assert "Timeout" in detail


def test_unlaunchable_script_fails_closed(tmp_path, monkeypatch):
    (tmp_path / "check.ps1").write_text("exit 0", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "run_with_watchdog", _fake_run(raises=OSError("no shell")))

    passed, detail = orchestrator._run_verify_script("check.ps1", str(tmp_path))
    assert passed is False
    assert "nicht ausführbar" in detail


def test_py_script_is_launched_through_the_interpreter(tmp_path, monkeypatch):
    """Regression: a bare .py path raises WinError 193, which fail-closed turns into a
    permanent alarm on every SUCCESSFUL run. scripts/ in this repo is mostly .py."""
    (tmp_path / "check.py").write_text("print('ok')", encoding="utf-8")
    rec = {}
    monkeypatch.setattr(orchestrator, "run_with_watchdog", _fake_run(0, "ok", record=rec))

    passed, _detail = orchestrator._run_verify_script("check.py", str(tmp_path))

    assert passed is True
    assert rec["cmd"][0] == sys.executable
    assert str(tmp_path / "check.py") in rec["cmd"]


def test_py_verify_script_really_runs_end_to_end(tmp_path):
    """No mock: proves the interpreter dispatch actually executes a .py check."""
    (tmp_path / "check.py").write_text(
        "import sys; print('Block vorhanden'); sys.exit(0)", encoding="utf-8"
    )
    passed, detail = orchestrator._run_verify_script("check.py", str(tmp_path))
    assert passed is True
    assert "Block vorhanden" in detail


def test_py_verify_script_failure_is_detected_end_to_end(tmp_path):
    (tmp_path / "check.py").write_text(
        "import sys; print('Block fehlt'); sys.exit(1)", encoding="utf-8"
    )
    passed, detail = orchestrator._run_verify_script("check.py", str(tmp_path))
    assert passed is False
    assert "Block fehlt" in detail


def test_unsupported_suffix_reports_clearly(tmp_path):
    """Better an explicit message than a raw WinError from CreateProcess."""
    (tmp_path / "check.rb").write_text("puts 'x'", encoding="utf-8")
    passed, detail = orchestrator._run_verify_script("check.rb", str(tmp_path))
    assert passed is False
    assert "nicht unterstützter Skript-Typ" in detail


def test_missing_script_reports_not_found(tmp_path):
    passed, detail = orchestrator._run_verify_script("nope.ps1", str(tmp_path))
    assert passed is False
    assert "nicht gefunden" in detail


def test_relative_path_resolves_against_cwd(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "check.ps1").write_text("exit 0", encoding="utf-8")
    rec = {}
    monkeypatch.setattr(orchestrator, "run_with_watchdog", _fake_run(0, "ok", record=rec))

    passed, _detail = orchestrator._run_verify_script("sub/check.ps1", str(tmp_path))
    assert passed is True
    assert str(tmp_path / "sub" / "check.ps1") in rec["cmd"]
