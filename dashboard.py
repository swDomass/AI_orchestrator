"""
AI Orchestrator — Web Dashboard

Lightweight HTTP server serving an analytics dashboard.
Uses only stdlib (http.server); the page (HTML/CSS/JS, Chart.js and its zoom
plugin vendored) is served from dashboard_ui/ — no CDN.

Autostart (2026-10-09): ``python orchestrator.py --watch`` starts this server
itself via ``start_autostart()`` — a daemon thread on 127.0.0.1, no browser
(``config.DASHBOARD_OPEN_BROWSER``, default False), switched off with
``DASHBOARD_AUTOSTART=false``. It never raises into the orchestrator and never
makes ``run_watch`` wait: the bind happens inside the thread, and every failure
(no port, import error, ``serve_forever`` dying later) is one warning line.

Standalone usage (unchanged — these still open the browser unless --no-open):
    python dashboard.py              # open browser on port 8211 (or free fallback)
    python dashboard.py --port 9000  # custom port
    python dashboard.py --no-open    # don't auto-open browser

Programmatic usage:
    from dashboard import start_server
    start_server()                   # blocking
    start_server(background=True)    # returns immediately
    start_autostart()                # --watch path: never raises, never waits

The server is multi-threaded (``socketserver.ThreadingMixIn``, daemon threads):
a slow request no longer blocks the others, but only one analytics computation
runs at a time (``_DATA_BUILD_LOCK``). /api/harness keeps its 3-s budget anyway,
because slow requests still tie up threads and browser connections.
"""

import argparse
import contextlib
import hashlib
import hmac
import importlib
import json
import logging
import re
import secrets
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, cast

import config
from analytics import get_dashboard_data
from config import DASHBOARD_PORT

logger = logging.getLogger(__name__)

# ── Provider → group / colour / label: the ONE place (P2, 2026-10-09) ─────────
# The page's JavaScript only looks names up in `provider_meta` (shipped with
# /api/data), it does not decide anything itself — JS inside the HTML string is
# not testable, this table is. Capacity-chart groups: "short" = 5-hour windows,
# "long" = 7-day windows, "other" = daily budget & everything else (opencode,
# Gemini, unknown names). A name nobody listed here still gets a group, a stable
# colour from _REST_PALETTE and a readable label instead of vanishing.
LIMIT_GROUPS = ("short", "long", "other")

# base provider → (label, colour, colour of the long window)
_PROVIDER_BASES: dict[str, tuple[str, str, str]] = {
    "claude": ("Claude", "#6c63ff", "#b0a8ff"),
    "codex": ("Codex", "#4caf50", "#81c784"),
    "opencode": ("opencode", "#29b6f6", "#81d4fa"),
    "gemini": ("Gemini", "#ffc107", "#ffda6a"),
    "openrouter": ("OpenRouter", "#ec407a", "#f48fb1"),
    "vibe": ("Vibe", "#ff7043", "#ffab91"),
}
# Window suffix of the capacity-log key → (group, window label). Derived from
# the key's window pattern, not per full key. Codex primary/secondary = 5 h /
# 7 days: measured in a Codex rollout (2026-10-08, rate_limits.primary
# window_minutes 300, secondary 10080) — the key itself does not carry it.
_WINDOW_SUFFIXES: dict[str, tuple[str, str]] = {
    "five_hour": ("short", "5 h"),
    "primary_window": ("short", "5 h"),
    "seven_day": ("long", "7 Tage"),
    "secondary_window": ("long", "7 Tage"),
}
# A bare key without window (e.g. "opencode") sits in "other"; these get a label
# that says what the number means.
_BARE_LABELS: dict[str, str] = {
    "opencode": "opencode (Tagesbudget)",
}
# Tile order of "Quoten jetzt"; anything not listed follows alphabetically.
_TILE_ORDER = (
    "claude_five_hour", "claude_seven_day",
    "codex_primary_window", "codex_secondary_window",
    "opencode", "gemini",
)
# Rest colours for names not in _PROVIDER_BASES — more than there are providers,
# none equal to a base colour; on overflow they repeat (never an error).
_REST_PALETTE = (
    "#ef5350", "#ab47bc", "#26a69a", "#d4e157", "#8d6e63", "#5c6bc0",
    "#ffa726", "#26c6da", "#9ccc65", "#7e57c2", "#bdbdbd", "#ff8a65",
)
_FALLBACK_GROUP = "other"


def _stable_index(name: str, size: int) -> int:
    """Index into a palette that is the same for a name in every process
    (``hash()`` is salted per process, sha256 is not)."""
    return int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:8], 16) % size


