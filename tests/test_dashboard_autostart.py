"""P1 — dashboard autostart for ``--watch`` (2026-10-09).

The contract under test: ``dashboard.start_autostart()`` and the call site in
``orchestrator.run_watch`` NEVER raise and never wait for the bind; every failure
is exactly one warning line; the server binds 127.0.0.1 only; the autostart does
not open a browser by default while the manual paths still do.

Real sockets on 127.0.0.1 are used (bind, HTTP GET); only the outermost edges are
faked: ``webbrowser.open``, the bind function where a failure is simulated, and
the main loop's ``time.sleep`` to end ``run_watch`` after one round.
"""

import http.client
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import config
import dashboard
import doctor
import heartbeat
import orchestrator


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
def no_browser(monkeypatch):
    opener = Mock()
    monkeypatch.setattr(dashboard.webbrowser, "open", opener)
    return opener


@pytest.fixture
def no_harness_thread(monkeypatch):
    """The autostart also schedules the harness index; keep these P1 tests on the server."""
    monkeypatch.setattr(config, "HARNESS_UPDATE_INTERVAL_SEC", 0, raising=False)


class _Reports:
    def __init__(self):
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warn(self, msg):
        self.warnings.append(msg)

    def info(self, msg):
        self.infos.append(msg)


def _wait_bound(handle, timeout=5.0):
    assert handle.bound.wait(timeout), "bind attempt never finished"


# ── dashboard.start_autostart ───────────────────────────────────────────────


def test_autostart_binds_loopback_serves_and_opens_no_browser(no_browser, no_harness_thread):
    port = _free_port()
    rep = _Reports()
    handle = dashboard.start_autostart(port=port, warn=rep.warn, info=rep.info)
    try:
        _wait_bound(handle)
        assert handle.error is None
        assert handle.server is not None
        host, bound_port = handle.server.server_address[:2]
        assert host == "127.0.0.1"
        assert bound_port == port
        assert handle.url == f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(handle.url + "/", timeout=5) as resp:
            assert resp.status == 200
            assert b"AI Orchestrator" in resp.read()
        assert rep.warnings == []
        assert rep.infos == [f"Dashboard läuft unter {handle.url}"]
        # Default config: DASHBOARD_OPEN_BROWSER is False for the autostart.
        assert config.DASHBOARD_OPEN_BROWSER is False
        no_browser.assert_not_called()
    finally:
        handle.shutdown()
    assert not handle.server_thread.is_alive()


def test_autostart_opens_browser_only_when_configured(monkeypatch, no_browser, no_harness_thread):
    monkeypatch.setattr(config, "DASHBOARD_OPEN_BROWSER", True)
    handle = dashboard.start_autostart(port=_free_port(), warn=Mock(), info=Mock())
    try:
        _wait_bound(handle)
        no_browser.assert_called_once_with(handle.url)
    finally:
        handle.shutdown()


@pytest.mark.parametrize(
    "exc",
    [OSError("no port at all"), ImportError("analytics broken"), RuntimeError("weird")],
    ids=["OSError", "ImportError", "RuntimeError"],
)
def test_autostart_never_raises_when_bind_fails(monkeypatch, no_browser, no_harness_thread, exc):
    def boom(_port):
        raise exc

    monkeypatch.setattr(dashboard, "_bind_server", boom)
    rep = _Reports()
    handle = dashboard.start_autostart(port=_free_port(), warn=rep.warn, info=rep.info)
    _wait_bound(handle)
    handle.server_thread.join(5)
    assert not handle.server_thread.is_alive()
    assert handle.server is None
    assert handle.error == f"{type(exc).__name__}: {exc}"
    assert len(rep.warnings) == 1, rep.warnings
    assert "kein Port frei" in rep.warnings[0]
    assert rep.infos == []
    no_browser.assert_not_called()


def test_autostart_never_raises_when_thread_start_fails(monkeypatch, no_harness_thread):
    class _NoThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(dashboard.threading, "Thread", _NoThread)
    rep = _Reports()
    handle = dashboard.start_autostart(port=_free_port(), warn=rep.warn, info=rep.info)
    assert handle.bound.is_set()
    assert handle.server_thread is None
    assert handle.error == "RuntimeError: can't start new thread"
    assert len(rep.warnings) == 1
    assert "Dashboard-Autostart fehlgeschlagen" in rep.warnings[0]


