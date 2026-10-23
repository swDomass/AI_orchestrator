"""B0-S4a: the write gate (POST) of the dashboard server.

Base state of every case is "everything valid"; each case breaks EXACTLY ONE
condition, so each refusal has its own counter-probe. Real server on
127.0.0.1, raw headers via http.client (skip_host / skip_accept_encoding).
Groups: gate checks 1-4, route 5, body checks 6-10, route errors 11, other
methods / no CORS, key embedding, one key per server start.
"""

import contextlib
import http.client
import json
import logging
import os
import pathlib
import re
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

import analytics
import config
import dashboard

HEADER_NAMES = {
    "host": "Host", "origin": "Origin", "token": "X-Orch-Token", "sfs": "Sec-Fetch-Site",
    "ctype": "Content-Type", "clen": "Content-Length",
}
VALID = {
    "host": "127.0.0.1:{p}", "origin": "http://127.0.0.1:{p}", "token": "{t}", "sfs": "same-origin",
    "ctype": "application/json", "clen": "{n}",
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@contextlib.contextmanager
def _serving():
    server, port = dashboard._bind_server(_free_port())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server, port
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def srv(monkeypatch):
    monkeypatch.setattr(analytics, "_load_active_runs", lambda: [])
    with _serving() as pair:
        yield pair


def _result(resp, data: bytes) -> SimpleNamespace:
    text = data.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    return SimpleNamespace(
        status=resp.status, headers={k.lower(): v for k, v in resp.getheaders()}, text=text, json=parsed,
    )


def _call(srv, *, method="POST", path="/api/ping", body=b"{}", send_body=True, extra=(), **over):
    """One request; ``over`` replaces VALID entries (None = omit, list = repeated header)."""
    server, port = srv
    token = server.write_token
    fmt = {"p": port, "p2": port + 1, "t": token, "t1": token[:-1] + ("A" if token[-1] != "A" else "B"), "n": len(body)}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for key, default in VALID.items():
            value = over.get(key, default)
            for v in ([] if value is None else value if isinstance(value, list) else [value]):
                conn.putheader(HEADER_NAMES[key], v.format(**fmt))
        for name, value in extra:
            conn.putheader(name, value)
        conn.endheaders(body if send_body else None)
        resp = conn.getresponse()
        return _result(resp, resp.read())
    finally:
        conn.close()


def _raw(srv, head: str, body: bytes = b"") -> SimpleNamespace:
    """Raw socket: the request goes out exactly as written."""
    server, port = srv
    head = head.format(p=port, t=server.write_token)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(head.encode("latin-1") + b"\r\n\r\n" + body)
        buf = b""
        while chunk := sock.recv(65536):
            buf += chunk
    raw_head, _, payload = buf.partition(b"\r\n\r\n")
    status = int(raw_head.split(b" ", 2)[1])
    text = payload.decode("utf-8", "replace")
    return SimpleNamespace(status=status, text=text, json=json.loads(text))


def _get(srv, path="/", host="127.0.0.1:{p}", method="GET"):
    _, port = srv
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not None:
            conn.putheader("Host", host.format(p=port) if "{p}" in host else host)
        conn.endheaders()
        resp = conn.getresponse()
        return _result(resp, resp.read())
    finally:
        conn.close()


def _assert_refused(r, status, code):
    assert r.status == status, r.text
    assert r.json["ok"] is False and r.json["code"] == code
    assert r.headers["connection"] == "close"
    assert set(r.json) == {"ok", "error", "code"}


# ---- success -------------------------------------------------------------------------

def test_valid_post_succeeds(srv):
    r = _call(srv)
    assert r.status == 200 and r.json == {"ok": True}
    assert r.headers["connection"] == "close"


@pytest.mark.parametrize("over", [
    {"sfs": None},
    {"host": "localhost:{p}", "origin": "http://localhost:{p}"},
    {"host": "[::1]:{p}", "origin": "http://[::1]:{p}"},
    {"host": "localhost:{p}", "origin": " HTTP://LOCALHOST:{p} "},
    {"ctype": "application/json; charset=utf-8"},
    {"ctype": "Application/JSON; charset=UTF-8"},
], ids=["no-sec-fetch-site", "localhost", "ipv6-loopback", "origin-case-space", "charset-utf8", "case-charset"])
def test_valid_variants_succeed(srv, over):
    r = _call(srv, **over)
    assert r.status == 200 and r.json == {"ok": True}


def test_body_of_exactly_the_limit_succeeds(srv):
    body = b'{"a":"' + b"x" * (262144 - 8) + b'"}'
    assert len(body) == 262144
    assert _call(srv, body=body).status == 200


# ---- seven mandatory cases + checks 1-4 -------------------------------------------------

@pytest.mark.parametrize("over,status,code", [
    ({"token": None}, 403, "token"),
    ({"token": "{t1}"}, 403, "token"),
    ({"token": ""}, 403, "token"),
    ({"token": ["{t}", "{t}"]}, 403, "token"),
    ({"origin": "http://evil.example"}, 403, "forbidden"),
    ({"origin": "null"}, 403, "forbidden"),
    ({"origin": "http://127.0.0.1:{p2}"}, 403, "forbidden"),
    ({"origin": "https://127.0.0.1:{p}"}, 403, "forbidden"),
    ({"origin": None}, 403, "forbidden"),
    ({"origin": ["http://127.0.0.1:{p}", "http://127.0.0.1:{p}"]}, 403, "forbidden"),
    ({"origin": "http://localhost:{p}"}, 403, "forbidden"),  # loopback too, but not THIS host
    ({"host": "[::1]:{p}"}, 403, "forbidden"),               # same, the other way round
    ({"host": None}, 403, "forbidden"),
    ({"host": "evil.example"}, 403, "forbidden"),
    ({"host": "127.0.0.1:{p2}"}, 403, "forbidden"),
    ({"host": "127.0.0.1"}, 403, "forbidden"),
    ({"host": "dash.example:{p}"}, 403, "forbidden"),  # in DASHBOARD_ALLOWED_HOSTS, still no
    ({"host": ["127.0.0.1:{p}", "127.0.0.1:{p}"]}, 400, "bad_request"),
    ({"sfs": "cross-site"}, 403, "forbidden"),
    ({"sfs": "same-site"}, 403, "forbidden"),
    ({"sfs": "none"}, 403, "forbidden"),
    ({"sfs": ["same-origin", "cross-site"]}, 403, "forbidden"),
    ({"ctype": "text/plain"}, 415, "media_type"),
    ({"ctype": None}, 415, "media_type"),
    ({"ctype": "application/json; charset=latin-1"}, 415, "media_type"),
    ({"ctype": ["application/json", "application/json"]}, 415, "media_type"),
    ({"clen": "abc"}, 400, "bad_request"),
    ({"clen": "-1"}, 400, "bad_request"),
    ({"clen": "12345678"}, 400, "bad_request"),
    ({"clen": ["2", "2"]}, 400, "bad_request"),
], ids=[
    "no-token", "wrong-token", "empty-token", "double-token", "foreign-origin", "origin-null", "origin-other-port",
    "origin-https", "no-origin", "double-origin", "origin-host-mismatch", "host-origin-mismatch", "no-host", "host-evil", "host-wrong-port", "host-no-port",
    "host-allowed-name", "double-host", "cross-site", "same-site", "none", "double-sec-fetch", "text-plain",
    "no-content-type", "latin1", "double-content-type", "clen-abc", "clen-minus", "clen-8-digits", "double-clen",
])
def test_single_violation_is_refused(srv, monkeypatch, over, status, code):
    monkeypatch.setattr(config, "DASHBOARD_ALLOWED_HOSTS", ("dash.example",))
    _assert_refused(_call(srv, **over), status, code)


def test_oversize_is_refused_from_the_header_alone_and_fast(srv):
    t0 = time.monotonic()
    r = _call(srv, clen="262145", send_body=False)
    assert time.monotonic() - t0 < 2.5  # reading the body instead would end at the 5-s client timeout
    _assert_refused(r, 413, "too_large")


def test_missing_or_chunked_length_is_411(srv):
    base = ("POST /api/ping HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\nOrigin: http://127.0.0.1:{p}\r\n"
            "X-Orch-Token: {t}\r\nContent-Type: application/json\r\nConnection: keep-alive")
    for r in (_raw(srv, base), _raw(srv, base + "\r\nTransfer-Encoding: chunked", b"2\r\n{}\r\n0\r\n\r\n"),
              _raw(srv, base + "\r\nTransfer-Encoding: chunked\r\nContent-Length: 2", b"{}")):
        assert r.status == 411 and r.json["code"] == "length_required"


def test_short_body_is_400(srv):
    server, port = srv
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/api/ping", skip_host=True, skip_accept_encoding=True)
    for key, value in VALID.items():
        conn.putheader(HEADER_NAMES[key], value.format(p=port, t=server.write_token, n=10))
    conn.endheaders(b"{}")
    conn.sock.shutdown(socket.SHUT_WR)
    resp = conn.getresponse()
    r = _result(resp, resp.read())
    conn.close()
    _assert_refused(r, 400, "bad_request")


def test_each_gate_check_rejects_alone_and_wins_over_later_ones(srv):
    # token missing AND bad content type AND unknown route: the key decides
    r = _call(srv, path="/api/nope", token=None, ctype="text/plain")
    _assert_refused(r, 403, "token")


def test_done_curl_case_is_403(srv):
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl is None:
        pytest.skip("curl not installed")
    _, port = srv
    out = subprocess.run(
        [curl, "-s", "-o", os.devnull, "-w", "%{http_code}", "-X", "POST", "-H", "Content-Type: application/json",
         "-d", "{}", f"http://127.0.0.1:{port}/api/ping"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert out.stdout.strip() == "403"


def test_token_of_another_server_is_403():
    with _serving() as a, _serving() as b:
        assert a[0].write_token != b[0].write_token
        r = _call(a, token=b[0].write_token)
        _assert_refused(r, 403, "token")


# ---- route (5) and body (10) ------------------------------------------------------------------

def test_unknown_route_is_404_with_key_and_403_without(srv):
    _assert_refused(_call(srv, path="/api/nope"), 404, "not_found")
    r = _call(srv, path="/api/nope", token=None)
    ping = _call(srv, token=None)
    _assert_refused(r, 403, "token")
    assert (r.status, r.json) == (ping.status, ping.json)  # endpoints cannot be probed without the key


@pytest.mark.parametrize("body", [b"{", b"[]", b"3", b"\xff\xfe{}", b"[" * 100000], ids=["syntax", "list", "number", "utf8", "deep"])
def test_bad_json_is_400(srv, body):
    r = _call(srv, body=body)
    _assert_refused(r, 400, "bad_json")
    assert "Expecting" not in r.text and "line" not in r.text


# ---- route errors (11) ------------------------------------------------------------------------

def test_route_exception_is_500_without_secret_text(srv, monkeypatch, caplog):
    def boom(body):
        raise RuntimeError("geheim-XYZ C:\\Users\\x\\secret")

    monkeypatch.setitem(dashboard._POST_ROUTES, "/api/boom", boom)
    with caplog.at_level(logging.DEBUG):
        r = _call(srv, path="/api/boom")
    _assert_refused(r, 500, "internal")
    assert "geheim" not in r.text and "secret" not in r.text
    assert "geheim-XYZ" in caplog.text            # the operator still sees it
    assert srv[0].write_token not in caplog.text  # the key is never logged


@pytest.mark.parametrize("exc", [TimeoutError, ConnectionResetError])
def test_route_raising_a_network_error_is_still_a_500(srv, monkeypatch, caplog, exc):
    # Only the body read counts as "client gone"; a route's own lock/pipe timeout is a fault.
    def slow(body):
        raise exc("route-internal")

    monkeypatch.setitem(dashboard._POST_ROUTES, "/api/slow", slow)
    with caplog.at_level(logging.DEBUG):
        r = _call(srv, path="/api/slow")
    _assert_refused(r, 500, "internal")
    assert [rec for rec in caplog.records if rec.levelno >= logging.ERROR]


def test_unserialisable_route_result_is_500_json_not_a_dead_connection(srv, monkeypatch, caplog):
    monkeypatch.setitem(dashboard._POST_ROUTES, "/api/obj", lambda body: {"x": object()})
    with caplog.at_level(logging.DEBUG):
        r = _call(srv, path="/api/obj")
    _assert_refused(r, 500, "internal")
    assert "object" not in r.text and "serializable" not in r.text
    assert "POST handler failed" in caplog.text   # the operator still sees it


def _request_head(port: int, length: int, token: str | None) -> bytes:
    lines = ["POST /api/ping HTTP/1.1", f"Host: 127.0.0.1:{port}", f"Origin: http://127.0.0.1:{port}",
             "Content-Type: application/json", f"Content-Length: {length}"]
    if token:
        lines.append(f"X-Orch-Token: {token}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def test_body_timeout_closes_silently_without_error_log(srv, monkeypatch, caplog):
    monkeypatch.setattr(dashboard._Handler, "timeout", 0.5)
    server, port = srv
    buf = b""
    with caplog.at_level(logging.DEBUG), socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(_request_head(port, 10, server.write_token) + b"{}")  # 2 of 10 bytes, socket stays open
        with contextlib.suppress(OSError):  # a reset is as good as EOF here
            while chunk := sock.recv(65536):
                buf += chunk
    assert buf == b""                                    # no 500, no answer at all
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_drain_of_a_trickling_body_has_a_total_deadline(srv):
    _, port = srv
    t0 = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=0.4) as sock:
        sock.sendall(_request_head(port, 1000, None))  # no key: refused, body left unread
        with contextlib.suppress(OSError):             # a reset counts as closed
            while time.monotonic() - t0 < 6:
                try:
                    if not sock.recv(65536):
                        break                          # the server closed
                except TimeoutError:
                    sock.send(b"x")                    # one byte per 0.4 s
    assert time.monotonic() - t0 < 3.0


def test_api_error_carries_its_fixed_text_and_route_gets_the_dict(srv, monkeypatch):
    seen = {}

    def route(body):
        seen.update(body)
        if body.get("fail"):
            raise dashboard.ApiError(409, "conflict", "Konflikt.")
        return {"ok": True, "n": 1}

    monkeypatch.setitem(dashboard._POST_ROUTES, "/api/t", route)
    assert _call(srv, path="/api/t", body=b'{"a": 1}').json == {"ok": True, "n": 1}
    assert seen == {"a": 1}
    r = _call(srv, path="/api/t", body=b'{"fail": true}')
    assert r.status == 409 and r.json == {"ok": False, "error": "Konflikt.", "code": "conflict"}


@pytest.mark.parametrize("args", [
    (409, "conflict", pathlib.PurePath("C:/x")),   # not a str
    ("409", "conflict", "Konflikt."),              # status as text
    (409, "conflict", "bad \ud800"),               # lone surrogate, not UTF-8 encodable
    (409, "bad\ud800", "Konflikt."),               # same in the code field
], ids=["path-message", "str-status", "surrogate", "code-surrogate"])
def test_malformed_api_error_in_a_route_is_a_500_not_a_dead_connection(srv, monkeypatch, args):
    def route(body):
        raise dashboard.ApiError(*args)

    monkeypatch.setitem(dashboard._POST_ROUTES, "/api/bad", route)
    _assert_refused(_call(srv, path="/api/bad"), 500, "internal")


def test_post_route_registration_rules(monkeypatch):
    monkeypatch.setattr(dashboard, "_POST_ROUTES", dict(dashboard._POST_ROUTES))
    with pytest.raises(ValueError):
        dashboard.post_route("/nope")(lambda b: {})
    with pytest.raises(ValueError):
        dashboard.post_route("/api/ping")(lambda b: {})
    assert list(dashboard._POST_ROUTES) == ["/api/ping"]


# ---- other methods, no CORS -------------------------------------------------------------------

@pytest.mark.parametrize("method", ["OPTIONS", "PUT", "DELETE", "PATCH"])
def test_other_methods_are_405_without_cors(srv, method):
    r = _call(srv, method=method, extra=[("Access-Control-Request-Method", "POST")], body=b"")
    _assert_refused(r, 405, "method_not_allowed")
    assert r.headers["allow"] == "GET, POST"
    assert not any(k.startswith("access-control-") for k in r.headers)


def test_no_cors_header_anywhere(srv):
    answers = [_get(srv, "/"), _get(srv, "/api/data?only=active_runs"), _call(srv), _call(srv, token=None)]
    assert [a.status for a in answers] == [200, 200, 200, 403]
    for a in answers:
        assert not any(k.startswith("access-control-") for k in a.headers)


# ---- key embedding ----------------------------------------------------------------------------

def _meta(page: str) -> str:
    m = re.search(r'<meta name="orch-token" content="([^"]*)"', page)
    assert m, "meta tag missing"
    return m.group(1)


def test_index_carries_the_key_on_loopback(srv):
    for host in ("127.0.0.1:{p}", "localhost:{p}"):
        page = _get(srv, "/", host=host).text
        assert _meta(page) == srv[0].write_token and "__ORCH_TOKEN__" not in page


def test_index_has_no_key_for_other_hosts(srv, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_ALLOWED_HOSTS", ("dash.example",))
    _, port = srv
    for host in ("dash.example:{p}", "127.0.0.1", f"127.0.0.1:{port + 1}", None):
        r = _get(srv, "/", host=host)
        assert r.status == 200 and _meta(r.text) == "" and "__ORCH_TOKEN__" not in r.text
    assert srv[0].write_token not in _get(srv, "/", host="dash.example:{p}").text


def test_every_server_start_has_its_own_key():
    with _serving() as a, _serving() as b:
        assert a[0].write_token != b[0].write_token
        assert len(a[0].write_token) >= 40
