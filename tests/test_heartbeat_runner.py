"""Tests for HeartbeatRunner lock safety and start_heartbeat_thread."""
import threading
import time
from unittest.mock import MagicMock, patch

import heartbeat
from heartbeat import HeartbeatRunner, start_heartbeat_thread


def _make_runner_no_file() -> HeartbeatRunner:
    """Create a HeartbeatRunner that never loads a real HEARTBEAT.md."""
    with patch.object(HeartbeatRunner, "_reload_if_changed", return_value=None):
        runner = HeartbeatRunner()
    runner._items = []
    return runner


def test_run_due_skips_when_lock_held():
    """run_due() must return immediately if the lock is already held."""
    runner = _make_runner_no_file()
    call_count = 0

    def _slow_due_locked(queue_read_fn, dispatcher=None):
        nonlocal call_count
        call_count += 1
        time.sleep(0.1)

    runner._run_due_locked = _slow_due_locked

    # Acquire the lock externally to simulate a concurrent run_due()
    assert runner._lock.acquire(blocking=False)
    try:
        runner.run_due(lambda: [])
    finally:
        runner._lock.release()

    # _run_due_locked was never called because the lock was held
    assert call_count == 0


def test_run_due_releases_lock_after_run():
    """run_due() must release the lock after execution so it can be called again."""
    runner = _make_runner_no_file()

    with patch.object(runner, "_run_due_locked") as mock_inner:
        runner.run_due(lambda: [])
        runner.run_due(lambda: [])

    assert mock_inner.call_count == 2
    # Lock must be free after both calls
    assert runner._lock.acquire(blocking=False)
    runner._lock.release()


def test_start_heartbeat_thread_returns_daemon_thread():
    """start_heartbeat_thread() must return a running daemon thread."""
    runner = _make_runner_no_file()
    stop = threading.Event()

    with patch.object(runner, "run_due") as mock_due:
        t = start_heartbeat_thread(runner, lambda: [], stop, poll_sec=1)
        try:
            assert t.is_alive()
            assert t.daemon
            time.sleep(1.3)
            assert mock_due.call_count >= 1
        finally:
            stop.set()
            t.join(timeout=3)


def test_start_heartbeat_thread_stops_on_event():
    """Background thread must exit promptly when the stop event is set."""
    runner = _make_runner_no_file()
    stop = threading.Event()

    with patch.object(runner, "run_due"):
        t = start_heartbeat_thread(runner, lambda: [], stop, poll_sec=30)
        assert t.is_alive()
        stop.set()
        t.join(timeout=2)
        assert not t.is_alive()
