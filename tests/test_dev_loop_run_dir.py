"""Tests for the per-task dev-loop output directory (.dev-loop/<task-hash>/).

Before 2026-09-09 ``DEV_LOOP_DIR`` was keyed by cwd alone: ``_task_hash()`` was
computed and written INTO state.json but never reached the path, so two
dev-loops in the same repository overwrote each other's research-and-plan.md,
round-00N.md, summary.md and state.json.

Two invariants have to survive the fix: the ``.dev-loop/traces/`` location that
``ToolTracer`` and ``analytics._parse_tool_traces()`` agree on, and the ability
to resume a run that was interrupted by the upgrade itself.
"""

import json

import pytest

from providers.base import RunResult
from tools.dev_loop import (
    DEV_LOOP_DIR,
    DevLoopTool,
    _clear_state,
    _load_state,
    _run_dir,
    _save_state,
    _task_hash,
)


class _ScriptedProvider:
    name = "claude"
    supports_sessions = False

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    def run(self, task, cwd=None, timeout=0, **kwargs):
        self.prompts.append(task)
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))


def _patch(monkeypatch):
    monkeypatch.setattr("tools.dev_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.dev_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.dev_loop.time.sleep", lambda _: None)
    monkeypatch.setattr("tools.dev_loop.is_cached_provider_available", lambda _n: True)


def _clean_run():
    return [
        "## Problem Analysis\nFound.\n## Implementation Plan\n1. Fix.",
        "Executed.",
        "No P1/P2/P3 findings.",
        "RESOLVED: done.",
    ]


# ---------------------------------------------------------------------------
# _run_dir
# ---------------------------------------------------------------------------

def test_run_dir_nests_the_hash_under_the_parent(tmp_path):
    d = _run_dir(str(tmp_path), "deadbeef")
    assert d == tmp_path / DEV_LOOP_DIR / "deadbeef"


def test_run_dir_tolerates_missing_cwd():
    assert _run_dir(None, "deadbeef").parts[-2:] == (DEV_LOOP_DIR, "deadbeef")


def test_different_tasks_get_different_dirs(tmp_path):
    a = _run_dir(str(tmp_path), _task_hash("task A"))
    b = _run_dir(str(tmp_path), _task_hash("task B"))
    assert a != b


def test_identical_task_text_keeps_the_same_dir(tmp_path):
    # Deliberate: that is what makes a resume find its own plan again.
    assert _run_dir(str(tmp_path), _task_hash("same")) == _run_dir(str(tmp_path), _task_hash("same"))


# ---------------------------------------------------------------------------
# The actual defect: two runs in one repo must not overwrite each other
# ---------------------------------------------------------------------------

def test_two_tasks_in_one_cwd_keep_separate_round_files(monkeypatch, tmp_path):
    _patch(monkeypatch)
    DevLoopTool().run("Task one", _ScriptedProvider(_clean_run()), cwd=str(tmp_path))
    DevLoopTool().run("Task two", _ScriptedProvider(_clean_run()), cwd=str(tmp_path))

    first = _run_dir(str(tmp_path), _task_hash("Task one")) / "round-001.md"
    second = _run_dir(str(tmp_path), _task_hash("Task two")) / "round-001.md"

    assert first.exists() and second.exists()
    assert first != second
    assert "Task one" in first.read_text(encoding="utf-8")
    assert "Task two" in second.read_text(encoding="utf-8")


def test_two_tasks_in_one_cwd_keep_separate_plans_and_summaries(monkeypatch, tmp_path):
    _patch(monkeypatch)
    DevLoopTool().run("Alpha work", _ScriptedProvider(_clean_run()), cwd=str(tmp_path))
    DevLoopTool().run("Beta work", _ScriptedProvider(_clean_run()), cwd=str(tmp_path))

    for task in ("Alpha work", "Beta work"):
        run_dir = _run_dir(str(tmp_path), _task_hash(task))
        assert task in (run_dir / "research-and-plan.md").read_text(encoding="utf-8")
        assert task in (run_dir / "summary.md").read_text(encoding="utf-8")


def test_traces_stay_at_the_parent_level(monkeypatch, tmp_path):
    """analytics._parse_tool_traces() globs `**/.*/traces/*.jsonl` and attributes
    by the parent directory name — the hash must not move that."""
    _patch(monkeypatch)
    DevLoopTool().run("Trace me", _ScriptedProvider(_clean_run()), cwd=str(tmp_path))

    traces = list((tmp_path / DEV_LOOP_DIR / "traces").glob("*.jsonl"))
    assert traces, "trace file left the .dev-loop/traces/ location"