def test_autostart_logs_and_closes_socket_when_serve_forever_dies(monkeypatch, no_browser, no_harness_thread):
    def dies(self, *a, **kw):
        raise RuntimeError("select() exploded")

    monkeypatch.setattr(dashboard._ReuseServer, "serve_forever", dies)
    rep = _Reports()
    handle = dashboard.start_autostart(port=_free_port(), warn=rep.warn, info=rep.info)
    handle.server_thread.join(5)
    assert not handle.server_thread.is_alive()
    assert handle.error == "RuntimeError: select() exploded"
    assert len(rep.warnings) == 1
    assert "Server beendet" in rep.warnings[0]
    # No half-open socket stays behind.
    assert handle.server.socket.fileno() == -1


def test_autostart_falls_back_when_port_taken_and_names_the_url(no_browser, no_harness_thread):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    taken = blocker.getsockname()[1]
    rep = _Reports()
    try:
        handle = dashboard.start_autostart(port=taken, warn=rep.warn, info=rep.info)
        try:
            _wait_bound(handle)
            assert handle.error is None
            actual = handle.server.server_address[1]
            assert actual != taken
            assert handle.server.server_address[0] == "127.0.0.1"
            assert len(rep.warnings) == 1
            assert f"http://127.0.0.1:{actual}" in rep.warnings[0]
            assert f"Port {taken} belegt" in rep.warnings[0]
        finally:
            handle.shutdown()
    finally:
        blocker.close()


def test_autostart_does_not_wait_for_the_bind(monkeypatch, no_browser, no_harness_thread):
    """A slow bind must not delay the caller (run_watch): no join, no wait."""
    release = threading.Event()
    real_bind = dashboard._bind_server

    def slow_bind(port):
        release.wait(10)
        return real_bind(port)

    monkeypatch.setattr(dashboard, "_bind_server", slow_bind)
    started = time.monotonic()
    handle = dashboard.start_autostart(port=_free_port(), warn=Mock(), info=Mock())
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 1.0, elapsed
        assert not handle.bound.is_set()
    finally:
        release.set()
        _wait_bound(handle)
        handle.shutdown()


def test_bind_server_binds_loopback_only():
    server, port = dashboard._bind_server(_free_port())
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_address[1] == port
    finally:
        server.server_close()


# ── manual paths stay as they were ──────────────────────────────────────────


def _run_dashboard_main(monkeypatch, argv):
    def stop_immediately(self, *a, **kw):
        raise KeyboardInterrupt  # how a user ends `python dashboard.py`

    monkeypatch.setattr(dashboard._ReuseServer, "serve_forever", stop_immediately)
    monkeypatch.setattr(sys, "argv", argv)
    dashboard.main()


def test_manual_dashboard_still_opens_browser(monkeypatch, no_browser):
    port = _free_port()
    _run_dashboard_main(monkeypatch, ["dashboard.py", "--port", str(port)])
    no_browser.assert_called_once_with(f"http://127.0.0.1:{port}")


def test_manual_dashboard_no_open_flag(monkeypatch, no_browser):
    _run_dashboard_main(monkeypatch, ["dashboard.py", "--port", str(_free_port()), "--no-open"])
    no_browser.assert_not_called()


def test_orchestrator_dashboard_flag_keeps_blocking_browser_default(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "start_server", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "--dashboard"])
    orchestrator._main()
    # Unchanged call shape: start_server() with its own defaults (open_browser=True).
    assert calls == [((), {})]


# ── call site: orchestrator._start_dashboard_autostart / run_watch ─────────


@pytest.fixture
def captured_log(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(orchestrator, "append_log", lines.append)
    return lines


def test_call_site_swallows_a_raising_start_autostart(monkeypatch, captured_log, caplog):
    def boom(**kw):
        raise RuntimeError("dashboard exploded")

    monkeypatch.setattr(config, "DASHBOARD_AUTOSTART", True)
    monkeypatch.setattr(dashboard, "start_autostart", boom)
    with caplog.at_level("WARNING", logger="orchestrator"):
        orchestrator._start_dashboard_autostart()  # must not raise
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and r.name == "orchestrator"]
    assert len(warnings) == 1
    assert "dashboard exploded" in warnings[0].getMessage()
    assert len(captured_log) == 1
    assert "Dashboard-Autostart fehlgeschlagen" in captured_log[0]


