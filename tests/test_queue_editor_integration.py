"""Queue-Editor S3a: Zusammenspiel mit dem Orchestrator-Parser und -Stempel (nur tmp_path)."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    import config
    import queue_editor
    import queue_linter
    import queue_manager

NOW = datetime(2026, 10, 23, 12, 0)
BASE = "# Queue\r\n\r\n## Queue\r\n- [ ] Alpha\r\n- [ ] Beta\r\n"


@pytest.fixture
def qfile(tmp_path, monkeypatch):
    path = tmp_path / "agent-queue.md"
    monkeypatch.setattr(queue_manager, "QUEUE_FILE", path)
    monkeypatch.setattr(queue_manager, "QUEUE_EVENTS_LOG_FILE", tmp_path / "events.log")
    assert queue_manager.queue_file().is_relative_to(tmp_path)
    assert Path(config.QUEUE_BACKUP_DIR).is_relative_to(tmp_path)
    path.write_bytes(BASE.encode())
    return path


def apply(op, body):
    return queue_editor.apply_edit(op, body, in_flight=None, mode="inprocess", now=NOW)


def test_read_queue_items_finds_the_added_task_with_text_and_line_number(qfile):
    apply("add", {"fields": {"text": "Per Dashboard", "model": "#claude"}, "position": "top"})
    items = {t.task_text: t for t in queue_manager.read_queue_items()}
    assert items["Per Dashboard #claude"].line_no == 4 and items["Alpha"].line_no == 5


def test_mark_done_stamps_an_edited_line_after_it_moved(qfile):
    apply("edit", {"ref": {"lines": ["- [ ] Beta"], "occurrence": 0, "occurrences": 1}, "text": "Beta neu #id:b1"})
    task = next(t for t in queue_manager.read_queue_items() if t.task_text.startswith("Beta neu"))
    apply("add", {"fields": {"text": "Davor"}, "position": "top"})  # die Zeile rutscht um eins nach unten
    assert queue_manager.mark_done(task.task_text, "claude", line_no=task.line_no) is True
    text = qfile.read_bytes().decode("utf-8")
    assert "- [x] Beta neu #id:b1 ✅ " in text and "- [ ] Davor\r\n" in text and "- [ ] Alpha\r\n" in text


def test_lint_queue_reports_no_error_for_editor_lines(qfile):
    apply("add", {"fields": {"text": "Sauber", "model": "#claude", "id": "s1"}, "position": "bottom"})
    apply("edit", {"ref": {"lines": ["- [ ] Alpha"], "occurrence": 0, "occurrences": 1}, "text": "Alpha 2"})
    seen = [t.task_text for t in queue_manager.read_queue_items()]  # der Linter sieht diese Zeilen als offene Tasks
    assert any(t.startswith("Sauber") and "#id:s1" in t for t in seen) and "Alpha 2" in seen
    findings = queue_linter.lint_queue(qfile.read_bytes().decode("utf-8"))
    assert [f for f in findings if f.level == queue_linter.LEVEL_ERROR] == []


def _stamped_lines(qfile, monkeypatch):
    """Echte Stempel aus queue_manager: mark_done (OK/fehlgeschlagen), finalize (fehlgeschlagen), restamp."""
    qfile.write_bytes(b"# Queue\n\n## Queue\n- [ ] A1\n- [ ] B1\n- [ ] C1\n- [ ] D1\n")
    assert queue_manager.mark_done("A1", "claude") is True
    assert queue_manager.mark_done("B1", "claude", failed=True) is True
    assert queue_manager.finalize_task_with_result("C1", "r", "codex", failed=True) is True
    assert queue_manager.mark_done("D1", "claude") is True
    assert queue_manager.restamp_done_as_failed("D1") is True
    return qfile.read_text(encoding="utf-8").splitlines()[3:]


def test_real_stamps_match_stamp_re_parse_and_are_refused_when_duplicated_in_raw(qfile, monkeypatch):
    lines = _stamped_lines(qfile, monkeypatch)
    assert [("✅" in ln, "❌" in ln) for ln in lines] == [(True, False), (False, True), (False, True), (False, True)]
    assert all(queue_editor._STAMP_RE.search(ln) for ln in lines)
    entries = queue_editor.parse_entries(qfile.read_text(encoding="utf-8"), now=NOW)
    assert [e.state for e in entries] == ["erledigt", "fehlgeschlagen", "fehlgeschlagen", "fehlgeschlagen"]
    assert all(e.stamp and e.stamp in ln for e, ln in zip(entries, lines, strict=True))
    sha = hashlib.sha256(qfile.read_bytes()).hexdigest()
    import dashboard  # noqa: PLC0415
    for ln in lines:
        content = qfile.read_text(encoding="utf-8").rstrip("\n") + "\n" + ln + "\n"
        with pytest.raises(queue_editor.QueueEditError) as ei:
            apply("raw", {"sha": sha, "content": content})
        assert ei.value.code == "stamp_in_raw" and dashboard._QUEUE_STATUS["stamp_in_raw"] == 422