def _gemini_model_label(name: str) -> str:
    # gemini_gemini_2_5_flash_ → "Gemini 2.5 Flash" (former providerLabel in JS)
    parts = [p for p in name.removeprefix("gemini_gemini_").split("_") if p]
    ver = [p for p in parts if p.isdigit()]
    words = [p[:1].upper() + p[1:] for p in parts if not p.isdigit()]
    return " ".join(["Gemini", ".".join(ver), *words]).replace("  ", " ").strip()


def provider_meta(name: str) -> dict:
    """Group, colour, label and tile order for one provider/window key.

    Never raises and never returns None: an unknown name lands in the "other"
    group with a stable rest colour.
    """
    name = str(name)
    base, _, suffix = name.partition("_")
    order = _TILE_ORDER.index(name) if name in _TILE_ORDER else len(_TILE_ORDER)
    known = _PROVIDER_BASES.get(base)
    if known is not None:
        label, colour, long_colour = known
        if not suffix:
            return {"group": _FALLBACK_GROUP, "color": colour,
                    "label": _BARE_LABELS.get(name, label), "order": order, "known": True}
        window = _WINDOW_SUFFIXES.get(suffix)
        if window is not None:
            group, window_label = window
            return {"group": group, "color": colour if group == "short" else long_colour,
                    "label": f"{label} {window_label}", "order": order, "known": True}
        if base == "gemini":
            return {"group": _FALLBACK_GROUP, "color": long_colour if name.startswith("gemini_gemini_1") else colour,
                    "label": _gemini_model_label(name) if name.startswith("gemini_gemini_") else name.replace("_", " "),
                    "order": order, "known": True}
    return {
        "group": _FALLBACK_GROUP,
        "color": _REST_PALETTE[_stable_index(name, len(_REST_PALETTE))],
        "label": name.replace("_", " "),
        "order": order,
        "known": False,
    }


def provider_meta_map(names) -> dict[str, dict]:
    """``provider_meta`` for every name, with distinct rest colours.

    Unknown names keep their stable palette slot unless another name in the same
    set already uses that colour; then the next free slot is taken (sorted
    order, so the result is deterministic). Only when the palette is used up do
    colours repeat — the doughnut never runs out of colours.
    """
    result: dict[str, dict] = {}
    used: set[str] = set()
    unknown: list[str] = []
    for name in sorted({str(n) for n in names if n is not None}):
        meta = provider_meta(name)
        result[name] = meta
        if meta["known"]:
            used.add(meta["color"])
        else:
            unknown.append(name)
    for name in unknown:
        start = _stable_index(name, len(_REST_PALETTE))
        for step in range(len(_REST_PALETTE)):
            candidate = _REST_PALETTE[(start + step) % len(_REST_PALETTE)]
            if candidate not in used:
                result[name]["color"] = candidate
                break
        used.add(result[name]["color"])
    return result


def _provider_names_in(data: dict) -> set[str]:
    """Every provider/window name the page will draw from a /api/data payload."""
    names: set[str] = set()
    names.update((data.get("limits_timeline") or {}).keys())
    names.update((data.get("limits_now") or {}).keys())
    names.update((data.get("provider_distribution") or {}).get("labels") or [])
    return names


# ── Frontend files (2026-10-23) ───────────────────────────────────────────────
# The page used to be one Python string; it now lives in dashboard_ui/ and is
# served from this FIXED list. The request path is only ever compared against
# the keys (no unquote, no join), the file path always comes from the table, so
# there is no traversal to defend against. Nothing is read at import time and
# every request reads afresh (no cache: an edited file shows up on reload).
_UI_DIR = Path(__file__).resolve().parent / "dashboard_ui"
_JS_TYPE = "text/javascript; charset=utf-8"
_STATIC_FILES: dict[str, tuple[str, str]] = {
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/core.js": ("core.js", _JS_TYPE),
    "/static/overview.js": ("overview.js", _JS_TYPE),
    "/static/harness.js": ("harness.js", _JS_TYPE),
    "/static/app.js": ("app.js", _JS_TYPE),
    "/static/vendor/chart.js-4.6.1/chart.umd.min.js": (
        "vendor/chart.js-4.6.1/chart.umd.min.js",
        _JS_TYPE,
    ),
    "/static/vendor/chartjs-plugin-zoom-1.2.1/chartjs-plugin-zoom.min.js": (
        "vendor/chartjs-plugin-zoom-1.2.1/chartjs-plugin-zoom.min.js",
        _JS_TYPE,
    ),
}
_INDEX_PLACEHOLDER = b"__ORCH_TOKEN__"