def test_call_site_swallows_an_import_error(monkeypatch, captured_log):
    monkeypatch.setattr(config, "DASHBOARD_AUTOSTART", True)
    monkeypatch.setitem(sys.modules, "dashboard", None)  # import → ImportError
    orchestrator._start_dashboard_autostart()
    assert len(captured_log) == 1
    assert "ModuleNotFoundError" in captured_log[0]  # an ImportError subclass


def test_call_site_does_nothing_when_disabled(monkeypatch, captured_log):
    started = Mock()
    monkeypatch.setattr(config, "DASHBOARD_AUTOSTART", False)
    monkeypatch.setattr(dashboard, "start_autostart", started)
    orchestrator._start_dashboard_autostart()
    started.assert_not_called()
    assert captured_log == []


def test_call_site_passes_the_autostart_browser_flag(monkeypatch, captured_log):
    started = Mock()
    monkeypatch.setattr(config, "DASHBOARD_AUTOSTART", True)
    monkeypatch.setattr(config, "DASHBOARD_OPEN_BROWSER", False)
    monkeypatch.setattr(dashboard, "start_autostart", started)
    orchestrator._start_dashboard_autostart()
    started.assert_called_once()
    assert started.call_args.kwargs["open_browser"] is False


class _StopLoopError(Exception):
    """Ends run_watch after its first main-loop round."""


class _FakeTime:
    """orchestrator.time with a sleep that ends the main loop; everything else real."""

    def __init__(self):
        self.sleeps: list[float] = []

    def sleep(self, sec):
        self.sleeps.append(sec)
        raise _StopLoopError

    def __getattr__(self, name):
        return getattr(time, name)


def _patch_run_watch_startup(monkeypatch):
    monkeypatch.setattr(doctor, "run_startup_checks", lambda: True)
    monkeypatch.setattr(heartbeat, "HeartbeatRunner", lambda: SimpleNamespace(run_due=lambda *_a: None))
    monkeypatch.setattr(heartbeat, "_log_capacity", lambda: None)
    monkeypatch.setattr(heartbeat, "start_heartbeat_thread", lambda *a, **kw: None)

    class _Listener:
        def __init__(self, *_a):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(orchestrator, "TelegramListener", _Listener)
    monkeypatch.setattr(orchestrator, "STARTUP_DELAY_SEC", 0)
    monkeypatch.setattr(orchestrator, "start_session", lambda: None)
    monkeypatch.setattr(orchestrator, "set_queue_idle", lambda *_a: None)
    reads = []

    def read_queue():
        reads.append(1)
        return []

    monkeypatch.setattr(orchestrator, "read_queue", read_queue)
    fake_time = _FakeTime()
    monkeypatch.setattr(orchestrator, "time", fake_time)
    return reads, fake_time


@pytest.mark.parametrize("failure", ["start_autostart_raises", "bind_fails_in_thread", "import_fails"])
def test_run_watch_keeps_running_when_dashboard_fails(
    monkeypatch, captured_log, no_browser, no_harness_thread, failure,
):
    """Dashboard failure at the call site → run_watch still reaches its main loop."""
    reads, fake_time = _patch_run_watch_startup(monkeypatch)
    monkeypatch.setattr(config, "DASHBOARD_AUTOSTART", True)
    bind_done = threading.Event()
    if failure == "start_autostart_raises":
        def boom(**kw):
            raise RuntimeError("dashboard exploded")
        monkeypatch.setattr(dashboard, "start_autostart", boom)
    elif failure == "bind_fails_in_thread":
        def no_port(_port):
            try:
                raise OSError("every candidate refused")
            finally:
                bind_done.set()
        monkeypatch.setattr(dashboard, "_bind_server", no_port)
    else:
        monkeypatch.setitem(sys.modules, "dashboard", None)

    with pytest.raises(_StopLoopError):
        orchestrator.run_watch()

    # The main loop ran one round: read the queue, went to sleep.
    assert reads, "run_watch never reached its main loop"
    assert fake_time.sleeps == [orchestrator.SLEEP_POLL_INTERVAL]
    if failure == "bind_fails_in_thread":
        assert bind_done.wait(5)
        deadline = time.monotonic() + 5
        while not any("kein Port frei" in m for m in captured_log) and time.monotonic() < deadline:
            time.sleep(0.01)
    dash_lines = [m for m in captured_log if "Dashboard" in m]
    assert len(dash_lines) == 1, captured_log


