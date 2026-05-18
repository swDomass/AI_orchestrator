"""Tests for replay.py — JSONL run record store + rotation."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import replay
from replay import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_RETRY,
    RunRecord,
    TokenUsage,
    append_run,
    build_record,
    new_run_id,
    prompt_hash,
    read_runs,
    rotate_now,
    set_store_path,
)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path: Path):
    store = tmp_path / "runs.jsonl"
    archive = tmp_path / "runs-archive"
    set_store_path(store, archive)
    yield
    replay.reset_for_tests()


def _make_record(
    *,
    run_id: str = "r1",
    ts_start: str = "2026-05-18T10:00:00",
    ts_end: str = "2026-05-18T10:01:00",
    exit_status: str = EXIT_OK,
    error_code: str | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        ts_start=ts_start,
        ts_end=ts_end,
        task_text="Test task",
        task_id="t-1",
        cwd="/tmp/x",
        provider="claude",
        model="claude-opus-4-7",
        tool="dev-loop",
        profile="default",
        prompt_hash="sha256:abc",
        tokens=TokenUsage(input=10, output=20, cache_creation=5, cache_read=3),
        duration_sec=60.0,
        exit_status=exit_status,
        error_code=error_code,
        retry_count=0,
        needs_satisfied_by=[],
        log_refs=[],
    )


def test_append_writes_jsonl_line():
    rec = _make_record()
    assert append_run(rec) is True

    runs = read_runs()
    assert len(runs) == 1
    assert runs[0]["run_id"] == "r1"
    assert runs[0]["tokens"]["input"] == 10
    assert runs[0]["exit_status"] == EXIT_OK


def test_append_multiple_records():
    append_run(_make_record(run_id="a"))
    append_run(_make_record(run_id="b"))
    append_run(_make_record(run_id="c"))

    runs = read_runs()
    assert [r["run_id"] for r in runs] == ["a", "b", "c"]


def test_invalid_exit_status_coerced_to_error():
    rec = _make_record(exit_status="bogus")
    append_run(rec)

    runs = read_runs()
    assert runs[0]["exit_status"] == EXIT_ERROR


def test_read_runs_since_filter():
    append_run(_make_record(run_id="old", ts_start="2026-05-01T10:00:00"))
    append_run(_make_record(run_id="new", ts_start="2026-05-18T10:00:00"))

    cutoff = datetime(2026, 5, 10)
    runs = read_runs(since=cutoff)
    assert [r["run_id"] for r in runs] == ["new"]


def test_read_runs_until_filter():
    append_run(_make_record(run_id="old", ts_start="2026-05-01T10:00:00"))
    append_run(_make_record(run_id="new", ts_start="2026-05-18T10:00:00"))

    runs = read_runs(until=datetime(2026, 5, 10))
    assert [r["run_id"] for r in runs] == ["old"]


def test_read_runs_limit():
    for i in range(5):
        append_run(_make_record(run_id=f"r{i}"))

    runs = read_runs(limit=2)
    assert len(runs) == 2


def test_corrupt_line_is_skipped():
    rec = _make_record(run_id="ok")
    append_run(rec)

    # Inject a corrupt line directly
    with open(replay.get_store_path(), "a", encoding="utf-8") as f:
        f.write("garbage-not-json\n")

    append_run(_make_record(run_id="ok2"))
    runs = read_runs()
    assert [r["run_id"] for r in runs] == ["ok", "ok2"]


def test_prompt_hash_stable():
    h1 = prompt_hash("hello")
    h2 = prompt_hash("hello")
    h3 = prompt_hash("world")
    assert h1 == h2
    assert h1 != h3
    assert h1.startswith("sha256:")


def test_prompt_hash_empty():
    assert prompt_hash("") == ""


def test_build_record_populates_fields():
    start = datetime(2026, 5, 18, 10, 0)
    end = datetime(2026, 5, 18, 10, 5)
    rec = build_record(
        run_id="x",
        ts_start=start,
        ts_end=end,
        task_text="do the thing",
        task_id="t-1",
        cwd="/proj",
        provider="claude",
        model="claude-opus-4-7",
        tool="dev-loop",
        profile="work",
        prompt="hash-me",
        input_tokens=100,
        output_tokens=200,
        cache_creation_input_tokens=50,
        cache_read_input_tokens=25,
        exit_status=EXIT_OK,
        retry_count=2,
        needs_satisfied_by=["dep-a"],
    )
    assert rec.duration_sec == 300.0
    assert rec.tokens.input == 100
    assert rec.tokens.output == 200
    assert rec.tokens.cache_creation == 50
    assert rec.tokens.cache_read == 25
    assert rec.prompt_hash.startswith("sha256:")
    assert rec.needs_satisfied_by == ["dep-a"]


def test_build_record_invalid_status_coerced():
    rec = build_record(
        run_id="x",
        ts_start=datetime.now(),
        task_text="t",
        exit_status="garbage",
    )
    assert rec.exit_status == EXIT_ERROR


def test_rotation_archives_old_records():
    from datetime import date

    # Suppress lazy rotation during append so we can rotate explicitly below.
    replay._last_rotate_date = date.today()

    old_ts = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S")
    new_ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    append_run(_make_record(run_id="old", ts_start=old_ts))
    append_run(_make_record(run_id="new", ts_start=new_ts))

    archived, _ = rotate_now(max_age_days=30)
    assert archived == 1

    live_runs = read_runs()
    assert [r["run_id"] for r in live_runs] == ["new"]

    all_runs = read_runs(include_archive=True)
    ids = sorted(r["run_id"] for r in all_runs)
    assert ids == ["new", "old"]


def test_lazy_rotation_runs_once_per_day():
    """append_run() should trigger rotation at most once per calendar day."""
    from datetime import date

    old_ts = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S")
    new_ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    append_run(_make_record(run_id="old", ts_start=old_ts))
    # First append triggered rotation; live store has only the just-written
    # record (which was old → archived), so store is empty.
    assert read_runs() == []
    assert any(r["run_id"] == "old" for r in read_runs(include_archive=True))

    # A subsequent old record stays in live store: rotation already ran today.
    append_run(_make_record(run_id="old2", ts_start=old_ts))
    assert replay._last_rotate_date == date.today()
    assert [r["run_id"] for r in read_runs()] == ["old2"]


def test_rotation_appends_to_existing_archive():
    from datetime import date

    replay._last_rotate_date = date.today()

    old_ts1 = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S")
    append_run(_make_record(run_id="a", ts_start=old_ts1))
    rotate_now(max_age_days=30)

    replay._last_rotate_date = date.today()
    append_run(_make_record(run_id="b", ts_start=old_ts1))
    rotate_now(max_age_days=30)

    archive_files = list(replay.get_archive_dir().glob("*.jsonl.gz"))
    assert len(archive_files) == 1

    contents = []
    with gzip.open(archive_files[0], "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                contents.append(json.loads(line)["run_id"])
    assert sorted(contents) == ["a", "b"]


def test_rotation_keeps_malformed_lines():
    from datetime import date

    replay._last_rotate_date = date.today()

    with open(replay.get_store_path(), "w", encoding="utf-8") as f:
        f.write("not-json\n")
    append_run(_make_record(run_id="ok"))

    archived, _ = rotate_now(max_age_days=30)
    assert archived == 0  # malformed line not counted as archived

    runs = read_runs()
    # Both lines preserved (malformed survives + ok still there)
    assert any(r.get("run_id") == "ok" for r in runs)


def test_new_run_id_is_unique():
    ids = {new_run_id() for _ in range(50)}
    assert len(ids) == 50


def test_empty_store_returns_empty_list():
    assert read_runs() == []


def test_set_store_path_resets_state(tmp_path: Path):
    set_store_path(tmp_path / "alt.jsonl")
    append_run(_make_record(run_id="alt"))

    assert (tmp_path / "alt.jsonl").exists()
    runs = read_runs()
    assert [r["run_id"] for r in runs] == ["alt"]
