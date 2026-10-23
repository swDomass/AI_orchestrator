"""B0-S1: the dashboard server is multi-threaded, analytics runs one at a time.

T1  a blocked analytics computation does not hold up GET /, /api/harness and
    /api/data?only=active_runs
T2  parallel /api/data?days=30 never run get_dashboard_data concurrently
T3  ?only=active_runs stays outside the build lock
T4  shutdown()/server_close() return despite a hung handler; daemon_threads

All synchronous; threads only as parallel clients. Real server on 127.0.0.1.
"""

import http.client
import socket
import threading
import time

import pytest

import analytics
import dashboard

CLIENT_TIMEOUT = 4  # must stay above FAST, so a broken assertion fails there, not as a client timeout
FAST = 2.5  # seconds; generous for Windows under parallel load


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _get(port: int, path: str, timeout: float = CLIENT_TIMEOUT) -> tuple[int, float]:
    """GET path, return (status, elapsed seconds)."""
    start = time.monotonic()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        resp.read()
        return resp.status, time.monotonic() - start
    finally:
        conn.close()


def _in_threads(port: int, paths: list[str]) -> list[tuple[int, float]]:
    results: list = [None] * len(paths)

    def work(i: int, path: str) -> None:
        results[i] = _get(port, path)

    threads = [threading.Thread(target=work, args=(i, p), daemon=True) for i, p in enumerate(paths)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(CLIENT_TIMEOUT + 2)
    assert all(r is not None for r in results), "a client did not finish"
    return results


@pytest.fixture
def started_server(monkeypatch):
    """Real _ReuseServer on a free port; yields (server, port)."""
    monkeypatch.setattr(analytics, "_load_active_runs", lambda: [])
    server, port = dashboard._bind_server(_free_port())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server, port
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def blocked_analytics(monkeypatch, started_server):
    """get_dashboard_data blocks until released; yields (port, entered, release)."""
    _server, port = started_server
    entered = threading.Event()
    release = threading.Event()

    def blocking(days=7):
        entered.set()
        release.wait(15)
        return {}

    monkeypatch.setattr(dashboard, "get_dashboard_data", blocking)
    try:
        yield port, entered, release
    finally:
        release.set()  # never leave a handler thread waiting


def _start_blocked_request(port: int, entered: threading.Event) -> threading.Thread:
    t = threading.Thread(target=_get, args=(port, "/api/data?days=30", 20), daemon=True)
    t.start()
    assert entered.wait(CLIENT_TIMEOUT), "blocking computation was never entered"
    return t


# -- T1: parallelism --------------------------------------------------------


def test_t1_other_endpoints_answer_while_analytics_blocks(blocked_analytics):
    port, entered, release = blocked_analytics
    slow = _start_blocked_request(port, entered)
    paths = ["/", "/api/harness", "/api/data?only=active_runs"]
    for status, elapsed in _in_threads(port, paths):
        assert status == 200
        assert elapsed < FAST
    release.set()
    slow.join(CLIENT_TIMEOUT)
    assert not slow.is_alive()


# -- T2: one computation at a time -----------------------------------------


def test_t2_parallel_data_requests_never_overlap_in_computation(monkeypatch, started_server):
    _server, port = started_server
    state = {"active": 0, "max": 0, "calls": 0}
    guard = threading.Lock()

    def counting(days=7):
        with guard:
            state["active"] += 1
            state["calls"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.2)
        with guard:
            state["active"] -= 1
        return {}

    monkeypatch.setattr(dashboard, "get_dashboard_data", counting)
    results = _in_threads(port, ["/api/data?days=30"] * 3)
    assert [r[0] for r in results] == [200, 200, 200]
    assert state["calls"] == 3
    assert state["max"] == 1


def test_t2_second_data_request_waits_for_the_first(monkeypatch, blocked_analytics):
    port, entered, release = blocked_analytics
    stub, entries = dashboard.get_dashboard_data, []

    def counting(days=7):
        entries.append(days)
        return stub(days)

    monkeypatch.setattr(dashboard, "get_dashboard_data", counting)
    first = _start_blocked_request(port, entered)
    second = threading.Thread(target=_get, args=(port, "/api/data?days=7", 20), daemon=True)
    second.start()
    second.join(0.5)
    assert second.is_alive(), "second computation was not held back by the lock"
    assert len(entries) == 1, "second request entered the computation while the first one holds"
    assert dashboard._DATA_BUILD_LOCK.locked()
    release.set()
    first.join(CLIENT_TIMEOUT)
    second.join(CLIENT_TIMEOUT)
    assert len(entries) == 2  # it did run after the release
    assert not first.is_alive()
    assert not second.is_alive()


# -- T3: active_runs outside the lock --------------------------------------


def test_t3_active_runs_answers_fast_while_lock_is_held(blocked_analytics):
    port, entered, release = blocked_analytics
    slow = _start_blocked_request(port, entered)
    assert dashboard._DATA_BUILD_LOCK.locked()
    status, elapsed = _get(port, "/api/data?only=active_runs")
    assert status == 200
    assert elapsed < FAST
    release.set()
    slow.join(CLIENT_TIMEOUT)


# -- T4: daemon threads, shutdown ------------------------------------------


def test_t4_server_class_uses_daemon_threads_without_joining():
    assert dashboard._ReuseServer.daemon_threads is True
    assert dashboard._ReuseServer.block_on_close is False
    assert dashboard._ReuseServer.allow_reuse_address is False


def test_t4_shutdown_and_close_return_with_a_hung_handler():
    server, port = dashboard._bind_server(_free_port())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # An open connection that never sends: its handler thread waits (timeout 10 s).
    hung = socket.create_connection(("127.0.0.1", port), timeout=CLIENT_TIMEOUT)
    try:
        time.sleep(0.2)  # let the server accept it and start the handler thread
        start = time.monotonic()
        server.shutdown()
        server.server_close()
        assert time.monotonic() - start < FAST
        t.join(CLIENT_TIMEOUT)
        assert not t.is_alive()
    finally:
        hung.close()