# ── anti DNS-rebinding: only loopback Host headers ──────────────────────────


def _request_with_host(port: int, path: str, host: str | None) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True)
        if host is not None:
            conn.putheader("Host", host)
        conn.endheaders()
        return conn.getresponse().status
    finally:
        conn.close()


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setattr(dashboard, "get_dashboard_data", lambda days=7: {})
    server, port = dashboard._bind_server(_free_port())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield port
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("path", ["/", "/api/data", "/api/harness"])
@pytest.mark.parametrize("host", ["evil.example", "evil.example:8211", "127.0.0.1.evil.example"])
def test_foreign_host_header_is_refused(served, path, host):
    assert _request_with_host(served, path, host) == 403


@pytest.mark.parametrize("host", [None, "127.0.0.1", "localhost:8211", "[::1]:8211"])
def test_loopback_host_header_is_served(served, host):
    assert _request_with_host(served, "/", host) == 200


def test_extra_allowed_host_from_config(served, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_ALLOWED_HOSTS", ("pc.tailnet.example",))
    assert _request_with_host(served, "/", "pc.tailnet.example:443") == 200
    assert _request_with_host(served, "/", "other.example") == 403


# ── K6: a second dashboard never shares the port ────────────────────────────


def test_second_dashboard_on_the_same_port_falls_back_instead_of_sharing():
    """Two real _ReuseServer instances (both with the server's own socket options),
    not a plain blocker socket: on Windows SO_REUSEADDR let the second one bind
    the busy port. The fallback warning comes from _bind_server unchanged."""
    port = _free_port()
    first, first_port = dashboard._bind_server(port)
    try:
        second, second_port = dashboard._bind_server(port)
        try:
            assert first_port == port
            assert second_port != port
            assert second.server_address[0] == "127.0.0.1"
        finally:
            second.server_close()
    finally:
        first.server_close()


def test_reuse_server_socket_options_per_platform():
    """The options the REAL socket ends up with, on the platform the suite runs on."""
    server = dashboard._ReuseServer(("127.0.0.1", 0), dashboard._Handler)
    try:
        reuse = server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
        if sys.platform == "win32":
            assert server.socket.getsockopt(socket.SOL_SOCKET, dashboard._SO_EXCLUSIVEADDRUSE) != 0
            assert reuse == 0
        else:
            assert reuse != 0
    finally:
        server.server_close()


class _RecordingSocket:
    """Stands in for the server socket: records setsockopt, binds for real."""

    def __init__(self, real: socket.socket) -> None:
        self.real = real
        self.options: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, name: int, value: int) -> None:
        self.options.append((level, name, value))

    def __getattr__(self, name: str):
        return getattr(self.real, name)


@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
def test_bind_sets_exclusive_use_on_windows_and_reuse_elsewhere(monkeypatch, windows):
    """Runs the Windows branch on every platform. Without it, the K6 regression
    (SO_REUSEADDR on Windows) stays green everywhere but on Windows — measured:
    that mutation passed all of this file on Linux."""
    monkeypatch.setattr(dashboard, "_is_windows", lambda: windows)
    server = dashboard._ReuseServer(("127.0.0.1", 0), dashboard._Handler, bind_and_activate=False)
    recorder = _RecordingSocket(server.socket)
    server.socket = recorder
    try:
        server.server_bind()
        expected = dashboard._SO_EXCLUSIVEADDRUSE if windows else socket.SO_REUSEADDR
        assert recorder.options == [(socket.SOL_SOCKET, expected, 1)]
        assert server.server_address[0] == "127.0.0.1"
    finally:
        recorder.real.close()


# ── K8: the dashboard starts with the orchestrator, not after the delay ─────


