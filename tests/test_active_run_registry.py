"""Tests for ActiveRunRegistry + ToolTracer integration."""

import json
import time
from pathlib import Path

import pytest

from tools.base_tool import ActiveRunRegistry, ToolTracer


@pytest.fixture
def isolated_active_dir(monkeypatch, tmp_path):
    """Redirect ActiveRunRegistry to tmp_path so tests don't pollute logs/."""
    active_dir = tmp_path / "active_runs"
    monkeypatch.setattr("tools.base_tool.ACTIVE_RUNS_DIR", active_dir)
    return active_dir


def test_start_creates_file_with_expected_fields(isolated_active_dir):
    ActiveRunRegistry.start("run-1", "review-loop", "Fix bug", "/cwd", "claude")

    path = isolated_active_dir / "run-1.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["run_id"] == "run-1"
    assert data["tool"] == "review-loop"
    assert data["task"] == "Fix bug"
    assert data["cwd"] == "/cwd"
    assert data["provider"] == "claude"
    assert data["status"] == "running"
    assert data["tokens"] == {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}


def test_update_merges_fields_without_clobbering_others(isolated_active_dir):
    ActiveRunRegistry.start("run-2", "dev-loop", "Task", "/c", "codex")
    ActiveRunRegistry.update("run-2", iteration_current=3, phase="exec")

    data = json.loads((isolated_active_dir / "run-2.json").read_text(encoding="utf-8"))
    assert data["iteration_current"] == 3
    assert data["phase"] == "exec"
    # untouched fields still present
    assert data["provider"] == "codex"
    assert data["tool"] == "dev-loop"


def test_update_accumulates_token_deltas(isolated_active_dir):
    ActiveRunRegistry.start("run-3", "review-loop", "T", "/c", "claude")
    ActiveRunRegistry.update("run-3", tokens_delta={"input": 100, "output": 50})
    ActiveRunRegistry.update("run-3", tokens_delta={"input": 200, "cache_read": 1000})

    data = json.loads((isolated_active_dir / "run-3.json").read_text(encoding="utf-8"))
    assert data["tokens"]["input"] == 300
    assert data["tokens"]["output"] == 50
    assert data["tokens"]["cache_read"] == 1000
    assert data["tokens"]["cache_creation"] == 0


def test_end_deletes_file(isolated_active_dir):
    ActiveRunRegistry.start("run-4", "review-loop", "T", "/c", "claude")
    assert (isolated_active_dir / "run-4.json").exists()
    ActiveRunRegistry.end("run-4")
    assert not (isolated_active_dir / "run-4.json").exists()


def test_end_idempotent_when_file_missing(isolated_active_dir):
    # No start → should not raise
    ActiveRunRegistry.end("never-started")
    ActiveRunRegistry.end("never-started")


def test_update_on_missing_record_is_silent_noop(isolated_active_dir):
    # No start → update should not create a file
    ActiveRunRegistry.update("ghost", iteration_current=99)
    assert not (isolated_active_dir / "ghost.json").exists()


def test_list_active_filters_stale_and_cleans_old(isolated_active_dir):
    isolated_active_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    fresh = {
        "run_id": "fresh", "tool": "x", "task": "", "cwd": "", "provider": "",
        "started_at": now, "last_update": now, "tokens": {},
    }
    stale = {
        "run_id": "stale", "tool": "x", "task": "", "cwd": "", "provider": "",
        "started_at": now - 8 * 3600, "last_update": now - 7 * 3600, "tokens": {},
    }
    old = {
        "run_id": "old", "tool": "x", "task": "", "cwd": "", "provider": "",
        "started_at": now - 48 * 3600, "last_update": now - 48 * 3600, "tokens": {},
    }

    for record in (fresh, stale, old):
        (isolated_active_dir / f"{record['run_id']}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )

    active = ActiveRunRegistry.list_active()
    ids = {r["run_id"] for r in active}
    assert "fresh" in ids
    assert "stale" in ids
    assert "old" not in ids
    # old file was cleaned up
    assert not (isolated_active_dir / "old.json").exists()

    stale_record = next(r for r in active if r["run_id"] == "stale")
    assert stale_record["status"] == "stale"


def test_list_active_returns_empty_when_no_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.base_tool.ACTIVE_RUNS_DIR", tmp_path / "does_not_exist")
    assert ActiveRunRegistry.list_active() == []


def test_list_active_ignores_garbage_json(isolated_active_dir):
    isolated_active_dir.mkdir(parents=True, exist_ok=True)
    (isolated_active_dir / "broken.json").write_text("not json", encoding="utf-8")
    # Should not raise and not include the broken entry
    assert ActiveRunRegistry.list_active() == []


def test_tooltracer_emit_run_start_creates_active_run(isolated_active_dir, tmp_path):
    tracer = ToolTracer.create("review-loop", str(tmp_path))
    tracer.emit("run_start", task="Test task", cwd=str(tmp_path),
                provider="claude", max_iterations=20)

    files = list(isolated_active_dir.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["task"] == "Test task"
    assert data["iteration_max"] == 20


def test_tooltracer_emit_run_end_removes_active_run(isolated_active_dir, tmp_path):
    tracer = ToolTracer.create("review-loop", str(tmp_path))
    tracer.emit("run_start", task="t", cwd=str(tmp_path), provider="claude")
    assert len(list(isolated_active_dir.glob("*.json"))) == 1
    tracer.emit("run_end", success=True)
    assert len(list(isolated_active_dir.glob("*.json"))) == 0


def test_tooltracer_emit_iteration_updates_active_run(isolated_active_dir, tmp_path):
    tracer = ToolTracer.create("review-loop", str(tmp_path))
    tracer.emit("run_start", task="t", cwd=str(tmp_path), provider="claude",
                max_iterations=10)
    tracer.emit("iteration_start", iteration=3, max_iterations=10)
    tracer.emit("subprocess_result", phase="review", input_tokens=100,
                output_tokens=50)

    files = list(isolated_active_dir.glob("*.json"))
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["iteration_current"] == 3
    assert data["iteration_max"] == 10
    assert data["tokens"]["input"] == 100
    assert data["tokens"]["output"] == 50


def test_atomic_write_no_partial_read(isolated_active_dir):
    """Quick sanity check: after a write, the file is parseable."""
    ActiveRunRegistry.start("atom", "x", "task", "/c", "p")
    # Read 5 times — should never hit partial JSON
    for _ in range(5):
        path = isolated_active_dir / "atom.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["run_id"] == "atom"
        ActiveRunRegistry.update("atom", iteration_current=1)
