"""Queue-Editor S2: lint_draft (reduziertes Dokument), list_profile_names, queue_meta (nur tmp_path)."""

from __future__ import annotations

import importlib
import inspect
from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    import config
    import profiles
    import queue_editor as qe
    import queue_linter
    import queue_manager

DOC = (
    "## Queue\n\n- [ ] A #id:a\n- [ ] B #id:b #needs:a\n- [ ] P #parallel #id:p\n  - sub1\n  - sub2\n\n"
    "- [x] Done #id:d ✅ 2026-09-01 10:00 (claude)\n\n## Rest\n- [ ] ausserhalb #id:a\n"
)
P_REF = {"lines": ["- [ ] P #parallel #id:p", "  - sub1", "  - sub2"], "occurrence": 0}


def _full(lines, idx):
    """Befunde der Entwurfszeile im VOLLEN Dokument (nichts ersetzt)."""
    found = queue_linter.lint_queue("\n".join(lines))
    return [{"level": f.level, "code": f.code, "message": f.message} for f in found if f.line_no == idx + 1]


@pytest.mark.parametrize("position", ["top", "bottom"])
@pytest.mark.parametrize("draft, ref, expect_code", [
    ("C #id:a", None, "duplicate_id"),
    ("C #id:c #needs:a,d", None, None),
    ("C #needs:zz", None, "unknown_needs"),
    ("C #id:b #needs:b", None, "duplicate_id"),
    ("C #parallel", None, "parallel_no_subtasks"),
    ("P2 #parallel #id:p", P_REF, None),
    ("P2 #parallel #id:a", P_REF, "duplicate_id"),
])
def test_reduced_document_equals_full_document_for_the_draft_line(draft, ref, expect_code, position):
    res = qe.lint_draft(DOC, line="- [ ] " + draft, replace_ref=ref, position=position)
    lines, idx = qe._place_draft(DOC, "- [ ] " + draft, ref, position)
    assert res["findings"] == _full(lines, idx)
    assert (expect_code in {f["code"] for f in res["findings"]}) == (expect_code is not None)


def test_duplicate_id_names_the_line_of_the_other_task():
    top = qe.lint_draft(DOC, line="- [ ] C #id:a")["findings"][0]["message"]
    bottom = qe.lint_draft(DOC, line="- [ ] C #id:a", position="bottom")["findings"][0]["message"]
    assert top != bottom and "4" in top  # A rutscht von Zeile 3 auf 4, wenn oben eingefuegt wird


def test_schema_and_valid_flag():
    res = qe.lint_draft(DOC, line="- [ ] C #id:a")
    assert set(res) == {"ok", "line", "valid", "field_errors", "findings", "file_findings", "dangers",
                        "schedule_hint", "lint_available"}
    assert res["ok"] and res["lint_available"] and res["line"] == "- [ ] C #id:a" and res["valid"] is False
    warn_only = qe.lint_draft(DOC, line="- [ ] C #id:c")
    assert warn_only["valid"] is True and {f["level"] for f in warn_only["findings"]} == {"warning"}
    assert all(set(f) == {"level", "code", "message"} for f in warn_only["findings"])


def test_other_open_lines_are_reduced_but_line_count_and_rest_stay():
    lines, idx = qe._place_draft(DOC, "- [ ] N", None, "top")
    reduced = qe._reduce(lines, idx).split("\n")
    assert len(reduced) == len(lines) and reduced[idx] == "- [ ] N"
    assert reduced[3:5] == ["- [ ] x #id:a", "- [ ] x #id:b"]
    assert reduced[5] == "- [ ] x #id:p" and reduced[6:8] == ["  - sub1", "  - sub2"]
    assert reduced[-3:-1] == lines[-3:-1] and reduced[-1] == "- [ ] ausserhalb #id:a"  # ausserhalb der Sektion unveraendert


def test_edit_replaces_only_the_first_line_and_unknown_ref_is_reported():
    lines, idx = qe._place_draft(DOC, "- [ ] NEU", P_REF, "top")
    assert lines[idx:idx + 3] == ["- [ ] NEU", "  - sub1", "  - sub2"] and len(lines) == len(DOC.splitlines())
    res = qe.lint_draft(DOC, line="- [ ] X", replace_ref={"lines": ["- [ ] gibts nicht"], "occurrence": 0})
    assert res["line"] is None and [(e["field"], e["code"]) for e in res["field_errors"]] == [("ref", "changed")]


def test_field_errors_skip_the_linter_and_free_text_gets_dangers_and_hint():
    res = qe.lint_draft(DOC, line="- [ ] a\nb #shutdown")
    assert res["line"] is None and res["valid"] is False and res["findings"] == []
    assert [e["code"] for e in res["field_errors"]] == ["newline"]
    ok = qe.lint_draft(DOC, line="T #approve:x #at:2099-01-01T09:00")  # reiner Freitext ohne Praefix
    assert ok["line"] == "- [ ] T #approve:x #at:2099-01-01T09:00" and [d["tag"] for d in ok["dangers"]] == ["#approve:"]
    assert ok["schedule_hint"] == "Erster Lauf: 2099-01-01 09:00."


