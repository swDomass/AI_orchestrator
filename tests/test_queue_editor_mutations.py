"""Queue-Editor S3a: apply_edit add/edit/delete (nur tmp_path, echte Queue/Sicherungen unberuehrt)."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    import analytics
    import config
    import queue_editor
    import queue_manager
    from tools.base_tool import ToolTracer
    from tools.registry import extract_tool_tag

NOW = datetime(2026, 10, 23, 12, 0)
RETRY = "<!-- retry: 2099-01-01 10:00 --> <!-- hang: 2 -->"
ROWS = [
    "# Queue", "", "## Queue", "### Queue", "<!-- Kommentar -->", "",
    "- [ ] Alpha", f"- [ ] Beta {RETRY}", "  - Kind eins", "  - Kind zwei", "",
    "- [ ] Gamma #parallel", "  - sub a", "  - sub b",
    "- [x] Fertig ✅ 2026-09-01 10:00 (claude)", "", "## Ergebnisse", "- [ ] ausserhalb",
]
CRLF = ("\r\n".join(ROWS) + "\r\n").encode("utf-8")


@pytest.fixture
def qfile(tmp_path, monkeypatch):
    path = tmp_path / "agent-queue.md"
    monkeypatch.setattr(queue_manager, "QUEUE_FILE", path)
    monkeypatch.setattr(queue_manager, "QUEUE_EVENTS_LOG_FILE", tmp_path / "events.log")
    assert queue_manager.queue_file().is_relative_to(tmp_path)
    assert Path(config.QUEUE_BACKUP_DIR).is_relative_to(tmp_path)
    path.write_bytes(CRLF)
    return path


def apply(op, body, **kw):
    return queue_editor.apply_edit(op, body, **{"in_flight": None, "mode": "inprocess", "now": NOW, **kw})


def put(path, *rows, eol="\r\n"):
    path.write_bytes((eol.join(["## Queue", *rows]) + eol).encode("utf-8"))


def running(text, subtasks=()):
    return SimpleNamespace(task_text=text, subtasks=list(subtasks), started_at=None, tool=None)


def ref_of(path, prefix):
    e = next(x for x in queue_editor.parse_entries(path.read_bytes().decode(), now=NOW) if x.task_text.startswith(prefix))
    return {"lines": e.lines, "occurrence": e.occurrence, "occurrences": e.occurrences}


def backups():
    return sorted(Path(config.QUEUE_BACKUP_DIR).glob("*")) if Path(config.QUEUE_BACKUP_DIR).exists() else []


def lines_of(data: bytes) -> list[bytes]:
    return data.splitlines(keepends=True)


# --- add -------------------------------------------------------------------------------

@pytest.mark.parametrize("position,after", [("top", b"## Queue"), ("bottom", "- [x] Fertig ✅ 2026-09-01 10:00 (claude)".encode())])
def test_add_inserts_one_crlf_line_and_keeps_every_other_byte(qfile, position, after):
    old = lines_of(CRLF)
    res = apply("add", {"fields": {"text": "Neu"}, "position": position})
    new = lines_of(qfile.read_bytes())
    at = next(i for i, ln in enumerate(old) if ln.rstrip() == after) + 1
    assert res["changed"] is True and res["ok"] is True
    assert new == [*old[:at], b"- [ ] Neu\r\n", *old[at:]]
    assert any(e["text"] == "Neu" for e in res["queue"]["entries"])


def test_add_uses_lf_in_lf_file_and_terminates_a_last_line_without_eol(qfile):
    qfile.write_bytes(b"## Queue\n- [ ] A")
    apply("add", {"fields": {"text": "Neu"}, "position": "bottom"})
    assert qfile.read_bytes() == b"## Queue\n- [ ] A\n- [ ] Neu\n"


def test_add_creates_missing_queue_section_at_end(qfile):
    qfile.write_bytes(b"# Titel\r\nText\r\n")
    apply("add", {"fields": {"text": "Neu"}})
    assert qfile.read_bytes() == b"# Titel\r\nText\r\n\r\n## Queue\r\n- [ ] Neu\r\n"


def test_add_writes_the_line_build_task_line_returns(qfile):
    apply("add", {"fields": {"text": "Dashboard-Test", "model": "#claude", "at": "2030-01-02T09:00"}})
    assert b"- [ ] Dashboard-Test #claude #at:2030-01-02T09:00\r\n" in qfile.read_bytes()


# --- edit ------------------------------------------------------------------------------

def test_edit_keeps_marker_tail_children_and_own_eol(qfile):
    apply("edit", {"ref": ref_of(qfile, "Beta"), "text": "  Beta neu  "})
    expect = CRLF.replace(f"- [ ] Beta {RETRY}".encode(), f"- [ ] Beta neu {RETRY}".encode())
    assert qfile.read_bytes() == expect and b"  - Kind eins\r\n  - Kind zwei\r\n" in expect


def test_edit_with_only_a_changed_marker_is_changed(qfile):
    ref = ref_of(qfile, "Beta")
    qfile.write_bytes(CRLF.replace(b"hang: 2", b"hang: 3"))
    before = qfile.read_bytes()
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply("edit", {"ref": ref, "text": "Beta neu"})
    assert exc.value.code == "changed" and str(exc.value) == "Inzwischen geändert — neu laden."
    assert qfile.read_bytes() == before and backups() == []


def test_edit_is_locked_for_a_foreign_comment_and_unchanged_text_is_a_noop(qfile):
    qfile.write_bytes(b"## Queue\r\n- [ ] Foo <!-- note -->\r\n- [ ] Bar\r\n")
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply("edit", {"ref": ref_of(qfile, "Foo"), "text": "Foo 2"})
    assert exc.value.code == "edit_locked"
    res = apply("edit", {"ref": ref_of(qfile, "Bar"), "text": "Bar"})
    assert res["changed"] is False and backups() == []


def test_edit_rejects_bad_text_and_done_lines(qfile):
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply("edit", {"ref": ref_of(qfile, "Alpha"), "text": "a <!-- x -->"})
    assert exc.value.code == "html_comment" and exc.value.field_errors
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply("edit", {"ref": ref_of(qfile, "Fertig"), "text": "x"})
    assert exc.value.code == "changed"


# --- delete ----------------------------------------------------------------------------

def test_delete_removes_exactly_the_block_with_children(qfile):
    apply("delete", {"ref": ref_of(qfile, "Beta")})
    assert qfile.read_bytes() == CRLF.replace(f"- [ ] Beta {RETRY}\r\n  - Kind eins\r\n  - Kind zwei\r\n".encode(), b"")


def test_ambiguous_and_second_occurrence(qfile):
    qfile.write_bytes(b"## Queue\r\n- [ ] Dup\r\n- [ ] Mid\r\n- [ ] Dup\r\n")
    ref = {**ref_of(qfile, "Dup"), "occurrence": 1}
    apply("delete", {"ref": ref})
    assert qfile.read_bytes() == b"## Queue\r\n- [ ] Dup\r\n- [ ] Mid\r\n"
    qfile.write_bytes(b"## Queue\r\n- [ ] Dup\r\n- [ ] Dup\r\n")
    ref = ref_of(qfile, "Dup")
    qfile.write_bytes(qfile.read_bytes() + b"- [ ] Dup\r\n")
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply("delete", {"ref": ref})
    assert exc.value.code == "ambiguous"


# --- Erhalt ueber alle Aktionen --------------------------------------------------------

@pytest.mark.parametrize("op", ["add", "edit", "delete"])
def test_every_other_line_survives_byte_identical(qfile, op):
    old = lines_of(CRLF)
    body = {"add": {"fields": {"text": "Neu"}, "position": "bottom"},
            "edit": {"ref": ref_of(qfile, "Gamma"), "text": "Gamma neu #parallel"},
            "delete": {"ref": ref_of(qfile, "Gamma")}}[op]
    apply(op, body)
    new = lines_of(qfile.read_bytes())
    dropped = {"add": [], "edit": [b"- [ ] Gamma #parallel\r\n"], "delete": old[11:14]}[op]
    assert [ln for ln in old if ln not in dropped] == [ln for ln in new if ln not in (b"- [ ] Neu\r\n", b"- [ ] Gamma neu #parallel\r\n")]
    assert new.count(b"  - sub b\r\n") == (0 if op == "delete" else 1)
    assert b"\n" not in b"".join(ln.replace(b"\r\n", b"") for ln in new)
    assert RETRY.encode() in qfile.read_bytes()


# --- Datei-Zustand, Rennen, Pruefung ---------------------------------------------------

def test_non_utf8_file_is_refused_and_left_alone(qfile):
    qfile.write_bytes(b"## Queue\r\n- [ ] caf\xe9\r\n")
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply("add", {"fields": {"text": "Neu"}})
    assert exc.value.code == "not_utf8"
    assert qfile.read_bytes() == b"## Queue\r\n- [ ] caf\xe9\r\n" and backups() == []


def test_obsidian_write_between_read_and_return_is_detected(qfile, monkeypatch):
    real, calls = queue_editor._read_raw, []

    def racing(path):
        data = real(path)
        calls.append(1)
        if len(calls) == 2:  # der zweite Vergleich: Obsidian hat inzwischen geschrieben
            path.write_bytes(data + b"- [ ] Obsidian\r\n")
            return path.read_bytes()
        return data

    monkeypatch.setattr(queue_editor, "_read_raw", racing)
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply("add", {"fields": {"text": "Neu"}})
    assert exc.value.code == "changed" and len(calls) == 2
    assert qfile.read_bytes() == CRLF + b"- [ ] Obsidian\r\n" and backups() == []


class _LockSpy:
    """Ersatz fuer ``_EDIT_LOCK``: meldet, dass Thread "B" am Lock steht, BEVOR er den echten Lock nimmt."""

    def __init__(self, b_at_lock):
        self._lock, self._b_at_lock = threading.Lock(), b_at_lock

    def __enter__(self):
        if threading.current_thread().name == "B":
            self._b_at_lock.set()
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()


def test_two_threads_adding_at_once_both_land(qfile, monkeypatch):
    """Ohne Sidecar-Lock haelt NUR _EDIT_LOCK die zweite Aenderung zurueck (sonst 'changed' bzw. verlorene Zeile).

    A wartet nicht auf eine feste Zeit, sondern auf ein Signal: "B steht am Lock" (mit Lock) oder "B ist fertig"
    (ohne Lock: der Spion wird nie betreten, B laeuft durch, A schreibt mit veraltetem Stand -> rot).
    """
    monkeypatch.setattr(queue_manager, "_lock_file", lambda f: None)
    monkeypatch.setattr(queue_manager, "_unlock_file", lambda f: None)
    a_reading, b_at_lock, b_done, errors = threading.Event(), threading.Event(), threading.Event(), []
    monkeypatch.setattr(queue_editor, "_EDIT_LOCK", _LockSpy(b_at_lock))
    real = queue_editor._read_raw

    def slow_first_read(path):
        data = real(path)
        if threading.current_thread().name == "A" and not a_reading.is_set():
            a_reading.set()  # A hat gelesen und haelt den Lock, bis B am Lock steht oder (ohne Lock) fertig ist
            deadline = time.monotonic() + 10
            while not (b_at_lock.is_set() or b_done.is_set()) and time.monotonic() < deadline:
                time.sleep(0.01)
        return data

    monkeypatch.setattr(queue_editor, "_read_raw", slow_first_read)

    def worker(name):
        if name == "T-zwei":
            a_reading.wait(5)
        try:
            apply("add", {"fields": {"text": name}, "position": "bottom"})
        except Exception as exc:  # pragma: no cover - the assertion below reports it
            errors.append(exc)
        if name == "T-zwei":
            b_done.set()

    threads = [threading.Thread(target=worker, args=(n,), name=t) for n, t in (("T-eins", "A"), ("T-zwei", "B"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads)
    assert b_at_lock.is_set()  # B hat den Lock tatsaechlich angefordert (ohne _EDIT_LOCK-Nutzung bliebe das aus)
    data = qfile.read_bytes()
    assert errors == [] and b"- [ ] T-eins\r\n" in data and b"- [ ] T-zwei\r\n" in data
    assert len(lines_of(data)) == len(lines_of(CRLF)) + 2


@pytest.mark.parametrize("op,body,code", [
    ("frobnicate", {}, "bad_request"), ("add", [], "bad_request"), ("add", {"fields": "x"}, "bad_request"),
    ("add", {"fields": {"text": "x"}, "position": "mid"}, "bad_request"),
    ("delete", {"ref": {"lines": ["x"], "occurrence": 1, "occurrences": 1}}, "bad_request"),
    ("edit", {"ref": {"lines": ["x"], "occurrence": 0, "occurrences": 1}, "text": 5}, "bad_request"),
    ("add", {"fields": {"text": ""}}, "empty"),
    ("add", {"fields": {"text": "x #shutdown"}}, "confirm_dangerous"),
    ("move", {"ref": {"lines": ["x"], "occurrence": 0, "occurrences": 1}, "direction": "left"}, "bad_request"),
    ("undo", {}, "bad_request"), ("undo", {"backup_id": 5}, "bad_request"),
    ("delete", {"ref": {"lines": ["x"], "occurrence": 0, "occurrences": 1}, "confirm_unknown_running": 1}, "bad_request"),
])
def test_invalid_requests_never_touch_the_file(qfile, op, body, code):
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply(op, body)
    assert exc.value.code == code and qfile.read_bytes() == CRLF and backups() == []


def test_dangerous_tag_goes_through_with_confirmation(qfile):
    apply("add", {"fields": {"text": "x #shutdown"}, "confirm_dangerous": True})
    assert b"- [ ] x #shutdown\r\n" in qfile.read_bytes()


def test_a_change_is_logged_to_the_events_log_but_a_noop_is_not(qfile):
    log = qfile.parent / "events.log"
    apply("add", {"fields": {"text": "Neu"}})
    assert log.exists() and "Dashboard: add — Neu" in log.read_text(encoding="utf-8")
    apply("edit", {"ref": ref_of(qfile, "Alpha"), "text": "Alpha"})
    assert log.read_text(encoding="utf-8").count("Dashboard:") == 1


# --- retry / duplicate / move ----------------------------------------------------------

STAMP_F = "❌ 2026-09-02 11:00 (claude)"
STAMP_D = "✅ 2026-09-01 10:00 (claude)"


def test_retry_reopens_a_failed_line_and_only_cuts_the_real_stamp(qfile):
    put(qfile, f"- [x] Ersetze ❌ durch ✅ #id:k1 <!-- hang: 2 --> {STAMP_F}", "  - Kind", "- [ ] Alpha")
    res = apply("retry", {"ref": ref_of(qfile, "Ersetze")})
    assert res["changed"] is True
    assert qfile.read_bytes() == "## Queue\r\n- [ ] Ersetze ❌ durch ✅ #id:k1\r\n  - Kind\r\n- [ ] Alpha\r\n".encode()
    put(qfile, f"- [x] Mit Marker <!-- retry: 2099-01-01 10:00 --> <!-- hang: 1 --> {STAMP_F}", "- [ ] Alpha")
    apply("retry", {"ref": ref_of(qfile, "Mit Marker")})
    assert qfile.read_bytes() == b"## Queue\r\n- [ ] Mit Marker\r\n- [ ] Alpha\r\n"


@pytest.mark.parametrize("prefix", ["Fertig", "Alpha"])
def test_retry_refuses_done_and_open_lines(qfile, prefix):
    before = qfile.read_bytes()
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply("retry", {"ref": ref_of(qfile, prefix)})
    assert exc.value.code == "changed" and qfile.read_bytes() == before and backups() == []


@pytest.mark.parametrize("row,copy", [
    (f"- [ ] Beta #id:b1 {RETRY}", "- [ ] Beta"),
    (f"- [x] Fertig #id:f1 {STAMP_D}", "- [ ] Fertig"),
    (f"- [x] Kaputt #needs:x #id:k1 {STAMP_F}", "- [ ] Kaputt #needs:x"),
])
def test_duplicate_copies_children_without_id_stamp_and_markers(qfile, row, copy):
    put(qfile, row, "  - Kind", "", "- [ ] Alpha")
    res = apply("duplicate", {"ref": ref_of(qfile, row[6:].split()[0])})
    assert res["changed"] is True
    assert qfile.read_bytes() == f"## Queue\r\n{row}\r\n  - Kind\r\n{copy}\r\n  - Kind\r\n\r\n- [ ] Alpha\r\n".encode()


def test_duplicate_at_the_end_of_a_file_without_line_ending(qfile):
    qfile.write_bytes(b"## Queue\n- [ ] A #id:x")
    apply("duplicate", {"ref": ref_of(qfile, "A")})
    assert qfile.read_bytes() == b"## Queue\n- [ ] A #id:x\n- [ ] A"


MOVE_ROWS = ("- [ ] A", "", f"- [x] Fertig {STAMP_D}", "- [ ] B", "  - Kind", "- [ ] C")


@pytest.mark.parametrize("who,direction", [("B", "up"), ("A", "down")])
def test_move_swaps_with_the_next_open_block_across_done_lines_and_blanks(qfile, who, direction):
    put(qfile, *MOVE_ROWS)
    res = apply("move", {"ref": ref_of(qfile, who), "direction": direction})
    assert res["changed"] is True
    expect = ("- [ ] B", "  - Kind", "", f"- [x] Fertig {STAMP_D}", "- [ ] A", "- [ ] C")
    assert qfile.read_bytes() == ("\r\n".join(["## Queue", *expect]) + "\r\n").encode()


@pytest.mark.parametrize("who,direction", [("A", "up"), ("C", "down")])
def test_move_at_the_edge_changes_nothing_and_writes_no_backup(qfile, who, direction):
    put(qfile, *MOVE_ROWS)
    before = qfile.read_bytes()
    assert apply("move", {"ref": ref_of(qfile, who), "direction": direction})["changed"] is False
    assert qfile.read_bytes() == before and backups() == []


def test_move_keeps_a_last_line_without_line_ending_last(qfile):
    qfile.write_bytes(b"## Queue\n- [ ] A\n- [ ] B")
    apply("move", {"ref": ref_of(qfile, "A"), "direction": "down"})
    assert qfile.read_bytes() == b"## Queue\n- [ ] B\n- [ ] A"


# --- Laufsperre ------------------------------------------------------------------------

@pytest.mark.parametrize("op,who,extra", [
    ("edit", "Alpha", {"text": "x"}), ("delete", "Alpha", {}), ("duplicate", "Alpha", {}),
    ("move", "Alpha", {"direction": "down"}), ("move", "Beta", {"direction": "up"}),  # Nachbar laeuft
])
def test_the_running_line_and_its_move_neighbour_are_locked_in_process(qfile, op, who, extra):
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply(op, {"ref": ref_of(qfile, who), **extra}, in_flight=running("Alpha"))
    assert exc.value.code == "running" and qfile.read_bytes() == CRLF and backups() == []


def test_a_parallel_task_runs_only_with_matching_subtasks(qfile):
    subs = next(e for e in queue_editor.parse_entries(CRLF.decode(), now=NOW) if e.task_text.startswith("Gamma")).subtasks
    assert subs
    apply("edit", {"ref": ref_of(qfile, "Gamma"), "text": "Gamma #parallel"}, in_flight=running("Gamma #parallel", ["x"]))
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply("delete", {"ref": ref_of(qfile, "Gamma")}, in_flight=running("Gamma #parallel", subs))
    assert exc.value.code == "running"


@pytest.mark.parametrize("op,extra", [("edit", {"text": "Alpha 2"}), ("delete", {}), ("move", {"direction": "down"})])
def test_standalone_asks_before_changing_an_open_line_and_goes_ahead_when_confirmed(qfile, op, extra):
    body = {"ref": ref_of(qfile, "Alpha"), **extra}
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply(op, body, mode="standalone")
    assert exc.value.code == "confirm_unknown_running" and qfile.read_bytes() == CRLF and backups() == []
    assert apply(op, {**body, "confirm_unknown_running": True}, mode="standalone")["changed"] is True


@pytest.mark.parametrize("op,body", [
    ("add", {"fields": {"text": "Neu"}}), ("duplicate", {"ref": "Alpha"}), ("delete", {"ref": "Fertig"}),
])
def test_standalone_asks_nothing_for_add_duplicate_and_done_lines(qfile, op, body):
    if "ref" in body:
        body = {"ref": ref_of(qfile, body["ref"])}
    assert apply(op, body, mode="standalone")["changed"] is True


def test_standalone_asks_nothing_for_retry(qfile):
    put(qfile, f"- [x] Kaputt {STAMP_F}")
    assert apply("retry", {"ref": ref_of(qfile, "Kaputt")}, mode="standalone")["changed"] is True


TEXT = "Baue es #tool:dev-loop #id:x1 cwd:/tmp"
CLEAN = queue_manager.strip_metadata_tags(TEXT)[:200]
RUN = {"status": "running", "tool": "dev-loop", "task": CLEAN}


@pytest.mark.parametrize("mode", ["inprocess", "standalone"])
def test_a_matching_active_run_locks_the_line_in_both_modes_even_without_confirmation(qfile, mode):
    put(qfile, f"- [ ] {TEXT}")
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply("delete", {"ref": ref_of(qfile, "Baue")}, mode=mode, active_runs=[{**RUN, "run_id": "r"}, "kaputt"])
    assert exc.value.code == "running"


@pytest.mark.parametrize("record", [
    {**RUN, "status": "stale"}, {**RUN, "tool": "review-loop"}, {**RUN, "task": CLEAN[:-1]}, {**RUN, "status": "done"},
])
def test_an_active_run_for_another_tool_task_or_a_stale_one_does_not_lock(qfile, record):
    put(qfile, f"- [ ] {TEXT}")
    assert apply("delete", {"ref": ref_of(qfile, "Baue")}, active_runs=[record])["changed"] is True


@pytest.mark.parametrize("tool,source", [("dev-loop", "tools/dev_loop.py"), ("review-loop", "tools/review_loop.py")])
def test_the_active_run_record_a_tool_writes_matches_what_the_gate_compares(qfile, tool, source):
    """Vertrag: Orchestrator uebergibt strip_metadata_tags(task) an tool.run, das Tool meldet task[:200]."""
    root = Path(queue_editor.__file__).parent
    assert 'tracer.emit("run_start", task=task[:200]' in (root / source).read_text(encoding="utf-8")
    assert "clean_task, provider, cwd=cwd" in (root / "orchestrator.py").read_text(encoding="utf-8")
    text = f"Baue es {'x' * 250} #tool:{tool} #id:x1"
    assert extract_tool_tag(text) == queue_editor._TOOL_RE.search(text).group(1) == tool
    ToolTracer.create(tool, None).emit("run_start", task=queue_manager.strip_metadata_tags(text)[:200], provider="p")
    put(qfile, f"- [ ] {text}")
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply("edit", {"ref": ref_of(qfile, "Baue"), "text": "x"}, active_runs=analytics._load_active_runs())
    assert exc.value.code == "running"


# --- R2a P3: duplicate/retry bei fremdem Kommentar im Text, ref bei undo ----------------------

def test_a_foreign_comment_in_the_text_removes_duplicate_and_retry_and_the_post_stays_locked(qfile):
    put(qfile, "- [ ] Foo <!-- x --> bar", "- [x] Baz <!-- y --> q ❌ 2026-09-01 10:00 (claude)")
    acts = {e.task_text[:3]: e.actions for e in queue_editor.parse_entries(qfile.read_bytes().decode(), now=NOW)}
    assert "duplicate" not in acts["Foo"] and "duplicate" not in acts["Baz"] and "retry" not in acts["Baz"]
    before = qfile.read_bytes()
    with pytest.raises(queue_editor.QueueEditError) as exc:
        apply("duplicate", {"ref": ref_of(qfile, "Foo")})
    assert exc.value.code == "changed" and qfile.read_bytes() == before
    with pytest.raises(queue_editor.QueueEditError) as exc:  # retry wuerde eine offene Zeile mit <!-- schreiben
        apply("retry", {"ref": ref_of(qfile, "Baz")})
    assert exc.value.code == "edit_locked" and qfile.read_bytes() == before


def test_undo_ignores_a_foreign_ref_field(qfile):
    apply("add", {"fields": {"text": "Neu"}})
    bid = queue_editor.queue_state(in_flight=None, mode="inprocess", now=NOW)["undo"]["id"]
    assert apply("undo", {"backup_id": bid, "ref": 5})["changed"] is True and qfile.read_bytes() == CRLF