def test_autostart_runs_before_the_first_sleep_of_the_startup_delay(monkeypatch, captured_log):
    _patch_run_watch_startup(monkeypatch)
    monkeypatch.setattr(orchestrator, "STARTUP_DELAY_SEC", 300)
    events: list[str] = []
    monkeypatch.setattr(orchestrator, "_start_dashboard_autostart", lambda: events.append("autostart"))

    class _DelayTime(_FakeTime):
        def sleep(self, sec):
            events.append(f"sleep {sec}")
            raise _StopLoopError

    monkeypatch.setattr(orchestrator, "time", _DelayTime())
    with pytest.raises(_StopLoopError):
        orchestrator.run_watch()
    assert events == ["autostart", "sleep 10"]  # the first sleep IS the startup delay's


def test_no_autostart_when_the_startup_checks_end_the_process(monkeypatch, captured_log):
    _patch_run_watch_startup(monkeypatch)
    monkeypatch.setattr(doctor, "run_startup_checks", lambda: False)
    started = Mock()
    monkeypatch.setattr(orchestrator, "_start_dashboard_autostart", started)
    with pytest.raises(SystemExit):
        orchestrator.run_watch()
    started.assert_not_called()


# ── K10: one warning line, bounds, host list, cross-site, handler timeout ──


def test_server_and_index_thread_failing_together_is_one_warning(monkeypatch):
    class _NoThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(dashboard.threading, "Thread", _NoThread)
    monkeypatch.setattr(config, "HARNESS_UPDATE_INTERVAL_SEC", 1800)
    rep = _Reports()
    handle = dashboard.start_autostart(port=_free_port(), warn=rep.warn, info=rep.info)
    assert len(rep.warnings) == 1, rep.warnings
    assert "Dashboard-Autostart fehlgeschlagen" in rep.warnings[0]
    assert "Harness-Index-Thread ebenfalls nicht gestartet" in rep.warnings[0]
    assert handle.index_thread is None


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("HARNESS_LOCK_STALE_SEC", "0", 7200), ("HARNESS_LOCK_STALE_SEC", "599", 7200),
        ("HARNESS_LOCK_STALE_SEC", "600", 600),
        ("HARNESS_UPDATE_INTERVAL_SEC", "0", 0), ("HARNESS_UPDATE_INTERVAL_SEC", "-1", 1800),
        ("HARNESS_UPDATE_INTERVAL_SEC", "299", 1800), ("HARNESS_UPDATE_INTERVAL_SEC", "300", 300),
    ],
)
def test_harness_interval_bounds(monkeypatch, key, raw, expected):
    """The REAL rules: config's own reader functions with the numbers spelled out
    here (K17 — the test used to carry a copy of the rules, and stayed green when
    config's bound was changed)."""
    monkeypatch.setenv(key, raw)
    monkeypatch.setattr(config, "STARTUP_WARNINGS", [])
    reader = {"HARNESS_LOCK_STALE_SEC": config._harness_lock_stale_sec,
              "HARNESS_UPDATE_INTERVAL_SEC": config._harness_update_interval_sec}[key]
    assert reader() == expected
    warned = [w for w in config.STARTUP_WARNINGS if key in w]
    assert len(warned) == (0 if str(expected) == raw else 1)


