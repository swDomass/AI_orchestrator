import pytest
import time
import threading
from types import SimpleNamespace
from shutdown import request_shutdown, cancel_shutdown, shutdown_pending, shutdown_cancel, execute_shutdown

def test_shutdown_state_management():
    # Reset states
    shutdown_pending.clear()
    shutdown_cancel.clear()
    
    assert request_shutdown() is True
    assert shutdown_pending.is_set()
    assert request_shutdown() is False # Already pending
    
    cancel_shutdown()
    assert shutdown_cancel.is_set()

def test_execute_shutdown_cancellation(monkeypatch):
    # Mocking notifications to avoid external side effects
    monkeypatch.setattr("notifier.notify_shutdown_pending", lambda x: None)
    monkeypatch.setattr("notifier.notify_shutdown_cancelled", lambda: None)
    
    shutdown_pending.set()
    shutdown_cancel.clear()
    
    # Run execute_shutdown in a short delay mode
    # We'll cancel it from another thread
    def cancel_after_a_bit():
        time.sleep(0.1)
        cancel_shutdown()
    
    import threading
    t = threading.Thread(target=cancel_after_a_bit)
    t.start()
    
    # Use a 1 second delay for the test
    execute_shutdown(delay_sec=1)
    
    assert not shutdown_pending.is_set()
    assert not shutdown_cancel.is_set()
    t.join()


def test_execute_shutdown_ignores_duplicate_concurrent_countdown(monkeypatch):
    monkeypatch.setattr("notifier.notify_shutdown_pending", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("notifier.notify_shutdown_cancelled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("notifier.notify_shutdown_executing", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("queue_manager.append_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("shutdown.check_queue_abort", lambda: False)

    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def fake_run(*_args, **_kwargs):
        calls["n"] += 1
        started.set()
        release.wait(timeout=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("shutdown.subprocess.run", fake_run)

    shutdown_pending.set()
    shutdown_cancel.clear()

    t1 = threading.Thread(target=execute_shutdown, kwargs={"delay_sec": 0}, daemon=True)
    t2 = threading.Thread(target=execute_shutdown, kwargs={"delay_sec": 0}, daemon=True)
    t1.start()
    assert started.wait(timeout=0.2)
    t2.start()

    time.sleep(0.05)
    assert calls["n"] == 1

    release.set()
    t1.join(timeout=1)
    t2.join(timeout=1)
