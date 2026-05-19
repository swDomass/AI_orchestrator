"""Pytest fixtures shared across the suite.

- ``sys.path``: makes project packages importable.
- ``_isolate_replay_store``: autouse — prevents tests from polluting the
  production ``logs/runs.jsonl`` when they exercise code paths that emit
  replay records (e.g. orchestrator ``_RunSpan.emit``). Individual test
  files can still override the path with their own fixture.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import replay  # noqa: E402 — must follow sys.path tweak


@pytest.fixture(autouse=True)
def _isolate_replay_store(tmp_path: Path):
    """Redirect the replay JSONL store + archive into pytest's tmp_path.

    Restores the previous paths after each test so production defaults are
    not mutated globally. Falls back to a no-op when ``replay`` lacks the
    expected hooks (e.g. partial import during collection-time errors).
    """
    saved_store = getattr(replay, "_store_path", None)
    saved_archive = getattr(replay, "_archive_dir", None)
    setter = getattr(replay, "set_store_path", None)
    resetter = getattr(replay, "reset_for_tests", None)
    if setter is None:
        yield
        return
    setter(tmp_path / "runs.jsonl", tmp_path / "runs-archive")
    try:
        yield
    finally:
        if resetter is not None:
            try:
                resetter()
            except Exception:  # noqa: BLE001 — teardown must never fail tests
                pass
        if saved_store is not None and setter is not None:
            setter(saved_store, saved_archive)