def _render_index(raw: bytes, token: str = "") -> bytes:
    """index.html with the start key filled in ("" when the Host is not the bound loopback)."""
    return raw.replace(_INDEX_PLACEHOLDER, token.encode("ascii", "replace"), 1)


_CLIENT_DISCONNECT_ERRORS = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_bound_loopback(value: str | None, port: int) -> bool:
    """``Host`` value is a loopback name WITH the explicit bound port (POST check 1,
    key embedding). ``DASHBOARD_ALLOWED_HOSTS`` deliberately does not count here."""
    v = (value or "").strip().lower()
    if v.startswith("["):
        name, _, rest = v[1:].partition("]")
        port_s = rest[1:] if rest.startswith(":") else ""
        bracketed = True
    else:
        name, _, port_s = v.partition(":")
        bracketed = False
    return (
        name in _LOOPBACK_HOSTS and bracketed == (name == "::1")
        and port_s.isascii() and port_s.isdigit() and port_s == str(port)
    )


def _normalize_host(value: str) -> str:
    """``Host``-style value → bare lower-case name: port and IPv6 brackets off."""
    host = value.strip().lower()
    if host.startswith("["):          # [::1]:8211
        return host[1:host.find("]")] if "]" in host else host[1:]
    if host.count(":") == 1:          # name:port
        return host.rsplit(":", 1)[0]
    return host


def _allowed_hosts() -> frozenset[str]:
    extra = {_normalize_host(h) for h in getattr(config, "DASHBOARD_ALLOWED_HOSTS", ()) if str(h).strip()}
    return _LOOPBACK_HOSTS | extra


def _host_allowed(host_header: str | None) -> bool:
    """Anti DNS-rebinding: a browser always sends the name it resolved, so a page
    on evil.example that rebinds to 127.0.0.1 arrives with Host: evil.example.
    No Host header at all (a non-browser client) is let through. Configured extra
    names are normalized exactly like the header (port, brackets, case)."""
    if not host_header:
        return True
    return _normalize_host(host_header) in _allowed_hosts()


def _cross_site_refused(headers, path: str) -> bool:
    """Blind cross-site requests: any web page could fire ``no-cors`` fetches
    (with varying ``days``) and keep the log parser in the orchestrator process
    busy, without ever reading the answer. Refused: ``Sec-Fetch-Site:
    cross-site|same-site``, or an ``Origin`` that is not an allowed host. One
    exception, deliberately: a top-level NAVIGATION to the page itself (a link
    to the dashboard clicked on another site) only gets the static HTML. Without
    these headers (curl, old clients) nothing changes."""
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site in ("cross-site", "same-site"):
        mode = (headers.get("Sec-Fetch-Mode") or "").strip().lower()
        return not (mode == "navigate" and path in ("/", "/index.html"))
    origin = headers.get("Origin")
    if origin is None:
        return False
    hostname = urllib.parse.urlsplit(origin.strip()).hostname
    return hostname is None or hostname.lower() not in _allowed_hosts()


# Serialises get_dashboard_data(): its cache is not thread-safe (see _json_response).
_DATA_BUILD_LOCK = threading.Lock()


# Sent with EVERY response (also send_error pages). The page needs no inline
# script/style and no foreign origin, so the CSP can stay this strict; the
# frontend is written to fit it (data-action delegation, CSSOM styles).
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", _CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)


_MAX_POST_BODY = 262144
_POST_ROUTES: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
_MESSAGES = {
    "forbidden": "Anfrage abgelehnt.",
    "token": "Schlüssel fehlt oder ist ungültig.",
    "not_found": "Unbekannter Endpunkt.",
    "media_type": "Nur JSON (application/json, UTF-8) erlaubt.",
    "length_required": "Content-Length ist erforderlich.",
    "bad_request": "Ungültige Anfrage.",
    "too_large": "Anfrage zu groß.",
    "bad_json": "Ungültiger JSON-Inhalt.",
    "internal": "Interner Fehler.",
    "method_not_allowed": "Methode nicht erlaubt.",
}


class ApiError(Exception):
    """Raised by a POST route (or a gate check) to answer with a fixed status/code/text."""

    def __init__(self, status: int, code: str, message: str) -> None:
        # Checked here, at the source: a malformed ApiError raised in a route then becomes a
        # TypeError there (→ 500 "internal"), instead of breaking the answer after the fact.
        if (not isinstance(status, int) or isinstance(status, bool) or not 400 <= status <= 599
                or not isinstance(code, str) or not isinstance(message, str)):
            raise TypeError("ApiError needs an int 4xx/5xx status and str code/message")
        try:
            for text in (code, message):  # both end up in the JSON answer
                text.encode("utf-8")
        except UnicodeEncodeError:
            raise TypeError("ApiError code and message must be encodable as UTF-8") from None
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