@pytest.mark.parametrize(
    ("interval", "stale", "expected"),
    [("299", "599", "1800 7200"), ("300", "600", "300 600")],
)
def test_the_module_values_follow_the_bounds(interval, stale, expected):
    """End values of a real import (child process) — the wiring, not only the rule."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env.update({"HARNESS_UPDATE_INTERVAL_SEC": interval, "HARNESS_LOCK_STALE_SEC": stale})
    proc = subprocess.run(
        [sys.executable, "-c",
         "import config; print(config.HARNESS_UPDATE_INTERVAL_SEC, config.HARNESS_LOCK_STALE_SEC)"],
        cwd=str(Path(config.__file__).parent), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


def _request(port: int, path: str, headers: dict[str, str]) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host="Host" in headers)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        return conn.getresponse().status
    finally:
        conn.close()


def test_allowed_hosts_are_normalized_like_the_header(served, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_ALLOWED_HOSTS", ("PC.Tailnet.Example:443", "[fe80::1]"))
    assert _request(served, "/", {"Host": "pc.tailnet.example"}) == 200
    assert _request(served, "/", {"Host": "[FE80::1]:8211"}) == 200
    assert _request(served, "/", {"Host": "other.example"}) == 403


@pytest.mark.parametrize(
    ("path", "headers", "status"),
    [
        ("/api/data", {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}, 403),
        ("/api/harness?days=90", {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors"}, 403),
        ("/api/data", {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}, 403),
        ("/", {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}, 403),
        ("/", {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}, 200),
        ("/api/data", {"Origin": "https://evil.example"}, 403),
        ("/api/data", {"Origin": "null"}, 403),
        ("/api/data", {"Origin": "http://127.0.0.1:8211", "Sec-Fetch-Site": "same-origin"}, 200),
        ("/api/data", {"Sec-Fetch-Site": "none"}, 200),
        ("/api/data", {}, 200),
    ],
    ids=["xsite-nocors", "samesite-cors", "xsite-navigate-api", "xsite-nocors-page", "xsite-navigate-page",
         "foreign-origin", "null-origin", "same-origin", "typed-url", "curl"],
)
def test_blind_cross_site_requests_are_refused(served, path, headers, status):
    assert _request(served, path, headers) == status


def test_a_silent_connection_does_not_hold_the_server(monkeypatch, served):
    monkeypatch.setattr(dashboard._Handler, "timeout", 0.3)
    assert dashboard._Handler.timeout == 0.3
    idle = socket.create_connection(("127.0.0.1", served))  # connects, never sends a request
    try:
        started = time.monotonic()
        assert _request(served, "/", {}) == 200
        assert time.monotonic() - started < 3.0
    finally:
        idle.close()


def test_handler_timeout_default():
    assert dashboard._Handler.timeout == 10


# ── Korrekturrunde 2: K16 warnings from the config import ──────────────────


def test_an_invalid_bound_is_collected_not_logged_at_import(monkeypatch, caplog):
    """Logged at import it went to the last-resort handler — lost in the hidden
    window of the Scheduled Task."""
    monkeypatch.setattr(config, "STARTUP_WARNINGS", [])
    monkeypatch.setenv("HARNESS_LOCK_STALE_SEC", "5")
    with caplog.at_level("DEBUG"):
        value = config._bounded_int_env("HARNESS_LOCK_STALE_SEC", 7200, valid=lambda v: v >= 600, rule=">= 600")
    assert value == 7200
    assert config.STARTUP_WARNINGS == ["config: HARNESS_LOCK_STALE_SEC=5 ungültig (>= 600) — Standardwert 7200"]
    assert caplog.records == []


def test_the_real_import_collects_the_warning_and_prints_nothing():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env["HARNESS_UPDATE_INTERVAL_SEC"] = "5"
    proc = subprocess.run(
        [sys.executable, "-c", "import config; print(repr(config.STARTUP_WARNINGS))"],
        cwd=str(Path(config.__file__).parent), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "HARNESS_UPDATE_INTERVAL_SEC=5 ungültig" in proc.stdout
    assert "ungültig" not in proc.stderr


def test_autostart_reports_each_startup_warning_exactly_once(monkeypatch, no_browser, no_harness_thread):
    monkeypatch.setattr(config, "STARTUP_WARNINGS", ["config: HARNESS_LOCK_STALE_SEC=5 ungültig (>= 600) — x"])
    rep = _Reports()
    first = dashboard.start_autostart(port=_free_port(), warn=rep.warn, info=rep.info)
    second = dashboard.start_autostart(port=_free_port(), warn=rep.warn, info=rep.info)
    try:
        _wait_bound(first)
        _wait_bound(second)
        assert rep.warnings == ["config: HARNESS_LOCK_STALE_SEC=5 ungültig (>= 600) — x"]
        assert config.STARTUP_WARNINGS == []
    finally:
        first.shutdown()
        second.shutdown()


def test_the_orchestrator_call_site_routes_startup_warnings_to_the_log(monkeypatch, captured_log):
    monkeypatch.setattr(config, "STARTUP_WARNINGS", ["config: HARNESS_UPDATE_INTERVAL_SEC=5 ungültig — y"])
    monkeypatch.setattr(config, "DASHBOARD_AUTOSTART", True)
    monkeypatch.setattr(config, "HARNESS_UPDATE_INTERVAL_SEC", 0)
    monkeypatch.setattr(dashboard, "_autostart_serve", lambda handle, *a: handle.bound.set())
    orchestrator._start_dashboard_autostart()
    assert [m for m in captured_log if "ungültig" in m] == ["config: HARNESS_UPDATE_INTERVAL_SEC=5 ungültig — y"]
