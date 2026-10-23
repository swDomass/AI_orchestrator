"""Queue-Editor S3a: Sicherungspaare, Rotation, Undo-Verfuegbarkeit (nur tmp_path)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    import config
    import queue_editor
    import queue_manager

NOW = datetime(2026, 10, 23, 12, 0)
BASE = b"# Queue\r\n\r\n## Queue\r\n- [ ] Alpha\r\n- [ ] Beta\r\n"
STEM_RE = re.compile(r"^\d{8}-\d{6}-\d{6}-(add|edit|delete|retry|duplicate|move|undo|raw)$")


@pytest.fixture
def qfile(tmp_path, monkeypatch):
    path = tmp_path / "agent-queue.md"
    monkeypatch.setattr(queue_manager, "QUEUE_FILE", path)
    monkeypatch.setattr(queue_manager, "QUEUE_EVENTS_LOG_FILE", tmp_path / "events.log")
    assert queue_manager.queue_file().is_relative_to(tmp_path)
    assert Path(config.QUEUE_BACKUP_DIR).is_relative_to(tmp_path)
    path.write_bytes(BASE)
    return path


def apply(op, body, **kw):
    return queue_editor.apply_edit(op, body, **{"in_flight": None, "mode": "inprocess", "now": NOW, **kw})


def undo(**kw):
    return apply("undo", {"backup_id": state()["undo"]["id"]}, **kw)


def code_of(op, body, **kw):
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply(op, body, **kw)
    return exc.value.code


def add(text):
    return apply("add", {"fields": {"text": text}, "position": "bottom"})


def files(suffix):
    d = Path(config.QUEUE_BACKUP_DIR)
    return sorted(d.glob(f"*{suffix}")) if d.exists() else []


def state():
    return queue_editor.queue_state(in_flight=None, mode="inprocess", now=NOW)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def test_pair_holds_the_bytes_before_the_change_and_the_sha_of_the_new_file(qfile):
    add("Neu")
    (md,), (js,) = files(".md"), files(".json")
    meta = json.loads(js.read_text(encoding="utf-8"))
    assert md.read_bytes() == BASE and md.stem == js.stem == meta["id"] and STEM_RE.match(meta["id"])
    assert meta["version"] == 1 and meta["action"] == "add" and meta["summary"] == "Neu"
    assert meta["before_sha"] == sha(BASE) and meta["after_sha"] == sha(qfile.read_bytes())
    assert meta["queue_file"] == str(qfile.resolve()) and meta["created_at"]


def test_rotation_keeps_the_newest_20_pairs_and_leaves_other_queues_alone(qfile, tmp_path):
    d = Path(config.QUEUE_BACKUP_DIR)
    d.mkdir(parents=True)
    other = {"version": 1, "id": "20200101-000000-000000-add", "queue_file": str(tmp_path / "other.md")}
    (d / "20200101-000000-000000-add.json").write_text(json.dumps(other), encoding="utf-8")
    (d / "20200101-000000-000000-add.md").write_bytes(b"other")
    (d / "20210101-000000-000000-add.md").write_bytes(b"orphan")  # .md ohne Marker, aelter als die neueste
    assert config.QUEUE_BACKUP_KEEP == 20
    for i in range(23):
        add(f"T{i:02d}")
    mine = [p for p in files(".json") if json.loads(p.read_text(encoding="utf-8"))["queue_file"] == str(qfile.resolve())]
    assert len(mine) == 20 and len([p for p in files(".md") if p.stem in {m.stem for m in mine}]) == 20
    assert (d / "20200101-000000-000000-add.json").exists() and (d / "20200101-000000-000000-add.md").read_bytes() == b"other"
    assert not (d / "20210101-000000-000000-add.md").exists()
    assert len(files(".md")) == 21 and len(files(".json")) == 21  # 20 eigene + 1 fremdes Paar
    oldest = json.loads(mine[0].read_text(encoding="utf-8"))
    assert oldest["summary"] == "T03"  # T00-T02 sind rotiert


def test_a_failing_backup_leaves_the_queue_untouched(qfile, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_bytes(b"x")
    monkeypatch.setattr(config, "QUEUE_BACKUP_DIR", blocker)
    with pytest.raises(queue_editor.QueueEditError) as exc:
        add("Neu")
    assert exc.value.code == "backup_failed" and qfile.read_bytes() == BASE


def test_a_failed_write_removes_the_backup_of_that_pass_and_reuses_it_on_retry(qfile, monkeypatch):
    real, calls = queue_manager._write_bytes_atomic, []

    def deny(path, data):
        if Path(path) == qfile:
            raise PermissionError("denied")
        return real(path, data)

    spy = queue_editor._write_backup
    monkeypatch.setattr(queue_manager, "_write_bytes_atomic", deny)
    monkeypatch.setattr(queue_editor, "_write_backup", lambda *a: calls.append(1) or spy(*a))
    with pytest.raises(queue_editor.QueueEditError) as exc:
        add("Neu")
    assert exc.value.code == "unavailable" and qfile.read_bytes() == BASE
    assert len(calls) == 1 and files(".md") == [] and files(".json") == []


def test_unchanged_result_writes_no_backup(qfile):
    ref = {"lines": ["- [ ] Beta"], "occurrence": 0, "occurrences": 1}
    assert apply("edit", {"ref": ref, "text": "Beta"})["changed"] is False
    assert files(".md") == [] and state()["undo"] == {"available": False}


def test_undo_is_offered_for_the_newest_change_only_while_the_file_is_unchanged(qfile):
    assert state()["undo"] == {"available": False}
    add("Neu")
    undo = state()["undo"]
    assert undo["available"] is True and undo["action"] == "add" and undo["summary"] == "Neu"
    assert undo["id"] == files(".json")[-1].stem and undo["at"]
    qfile.write_bytes(qfile.read_bytes() + b"- [ ] von Obsidian\r\n")  # Aenderung von aussen -> undo_stale
    assert state()["undo"] == {"available": False}


def test_undo_is_not_offered_after_an_undo_backup(qfile):
    raw = qfile.read_bytes()
    queue_editor._write_backup(qfile, b"alt", raw.decode(), "edit", "x")
    assert state()["undo"]["available"] is True
    queue_editor._write_backup(qfile, b"alt", raw.decode(), "undo", "x")
    assert state()["undo"] == {"available": False}


def test_undo_looks_only_at_backups_of_the_current_queue_file(qfile, tmp_path):
    add("Neu")
    other = tmp_path / "other-queue.md"
    other.write_bytes(b"## Queue\r\n")
    queue_editor._write_backup(other, b"", "## Queue\r\n- [ ] x\r\n", "add", "x")  # neuer, aber fremd
    assert state()["undo"]["available"] is True and state()["undo"]["summary"] == "Neu"


def test_keep_value_is_read_at_call_time(qfile, monkeypatch):
    keep = 3
    monkeypatch.setattr(config, "QUEUE_BACKUP_KEEP", keep)
    for i in range(5):
        add(f"K{i}")
    assert len(files(".json")) == keep


# --- undo ------------------------------------------------------------------------------

def test_delete_then_undo_restores_the_file_byte_for_byte_and_allows_no_second_undo(qfile):
    apply("delete", {"ref": {"lines": ["- [ ] Beta"], "occurrence": 0, "occurrences": 1}})
    assert qfile.read_bytes() != BASE
    old_id = state()["undo"]["id"]
    res = undo()
    assert res["changed"] is True and qfile.read_bytes() == BASE
    assert res["queue"]["undo"] == {"available": False} and files(".json")[-1].stem.endswith("-undo")
    assert code_of("undo", {"backup_id": old_id}) == "no_undo" and qfile.read_bytes() == BASE


def test_undo_without_any_backup_is_no_undo(qfile):
    assert code_of("undo", {"backup_id": "x"}) == "no_undo" and qfile.read_bytes() == BASE


def test_undo_refuses_a_change_from_outside_and_a_wrong_backup_id(qfile):
    add("Neu")
    uid = state()["undo"]["id"]
    assert code_of("undo", {"backup_id": uid + "x"}) == "undo_changed"
    after_add = qfile.read_bytes()
    qfile.write_bytes(after_add + b"- [ ] von Obsidian\r\n")
    assert code_of("undo", {"backup_id": uid}) == "undo_stale"
    assert qfile.read_bytes() == after_add + b"- [ ] von Obsidian\r\n" and len(files(".json")) == 1


def test_undo_refuses_a_tampered_backup(qfile):
    add("Neu")
    after_add = qfile.read_bytes()
    files(".md")[-1].write_bytes(BASE + b"- [ ] manipuliert\r\n")
    assert code_of("undo", {"backup_id": state()["undo"]["id"]}) == "backup_corrupt"
    assert qfile.read_bytes() == after_add and len(files(".json")) == 1


def test_undo_that_removes_the_running_line_is_locked_and_standalone_asks_first(qfile):
    ref = {"lines": ["- [ ] Alpha"], "occurrence": 0, "occurrences": 1}
    apply("edit", {"ref": ref, "text": "Alpha 2"})
    after_edit = qfile.read_bytes()
    run = SimpleNamespace(task_text="Alpha 2", subtasks=[], started_at=None, tool=None)
    assert code_of("undo", {"backup_id": state()["undo"]["id"]}, in_flight=run) == "running"
    assert code_of("undo", {"backup_id": state()["undo"]["id"]}, mode="standalone") == "confirm_unknown_running"
    assert qfile.read_bytes() == after_edit and len(files(".json")) == 1
    res = apply("undo", {"backup_id": state()["undo"]["id"], "confirm_unknown_running": True}, mode="standalone")
    assert res["changed"] is True and qfile.read_bytes() == BASE


def test_undo_of_a_done_line_change_asks_nothing_in_standalone_mode(qfile):
    qfile.write_bytes(b"## Queue\r\n- [x] Fertig \xe2\x9c\x85 2026-09-01 10:00 (claude)\r\n- [ ] Alpha\r\n")
    before = qfile.read_bytes()
    row = "- [x] Fertig ✅ 2026-09-01 10:00 (claude)"
    apply("delete", {"ref": {"lines": [row], "occurrence": 0, "occurrences": 1}}, mode="standalone")
    assert undo(mode="standalone")["changed"] is True and qfile.read_bytes() == before


# --- R2a P3: Wiederholung nach erfolgreichem Schreiben, OSError beim Lesen, UTC-Namen -----------

def _unlock_fails_once_after_the_write(monkeypatch, path):
    real, seen = queue_manager._unlock_file, {"raised": False}

    def flaky(f):
        real(f)
        if not seen["raised"] and path.read_bytes() != BASE:
            seen["raised"] = True
            raise PermissionError("unlock")

    monkeypatch.setattr(queue_manager, "_unlock_file", flaky)
    monkeypatch.setattr(queue_manager.time, "sleep", lambda _s: None)
    return seen


def test_a_retry_after_a_successful_write_does_not_mutate_twice_and_keeps_the_backup(qfile, monkeypatch):
    seen = _unlock_fails_once_after_the_write(monkeypatch, qfile)
    res = apply("add", {"fields": {"text": "Neu"}, "position": "top"})
    assert seen["raised"] and res["changed"] is True
    assert qfile.read_bytes().count(b"- [ ] Neu") == 1 and len(list(Path(config.QUEUE_BACKUP_DIR).glob("*.json"))) == 1
    undo()
    assert qfile.read_bytes() == BASE


def test_a_permission_error_in_the_own_read_is_retried_and_never_a_500(qfile, monkeypatch):
    real, calls = queue_editor._read_raw, {"n": 0}

    def flaky(p):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("busy")
        return real(p)

    monkeypatch.setattr(queue_editor, "_read_raw", flaky)
    monkeypatch.setattr(queue_manager.time, "sleep", lambda _s: None)
    assert apply("add", {"fields": {"text": "Neu"}})["changed"] is True and qfile.read_bytes().count(b"Neu") == 1
    monkeypatch.setattr(queue_editor, "_read_raw", lambda _p: (_ for _ in ()).throw(PermissionError("busy")))
    before = qfile.read_bytes()
    assert code_of("add", {"fields": {"text": "Zwei"}}) == "unavailable" and qfile.read_bytes() == before


def test_backup_names_use_utc_so_a_clock_change_cannot_reorder_them(qfile, monkeypatch):
    clock = [datetime(2026, 10, 25, 0, 30, tzinfo=UTC), datetime(2026, 10, 25, 1, 10, tzinfo=UTC)]

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.pop(0) if tz is UTC else datetime.now(tz)

    monkeypatch.setattr(queue_editor, "datetime", FakeDT)
    apply("add", {"fields": {"text": "Eins"}})
    apply("add", {"fields": {"text": "Zwei"}})
    metas = queue_editor._backup_metas(queue_manager.queue_file())
    assert [m["id"][:15] for m in metas] == ["20261025-003000", "20261025-011000"] and not clock
    assert queue_editor.queue_state(in_flight=None, mode="inprocess", now=NOW)["undo"]["id"] == metas[-1]["id"]
