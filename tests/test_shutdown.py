import pytest
import time
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
