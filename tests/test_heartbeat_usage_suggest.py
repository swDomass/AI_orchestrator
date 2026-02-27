"""Tests for heartbeat usage-suggest integration."""

from unittest.mock import MagicMock, patch

import heartbeat


def test_usage_suggest_starts_background_thread_and_returns_none():
    suggester = MagicMock()

    class _DummyThread:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target
            self.name = name
            self.daemon = daemon
            self.started = False

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

    heartbeat._usage_suggest_thread = None

    with patch("usage_suggester.get_suggester", return_value=suggester), patch(
        "heartbeat.threading.Thread", side_effect=lambda **kwargs: _DummyThread(**kwargs)
    ) as mock_thread:
        result = heartbeat._check_usage_suggest(lambda: [])

    assert result is None
    assert mock_thread.call_count == 1
    suggester.check_and_suggest.assert_not_called()
    assert heartbeat._usage_suggest_thread is not None
    assert heartbeat._usage_suggest_thread.is_alive()


def test_usage_suggest_skips_when_worker_thread_is_alive():
    class _AliveThread:
        def is_alive(self):
            return True

    heartbeat._usage_suggest_thread = _AliveThread()

    with patch("usage_suggester.get_suggester") as mock_get:
        result = heartbeat._check_usage_suggest(lambda: [])

    assert result is None
    mock_get.assert_not_called()

