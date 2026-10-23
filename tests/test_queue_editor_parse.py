"""Queue-Editor S1: Zerlegung, Zustaende, Identitaet, queue_state (HTTP-frei, nur tmp_path)."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    import config
    import queue_editor
    import queue_manager

_FMT = "%Y-%m-%d %H:%M"
STAMP = "2026-09-01 10:00 (claude)"
LONG = "x" * 6000


def _times():
    now = datetime.now().replace(second=0, microsecond=0)
    return now, (now + timedelta(days=1)).strftime(_FMT), (now - timedelta(days=1)).strftime(_FMT)


def _fixture_text(future: str, past: str) -> str:
    rows = [
        "# Queue", "", "## Queue", "### Queue", "<!-- Kommentarzeile -->", "",
        "- [ ] T-offen #tool:dev-loop",
        f"- [ ] T-emoji 🚀 faellig <!-- retry: {past} -->",
        f"- [ ] T-retryzukunft <!-- retry: {future} --> <!-- hang: 2 -->",
        f"- [ ] T-atzukunft #at:{future}",
        f"- [ ] T-atvorbei #at:{past}",
        f"- [ ] T-retrygewinnt #at:{future} <!-- retry: {past} -->",
        "- [ ] T-wartet #needs:fehlt,a1",
        f"- [ ] T-geplantwartet #needs:fehlt #at:{future}",
        "- [ ] T-needsok #needs:a1",
        "- [ ] T-every #every:24h #id:ev",
        f"- [ ] T-lang {LONG}",
        "- [ ] T-parallel #parallel", "  - sub eins", "\t- sub zwei", "",
        "- [ ] T-doppelt", "- [ ] T-doppelt",
        f"- [x] T-fertig #id:a1 ✅ {STAMP}",
        f"- [x] Replace ❌ with ✅ #id:a ✅ {STAMP}",
        f"- [x] T-gescheitert #id:b ❌ {STAMP}",
        "- [x] T-nackt ❌ ohne Stempel",
        "- [-] T-verworfen",
        "- [/] T-halb", "- [X] T-gross", "Freitext",
        "", "## Ergebnisse", "- [ ] T-ausserhalb",
    ]
    return "\r\n".join(rows) + "\r\n"


@pytest.fixture
def qfile(tmp_path, monkeypatch):
    path = tmp_path / "agent-queue.md"
    monkeypatch.setattr(queue_manager, "QUEUE_FILE", path)
    assert queue_manager.queue_file().is_relative_to(tmp_path)
    return path


def _by_text(entries):
    return {e.task_text: e for e in entries}


def _parse(content, in_flight=None):
    return queue_editor.parse_entries(content, now=datetime.now(), in_flight=in_flight)


@pytest.fixture
def parsed():
    _, future, past = _times()
    return _by_text(_parse(_fixture_text(future, past)))


# --- Zustaende -------------------------------------------------------------

@pytest.mark.parametrize("text,state", [
    ("T-offen #tool:dev-loop", "offen"),
    ("T-emoji 🚀 faellig", "offen"),
    ("T-retryzukunft", "geplant"),
    ("T-atzukunft", "geplant"),
    ("T-atvorbei", "offen"),
    ("T-retrygewinnt", "offen"),   # retry-Marker (faellig) schlaegt #at: (Zukunft)
    ("T-wartet", "wartet"),
    ("T-geplantwartet", "geplant"),   # geplant schlaegt wartet
    ("T-needsok", "offen"),
    ("T-every", "offen"),
    ("T-fertig #id:a1", "erledigt"),
    ("T-gescheitert #id:b", "fehlgeschlagen"),
    ("T-nackt ❌ ohne Stempel", "erledigt"),
    ("T-verworfen", "verworfen"),
    ("- [/] T-halb", "weitere"), ("- [X] T-gross", "weitere"), ("Freitext", "weitere"),
])
def test_state_per_line(parsed, text, state):
    hits = [e for k, e in parsed.items() if k.startswith(text)]
    assert hits and hits[0].state == state


def test_lang_emoji_parallel_and_markers(parsed):
    assert parsed["T-lang " + LONG].state == "offen"
    assert parsed["T-parallel #parallel"].subtasks == ["sub eins", "sub zwei"]
    ev = parsed["T-every #every:24h #id:ev"]
    assert (ev.every_sec, ev.id) == (86400, "ev")
    rz = parsed["T-retryzukunft"]
    assert (rz.hang, rz.retry_at is not None, rz.marker_tail.strip().startswith("<!--")) == (2, True, True)
    assert parsed["T-wartet #needs:fehlt,a1"].missing_needs == ["fehlt"]
    at = next(k for k in parsed if k.startswith("T-atzukunft #at:"))
    assert parsed[at].at == at.split("#at:")[1]


def test_outside_queue_section_and_headings_are_not_entries(parsed):
    assert not any("ausserhalb" in k for k in parsed)
    assert "### Queue" in parsed and parsed["### Queue"].state == "weitere"
    assert not any(k.startswith("## ") for k in parsed)


def test_running_beats_everything_and_matches_subtasks():
    _, future, past = _times()
    content = _fixture_text(future, past)
    flight = SimpleNamespace(task_text="T-retryzukunft", subtasks=())
    assert _by_text(_parse(content, flight))["T-retryzukunft"].state == "laeuft"
    par = SimpleNamespace(task_text="T-parallel #parallel", subtasks=("sub eins", "sub zwei"))
    assert _by_text(_parse(content, par))["T-parallel #parallel"].state == "laeuft"
    wrong = SimpleNamespace(task_text="T-parallel #parallel", subtasks=("andere",))
    assert _by_text(_parse(content, wrong))["T-parallel #parallel"].state == "offen"
    # nur offene Zeilen koennen laufen
    done = SimpleNamespace(task_text="T-verworfen", subtasks=())
    assert _by_text(_parse(content, done))["T-verworfen"].state == "verworfen"


# --- Gleichheit mit read_queue_items ---------------------------------------

def test_equals_read_queue_items(qfile):
    _, future, past = _times()
    content = _fixture_text(future, past)
    qfile.write_bytes(content.encode("utf-8"))
    items = queue_manager.read_queue_items()
    assert items, "Fixture muss lesbare Items liefern"
    entries = _parse(content)
    opened = [e for e in entries if e.kind == "open"]
    runnable = [e for e in opened if e.state in ("offen", "wartet")]
    assert [e.start for e in runnable] == [i.line_no for i in items]
    assert [e.task_text for e in runnable] == [i.task_text for i in items]
    assert [tuple(e.subtasks) for e in runnable] == [i.subtasks for i in items]
    assert [e.state == "wartet" for e in runnable] == [bool(i.blocked_reason) for i in items]
    assert {e.state for e in opened if e not in runnable} == {"geplant"}
    assert len(opened) == len(runnable) + 3
    # laeuft: exakt die Zeilennummer des Items
    flight = items[3]
    ran = [e for e in _parse(content, flight) if e.state == "laeuft"]
    assert [e.start for e in ran] == [flight.line_no]


# --- Stempel am Ende -------------------------------------------------------

def test_stamp_only_counts_at_line_end(parsed):
    e = parsed["Replace ❌ with ✅ #id:a"]
    assert (e.state, e.id, e.stamp) == ("erledigt", "a", f"✅ {STAMP}")
    f = parsed["T-gescheitert #id:b"]
    assert (f.state, f.stamp) == ("fehlgeschlagen", f"❌ {STAMP}")
    n = parsed["T-nackt ❌ ohne Stempel"]
    assert (n.state, n.stamp) == ("erledigt", None)


def test_failed_replace_line_keeps_description_emojis():
    line = f"- [x] Replace ❌ with ✅ #id:a ❌ {STAMP}"
    (e,) = _parse("## Queue\n" + line + "\n")
    assert (e.state, e.task_text, e.id) == ("fehlgeschlagen", "Replace ❌ with ✅ #id:a", "a")


# --- Bloecke und Leerzeilen ------------------------------------------------

def test_blocks_stop_at_blank_lines_and_headings():
    content = "## Queue\n- [ ] a\n  - k1\n\t- k2\n\n  - verwaist\n- [ ] b\n\n\n## Andere\n  - x\n"
    es = _parse(content)
    assert [e.lines for e in es] == [["- [ ] a", "  - k1", "\t- k2"], ["  - verwaist"], ["- [ ] b"]]
    assert [e.start for e in es] == [2, 6, 7]
    assert es[0].children == ["  - k1", "\t- k2"] and es[0].subtasks == []   # kein #parallel
    assert es[1].state == "weitere"


def test_whitespace_only_line_is_blank():
    es = _parse("## Queue\n- [ ] a\n   \n  - nicht dabei\n")
    assert [e.lines for e in es] == [["- [ ] a"], ["  - nicht dabei"]]


def test_identical_blocks_carry_occurrence():
    _, future, past = _times()
    es = [e for e in _parse(_fixture_text(future, past)) if e.task_text == "T-doppelt"]
    assert [(e.occurrence, e.occurrences) for e in es] == [(0, 2), (1, 2)]
    assert es[0].lines == es[1].lines and es[0].start + 1 == es[1].start
    single = _by_text(_parse(_fixture_text(future, past)))["T-offen #tool:dev-loop"]
    assert (single.occurrence, single.occurrences) == (0, 1)


# --- Zeilenenden -----------------------------------------------------------

def test_eols_are_exact_per_line():
    es = _parse("## Queue\r\n- [ ] a\r\n- [ ] b\n- [ ] c")
    assert [e.eols for e in es] == [["\r\n"], ["\n"], [""]]


def test_eol_kind_in_state(qfile):
    cases = {b"## Queue\r\n- [ ] a\r\n": "crlf", b"## Queue\n- [ ] a\n": "lf",
             b"## Queue\r\n- [ ] a\n": "mixed", b"## Queue": "none"}
    for raw, kind in cases.items():
        qfile.write_bytes(raw)
        assert queue_editor.queue_state(in_flight=None, mode="inprocess")["file"]["eol"] == kind


# --- comment_in_body und Aktionen ------------------------------------------

def test_comment_in_body_blocks_edit_only():
    past = _times()[2]
    es = _by_text(_parse(
        f"## Queue\n- [ ] foo <!-- x --> bar <!-- retry: {past} -->\n- [ ] mitten <!-- y --> text\n"
        f"- [ ] sauber <!-- retry: {past} --> <!-- hang: 1 -->\n"))
    bad1, bad2, ok = es["foo"], es["mitten <!-- y --> text"], es["sauber"]
    assert bad1.comment_in_body and bad2.comment_in_body and not ok.comment_in_body
    assert "edit" not in bad1.actions and "edit" not in bad2.actions
    assert ok.actions == ["edit", "delete", "duplicate", "move"]


def test_actions_per_state():
    a = queue_editor.allowed_actions
    for s in ("offen", "geplant", "wartet"):
        assert a(s) == ["edit", "delete", "duplicate", "move"]
    assert a("fehlgeschlagen") == ["retry", "delete", "duplicate"]
    assert a("erledigt") == a("verworfen") == ["delete", "duplicate"]
    assert a("laeuft") == a("weitere") == []


# --- Wrapper ---------------------------------------------------------------

def test_wrappers_delegate_exactly(qfile):
    def t(c):
        return None
    with patch.object(queue_manager, "_apply_update", return_value="U") as up:
        assert queue_manager.apply_queue_update(t) == "U"
    up.assert_called_once_with(t)
    with patch.object(queue_manager, "_read_queue_content", return_value="R") as rd:
        assert queue_manager.read_queue_text() == "R"
    rd.assert_called_once_with()
    assert queue_manager.queue_file() is qfile      # aktuelles Modulattribut, nicht beim Import gebunden


def test_wrappers_work_against_a_real_file(qfile):
    qfile.write_bytes(b"## Queue\r\n- [ ] a\r\n")
    assert queue_manager.read_queue_text() == "## Queue\r\n- [ ] a\r\n"
    assert queue_manager.apply_queue_update(lambda c: c.replace("a", "b")) is True
    assert qfile.read_bytes() == b"## Queue\r\n- [ ] b\r\n"


# --- Schema und SHA --------------------------------------------------------

def test_queue_state_schema_and_sha(qfile):
    _, future, past = _times()
    raw = _fixture_text(future, past).encode("utf-8")
    qfile.write_bytes(raw)
    st = queue_editor.queue_state(in_flight=None, mode="standalone")
    assert set(st) == {"file", "running", "entries", "counts", "undo"}
    f = st["file"]
    assert (f["sha"], f["size"], f["exists"], f["utf8"], f["eol"], f["has_queue_section"]) == (
        hashlib.sha256(raw).hexdigest(), len(raw), True, True, "crlf", True)
    assert f["path"] == str(qfile) and f["mtime"] == qfile.stat().st_mtime
    assert st["running"] == {"mode": "standalone", "task": None}
    assert st["undo"] == {"available": False}
    assert set(st["counts"]) == set(queue_editor.STATES)
    assert sum(st["counts"].values()) == len(st["entries"])
    assert st["counts"]["laeuft"] == 0 and st["counts"]["erledigt"] == 3
    e = st["entries"][0]
    assert set(e) >= {"ref", "state", "text", "line", "children", "subtasks", "markers", "schedule",
                      "waiting_for", "id", "needs", "stamp", "pos", "actions", "lock_reason"}
    assert set(e["ref"]) == {"start", "lines", "occurrence", "occurrences"}
    assert "raw" not in st
    assert queue_editor.queue_state(in_flight=None, mode="x", include_raw=True)["raw"] == raw.decode()


def test_queue_state_running_and_lock(qfile):
    qfile.write_bytes(b"## Queue\n- [ ] eins\n- [ ] zwei\n")
    fl = SimpleNamespace(task_text="zwei", subtasks=(), started_at=5.0, tool="dev-loop")
    st = queue_editor.queue_state(in_flight=fl, mode="inprocess")
    assert st["running"] == {"mode": "inprocess", "task": {"text": "zwei", "started_at": 5.0, "tool": "dev-loop"}}
    run = [e for e in st["entries"] if e["state"] == "laeuft"]
    assert len(run) == 1 and run[0]["actions"] == [] and run[0]["lock_reason"] == "running"
    assert st["counts"]["laeuft"] == 1 and st["counts"]["offen"] == 1


def test_queue_state_non_utf8_and_missing_file(qfile):
    qfile.write_bytes(b"## Queue\n- [ ] caf\xe9\n")
    st = queue_editor.queue_state(in_flight=None, mode="inprocess", include_raw=True)
    assert st["file"]["utf8"] is False and st["entries"] == [] and st["note"] and st["raw"] is None
    assert st["file"]["sha"] == hashlib.sha256(qfile.read_bytes()).hexdigest()
    qfile.unlink()
    gone = queue_editor.queue_state(in_flight=None, mode="inprocess")
    assert gone["file"]["exists"] is False and gone["file"]["mtime"] is None and gone["entries"] == []
    assert gone["file"]["has_queue_section"] is False and gone["file"]["eol"] == "none"


def test_queue_editor_source_is_lf_only():
    assert b"\r" not in Path(queue_editor.__file__).read_bytes()


# --- Testschutz der echten Queue -------------------------------------------

def test_config_queue_file_is_redirected_away_from_the_real_queue(real_vault_path):
    if os.environ.get("_ORCH_TEST_DISABLE_MEMORY_DOCS_ISOLATION"):
        pytest.skip("Kill-Switch aktiv")
    real_queue = real_vault_path / "99_System" / "AI" / "agent-queue.md"
    assert os.environ.get("ORCH_QUEUE_FILE") == ""
    assert real_queue != config.QUEUE_FILE
    assert not config.QUEUE_FILE.is_relative_to(real_vault_path)
    assert config.QUEUE_FILE.is_relative_to(config.VAULT_PATH)
