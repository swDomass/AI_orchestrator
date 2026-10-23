"""Queue-Editor S4: Rohtext speichern (raw) und die Gefahren-Nachfrage bei retry/duplicate (nur tmp_path)."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    import config
    import queue_editor
    import queue_manager

NOW = datetime(2026, 10, 23, 12, 0)
DONE = "- [x] Fertig ✅ 2026-09-01 10:00 (claude)"
BASE = ("# Queue\r\n\r\n## Queue\r\n- [ ] Alpha\r\n- [ ] Beta <!-- retry: 2026-10-23 13:00 -->\r\n" + DONE + "\r\n").encode()


@pytest.fixture
def qfile(tmp_path, monkeypatch):
    path = tmp_path / "agent-queue.md"
    monkeypatch.setattr(queue_manager, "QUEUE_FILE", path)
    monkeypatch.setattr(queue_manager, "QUEUE_EVENTS_LOG_FILE", tmp_path / "events.log")
    assert queue_manager.queue_file().is_relative_to(tmp_path)
    assert Path(config.QUEUE_BACKUP_DIR).is_relative_to(tmp_path)
    path.write_bytes(BASE)
    return path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw(path, content, **kw):
    body = {"sha": sha(path.read_bytes()), "content": content, **kw.pop("body", {})}
    return queue_editor.apply_edit("raw", body, **{"in_flight": None, "mode": "inprocess", "now": NOW, **kw})


def lf(data: bytes) -> str:
    return data.decode("utf-8").replace("\r\n", "\n")


def refused(path, content, code, **kw):
    before = path.read_bytes()
    with pytest.raises(queue_editor.QueueEditError) as ei:
        raw(path, content, **kw)
    assert ei.value.code == code and path.read_bytes() == before
    return ei.value


def test_raw_saves_the_text_and_puts_the_dominant_line_ending_back(qfile):
    res = raw(qfile, lf(BASE).replace("Alpha", "Alpha2"))
    assert res["changed"] is True
    assert qfile.read_bytes() == BASE.replace(b"Alpha", b"Alpha2")  # LF text in, CRLF file out, rest byte-equal
    assert queue_editor.queue_state(in_flight=None, mode="inprocess")["undo"]["action"] == "raw"


def test_raw_unifies_mixed_line_endings_to_the_dominant_one(qfile):
    qfile.write_bytes(BASE.replace(b"Alpha\r\n", b"Alpha\n"))
    raw(qfile, lf(BASE) + "- [ ] Gamma\n")
    assert b"\r\n" in qfile.read_bytes() and qfile.read_bytes().count(b"\n") == qfile.read_bytes().count(b"\r\n")


def test_raw_with_a_stale_sha_is_a_409_and_leaves_the_file_alone(qfile):
    old = sha(qfile.read_bytes())
    qfile.write_bytes(BASE + b"- [ ] Von Obsidian\r\n")
    err = refused(qfile, "x", "sha_mismatch", body={"sha": old})
    assert queue_editor.ERROR_TEXTS[err.code]


def test_raw_keeps_or_restores_the_bom(qfile):
    qfile.write_bytes(b"\xef\xbb\xbf" + BASE)
    raw(qfile, lf(BASE).replace("Alpha", "Alpha2"))  # the text field lost the BOM
    assert qfile.read_bytes() == b"\xef\xbb\xbf" + BASE.replace(b"Alpha", b"Alpha2")


def test_raw_without_any_change_writes_nothing(qfile):
    assert raw(qfile, lf(BASE))["changed"] is False
    assert qfile.read_bytes() == BASE and not Path(config.QUEUE_BACKUP_DIR).exists()


def test_raw_then_undo_restores_the_file_byte_for_byte(qfile):
    raw(qfile, lf(BASE).replace("Alpha", "Alpha2"))
    bid = queue_editor.queue_state(in_flight=None, mode="inprocess")["undo"]["id"]
    queue_editor.apply_edit("undo", {"backup_id": bid}, in_flight=None, mode="inprocess", now=NOW)
    assert qfile.read_bytes() == BASE


@pytest.mark.parametrize("edit", [
    lambda t: t.replace("- [ ] Alpha", "- [ ] Alpha ✅ 2026-10-23 11:00 (claude)"),   # open line gets stamped
    lambda t: t + "- [x] Neu ❌ 2026-10-23 11:00 (codex): kaputt\n".replace(": kaputt", ""),  # new stamped line
])
def test_raw_refuses_a_new_stamp(qfile, edit):
    refused(qfile, edit(lf(BASE)), "stamp_in_raw")


def test_raw_allows_a_typo_fix_in_a_done_line_and_moving_a_stamped_line(qfile):
    text = lf(BASE)
    raw(qfile, text.replace("Fertig", "Fertig!"))
    moved = lf(qfile.read_bytes()).replace(DONE.replace("Fertig", "Fertig!") + "\n", "")
    assert raw(qfile, moved.replace("## Queue\n", "## Queue\n" + DONE.replace("Fertig", "Fertig!") + "\n"))["changed"]


@pytest.mark.parametrize("edit", [
    lambda t: t.replace("- [ ] Alpha", "- [ ] Alpha <!-- weg -->"),
    lambda t: t + "- [ ] Neu <!-- x\n",
    lambda t: t.replace("Beta <!-- retry: 2026-10-23 13:00 -->", "Beta2 <!-- note -->"),
])
def test_raw_refuses_an_html_comment_in_a_changed_or_new_open_line(qfile, edit):
    refused(qfile, edit(lf(BASE)), "comment_in_raw")


def test_raw_keeps_the_orchestrators_markers_and_allows_comments_in_other_lines(qfile):
    text = lf(BASE).replace("Beta <!-- retry: 2026-10-23 13:00 -->", "Beta <!-- retry: 2026-10-23 14:00 -->")
    raw(qfile, text.replace("# Queue\n", "# Queue\n<!-- Notiz -->\n"))
    assert b"14:00" in qfile.read_bytes() and b"<!-- Notiz -->" in qfile.read_bytes()


def test_raw_removing_the_running_line_is_locked_and_nothing_is_written(qfile):
    run = SimpleNamespace(task_text="Alpha", subtasks=[], started_at=None, tool=None)
    refused(qfile, lf(BASE).replace("- [ ] Alpha\n", ""), "running", in_flight=run)
    assert not Path(config.QUEUE_BACKUP_DIR).exists()


def test_raw_standalone_asks_for_every_open_line_that_appears_or_vanishes(qfile):
    refused(qfile, lf(BASE).replace("- [ ] Alpha\n", ""), "confirm_unknown_running", mode="standalone")
    refused(qfile, lf(BASE) + "- [ ] Neu\n", "confirm_unknown_running", mode="standalone")
    assert raw(qfile, lf(BASE).replace("- [ ] Alpha\n", ""), mode="standalone", body={"confirm_unknown_running": True})["changed"]
    # a change that touches no open line needs no answer
    assert raw(qfile, lf(qfile.read_bytes()).replace("Fertig", "Fertig!"), mode="standalone")["changed"]


@pytest.mark.parametrize("body", [{"sha": 1, "content": ""}, {"sha": "x"}, {"content": "x"}, {"sha": "x", "content": "\ud800"}])
def test_raw_rejects_a_malformed_body(qfile, body):
    with pytest.raises(queue_editor.QueueEditError) as ei:
        queue_editor.apply_edit("raw", body, in_flight=None, mode="inprocess", now=NOW)
    assert ei.value.code == "bad_request" and qfile.read_bytes() == BASE


@pytest.mark.parametrize("op,row", [("duplicate", "- [ ] Aus #shutdown"), ("retry", "- [x] Aus #approve:x ❌ 2026-10-23 11:00 (claude)")])
def test_retry_and_duplicate_of_a_dangerous_line_need_confirm_dangerous(qfile, op, row):
    qfile.write_bytes(("## Queue\r\n" + row + "\r\n").encode())
    e = queue_editor.parse_entries(qfile.read_bytes().decode(), now=NOW)[0]
    body = {"ref": {"lines": e.lines, "occurrence": 0, "occurrences": 1}}
    before = qfile.read_bytes()
    with pytest.raises(queue_editor.QueueEditError) as ei:
        queue_editor.apply_edit(op, body, in_flight=None, mode="inprocess", now=NOW)
    assert ei.value.code == "confirm_dangerous" and qfile.read_bytes() == before
    assert queue_editor.apply_edit(op, {**body, "confirm_dangerous": True}, in_flight=None, mode="inprocess", now=NOW)["changed"]


def test_queue_state_marks_a_line_locked_that_an_active_run_reports(qfile):
    qfile.write_bytes(b"## Queue\r\n- [ ] Pruefen #tool:dev-loop #cwd:x\r\n- [ ] Andere\r\n")
    task = queue_editor.parse_entries(qfile.read_bytes().decode(), now=NOW)[0].task_text
    runs = [{"status": "running", "tool": "dev-loop", "task": queue_manager.strip_metadata_tags(task)[:200]}]
    locks = lambda r: [e["lock_reason"] for e in queue_editor.queue_state(in_flight=None, mode="standalone", active_runs=r)["entries"]]  # noqa: E731
    assert locks(runs) == ["running", None] and locks(None) == [None, None]


def test_raw_asks_to_confirm_a_dangerous_tag_only_in_new_or_changed_open_lines(qfile):
    err = refused(qfile, lf(BASE) + "- [ ] Neu #shutdown\n", "confirm_dangerous")
    assert queue_editor.ERROR_TEXTS[err.code]
    assert raw(qfile, lf(BASE) + "- [ ] Neu #shutdown\n", body={"confirm_dangerous": True})["changed"] is True
    again = lf(qfile.read_bytes()).replace("Alpha", "Alpha2")  # #shutdown-Zeile unveraendert, andere Zeile geaendert
    assert raw(qfile, again)["changed"] is True and b"Neu #shutdown" in qfile.read_bytes()


def test_raw_lets_an_unchanged_open_line_with_a_foreign_html_comment_stay(qfile):
    qfile.write_bytes(BASE.replace(b"- [ ] Alpha", b"- [ ] Al <!-- x --> pha"))
    assert raw(qfile, lf(qfile.read_bytes()).replace("Beta", "Beta2"))["changed"] is True
    assert b"Al <!-- x --> pha" in qfile.read_bytes()
    refused(qfile, lf(qfile.read_bytes()).replace("Al <!-- x --> pha", "Al <!-- y --> pha"), "comment_in_raw")


# --- R5a P1: eine zusaetzliche Kopie einer vorhandenen Zeile ist neu (verbrauchender Zaehler) --

def test_raw_asks_to_confirm_when_an_existing_dangerous_line_is_duplicated(qfile):
    qfile.write_bytes(BASE + b"- [ ] Aus #shutdown\r\n")
    twin = lf(qfile.read_bytes()) + "- [ ] Aus #shutdown\n"
    refused(qfile, twin, "confirm_dangerous")
    assert raw(qfile, twin, body={"confirm_dangerous": True})["changed"] is True
    assert qfile.read_bytes().count(b"- [ ] Aus #shutdown") == 2


def test_raw_refuses_to_duplicate_an_existing_open_line_with_an_html_comment(qfile):
    qfile.write_bytes(BASE.replace(b"- [ ] Alpha", b"- [ ] Al <!-- foo --> pha"))
    refused(qfile, lf(qfile.read_bytes()) + "- [ ] Al <!-- foo --> pha\n", "comment_in_raw")  # wie eine neue <!--Zeile
    assert qfile.read_bytes().count(b"Al <!-- foo --> pha") == 1


@pytest.mark.parametrize("move", [False, True], ids=["copy", "move"])
def test_raw_asks_to_confirm_a_dangerous_line_coming_from_another_section_into_the_queue(qfile, move):
    qfile.write_bytes(BASE + b"\r\n## Notes\r\n- [ ] Aus #shutdown\r\n")
    text = lf(qfile.read_bytes())
    if move:
        text = text.replace("\n- [ ] Aus #shutdown\n", "\n", 1)
    new = text.replace("- [ ] Alpha\n", "- [ ] Alpha\n- [ ] Aus #shutdown\n", 1)
    refused(qfile, new, "confirm_dangerous")


def test_raw_allows_reordering_known_dangerous_and_comment_lines_without_a_new_copy(qfile):
    qfile.write_bytes(BASE.replace(b"- [ ] Alpha", b"- [ ] Al <!-- foo --> pha") + b"- [ ] Aus #shutdown\r\n")
    lines = lf(qfile.read_bytes()).splitlines()
    i, j = lines.index("- [ ] Al <!-- foo --> pha"), lines.index("- [ ] Aus #shutdown")
    lines[i], lines[j] = lines[j], lines[i]
    assert raw(qfile, "\n".join(lines) + "\n")["changed"] is True  # ohne Rueckfrage, ohne Ablehnung
    assert lf(qfile.read_bytes()).splitlines() == lines


# --- R2a P1: Stempel im Rohtext nicht per Position umgehbar -------------------------------

FAIL = "- [x] Kaputt ❌ 2026-09-01 11:00 (codex)"


@pytest.mark.parametrize("old, new", [
    (FAIL, FAIL.replace("❌", "✅")),                                            # fehlgeschlagen -> erledigt
    (DONE, DONE.replace("2026-09-01 10:00", "2026-10-23 11:00")),               # Zeitstempel geaendert
    (DONE, DONE.replace("(claude)", "(codex)")),                                # Provider geaendert
    (DONE, DONE.replace("- [x]", "- [ ]")),                                     # Checkbox geaendert, Stempel bleibt
    (DONE, "- [x] Anderes ✅ 2026-09-02 12:00 (codex)"),                        # durch andere gestempelte Zeile ersetzt
])
def test_raw_refuses_to_change_or_swap_a_stamp_in_place(qfile, old, new):
    qfile.write_bytes(BASE + (FAIL + "\r\n").encode())
    refused(qfile, lf(qfile.read_bytes()).replace(old, new, 1), "stamp_in_raw")


def test_raw_allows_a_typo_in_a_stamped_line_with_unchanged_stamp_and_moving_it(qfile):
    qfile.write_bytes(BASE + (FAIL + "\r\n").encode())
    typo = lf(qfile.read_bytes()).replace("Kaputt", "Kaputtt")
    assert raw(qfile, typo)["changed"] is True
    line = FAIL.replace("Kaputt", "Kaputtt")
    moved = lf(qfile.read_bytes()).replace(line + "\n", "").replace("## Queue\n", "## Queue\n" + line + "\n")
    assert raw(qfile, moved)["changed"] is True and qfile.read_bytes().count("❌ 2026-09-01 11:00".encode()) == 1


# --- R2a P3: fremdes ref bei raw ist kein Fehler ---------------------------------------------

def test_raw_ignores_a_foreign_ref_field(qfile):
    assert raw(qfile, lf(BASE).replace("Alpha", "Alpha2"), body={"ref": 5})["changed"] is True


# --- R3: Tippfehler-Paarung nur bei gleicher #id ----------------------------------------------

ID_DONE = "- [x] Fertig #id:b1 ✅ 2026-09-01 10:00 (claude)"
WAIT = "- [ ] Wartet #needs:b2"


@pytest.mark.parametrize("old,new", [
    (DONE, DONE.replace("Fertig", "Fertig #id:b2")),        # id ergaenzt, Stempel gleich: #needs:b2 wuerde frei
    (ID_DONE, ID_DONE.replace("#id:b1", "#id:b2")),         # id geaendert
    (ID_DONE, ID_DONE.replace(" #id:b1", "")),              # id entfernt
])
def test_raw_refuses_an_id_change_on_a_stamped_line_and_the_dependant_stays_waiting(qfile, old, new):
    qfile.write_bytes(BASE.replace(DONE.encode(), old.encode()) + (WAIT + "\r\n").encode())
    refused(qfile, lf(qfile.read_bytes()).replace(old, new, 1), "stamp_in_raw")
    item = next(e for e in queue_editor.parse_entries(qfile.read_bytes().decode(), now=NOW) if e.task_text.startswith("Wartet"))
    assert item.missing_needs == ["b2"]


def test_raw_still_allows_a_typo_in_a_stamped_line_with_unchanged_id(qfile):
    qfile.write_bytes(BASE.replace(DONE.encode(), ID_DONE.encode()))
    assert raw(qfile, lf(qfile.read_bytes()).replace("Fertig", "Fertigg"))["changed"] is True
