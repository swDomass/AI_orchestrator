"""The orchestrator marks its own process tree as unattended (2026-11-24).

`scripts/safety_hook.py` allows a plain push only when Claude Code reports an
attended session AND `ORCH_UNATTENDED` is not set. Claude Code can report
`CLAUDE_CODE_SESSION_ATTENDED=1` for a headless child (live probe: entrypoint
`claude-vscode`, no parent session), so the orchestrator sets the marker itself —
as the FIRST thing `main()` does, before any child process can start.
"""
import os

import orchestrator

_MARKER = "ORCH_UNATTENDED"


def _unset_marker(monkeypatch):
    # setenv first so monkeypatch records the absent state and restores it afterwards
    monkeypatch.setenv(_MARKER, "")
    monkeypatch.delenv(_MARKER)


def test_mark_unattended_process_sets_one(monkeypatch):
    _unset_marker(monkeypatch)

    orchestrator._mark_unattended_process()

    assert os.environ.get(_MARKER) == "1"


def test_main_sets_marker_before_main_body_runs(monkeypatch):
    _unset_marker(monkeypatch)
    seen = {}

    def fake_main():
        seen["marker"] = os.environ.get(_MARKER)

    monkeypatch.setattr(orchestrator, "_main", fake_main)
    monkeypatch.setattr(orchestrator, "setup_logging", lambda: None)
    monkeypatch.setattr(orchestrator, "install_thread_excepthook", lambda: None)

    orchestrator.main()

    assert seen == {"marker": "1"}


def test_main_sets_marker_before_logging_setup(monkeypatch):
    _unset_marker(monkeypatch)
    seen = {}

    def fake_setup_logging():
        seen["marker"] = os.environ.get(_MARKER)

    monkeypatch.setattr(orchestrator, "_main", lambda: None)
    monkeypatch.setattr(orchestrator, "setup_logging", fake_setup_logging)
    monkeypatch.setattr(orchestrator, "install_thread_excepthook", lambda: None)

    orchestrator.main()

    assert seen == {"marker": "1"}
