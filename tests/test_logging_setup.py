"""Thread-level exception logging (`logging_setup.install_thread_excepthook`).

`run_orchestrator.ps1` starts `--watch` with a hidden window and no
stdout/stderr redirection, so Python's default `threading.excepthook` — which
prints to stderr — writes into nothing. A background thread that dies
(heartbeat, telegram listener, limits refresher) then simply stops working, with
no trace anywhere. These two tests pin the replacement hook: it logs the
traceback, and it keeps the stdlib's one exemption for `SystemExit`.

The hook is a process-wide mutation, so both tests restore
`threading.excepthook` in a fixture — that restore is what keeps them
independent of test order.
"""
import logging
import threading

import pytest

import logging_setup


@pytest.fixture(autouse=True)
def _restore_thread_excepthook():
    saved = threading.excepthook
    try:
        yield
    finally:
        threading.excepthook = saved


def _run_in_thread(target) -> None:
    t = threading.Thread(target=target, name="crash-thread")
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "worker thread did not finish"


def test_thread_excepthook_logs_the_traceback(caplog):
    """Plan test 16 — an uncaught thread exception becomes a CRITICAL log record.

    Asserted on `exc_info` rather than on formatted text: the record has to
    carry the exception so that whatever handler is configured (the production
    RotatingFileHandler) renders a real traceback into the file.
    """
    logging_setup.install_thread_excepthook()

    def boom():
        raise ValueError("thread blew up")

    with caplog.at_level(logging.INFO):
        _run_in_thread(boom)

    records = [r for r in caplog.records if r.exc_info and r.exc_info[0] is ValueError]
    assert records, [(r.levelname, r.getMessage()) for r in caplog.records]
    assert records[0].levelno >= logging.CRITICAL
    assert "crash-thread" in records[0].getMessage()
    assert "ValueError" in records[0].getMessage()


def test_thread_excepthook_ignores_system_exit(caplog):
    """Plan test 17 — `sys.exit()` in a thread ends that thread, it is not a fault.

    Mirrors the stdlib default (`threading.excepthook` skips `SystemExit`), so
    installing the hook does not turn ordinary thread shutdowns into CRITICAL
    noise in an unattended night run.
    """
    logging_setup.install_thread_excepthook()

    def quit_thread():
        raise SystemExit(0)

    with caplog.at_level(logging.INFO):
        _run_in_thread(quit_thread)

    assert not [r for r in caplog.records if r.exc_info], [
        (r.levelname, r.getMessage()) for r in caplog.records
    ]


def test_thread_excepthook_ignores_system_exit_subclasses(caplog):
    """`issubclass`, not `is` — the stdlib default ignores the whole family.

    A subclass carrying an exit code is an ordinary way to end a thread, and an
    identity check would log an orderly shutdown as CRITICAL. Found by external
    review (Codex, 2026-09-10); the identity check was the original version.
    """
    logging_setup.install_thread_excepthook()   # WITHOUT this the test proves nothing

    class OrderlyStop(SystemExit):
        pass

    def quit_thread():
        raise OrderlyStop(0)

    with caplog.at_level(logging.DEBUG):
        _run_in_thread(quit_thread)

    assert not [r for r in caplog.records if r.exc_info], [
        (r.levelname, r.getMessage()) for r in caplog.records
    ]