# ---------------------------------------------------------------------------
# State: per-task location, legacy fallback, and the deletion rule
# ---------------------------------------------------------------------------

def test_state_round_trips_through_the_per_task_dir(tmp_path):
    h = _task_hash("some task")
    _save_state(str(tmp_path), h, {"tool": "dev-loop", "task_hash": h, "phase": "x"})

    assert (_run_dir(str(tmp_path), h) / "state.json").exists()
    assert _load_state(str(tmp_path), h)["phase"] == "x"


def test_state_of_another_task_is_not_loaded(tmp_path):
    h = _task_hash("mine")
    _save_state(str(tmp_path), h, {"tool": "dev-loop", "task_hash": h, "phase": "x"})

    assert _load_state(str(tmp_path), _task_hash("theirs")) is None


def test_legacy_state_is_still_found(tmp_path):
    """A run interrupted BY the upgrade keeps its resume point."""
    h = _task_hash("interrupted task")
    legacy_dir = tmp_path / DEV_LOOP_DIR
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "state.json").write_text(
        json.dumps({"tool": "dev-loop", "task_hash": h, "phase": "research_and_plan_done",
                    "research_and_plan": "old plan"}),
        encoding="utf-8",
    )

    state = _load_state(str(tmp_path), h)
    assert state is not None
    assert state["research_and_plan"] == "old plan"


def test_legacy_state_of_a_different_task_is_ignored(tmp_path):
    legacy_dir = tmp_path / DEV_LOOP_DIR
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "state.json").write_text(
        json.dumps({"tool": "dev-loop", "task_hash": "ffffffff", "phase": "research_done"}),
        encoding="utf-8",
    )

    assert _load_state(str(tmp_path), _task_hash("unrelated")) is None


def test_legacy_state_of_a_different_tool_is_ignored(tmp_path):
    h = _task_hash("t")
    legacy_dir = tmp_path / DEV_LOOP_DIR
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "state.json").write_text(
        json.dumps({"tool": "review-loop", "task_hash": h}), encoding="utf-8"
    )

    assert _load_state(str(tmp_path), h) is None


def test_clear_state_removes_the_matching_legacy_file(tmp_path):
    h = _task_hash("done task")
    legacy = tmp_path / DEV_LOOP_DIR / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"tool": "dev-loop", "task_hash": h}), encoding="utf-8")

    _clear_state(str(tmp_path), h)

    assert not legacy.exists()


def test_clear_state_keeps_a_legacy_file_belonging_to_another_task(tmp_path):
    # The legacy location is shared by definition — deleting it blindly would
    # destroy a different task's resume point.
    legacy = tmp_path / DEV_LOOP_DIR / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"tool": "dev-loop", "task_hash": "ffffffff"}), encoding="utf-8")

    _clear_state(str(tmp_path), _task_hash("someone else"))

    assert legacy.exists()


def test_clear_state_on_a_missing_file_is_a_noop(tmp_path):
    _clear_state(str(tmp_path), _task_hash("never ran"))  # must not raise


def test_successful_run_clears_its_own_state(monkeypatch, tmp_path):
    _patch(monkeypatch)
    DevLoopTool().run("Clear me", _ScriptedProvider(_clean_run()), cwd=str(tmp_path))

    assert not (_run_dir(str(tmp_path), _task_hash("Clear me")) / "state.json").exists()


def test_legacy_state_lets_a_run_skip_the_research_phase(monkeypatch, tmp_path):
    """End-to-end resume across the upgrade: the plan comes from the legacy file,
    so the provider is never asked for research."""
    _patch(monkeypatch)
    task = "Resume me"
    legacy = tmp_path / DEV_LOOP_DIR / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps({
            "tool": "dev-loop",
            "task_hash": _task_hash(task),
            "phase": "research_and_plan_done",
            "research_and_plan": "## Implementation Plan\n1. Cached step.",
        }),
        encoding="utf-8",
    )

    provider = _ScriptedProvider([
        "Executed.",
        "No P1/P2/P3 findings.",
        "RESOLVED: done.",
    ])
    result = DevLoopTool().run(task, provider, cwd=str(tmp_path))

    assert result.success is True
    assert len(provider.prompts) == 3  # execution + 2 reviews, no research call
    assert "Cached step." in result.output


# ---------------------------------------------------------------------------
# Malformed state files
#
# json.load() succeeds on `null`, `[]`, `"x"` and `5` — valid JSON that is not
# an object — so json.JSONDecodeError never fires and `.get` used to raise
# AttributeError straight out of both _load_state() and _clear_state().
# ---------------------------------------------------------------------------

