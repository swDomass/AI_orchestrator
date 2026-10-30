"""Scheibe B: GET/POST /api/claude-billing des Dashboards (echter Server auf 127.0.0.1, nur tmp_path).

Gruppen: GET-Schema, POST schaltet + hebt Sperre, Schranke (Token/Origin), Eingabepruefung, OSError -> 503,
der Schluessel steht in keiner Antwort. Die autouse-Fixture in conftest lenkt Schalter-/Sperrdatei nach tmp_path.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import analytics
import claude_billing
import config
import dashboard

SECRET = "sk-test-SECRET"


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
def api(monkeypatch):
    monkeypatch.setattr(analytics, "_load_active_runs", lambda: [])
    with _serving() as (server, port):
        yield SimpleNamespace(server=server, port=port)


def _req(a, method, body=None, headers=None):
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
        conn.putrequest(method, "/api/claude-billing", skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            if v is not None:
                conn.putheader(k, v)
        conn.endheaders(data)
        r = conn.getresponse()
        text = r.read().decode("utf-8", "replace")
        parsed = None
        with contextlib.suppress(ValueError):
            parsed = json.loads(text)
        return SimpleNamespace(status=r.status, text=text, json=parsed)
    finally:
        conn.close()


def _mode_file() -> Path:
    return Path(config.CLAUDE_BILLING_MODE_FILE)


def test_get_answers_with_status_and_text_and_defaults_to_api_without_a_file(api):
    r = _req(api, "GET")
    assert r.status == 200 and r.json["ok"] is True
    assert r.json["mode"] == "api" and r.json["api_active"] is False   # no key in tests
    assert {"source", "since", "key_configured", "blocked_until", "block_reason"} <= set(r.json)
    assert r.json["text"] == claude_billing.describe(claude_billing.status())
    assert not _mode_file().exists()


def test_post_abo_writes_the_file_with_source_dashboard_and_get_follows(api):
    r = _req(api, "POST", {"mode": "abo"})
    assert r.status == 200 and r.json["ok"] is True and r.json["mode"] == "abo"
    assert r.json["lifted_block"] is False and "Abo" in r.json["text"]
    data = json.loads(_mode_file().read_text(encoding="utf-8"))
    assert data["mode"] == "abo" and data["source"] == "dashboard"
    g = _req(api, "GET")
    assert g.json["mode"] == "abo" and g.json["source"] == "dashboard"


def test_post_api_lifts_a_running_block_and_removes_the_state_file(api, monkeypatch):
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_KEY", SECRET)
    claude_billing.trip("credit")
    assert Path(config.CLAUDE_API_STATE_FILE).exists()
    assert _req(api, "GET").json["blocked_until"] is not None
    r = _req(api, "POST", {"mode": "api"})
    assert r.status == 200 and r.json["lifted_block"] is True and r.json["mode"] == "api"
    assert not Path(config.CLAUDE_API_STATE_FILE).exists()
    assert r.json["blocked_until"] is None


def test_post_without_token_or_with_foreign_origin_is_403_and_nothing_is_written(api):
    r = _req(api, "POST", {"mode": "abo"}, headers={"X-Orch-Token": None})
    assert r.status == 403 and not _mode_file().exists()
    r = _req(api, "POST", {"mode": "abo"}, headers={"Origin": "http://evil.example"})
    assert r.status == 403 and not _mode_file().exists()


def test_post_with_a_wrong_token_leaves_an_existing_switch_unchanged(api):
    _req(api, "POST", {"mode": "abo"})
    before = _mode_file().read_bytes()
    r = _req(api, "POST", {"mode": "api"}, headers={"X-Orch-Token": "nope"})
    assert r.status == 403 and r.json["code"] == "token"
    assert _mode_file().read_bytes() == before


@pytest.mark.parametrize("body", [{"mode": "API"}, {}, {"mode": 1}, {"mode": None}, {"mode": ["abo"]}, {"x": "abo"}])
def test_post_with_a_bad_mode_is_400_bad_request_and_the_file_stays(api, body):
    r = _req(api, "POST", body)
    assert r.status == 400 and r.json["code"] == "bad_request" and r.json["ok"] is False
    assert not _mode_file().exists()


def test_post_ignores_additional_body_keys(api):
    r = _req(api, "POST", {"mode": "abo", "source": "evil", "lifted_block": True})
    assert r.status == 200
    assert json.loads(_mode_file().read_text(encoding="utf-8"))["source"] == "dashboard"


def test_set_mode_oserror_is_503_unavailable_with_a_fixed_text(api, monkeypatch):
    def boom(*a, **kw):
        raise OSError("disk full secret-path")
    monkeypatch.setattr(claude_billing, "set_mode", boom)
    r = _req(api, "POST", {"mode": "abo"})
    assert r.status == 503 and r.json["code"] == "unavailable"
    assert r.json["error"] == "Schalter nicht gespeichert." and "secret" not in r.text


def test_get_failure_is_a_fixed_500_without_the_exception_text(api, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("leak-me")
    monkeypatch.setattr(claude_billing, "status", boom)
    r = _req(api, "GET")
    assert r.status == 500 and r.json["code"] == "internal" and "leak-me" not in r.text


def test_the_api_key_is_in_no_answer(api, monkeypatch):
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_KEY", SECRET)
    answers = [_req(api, "GET"), _req(api, "POST", {"mode": "abo"}), _req(api, "POST", {"mode": "api"}),
               _req(api, "POST", {"mode": "x"})]
    assert answers[0].json["key_configured"] is True and answers[2].json["api_active"] is True
    assert all(SECRET not in a.text for a in answers)