class _ClientGoneError(Exception):
    """The client vanished while its POST body was read — not a handler fault."""


def _refuse(status: int, code: str) -> ApiError:
    return ApiError(status, code, _MESSAGES[code])


def post_route(path: str):
    """Register ``fn(body: dict) -> dict`` as POST ``path`` (must start with /api/, once)."""
    if not path.startswith("/api/"):
        raise ValueError(f"POST route must start with /api/: {path!r}")

    def register(fn: Callable[[dict[str, Any]], dict[str, Any]]):
        if path in _POST_ROUTES:
            raise ValueError(f"POST route registered twice: {path!r}")
        _POST_ROUTES[path] = fn
        return fn

    return register


@post_route("/api/ping")
def _api_ping(body: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


def _json_content_type(value: str) -> bool:
    media, *params = (part.strip() for part in value.split(";"))
    if media.lower() != "application/json":
        return False
    for param in params:
        key, _, val = param.partition("=")
        if key.strip().lower() != "charset" or val.strip().strip('"').lower() != "utf-8":
            return False
    return True


def _check_post_gate(headers, port: int, expected_token: str) -> None:
    """POST checks 1-4, each on its own; raises ApiError. No body byte is read."""
    hosts = headers.get_all("Host") or []
    if len(hosts) > 1:
        raise _refuse(400, "bad_request")
    if not hosts or not _is_bound_loopback(hosts[0], port):
        raise _refuse(403, "forbidden")
    origins = headers.get_all("Origin") or []
    allowed = {f"http://{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")}
    if len(origins) != 1 or origins[0].strip().lower() not in allowed:
        raise _refuse(403, "forbidden")
    # "Same origin" literally: scheme + host + port of THIS request (Host passed check 1).
    if origins[0].strip().lower() != "http://" + hosts[0].strip().lower():
        raise _refuse(403, "forbidden")
    if any(v.strip().lower() != "same-origin" for v in headers.get_all("Sec-Fetch-Site") or []):
        raise _refuse(403, "forbidden")
    tokens = headers.get_all("X-Orch-Token") or []
    given = tokens[0].encode("utf-8", "replace") if len(tokens) == 1 else b""
    if not expected_token or not hmac.compare_digest(given, expected_token.encode("utf-8")):
        raise _refuse(403, "token")


def _post_body_length(headers) -> int:
    """POST checks 6-8: Content-Type, framing, size cap; raises ApiError."""
    types = headers.get_all("Content-Type") or []
    if len(types) != 1 or not _json_content_type(types[0]):
        raise _refuse(415, "media_type")
    lengths = headers.get_all("Content-Length") or []
    if headers.get_all("Transfer-Encoding") or not lengths:
        raise _refuse(411, "length_required")
    if len(lengths) > 1 or not re.fullmatch("[0-9]{1,7}", lengths[0].strip()):
        raise _refuse(400, "bad_request")
    length = int(lengths[0])
    if length > _MAX_POST_BODY:
        raise _refuse(413, "too_large")
    return length