def test_missing_linter_degrades_without_leaking_the_error(caplog):
    real = importlib.import_module

    def fake(name, *a, **k):
        if name == "queue_linter":
            raise ImportError("geheim-xyz")
        return real(name, *a, **k)

    with patch("queue_editor.importlib.import_module", fake), caplog.at_level("WARNING"):
        res = qe.lint_draft(DOC, line="- [ ] C #id:a")
    assert res["lint_available"] is False and res["findings"] == [] and res["line"] == "- [ ] C #id:a"
    assert "geheim-xyz" not in repr(res) and "queue_linter" in caplog.text
    assert "redirect_stdout" not in inspect.getsource(qe)


def _write_profiles(root, names):
    root.mkdir(parents=True)
    for n in names:
        (root / f"{n}.yaml").write_text("providers: [claude]\n", encoding="utf-8")


def test_list_profile_names_matches_load_profile(tmp_path, monkeypatch):
    vault, extra = tmp_path / "vault", tmp_path / "extra"
    _write_profiles(vault / "99_System" / "AI" / "profiles", ["zz_a", "zz_b"])
    _write_profiles(extra, ["zz_b", "zz_c"])
    monkeypatch.setattr(config, "PROFILES_DIR", extra)
    names = profiles.list_profile_names(vault)
    assert names == sorted(set(names)) and {"zz_a", "zz_b", "zz_c"} <= set(names)
    assert all(profiles.load_profile(n, vault) is not None for n in names)
    assert profiles.list_profile_names(tmp_path / "leer") == sorted(n for n in names if n not in {"zz_a"})


def test_queue_meta_schema(tmp_path, monkeypatch):
    queue = tmp_path / "agent-queue.md"
    monkeypatch.setattr(queue_manager, "QUEUE_FILE", queue)
    assert queue_manager.queue_file().is_relative_to(tmp_path)
    here = tmp_path.as_posix()
    rows = [f'- [ ] t{i} cwd:{here}/p{i % 20}' for i in range(40)] + [f'- [ ] q cwd:"{here}/mit leer" #id:q']
    queue.write_text("## Queue\n" + "\n".join(rows) + "\nprosa cwd:/nicht-eine-task-zeile\n", encoding="utf-8")
    (tmp_path / "agent-queue-erledigt.md").write_text(f"- [x] alt cwd:{here}/alt ✅ 2026-09-01 10:00 (claude)\n", encoding="utf-8")
    (tmp_path / "p0").mkdir()
    _write_profiles(tmp_path / "99_System" / "AI" / "profiles", ["zz_meta"])
    monkeypatch.setattr(config, "VAULT_PATH", tmp_path)
    meta = qe.queue_meta()
    assert set(meta) == {"tools", "providers", "profiles", "effort", "cwd", "switches"}
    tools = {t["name"]: t for t in meta["tools"]}
    assert tools["dev-loop"]["requires_clean_worktree"] and tools["dev-loop"]["hint"]
    assert not tools["review-loop"]["requires_clean_worktree"] and tools["review-loop"]["hint"] == ""
    prov = {p["name"]: p for p in meta["providers"]}
    assert {"claude", "codex", "opencode"} <= set(prov) and "#claude_opus" in prov["claude"]["tags"]
    assert prov["claude"]["registered"] is True and isinstance(prov["claude"]["allowed"], bool)
    assert "zz_meta" in meta["profiles"] and meta["effort"]["levels"] == list(config.CLAUDE_EFFORT_LEVELS)
    recent = meta["cwd"]["recent"]
    assert len(recent) == 15 and len({r["path"] for r in recent}) == 15 and "prosa" not in repr(recent)
    assert {r["path"]: r["exists"] for r in recent}[f"{here}/p0"] is True
    assert meta["cwd"]["roots"] == [str(r) for r in config.ALLOWED_CWD_ROOTS]
    sw = {s["field"]: s for s in meta["switches"]}
    assert [f for f, s in sw.items() if s["danger"]] == ["shutdown"] and sw["grace"]["kind"] == "value"
    assert sw["parallel"]["kind"] == "flag" and all(s["hint"] for s in sw.values())


# --- R2a P3: lint_request liefert fuer alle Formen das Schema von lint_draft ---------------------

@pytest.fixture
def lint_queue_file(tmp_path, monkeypatch):
    path = tmp_path / "agent-queue.md"
    path.write_text(DOC, encoding="utf-8")
    monkeypatch.setattr(queue_manager, "QUEUE_FILE", path)
    return path


@pytest.mark.parametrize("body", [
    {"fields": {"text": "Neu"}}, {"fields": {"text": ""}}, {"text": "Neu #id:a"},
    {"text": "P2 #parallel", "ref": {**P_REF, "occurrences": 1}, "position": "bottom"}, {"fields": {"text": "Neu"}, "position": "bottom"},
])
def test_lint_request_has_the_lint_draft_schema_for_fields_text_and_errors(lint_queue_file, body):
    expect = set(qe.lint_draft(DOC, line="- [ ] Neu"))
    res = qe.lint_request(body)
    assert set(res) == expect and res["ok"] is True
    assert body.get("fields") != {"text": ""} or (res["valid"] is False and res["field_errors"])


def test_lint_request_content_form_and_bad_requests(lint_queue_file):
    assert qe.lint_request({"content": DOC}) == qe.lint_content(DOC)
    for bad in ({}, {"fields": {"text": "x"}, "position": "mitte"}, {"text": "x", "ref": {"lines": []}}):
        with pytest.raises(qe.QueueEditError) as exc:
            qe.lint_request(bad)
        assert exc.value.code == "bad_request"
