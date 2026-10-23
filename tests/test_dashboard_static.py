"""B0-S2b: the dashboard page is served from dashboard_ui/ via a FIXED list.

Covered: every list entry (bytes + Content-Type), nothing on disk missing from
the list, everything else and every traversal spelling -> 404 (raw paths via
http.client, nothing normalised), no external URL, vendor hashes, the token
placeholder, independence from the working directory, a missing file -> fixed
500 text and the server keeps running, foreign Host / cross-site -> 403.
Real server on 127.0.0.1, synchronous.
"""

import hashlib
import http.client
import pathlib
import re
import socket
import threading

import pytest

import analytics
import dashboard

UI = dashboard._UI_DIR
VENDOR_SHA256 = {
    "vendor/chart.js-4.6.1/chart.umd.min.js": "87364cc2b2a76a6684929e7d6b80b18a897794db38400caff1fcf1791c6925d2",
    "vendor/chart.js-4.6.1/LICENSE.md": "41a84aa2caba645f966a18d9c2056b73e6d3a81d80bc0046bc0011a2634d4cce",
    "vendor/chartjs-plugin-zoom-1.2.1/chartjs-plugin-zoom.min.js": "711b558fad87393d4cdc419cfb810169c53fdfc858b18acd041e3ccc4567f2e5",
    "vendor/chartjs-plugin-zoom-1.2.1/LICENSE.md": "faf50ba4a21e0c740c96e9bbce1b862273cfe5dd2cdf200d3271a781094c6ba9",
}
VENDOR_BANNER_URLS = {"https://www.chartjs.org", "https://github.com/kurkle/color#readme"}
NOT_LISTED = [
    "/README.md", "/LICENSE", "/dashboard.py", "/.env", "/dashboard_ui/index.html", "/static/", "/static",
    "/static/vendor/README.md", "/static/vendor/chart.js-4.6.1/LICENSE.md", "/static/nope.js",
]
TRAVERSAL = [
    "/static/../dashboard.py", "/static/..%2fdashboard.py", "/static/%2e%2e/%2e%2e/config.py",
    "/static/..\\dashboard.py", "//etc/passwd", "/STATIC/APP.JS", "/static/app.js.", "/static/app.js::$DATA",
    "/static/app.js;x", "/static/./app.js", "/static//app.js", "/static/app.js%00.css",
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
def port(monkeypatch):
    monkeypatch.setattr(analytics, "_load_active_runs", lambda: [])
    server, p = dashboard._bind_server(_free_port())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield p
    finally:
        server.shutdown()
        server.server_close()


def _get(port: int, path: str, headers: dict | None = None, host: str | None = None):
    """Raw GET: the path goes out exactly as written. Returns (status, content-type, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{port}")
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type"), resp.read()
    finally:
        conn.close()


def _own_files() -> list:
    return [UI / "index.html"] + [UI / rel for rel, _ in dashboard._STATIC_FILES.values()
                                  if not rel.startswith("vendor/")]


def test_list_entries_serve_exact_bytes_and_content_type(port):
    assert len(dashboard._STATIC_FILES) == 8
    # The expected type is derived from the file extension HERE, not read from
    # the table the server uses (else a wrong table entry would stay green).
    by_ext = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
    for url, (rel, _) in dashboard._STATIC_FILES.items():
        status, got_type, body = _get(port, url)
        assert (status, got_type, body) == (200, by_ext[pathlib.PurePosixPath(rel).suffix], (UI / rel).read_bytes()), url
    for url in ("/", "/index.html"):
        status, got_type, _ = _get(port, url)
        assert (status, got_type) == (200, "text/html; charset=utf-8")
    assert _get(port, "/static/app.js?x=1")[0] == 200


def test_no_file_on_disk_is_missing_from_the_list():
    listed = {rel for rel, _ in dashboard._STATIC_FILES.values()} | {"index.html"}
    on_disk = {p.relative_to(UI).as_posix() for p in UI.rglob("*") if p.is_file()
               and not p.name.upper().startswith(("LICENSE", "README")) and "__pycache__" not in p.parts}
    assert on_disk == listed
    assert all((UI / rel).is_file() for rel in listed)


@pytest.mark.parametrize("path", NOT_LISTED + TRAVERSAL)
def test_unlisted_and_traversal_paths_are_404(port, path):
    status, _, body = _get(port, path)
    assert status == 404
    assert b"def " not in body and b"CLAUDE_" not in body


def test_no_external_url_in_own_files_or_served_page(port):
    page = _get(port, "/")[2].decode("utf-8")
    texts = [p.read_text(encoding="utf-8") for p in _own_files()] + [page]
    for text in texts:
        assert not re.search(r"https?://", text)
        assert not re.search(r"""(?:src|href)\s*=\s*["']?//|["'`]//[A-Za-z0-9]""", text)
    for url in re.findall(r"""(?:src|href)="([^"]+)\"""", page):
        assert url in dashboard._STATIC_FILES, url
    for rel in VENDOR_SHA256:
        if rel.endswith(".js"):
            urls = set(re.findall(r"https?://[^\s\"'<>)]+", (UI / rel).read_text(encoding="utf-8")))
            assert urls <= VENDOR_BANNER_URLS, urls


@pytest.mark.parametrize("rel,expected", sorted(VENDOR_SHA256.items()))
def test_vendor_files_have_the_recorded_sha256(rel, expected):
    assert hashlib.sha256((UI / rel).read_bytes()).hexdigest() == expected
    assert (UI / rel.rsplit("/", 1)[0] / "LICENSE.md").is_file()


def test_token_placeholder_once_in_index_never_in_js_never_served(port):
    assert (UI / "index.html").read_bytes().count(b"__ORCH_TOKEN__") == 1
    for rel, _ in dashboard._STATIC_FILES.values():
        if rel.endswith(".js"):
            assert b"__ORCH_TOKEN__" not in (UI / rel).read_bytes(), rel
    served = _get(port, "/")[2]
    assert b"__ORCH_TOKEN__" not in served
    # Loopback request: the per-start key is embedded (value checked in test_dashboard_write_gate).
    assert re.search(rb'<meta name="orch-token" content="[A-Za-z0-9_-]{43}">', served)


def test_served_regardless_of_working_directory(port, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    status, _, body = _get(port, "/static/app.js")
    assert status == 200 and body == (UI / "app.js").read_bytes()
    assert _get(port, "/")[0] == 200


def test_missing_ui_dir_is_a_fixed_500_and_server_keeps_running(port, monkeypatch, tmp_path):
    empty = tmp_path / "geheim-leer"
    empty.mkdir()
    monkeypatch.setattr(dashboard, "_UI_DIR", empty)
    for path in ("/", "/static/app.js"):
        status, _, body = _get(port, path)
        assert status == 500
        assert b"Dashboard-Dateien fehlen" in body
        assert str(tmp_path).encode() not in body and b"geheim" not in body
    assert _get(port, "/api/harness")[0] == 200


@pytest.mark.parametrize("path", ["/static/core.js", "/static/vendor/chart.js-4.6.1/chart.umd.min.js", "/"])
def test_foreign_host_is_refused(port, path):
    assert _get(port, path, host="evil.example")[0] == 403
    assert _get(port, path, host=f"evil.example:{port}")[0] == 403


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
@pytest.mark.parametrize("mode", ["no-cors", "navigate"])
def test_cross_site_is_refused_for_static_files(port, site, mode):
    headers = {"Sec-Fetch-Site": site, "Sec-Fetch-Mode": mode}
    assert _get(port, "/static/core.js", headers)[0] == 403
    assert _get(port, "/static/core.js", {"Origin": "https://evil.example"})[0] == 403
    assert _get(port, "/static/core.js", {"Sec-Fetch-Site": "same-origin"})[0] == 200


# ── B0-S3: security headers + CSP-clean frontend ──────────────────────────────
EXPECTED_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
EXPECTED_HEADERS = {
    "Content-Security-Policy": EXPECTED_CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _headers(port: int, path: str, host: str | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{port}")
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        return resp.status, resp.getheaders()
    finally:
        conn.close()


@pytest.mark.parametrize("path,status", [
    ("/", 200), ("/static/app.js", 200), ("/static/app.css", 200), ("/api/data", 200),
    ("/api/harness", 200), ("/does-not-exist", 404),
])
def test_security_headers_on_every_response(port, monkeypatch, path, status):
    monkeypatch.setattr(dashboard, "get_dashboard_data", lambda days=7: {"x": 1})
    got_status, pairs = _headers(port, path)
    assert got_status == status
    got = {}
    for k, v in pairs:
        got.setdefault(k.lower(), []).append(v)
    for name, value in EXPECTED_HEADERS.items():
        assert got.get(name.lower()) == [value], (path, name)
    assert not [k for k in got if k.startswith("access-control-")]
    assert "set-cookie" not in got


def test_security_headers_also_on_refused_requests(port):
    got_status, pairs = _headers(port, "/static/core.js", host="evil.example")
    assert got_status == 403
    names = {k.lower() for k, _ in pairs}
    assert {n.lower() for n in EXPECTED_HEADERS} <= names


JS_FORBIDDEN = [r"onclick", r"\.on[a-z]+\s*=(?!=)", r"""\son[a-z]+\s*=\s*\\?["']""", r'style="', r"setAttribute\(\s*['\"]style", r"\beval\(",
                r"new Function", r"javascript:"]


def _own_js() -> dict:
    return {rel: (UI / rel).read_text(encoding="utf-8")
            for rel, _ in dashboard._STATIC_FILES.values() if rel.endswith(".js") and not rel.startswith("vendor/")}


def test_index_html_has_no_inline_handler_style_or_script():
    html_text = (UI / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", html_text, re.I)
    assert not re.search(r"<[^>]*\sstyle\s*=", html_text, re.I)
    assert not re.search(r"<style\b", html_text, re.I)
    assert not re.search(r"<script\b(?![^>]*\ssrc=)", html_text, re.I)
    assert "javascript:" not in html_text


@pytest.mark.parametrize("pattern", JS_FORBIDDEN)
def test_own_js_has_no_csp_hostile_construct(pattern):
    for rel, text in _own_js().items():
        assert not re.search(pattern, text), (rel, pattern)


def _top_level_names(text: str) -> list:
    names = []
    for line in text.splitlines():
        m = re.match(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", line)
        if m:
            names.append(m.group(1))
            continue
        m = re.match(r"(?:let|const|var)\s+(.*)", line)
        if not m:
            continue
        depth, piece, pieces = 0, "", []
        for ch in m.group(1):
            depth += ch in "([{"
            depth -= ch in ")]}"
            if ch == "," and depth == 0:
                pieces.append(piece)
                piece = ""
            else:
                piece += ch
        pieces.append(piece)
        for piece in pieces:
            mm = re.match(r"\s*([A-Za-z_$][\w$]*)\s*(=|;|$)", piece)
            if mm:
                names.append(mm.group(1))
    return names


def test_no_top_level_name_is_declared_twice_across_own_js():
    seen: dict = {}
    for rel, text in _own_js().items():
        for name in _top_level_names(text):
            assert name not in seen, f"{name!r} declared in {seen[name]} and {rel}"
            seen[name] = rel
    assert {"ACTIONS", "registerActions", "escapeHtml", "showTab"} <= set(seen)


def test_queue_panels_sit_above_the_list_in_index_html():
    html = (pathlib.Path(dashboard.__file__).parent / "dashboard_ui" / "index.html").read_text(encoding="utf-8")
    pos = {k: html.index(f'id="{k}"') for k in ("q-form", "q-raw", "q-groups")}
    assert pos["q-form"] < pos["q-groups"] and pos["q-raw"] < pos["q-groups"]