class _Handler(BaseHTTPRequestHandler):
    """Handles GET / (HTML), GET /api/data and GET /api/harness (JSON), POST /api/* (gated)."""

    def end_headers(self) -> None:
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    # A silent open connection must not hold its server thread forever.
    timeout = 10

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            if not _host_allowed(self.headers.get("Host")):
                self.send_error(403, "Host not allowed")
                return
            if _cross_site_refused(self.headers, parsed.path):
                self.send_error(403, "Cross-site request refused")
                return
            if parsed.path == "/api/data":
                self._json_response(parsed.query)
            elif parsed.path == "/api/harness":
                self._harness_response(parsed.query)
            elif parsed.path in ("/", "/index.html"):
                self._html_response()
            elif urllib.parse.urlsplit(self.path).path in _STATIC_FILES:
                rel, content_type = _STATIC_FILES[urllib.parse.urlsplit(self.path).path]
                self._file_response(rel, content_type)
            else:
                self.send_error(404)
        except _CLIENT_DISCONNECT_ERRORS as e:
            logger.debug("dashboard: client disconnected during %s (%s)", parsed.path, e)

    def _bound_port(self) -> int:
        return int(cast("tuple[str, int]", self.server.server_address)[1])

    def _html_response(self):
        hosts = self.headers.get_all("Host") or []
        ok = len(hosts) == 1 and _is_bound_loopback(hosts[0], self._bound_port())
        token = str(getattr(self.server, "write_token", "")) if ok else ""
        self._file_response("index.html", "text/html; charset=utf-8", lambda raw: _render_index(raw, token))

    def _reply(self, status: int, payload: dict[str, Any] | bytes, allow: str | None = None) -> None:
        """JSON answer of the write side (a dict, or bytes already encoded inside the
        error net); always ``Connection: close``."""
        body = payload if isinstance(payload, bytes) else _json_bytes(payload)
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if allow:
            self.send_header("Allow", allow)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _drain_unread_body(self) -> None:
        """After a refusal (answer already sent): swallow a small announced body, so closing
        the socket does not reset the connection and take the answer with it. One total
        deadline (~1 s) over all reads, at most 64 KiB; never raises."""
        try:
            left = int(self.headers.get("Content-Length") or 0)
            deadline = time.monotonic() + 1.0
            while 0 < left <= 65536 and (budget := deadline - time.monotonic()) > 0:
                self.connection.settimeout(budget)
                chunk = self.rfile.read1(min(left, 4096))
                if not chunk:
                    break
                left -= len(chunk)
        except (ValueError, OSError):
            pass

    def do_POST(self) -> None:
        self.close_connection = True
        self._unread = True
        try:
            try:
                payload = _json_bytes(self._run_post())  # serialised INSIDE the net
                status = 200
            except ApiError as e:
                logger.debug("dashboard: POST refused (%s)", e.code)
                status, payload = e.status, _json_bytes({"ok": False, "error": e.message, "code": e.code})
            except _ClientGoneError:
                raise  # not a handler fault: the outer net closes the connection silently
            except Exception:
                logger.exception("dashboard: POST handler failed")
                status = 500
                payload = _json_bytes({"ok": False, "error": _MESSAGES["internal"], "code": "internal"})
            self._reply(status, payload)
            if self._unread:
                self._drain_unread_body()
        except (_ClientGoneError, *_CLIENT_DISCONNECT_ERRORS, TimeoutError) as e:
            logger.debug("dashboard: client gone during POST (%s)", e.__cause__ or e)

    def _run_post(self) -> dict[str, Any]:
        _check_post_gate(self.headers, self._bound_port(), str(getattr(self.server, "write_token", "")))
        route = _POST_ROUTES.get(urllib.parse.urlsplit(self.path).path)
        if route is None:
            raise _refuse(404, "not_found")
        length = _post_body_length(self.headers)
        try:
            raw = self.rfile.read(length)
        except (*_CLIENT_DISCONNECT_ERRORS, TimeoutError) as e:
            raise _ClientGoneError from e  # only the body read; a route's own timeout stays a 500
        self._unread = False
        if len(raw) != length:
            raise _refuse(400, "bad_request")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
            raise _refuse(400, "bad_json") from None
        if not isinstance(body, dict):
            raise _refuse(400, "bad_json")
        result = route(body)
        if not isinstance(result, dict):
            raise TypeError("POST route returned a non-dict")
        return result

    def _method_not_allowed(self) -> None:
        try:
            self._reply(405, {"ok": False, "error": _MESSAGES["method_not_allowed"], "code": "method_not_allowed"},
                        allow="GET, POST")
        except (*_CLIENT_DISCONNECT_ERRORS, TimeoutError) as e:
            logger.debug("dashboard: client gone during 405 (%s)", e)

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def _file_response(self, rel: str, content_type: str, transform=None) -> None:
        """Serve one file of the fixed list; a missing file is a 500 with a fixed text."""
        try:
            body = (_UI_DIR / rel).read_bytes()
        except OSError:
            logger.exception("dashboard: file missing: %s", _UI_DIR / rel)
            self.send_error(500, "Dashboard-Dateien fehlen")
            return
        if transform is not None:
            body = transform(body)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_response(self, query_string: str = ""):
        try:
            params = urllib.parse.parse_qs(query_string)
            only = params.get("only", [""])[0]
            if only == "active_runs":
                # Lightweight poll endpoint — bypass the full dashboard cache
                # and only read the live registry. Used by the 30s active-runs
                # refresh tick to avoid latency from billing/replay aggregation.
                from analytics import _load_active_runs
                data = {"active_runs": _load_active_runs()}
            else:
                days = max(1, min(int(params.get("days", ["7"])[0]), 365))
                # Copy: get_dashboard_data() returns its 90-s cache object.
                # One computation at a time: analytics' cache is not thread-safe
                # and parallel misses would each pay the full (up to ~1 min) cost.
                with _DATA_BUILD_LOCK:
                    full: dict = dict(get_dashboard_data(days=days))
                full["provider_meta"] = provider_meta_map(_provider_names_in(full))
                data = full
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        except Exception as e:
            logger.exception("dashboard data error")
            body = json.dumps({"error": str(e)}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _harness_response(self, query_string: str = "") -> None:
        """GET /api/harness?days=…&window=… — reads ONLY the harness index.

        Deliberately not part of get_dashboard_data(): its log parsers are slow
        and slow requests tie up threads and browser connections even though
        the server is multi-threaded. harness_index.dashboard_payload() keeps
        its 3-s budget: it opens the SQLite read-only with a short busy timeout,
        and answers {"harness": {"available": false, ...}} instead of raising.
        """
        try:
            params = urllib.parse.parse_qs(query_string)
            try:
                days = int(params.get("days", ["30"])[0])
            except ValueError:
                days = 30
            try:
                window = int(params.get("window", ["7"])[0])
            except ValueError:
                window = 7
            harness_index = importlib.import_module("harness_index")
            data = harness_index.dashboard_payload(config.HARNESS_DB_FILE, days=days, window=window)
        except Exception as e:
            logger.debug("harness payload failed", exc_info=True)
            data = {"harness": {"available": False, "reason": f"{type(e).__name__}: {e}"}}
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except _CLIENT_DISCONNECT_ERRORS as e:
            logger.debug("dashboard: client disconnect (%s)", e)
            self.close_connection = True

    def log_message(self, format, *args):
        """Suppress default stderr logging; use logger instead."""
        logger.debug("dashboard: %s", format % args)


# Windows: SO_EXCLUSIVEADDRUSE (winsock2.h, value ~SO_REUSEADDR == -5). Older
# Python builds do not export the name, hence the literal fallback.
_SO_EXCLUSIVEADDRUSE = getattr(socket, "SO_EXCLUSIVEADDRUSE", -5)


def _is_windows() -> bool:
    """Seam for the socket-option test: it runs the Windows branch on Linux too."""
    return sys.platform == "win32"


class _ReuseServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threaded TCP server that never shares its port with a second server.

    One daemon thread per request (``daemon_threads``); ``block_on_close = False``
    so ``server_close()`` does not wait for a hung handler.

    POSIX: SO_REUSEADDR only lets a restarted server rebind past TIME_WAIT; a
    port another socket is LISTENING on still fails, so the second dashboard
    falls back as documented. Windows: SO_REUSEADDR lets a second socket bind a
    port that is in use (measured twice by the Auftraggeber: two dashboards on
    8211, no fallback, no warning) — so there it is never set and
    SO_EXCLUSIVEADDRUSE is set instead. Both options are chosen in
    ``server_bind`` at bind time rather than in the class attribute, so a test
    can run the Windows branch on any platform.
    """

    allow_reuse_address = False  # TCPServer must not add SO_REUSEADDR itself
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # The write key: per server start, never logged, only reachable via GET /.
        self.write_token = secrets.token_urlsafe(32)
        super().__init__(*args, **kwargs)

    def server_bind(self) -> None:
        if _is_windows():
            self.socket.setsockopt(socket.SOL_SOCKET, _SO_EXCLUSIVEADDRUSE, 1)
        else:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


def _bind_server(port: int) -> tuple["_ReuseServer", int]:
    """Bind the dashboard to 127.0.0.1, tolerating an unavailable port.

    Windows dynamically reserves port ranges for Hyper-V/WSL (bind then fails
    with WSAEACCES / WinError 10013), and the preferred port may simply be in
    use. Try the preferred port first, then a few spaced fallbacks, and as a
    last resort let the OS pick a free ephemeral port — so the dashboard always
    comes up. Returns the bound server and the actual port.
    """
    candidates = [port, port + 100, port + 211, port - 100, 0]
    last_err: OSError | None = None
    seen: set[int] = set()
    for candidate in candidates:
        if candidate < 0 or candidate in seen:
            continue
        seen.add(candidate)
        try:
            server = _ReuseServer(("127.0.0.1", candidate), _Handler)
        except OSError as e:
            last_err = e
            logger.warning("dashboard: port %d unavailable (%s)", candidate, e)
            continue
        actual = server.server_address[1]
        if actual != port:
            logger.warning(
                "dashboard: preferred port %d unavailable, bound to %d instead",
                port, actual,
            )
        return server, actual
    # port 0 above should always succeed; be explicit if it somehow didn't.
    raise last_err or OSError(f"dashboard: could not bind any port near {port}")


class AutostartHandle:
    """What ``start_autostart()`` set in motion.

    ``run_watch`` ignores it on purpose (it must neither wait for nor depend on
    the dashboard); tests and diagnostics use it. ``bound`` is set once the bind
    attempt inside the server thread has finished — successfully (``server``/
    ``url`` set) or not (``error`` set). ``stop`` ends the server and every
    scheduler thread hanging off this handle.
    """

    def __init__(self) -> None:
        self.bound = threading.Event()
        self.stop = threading.Event()
        self.server: _ReuseServer | None = None
        self.url: str | None = None
        self.error: str | None = None
        self.server_thread: threading.Thread | None = None
        self.index_thread: threading.Thread | None = None
        self.index_runs = 0

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the server thread (tests; the orchestrator lets the daemon die)."""
        self.stop.set()
        server = self.server
        if server is not None:
            with contextlib.suppress(Exception):  # best effort, never raises
                server.shutdown()
        for thread in (self.server_thread, self.index_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout)


def _safe_report(report, message: str) -> None:
    """Call a reporting callback; a throwing reporter must not take the caller down."""
    with contextlib.suppress(Exception):  # the reporter is the last line
        report(message)


def _report_startup_warnings(warn) -> None:
    """``config.STARTUP_WARNINGS`` (collected at import, before logging was set
    up), each exactly once per process: a reported entry leaves the list."""
    pending = config.STARTUP_WARNINGS
    while pending:
        _safe_report(warn, pending.pop(0))


def _autostart_serve(
    handle: AutostartHandle,
    port: int,
    open_browser: bool,
    warn,
    info,
) -> None:
    """Server-thread body: bind, announce, serve. Never raises.

    The bind happens HERE and not in ``start_autostart()``: the caller is
    ``run_watch``, which must not wait for a bind result. Every failure ends in
    exactly one ``warn`` line; a server that bound and then died is closed in
    ``finally`` so no half-open socket stays behind.
    """
    server: _ReuseServer | None = None
    try:
        try:
            server, actual = _bind_server(port)
        except Exception as e:  # OSError and anything else alike
            handle.error = f"{type(e).__name__}: {e}"
            _safe_report(
                warn,
                f"Dashboard-Autostart: kein Port frei ({handle.error}) — "
                "Orchestrator läuft ohne Dashboard weiter",
            )
            return
        url = f"http://127.0.0.1:{actual}"
        handle.server = server
        handle.url = url
        handle.bound.set()
        if actual != port:
            _safe_report(warn, f"Dashboard-Autostart: Port {port} belegt, Dashboard läuft unter {url}")
        else:
            _safe_report(info, f"Dashboard läuft unter {url}")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception as e:  # a missing browser is not a dashboard failure
                logger.debug("dashboard autostart: webbrowser.open failed: %s", e)
        # Always reached after a successful bind: AutostartHandle.shutdown() relies
        # on serve_forever() running (socketserver.shutdown waits for it).
        server.serve_forever()
    except Exception as e:  # a dying server thread must log, not vanish
        handle.error = f"{type(e).__name__}: {e}"
        _safe_report(
            warn,
            f"Dashboard-Autostart: Server beendet ({handle.error}) — Orchestrator läuft weiter",
        )
    finally:
        if server is not None:
            with contextlib.suppress(Exception):
                server.server_close()
        handle.bound.set()


# First harness-index run this long after the autostart (the orchestrator's own
# startup goes first); then every config.HARNESS_UPDATE_INTERVAL_SEC.
HARNESS_FIRST_RUN_DELAY_SEC = 60.0


def _harness_index_loop(handle: AutostartHandle, interval: float, warn, info) -> None:
    """Scheduler-thread body: launch the index as its OWN child process. Never raises.

    The child (``harness_index.run_update_subprocess``) runs at low priority
    and is killed by handle after ``HARNESS_LOCK_STALE_SEC``; this thread only
    waits for it. A failure is reported once per distinct message (no warning
    every 30 minutes for the same broken state), a recovery once.
    """
    delay = min(HARNESS_FIRST_RUN_DELAY_SEC, interval)
    last_error: str | None = None
    while not handle.stop.wait(delay):
        delay = interval
        error: str | None
        try:
            harness_index = importlib.import_module("harness_index")
            result = harness_index.run_update_subprocess(timeout=float(config.HARNESS_LOCK_STALE_SEC))
            error = result.get("error")
            handle.index_runs += 1
            if not error:
                lines = (result.get("stdout") or "").splitlines()
                logger.info("harness index: %s", lines[-1] if lines else "ok")
            if result.get("stderr"):
                # Full error texts of the child: the index stores type names only,
                # the details land here, in the local orchestrator log.
                logger.info("harness index stderr: %s", result["stderr"])
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        if error and error != last_error:
            _safe_report(warn, f"Harness-Index: {error} — Orchestrator läuft weiter")
        elif not error and last_error:
            _safe_report(info, "Harness-Index läuft wieder")
        last_error = error


def _start_harness_index_thread(handle: AutostartHandle, warn, info) -> str | None:
    """Start the index scheduler (independent of the dashboard's bind result).

    Returns the error text if the thread could not be started — the caller
    reports it, together with a server failure in ONE line."""
    interval = float(config.HARNESS_UPDATE_INTERVAL_SEC)
    if interval <= 0:
        return None
    try:
        thread = threading.Thread(
            target=_harness_index_loop, args=(handle, interval, warn, info),
            name="harness-index-scheduler", daemon=True,
        )
        handle.index_thread = thread
        thread.start()
    except Exception as e:  # never raises, by contract
        handle.index_thread = None
        return f"{type(e).__name__}: {e}"
    return None


def start_autostart(
    *,
    open_browser: bool | None = None,
    port: int | None = None,
    warn=None,
    info=None,
) -> AutostartHandle:
    """Start the dashboard for ``--watch`` as a daemon thread. NEVER raises.

    Returns at once — no join, no waiting for the bind (that happens in the
    thread). Any failure (thread start, bind, ``serve_forever`` dying later) is
    reported through ``warn`` exactly once and recorded in ``handle.error``; the
    orchestrator keeps running either way. Also starts the harness-index
    scheduler thread (``HARNESS_UPDATE_INTERVAL_SEC``, 0 = off), whose work runs
    in a separate low-priority process. ``open_browser`` defaults to
    ``config.DASHBOARD_OPEN_BROWSER`` (False) — this is the unattended path; the
    manual paths (``python dashboard.py``, ``--dashboard``) keep their own default.
    Bound to 127.0.0.1 only (``_bind_server``).
    """
    handle = AutostartHandle()
    warn = warn or logger.warning
    info = info or logger.info
    with contextlib.suppress(Exception):  # never raises, by contract
        _report_startup_warnings(warn)
    # Local, not handle.error: the server thread sets handle.error itself on a
    # bind failure — concurrently — and reports that one on its own.
    start_error: str | None = None
    try:
        if open_browser is None:
            open_browser = bool(config.DASHBOARD_OPEN_BROWSER)
        port = port or config.DASHBOARD_PORT
        thread = threading.Thread(
            target=_autostart_serve,
            args=(handle, port, open_browser, warn, info),
            name="dashboard-autostart",
            daemon=True,
        )
        handle.server_thread = thread
        thread.start()
    except Exception as e:  # never raises, by contract
        start_error = handle.error = f"{type(e).__name__}: {e}"
        handle.server_thread = None
        handle.bound.set()
    # The index is useful even when this dashboard did not come up (a manual
    # `python dashboard.py` reads the same SQLite), so it is tried regardless —
    # and a failure of both is ONE warning line, not two.
    index_error = _start_harness_index_thread(handle, warn, info)
    if start_error and index_error:
        _safe_report(warn, f"Dashboard-Autostart fehlgeschlagen ({start_error}); Harness-Index-Thread "
                           f"ebenfalls nicht gestartet ({index_error}) — Orchestrator läuft ohne beide weiter")
    elif start_error:
        _safe_report(warn, f"Dashboard-Autostart fehlgeschlagen ({start_error}) — "
                           "Orchestrator läuft ohne Dashboard weiter")
    elif index_error:
        _safe_report(warn, f"Harness-Index-Thread nicht gestartet ({index_error}) — Orchestrator läuft weiter")
    return handle


def start_server(
    port: int | None = None,
    open_browser: bool = True,
    background: bool = False,
) -> None:
    """Start the dashboard HTTP server.

    Args:
        port: TCP port (default from config.DASHBOARD_PORT).
        open_browser: auto-open in default browser.
        background: if True, run in a daemon thread and return immediately.
    """
    port = port or DASHBOARD_PORT
    server, port = _bind_server(port)
    url = f"http://127.0.0.1:{port}"

    if background:
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info("Dashboard running at %s (background)", url)
        if open_browser:
            webbrowser.open(url)
        return

    print(f"Dashboard: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard gestoppt.")
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="AI Orchestrator Dashboard")
    parser.add_argument("--port", type=int, default=None,
                        help=f"HTTP port (default: {DASHBOARD_PORT})")
    parser.add_argument("--no-open", action="store_true",
                        help="Don't auto-open browser")
    args = parser.parse_args()
    with contextlib.suppress(Exception):
        _report_startup_warnings(lambda message: print(message, file=sys.stderr))
    start_server(port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