_MALFORMED_STATES = ["null", "[]", '"x"', "5", "true", "truncated{", ""]


@pytest.mark.parametrize("payload", _MALFORMED_STATES)
def test_malformed_per_task_state_is_treated_as_missing(tmp_path, payload):
    task_hash = _task_hash("Broken state")
    state_file = _run_dir(str(tmp_path), task_hash) / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(payload, encoding="utf-8")

    assert _load_state(str(tmp_path), task_hash) is None
    _clear_state(str(tmp_path), task_hash)  # must not raise


@pytest.mark.parametrize("payload", _MALFORMED_STATES)
def test_malformed_legacy_state_is_treated_as_missing(tmp_path, payload):
    task_hash = _task_hash("Broken legacy state")
    legacy = tmp_path / DEV_LOOP_DIR / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(payload, encoding="utf-8")

    assert _load_state(str(tmp_path), task_hash) is None
    _clear_state(str(tmp_path), task_hash)  # must not raise
    # Unreadable is not the same as ours: a file we cannot attribute stays put.
    assert legacy.exists()


def test_junk_legacy_state_does_not_fail_an_otherwise_complete_run(monkeypatch, tmp_path):
    """The consequence at the call site: _clear_state() runs immediately before
    dev-loop returns success, so a junk state file used to convert a finished
    run — plan written, reviews passed, work done — into an uncaught exception."""
    _patch(monkeypatch)
    legacy = tmp_path / DEV_LOOP_DIR / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("null", encoding="utf-8")

    result = DevLoopTool().run(
        "Finish cleanly", _ScriptedProvider(_clean_run()), cwd=str(tmp_path)
    )

    assert result.success is True


# ---------------------------------------------------------------------------
# State cut inside a multi-byte character
#
# The other half of "unusable state file", and the likelier half: _save_state()
# writes with ensure_ascii=False and truncate-then-write, so a run killed
# mid-write leaves the file ending part-way through a "—" or an umlaut. That
# raises UnicodeDecodeError — a ValueError, but NOT a json.JSONDecodeError, so
# the original guard let it out of both call sites uncaught.
# ---------------------------------------------------------------------------


def _cut_mid_character(task_hash: str) -> bytes:
    """Valid state json, cut so the last byte is an orphaned lead byte."""
    payload = json.dumps(
        {"task_hash": task_hash, "tool": "dev-loop", "phase": "research_and_plan_done",
         "research_and_plan": "Fix the — dash"},
        ensure_ascii=False,
    ).encode("utf-8")
    # "—" is three bytes (e2 80 94); keep only the first one.
    return payload[: payload.index("—".encode("utf-8")) + 1]


def test_state_cut_mid_character_really_is_not_a_json_error():
    """Guards the reason the two-tuple names ValueError instead of a decoder."""
    with pytest.raises(UnicodeDecodeError) as excinfo:
        _cut_mid_character("x").decode("utf-8")
    assert isinstance(excinfo.value, ValueError)
    assert not isinstance(excinfo.value, json.JSONDecodeError)


def test_per_task_state_cut_mid_character_is_treated_as_missing(tmp_path):
    task_hash = _task_hash("Killed mid-write")
    state_file = _run_dir(str(tmp_path), task_hash) / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_bytes(_cut_mid_character(task_hash))

    assert _load_state(str(tmp_path), task_hash) is None
    _clear_state(str(tmp_path), task_hash)  # must not raise


def test_legacy_state_cut_mid_character_is_treated_as_missing(tmp_path):
    task_hash = _task_hash("Killed mid-write, legacy")
    legacy = tmp_path / DEV_LOOP_DIR / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(_cut_mid_character(task_hash))

    assert _load_state(str(tmp_path), task_hash) is None
    _clear_state(str(tmp_path), task_hash)  # must not raise
    # Unattributable, so it stays put — same rule as the malformed cases above.
    assert legacy.exists()


def test_state_cut_mid_character_does_not_fail_an_otherwise_complete_run(monkeypatch, tmp_path):
    """The call-site consequence: _clear_state() runs immediately before the
    success return, so an unguarded decode error there reports a finished run —
    plan written, reviews passed, work done — as a failure."""
    _patch(monkeypatch)
    task = "Finish cleanly despite a shredded state file"
    task_hash = _task_hash(task)
    state_file = _run_dir(str(tmp_path), task_hash) / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_bytes(_cut_mid_character(task_hash))

    result = DevLoopTool().run(task, _ScriptedProvider(_clean_run()), cwd=str(tmp_path))

    assert result.success is True
    # The shredded cache was ignored, not consumed: research really ran.
    assert "Research+Plan aus Cache geladen" not in result.output
