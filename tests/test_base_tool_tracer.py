"""Unit tests for ToolTracer (tools/base_tool.py).

Covers:
- create() with a real cwd writes trace files under .{tool}/traces/{uuid}.jsonl
- create() with cwd=None silently disables (no exception, emit() is a no-op)
- create() recovers when mkdir fails (returns disabled tracer, logs warning)
- emit() writes one JSON line per call, appends across calls
- emit() never raises when the file handle goes away mid-run
- the emitted JSONL has the expected schema (ts, elapsed_sec, run_id, tool, action, details)
"""

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.base_tool import ToolTracer


def test_create_with_cwd_writes_file(tmp_path: Path) -> None:
    tracer = ToolTracer.create(tool_name="deep-security-audit", cwd=str(tmp_path))

    assert tracer.trace_file is not None
    assert tracer.trace_file.parent == tmp_path / ".deep-security-audit" / "traces"
    assert tracer.trace_file.name.endswith(".jsonl")
    # run_id is a valid uuid string
    uuid.UUID(tracer.run_id)


def test_create_without_cwd_returns_disabled_tracer() -> None:
    tracer = ToolTracer.create(tool_name="dev-loop", cwd=None)

    assert tracer.trace_file is None
    # emit must not raise
    tracer.emit("run_start", task="anything")


def test_create_when_mkdir_fails_returns_disabled_tracer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with patch("tools.base_tool.Path.mkdir", side_effect=OSError("disk full")):
        tracer = ToolTracer.create(tool_name="review-loop", cwd=str(tmp_path))

    assert tracer.trace_file is None
    assert any("Tool trace setup failed" in rec.message for rec in caplog.records)


def test_emit_writes_one_jsonl_line(tmp_path: Path) -> None:
    tracer = ToolTracer.create(tool_name="critical-review", cwd=str(tmp_path))
    tracer.emit("run_start", task="audit X", provider="claude")

    lines = tracer.trace_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["action"] == "run_start"
    assert entry["tool"] == "critical-review"
    assert entry["run_id"] == tracer.run_id
    assert entry["details"] == {"task": "audit X", "provider": "claude"}
    assert "ts" in entry
    assert isinstance(entry["elapsed_sec"], (int, float))


def test_emit_appends_multiple_lines(tmp_path: Path) -> None:
    tracer = ToolTracer.create(tool_name="deep-security-audit", cwd=str(tmp_path))
    tracer.emit("run_start", task="t")
    tracer.emit("phase_start", phase="pentester")
    tracer.emit("phase_end", phase="pentester", success=True)
    tracer.emit("run_end", success=True)

    lines = tracer.trace_file.read_text(encoding="utf-8").splitlines()
    actions = [json.loads(line)["action"] for line in lines]
    assert actions == ["run_start", "phase_start", "phase_end", "run_end"]


def test_emit_when_file_write_fails_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tracer = ToolTracer.create(tool_name="dev-loop", cwd=str(tmp_path))
    # Simulate a transient write failure (e.g. disk gone, lock contention).
    with patch("tools.base_tool.Path.open", side_effect=OSError("locked")):
        tracer.emit("phase_start", phase="execute")  # must not raise

    assert any("Tool trace write failed" in rec.message for rec in caplog.records)


def test_disabled_tracer_emit_is_silent_noop(tmp_path: Path) -> None:
    tracer = ToolTracer.create(tool_name="x", cwd=None)
    # Calling emit many times must do nothing and never raise.
    for i in range(50):
        tracer.emit("phase_start", phase=f"p{i}", iteration=i)
    # No file was created anywhere.
    assert tracer.trace_file is None


def test_emit_preserves_unicode_in_details(tmp_path: Path) -> None:
    tracer = ToolTracer.create(tool_name="deep-security-audit", cwd=str(tmp_path))
    tracer.emit("finding_detected", title="CWE-79 reflected XSS — Übersicht", severity="HIGH")

    raw = tracer.trace_file.read_text(encoding="utf-8")
    assert "Übersicht" in raw  # ensure_ascii=False preserved the umlaut
    entry = json.loads(raw)
    assert entry["details"]["title"] == "CWE-79 reflected XSS — Übersicht"


def test_create_uses_unique_run_ids_per_call(tmp_path: Path) -> None:
    t1 = ToolTracer.create(tool_name="dev-loop", cwd=str(tmp_path))
    t2 = ToolTracer.create(tool_name="dev-loop", cwd=str(tmp_path))
    assert t1.run_id != t2.run_id
    assert t1.trace_file != t2.trace_file


def test_elapsed_sec_monotonically_increases(tmp_path: Path) -> None:
    import time as _time
    tracer = ToolTracer.create(tool_name="dev-loop", cwd=str(tmp_path))
    tracer.emit("a")
    _time.sleep(0.01)
    tracer.emit("b")

    lines = tracer.trace_file.read_text(encoding="utf-8").splitlines()
    e1, e2 = json.loads(lines[0]), json.loads(lines[1])
    assert e2["elapsed_sec"] >= e1["elapsed_sec"]
