"""Queue-Editor S2: Zeilenbau, Ablehnungen, Rueckleseprobe, Zeitplan-Falle, Gefahren (nur reine Logik)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    import queue_editor as qe

NOW = datetime(2026, 10, 23, 12, 0)
DONE2 = {"text": "Dashboard-Test", "model": "#claude", "at": "2026-10-24T09:00"}


def _codes(errs):
    return {(e.field, e.code) for e in errs}


def test_done2_line_is_exact():
    line, errs, dangers, hint = qe.build_task_line(DONE2, NOW)
    assert line == "- [ ] Dashboard-Test #claude #at:2026-10-24T09:00"
    assert (errs, dangers) == ([], [])
    assert hint == "Erster Lauf: 2026-10-24 09:00."


def test_canonical_order_of_all_fields():
    f = {
        "shutdown": 1, "allow_dirty": True, "no_commit": True, "keep_worktree": True, "worktree": True,
        "parallel": True, "verify": r"C:\v w.ps1", "timeout": "5m", "freshonly": True, "grace": "2h",
        "every": "24h", "at": "2026-10-24T09:00", "needs": ["a", "b"], "id": "Z", "cwd": r"C:\a b",
        "effort": "high", "profile": "p", "model": "claude_opus", "tool": "dev-loop", "text": "T",
    }
    line, errs, _, _ = qe.build_task_line(f, NOW)
    assert errs == []
    assert line == (
        r'- [ ] T #tool:dev-loop #claude_opus #agent:p #effort:high cwd:"C:\a b" #id:Z #needs:a,b '
        r'#at:2026-10-24T09:00 #every:24h #grace:2h #freshonly #timeout:5m #verify:"C:\v w.ps1" '
        "#parallel #worktree #keep-worktree #no-commit #allow-dirty #shutdown"
    )


@pytest.mark.parametrize("extra", [
    {"tool": "dev-loop"}, {"model": "#claude"}, {"model": "codex_5_4"}, {"profile": "work"}, {"effort": "XHIGH"},
    {"cwd": "C:/plain"}, {"cwd": r"C:\mit Leerzeichen"}, {"cwd": r"C:\a#b"}, {"id": "My-Id_1"},
    {"needs": "a,b"}, {"needs": ["x"]}, {"at": "09:00"}, {"at": "2026-10-24 09:00"}, {"every": "30m"},
    {"every": "7d", "grace": "4h", "freshonly": "yes"}, {"timeout": "1h"}, {"verify": r"C:\s.ps1"},
    {"verify": r"C:\a b.ps1"}, {"parallel": True}, {"worktree": True}, {"keep_worktree": True},
    {"no_commit": True}, {"allow_dirty": True}, {"shutdown": True},
])
def test_roundtrip_each_field(extra):
    line, errs, _, _ = qe.build_task_line({"text": "Aufgabe", **extra}, NOW)
    assert errs == [] and line is not None and line.startswith("- [ ] Aufgabe")


def test_needs_list_and_string_agree():
    a = qe.build_task_line({"text": "T", "needs": "a, b"}, NOW)[0]
    assert a == qe.build_task_line({"text": "T", "needs": ["a", "b"]}, NOW)[0] == "- [ ] T #needs:a,b"


def test_roundtrip_catches_second_tag_in_text():
    line, errs, _, _ = qe.build_task_line({"text": "T #id:first", "id": "second"}, NOW)
    assert line is None and ("id", "roundtrip") in _codes(errs)


@pytest.mark.parametrize("text, code", [
    ("a\nb", "newline"), ("a\rb", "newline"), ("a\x85b", "newline"), ("a\x0cb", "newline"),
    ("a\u2028b", "newline"), ("a\x0bb", "newline"), ("a<!--x", "html_comment"),
    ("fertig ✅ 2026-09-01 10:00 (claude)", "stamp_shape"), ("x ❌ 2026-09-01 10:00", "stamp_shape"),
    ("[ ] foo", "checkbox"), ("  [x] foo", "checkbox"), ("[-] foo", "checkbox"), ("", "empty"), ("   ", "empty"),
    ("a\x00b", "control"), ("a\x1bb", "control"), ("a\ud800b", "control"), ("a\u202eb", "bidi"),
    ("a\u2066b", "bidi"), ("a\u2069b", "bidi"),
])
def test_text_rejections(text, code):
    assert code in {e.code for e in qe.validate_task_text(text)}
    line, errs, _, _ = qe.build_task_line({"text": text}, NOW)
    assert line is None and code in {e.code for e in errs}


@pytest.mark.parametrize("field", ["cwd", "verify"])
@pytest.mark.parametrize("bad", ["a\nb", "a<!--b", "a ✅ 2026-09-01 10:00 b", "a\u202eb", "a\x00b", 'a"b'])
def test_rejections_apply_to_every_free_field(field, bad):
    line, errs, _, _ = qe.build_task_line({"text": "ok", field: bad}, NOW)
    assert line is None and {e.field for e in errs} == {field}


@pytest.mark.parametrize("text", ["Replace ❌ with ✅ ok", "Fertig ✅", "🚀 Start", "tab\there", "[Notiz] x"])
def test_bare_emoji_and_tab_are_allowed(text):
    assert qe.validate_task_text(text) == []
    assert qe.build_task_line({"text": text}, NOW)[0] == f"- [ ] {text}"


@pytest.mark.parametrize("field, value", [
    ("id", "a b"), ("id", "a#b"), ("needs", "a;b"), ("every", "0m"), ("every", "5x"), ("every", "m"),
    ("grace", "-1h"), ("timeout", "1d"), ("timeout", "30"), ("at", "25:00"), ("at", "2026-13-01T10:00"),
    ("at", "9:00"), ("tool", "gibts-nicht"), ("model", "#nope"), ("effort", "ultra"), ("profile", "a b"),
])
def test_value_errors(field, value):
    line, errs, _, _ = qe.build_task_line({"text": "T", field: value}, NOW)
    assert line is None and field in {e.field for e in errs}


def test_recurring_with_clock_time_becomes_next_iso_occurrence():
    afternoon = datetime(2026, 10, 23, 15, 0)
    line, errs, _, hint = qe.build_task_line({"text": "T", "at": "09:00", "every": "24h"}, afternoon)
    assert errs == [] and line == "- [ ] T #at:2026-10-24T09:00 #every:24h"
    assert hint == "Erster Lauf: 2026-10-24 09:00. Danach alle 24h."
    morning = datetime(2026, 10, 23, 7, 0)
    assert qe.build_task_line({"text": "T", "at": "09:00", "every": "24h"}, morning)[0].endswith("#at:2026-10-23T09:00 #every:24h")
    iso = qe.build_task_line({"text": "T", "at": "2026-10-30T09:00", "every": "7d"}, afternoon)[0]
    assert iso == "- [ ] T #at:2026-10-30T09:00 #every:7d"


def test_one_shot_clock_time_stays_bare_and_hint_is_honest():
    line, _, _, hint = qe.build_task_line({"text": "T", "at": "09:00"}, datetime(2026, 10, 23, 15, 0))
    assert line == "- [ ] T #at:09:00" and hint == "Faellig sofort."
    assert qe.build_task_line({"text": "T", "every": "1h"}, NOW)[3] == "Faellig sofort (kein #at:)."
    assert qe.build_task_line({"text": "T"}, NOW)[3] == ""


@pytest.mark.parametrize("fields, tags", [
    ({"text": "T", "shutdown": True}, ["#shutdown"]),
    ({"text": "T #approve:git_push"}, ["#approve:"]),
    ({"text": "T #tool_providers:openrouter"}, ["#tool_providers:"]),
    ({"text": "T #shutdown #approve:x", "shutdown": True}, ["#shutdown", "#approve:"]),
    ({"text": "harmlos", "no_commit": True}, []),
])
def test_dangers_in_field_and_free_text(fields, tags):
    _, _, dangers, _ = qe.build_task_line(fields, NOW)
    assert [d["tag"] for d in dangers] == tags
    assert [d["tag"] for d in qe.detect_dangers(str(fields["text"]) + (" #shutdown" if fields.get("shutdown") else ""))] == tags
