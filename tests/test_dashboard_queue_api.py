"""B1-S4: Queue-Editor-Endpunkte des Dashboards (echter Server auf 127.0.0.1, nur tmp_path).

Gruppen: Routen + Schranke, GET-Schema (read-only), Status-Zuordnung, Modus (inprocess/standalone),
Ende-zu-Ende add/raw/lint, Anschluss an den Orchestrator (Getter, "zweite Modulkopie").
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import importlib.util
import json
import socket
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import analytics
import config
import dashboard
import orchestrator
import queue_editor
import queue_manager

BASE = b"## Queue\r\n- [ ] Alpha\r\n- [ ] Beta\r\n- [x] Fertig \xe2\x9c\x85 2026-09-01 10:00 (claude)\r\n"
OPS = ("add", "edit", "delete", "retry", "duplicate", "move", "raw", "undo", "lint")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextlib.contextmanager
def _serving():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server, port = dashboard._bind_server(port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server, port
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = tmp_path / "agent-queue.md"
    monkeypatch.setattr(queue_manager, "QUEUE_FILE", path)
    monkeypatch.setattr(queue_manager, "QUEUE_EVENTS_LOG_FILE", tmp_path / "events.log")
    assert queue_manager.queue_file().is_relative_to(tmp_path) and Path(config.QUEUE_BACKUP_DIR).is_relative_to(tmp_path)
    monkeypatch.setattr(analytics, "_load_active_runs", lambda: [])
    monkeypatch.setattr(dashboard, "_QUEUE_IN_FLIGHT", None)
    path.write_bytes(BASE)
    with _serving() as (server, port):
        yield SimpleNamespace(server=server, port=port, path=path)


def _req(a, method, path, body=None, headers=None):
    h = {"Host": f"127.0.0.1:{a.port}"}
    if method == "POST":
        h |= {"Origin": f"http://127.0.0.1:{a.port}", "X-Orch-Token": a.server.write_token,
              "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}
    h |= headers or {}
    data = json.dumps(body).encode() if method == "POST" else None
    if data is not None:
        h["Content-Length"] = str(len(data))
    conn = http.client.HTTPConnection("127.0.0.1", a.port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            if v is not None:
                conn.putheader(k, v)
        conn.endheaders(data)
        r = conn.getresponse()
        text = r.read().decode("utf-8", "replace")
        with contextlib.suppress(ValueError):
            return SimpleNamespace(status=r.status, text=text, json=json.loads(text))
        return SimpleNamespace(status=r.status, text=text, json=None)
    finally:
        conn.close()


def get(a, path="/api/queue", **kw):
    return _req(a, "GET", path, **kw)


def post(a, op, body=None, **kw):
    return _req(a, "POST", f"/api/queue/{op}", body or {}, **kw)


def ref_of(a, prefix):
    e = next(x for x in queue_editor.parse_entries(a.path.read_bytes().decode(), now=datetime.now())
             if x.task_text.startswith(prefix))
    return {"lines": e.lines, "occurrence": e.occurrence, "occurrences": e.occurrences}


# --- Routen + Schranke ----------------------------------------------------------------------

def test_all_nine_queue_routes_are_registered_as_post_only():
    assert all(f"/api/queue/{op}" in dashboard._POST_ROUTES for op in OPS) and len(OPS) == 9


def test_a_post_without_the_key_or_from_a_foreign_origin_is_403_and_the_file_stays(api):
    before = api.path.read_bytes()
    for op in OPS:
        for bad in ({"X-Orch-Token": None}, {"X-Orch-Token": "falsch"}, {"Origin": "http://evil.example"}):
            r = post(api, op, {"sha": sha(before), "content": "x", "fields": {"text": "x"}}, headers=bad)
            assert r.status == 403, (op, bad)
    assert api.path.read_bytes() == before and not Path(config.QUEUE_BACKUP_DIR).exists()


def test_get_on_a_post_route_is_404_and_changes_nothing(api):
    assert get(api, "/api/queue/delete").status == 404 and api.path.read_bytes() == BASE


# --- GET: Schema, read-only -----------------------------------------------------------------

def test_get_queue_has_the_documented_schema_and_sha_is_the_byte_hash(api):
    mtime = api.path.stat().st_mtime_ns
    j = get(api).json
    assert j["ok"] is True and j["file"]["sha"] == sha(BASE) and "raw" not in j
    assert {"instance", "file", "running", "entries", "counts", "undo"} <= set(j)
    assert j["instance"] != api.server.write_token and len(j["entries"]) == 3
    assert j["running"] == {"mode": "standalone", "task": None}
    assert get(api, "/api/queue?raw=1").json["raw"] == BASE.decode()
    assert get(api, "/api/queue/meta").json["ok"] is True and api.path.stat().st_mtime_ns == mtime and not Path(config.QUEUE_BACKUP_DIR).exists()


def test_get_refuses_a_foreign_host_and_cross_site(api):
    for path in ("/api/queue", "/api/queue?raw=1", "/api/queue/meta"):
        for bad in ({"Host": "evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://evil.example"}):
            assert get(api, path, headers=bad).status == 403, (path, bad)


def test_instance_differs_per_server_start_and_a_read_failure_has_a_fixed_text(api, monkeypatch):
    first = get(api).json["instance"]
    with _serving() as (_server, port):
        assert get(SimpleNamespace(port=port), "/api/queue").json["instance"] not in (first, "")
    monkeypatch.setattr(queue_editor, "queue_state", lambda **kw: (_ for _ in ()).throw(RuntimeError("GEHEIM-xyz")))
    r = get(api)
    assert r.status == 500 and "GEHEIM" not in r.text and r.json["code"] == "internal"


# --- Status-Zuordnung -----------------------------------------------------------------------

# Unabhaengig von dashboard._QUEUE_STATUS festgeschrieben: Code -> HTTP-Status.
EXPECTED_STATUS = {
    "changed": 409, "ambiguous": 409, "edit_locked": 409, "undo_changed": 409, "undo_stale": 409, "no_undo": 409,
    "not_utf8": 409, "sha_mismatch": 409, "running": 423, "confirm_dangerous": 428, "confirm_unknown_running": 428,
    "stamp_in_raw": 422, "comment_in_raw": 422, "roundtrip": 422, "backup_failed": 503, "unavailable": 503,
    "backup_corrupt": 500, "bad_request": 400,
}


def test_every_editor_code_has_an_expected_status():
    assert set(queue_editor.ERROR_TEXTS) <= set(EXPECTED_STATUS), set(queue_editor.ERROR_TEXTS) - set(EXPECTED_STATUS)


@pytest.mark.parametrize("code", sorted(EXPECTED_STATUS))
def test_each_editor_code_maps_to_its_status_with_the_fixed_text(api, monkeypatch, code):
    def boom(*a, **kw):
        errs = [queue_editor.FieldError("text", code, "x")] if code == "roundtrip" else None  # Feldcode -> 422
        raise queue_editor.QueueEditError(code, errs)
    monkeypatch.setattr(queue_editor, "apply_edit", boom)
    r = post(api, "delete", {"ref": {}})
    assert (r.status, r.json["code"], r.json["ok"]) == (EXPECTED_STATUS[code], code, False)
    assert r.json["error"] == queue_editor.ERROR_TEXTS[code]


def test_a_field_error_is_422_and_carries_field_errors(api):
    r = post(api, "add", {"fields": {"text": ""}})
    assert "field_errors" in r.json, r.json
    assert r.status == 422 and r.json["ok"] is False and r.json["field_errors"][0]["field"] == "text" and r.json["error"]
    assert api.path.read_bytes() == BASE


def test_an_unknown_exception_is_a_500_without_its_text(api, monkeypatch):
    monkeypatch.setattr(queue_editor, "apply_edit", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("GEHEIM-xyz")))
    r = post(api, "add", {"fields": {"text": "x"}})
    assert r.status == 500 and r.json["code"] == "internal" and "GEHEIM" not in r.text


def test_a_garbage_body_is_400_and_a_garbage_lint_body_too(api):
    assert post(api, "move", {"ref": 5}).status == 400 and post(api, "lint", {"x": 1}).status == 400
    assert post(api, "lint", {"text": "x", "ref": {"lines": 3}}).status == 400 and api.path.read_bytes() == BASE


# --- Modus ----------------------------------------------------------------------------------

def test_standalone_asks_first_and_inprocess_locks_the_running_line(api, monkeypatch):
    r = post(api, "delete", {"ref": ref_of(api, "Alpha")})
    assert (r.status, r.json["code"]) == (428, "confirm_unknown_running") and api.path.read_bytes() == BASE
    monkeypatch.setattr(dashboard, "_QUEUE_IN_FLIGHT", lambda: SimpleNamespace(task_text="Alpha", subtasks=(), started_at=1.0, tool=""))
    j = get(api).json
    assert j["running"]["mode"] == "inprocess" and j["running"]["task"]["text"] == "Alpha"
    assert j["entries"][0]["state"] == "laeuft"
    r = post(api, "delete", {"ref": ref_of(api, "Alpha"), "confirm_unknown_running": True})
    assert (r.status, r.json["code"]) == (423, "running") and api.path.read_bytes() == BASE
    assert post(api, "delete", {"ref": ref_of(api, "Beta")}).status == 200  # another line: no question in-process


def test_an_active_run_locks_the_line_through_the_post(api, monkeypatch):
    api.path.write_bytes(b"## Queue\r\n- [ ] Pruefen #tool:dev-loop\r\n")
    task = queue_editor.parse_entries(api.path.read_bytes().decode(), now=datetime.now())[0].task_text
    monkeypatch.setattr(analytics, "_load_active_runs", lambda: [{"status": "running", "tool": "dev-loop", "task": queue_manager.strip_metadata_tags(task)[:200]}])
    r = post(api, "delete", {"ref": ref_of(api, "Pruefen"), "confirm_unknown_running": True})
    assert (r.status, r.json["code"]) == (423, "running")
    assert get(api).json["entries"][0]["lock_reason"] == "running"


# --- Ende zu Ende ---------------------------------------------------------------------------

def test_add_over_http_writes_the_line_and_answers_with_the_queue(api):
    r = post(api, "add", {"fields": {"text": "Neu per HTTP"}, "position": "bottom"})
    assert r.status == 200 and r.json["changed"] is True and r.json["queue"]["ok"] is True and r.json["queue"]["instance"]
    assert b"- [ ] Neu per HTTP\r\n" in api.path.read_bytes()
    assert r.json["queue"]["file"]["sha"] == sha(api.path.read_bytes()) and r.json["queue"]["undo"]["available"] is True
    assert post(api, "undo", {"backup_id": r.json["queue"]["undo"]["id"], "confirm_unknown_running": True}).status == 200 and api.path.read_bytes() == BASE


def test_raw_over_http_sha_ok_then_409_and_a_new_stamp_is_422(api):
    text = BASE.decode().replace("\r\n", "\n")
    asked = post(api, "raw", {"sha": sha(BASE), "content": text.replace("Alpha", "Alpha2")})
    assert (asked.status, asked.json["code"]) == (428, "confirm_unknown_running") and api.path.read_bytes() == BASE
    r = post(api, "raw", {"sha": sha(BASE), "content": text.replace("Alpha", "Alpha2"), "confirm_unknown_running": True})
    assert r.status == 200 and api.path.read_bytes() == BASE.replace(b"Alpha", b"Alpha2")
    stale = post(api, "raw", {"sha": sha(BASE), "content": "x", "confirm_unknown_running": True})
    assert (stale.status, stale.json["code"]) == (409, "sha_mismatch")
    now = api.path.read_bytes()
    stamped = now.decode().replace("\r\n", "\n").replace("- [ ] Beta", "- [ ] Beta ✅ 2026-10-23 11:00 (claude)")
    r = post(api, "raw", {"sha": sha(now), "content": stamped, "confirm_unknown_running": True})
    assert (r.status, r.json["code"]) == (422, "stamp_in_raw") and api.path.read_bytes() == now


def test_lint_answers_for_fields_text_and_content_and_never_writes(api):
    f = post(api, "lint", {"fields": {"text": "Lint mich"}}).json
    assert f["ok"] is True and f["line"] == "- [ ] Lint mich" and f["field_errors"] == []
    bad = post(api, "lint", {"fields": {"text": ""}}).json
    assert bad["line"] is None and bad["field_errors"][0]["field"] == "text"
    t = post(api, "lint", {"text": "Alpha neu", "ref": ref_of(api, "Alpha")}).json
    assert t["line"] == "- [ ] Alpha neu"
    c = post(api, "lint", {"content": "## Queue\n- [ ] A #id:x\n- [ ] B #id:x\n"}).json
    assert c["ok"] is True and any(x.get("line") for x in c["findings"] + c["file_findings"])
    assert api.path.read_bytes() == BASE and not Path(config.QUEUE_BACKUP_DIR).exists()


# --- Anschluss an den Orchestrator ----------------------------------------------------------

def test_start_autostart_stores_the_getter_and_standalone_start_clears_it(monkeypatch):
    monkeypatch.setattr(dashboard, "_QUEUE_IN_FLIGHT", None)
    for name in ("_autostart_serve", "_start_harness_index_thread", "_report_startup_warnings"):
        monkeypatch.setattr(dashboard, name, lambda *a, **kw: None)
    getter = lambda: None  # noqa: E731
    dashboard.start_autostart(in_flight=getter, warn=lambda m: None, info=lambda m: None)
    assert dashboard._QUEUE_IN_FLIGHT is getter
    dashboard.start_autostart(warn=lambda m: None, info=lambda m: None)
    assert dashboard._QUEUE_IN_FLIGHT is None


def test_the_autostart_getter_reads_the_module_copy_whose_set_in_flight_ran(monkeypatch):
    """run_orchestrator.ps1 starts orchestrator.py as a script: other modules import a SECOND copy."""
    primary = orchestrator
    name = "orchestrator_second_copy"
    spec = importlib.util.spec_from_file_location(name, Path(primary.__file__))
    assert spec is not None and spec.loader is not None
    copy = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, copy)
    spec.loader.exec_module(copy)
    got = {}
    monkeypatch.setattr(dashboard, "start_autostart", lambda **kw: got.update(kw))
    monkeypatch.setattr(config, "DASHBOARD_AUTOSTART", True)
    try:
        copy._start_dashboard_autostart()
        getter = got.get("in_flight")
        assert getter is not None, "start_autostart wurde ohne in_flight-Getter aufgerufen"
        assert callable(getter) and copy is not primary and getter() is None
        copy._set_in_flight(SimpleNamespace(task_text="Aus der Kopie", line_no=1))
        assert primary._in_flight_task is None and getter().task_text == "Aus der Kopie"
        primary._set_in_flight(SimpleNamespace(task_text="Aus dem Original", line_no=2))
        assert getter().task_text == "Aus der Kopie" and getter() is copy._in_flight_task
    finally:
        copy._clear_in_flight()
        primary._clear_in_flight()
