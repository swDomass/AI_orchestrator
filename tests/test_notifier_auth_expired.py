"""Tests for notifier.notify_auth_expired() — the actionable OAuth-expiry notice."""

from unittest.mock import patch

import notifier


@patch("notifier._send")
def test_notify_auth_expired_names_provider_and_action(mock_send):
    notifier.notify_auth_expired("claude")

    text = mock_send.call_args[0][0]
    assert "claude" in text
    assert "claude login" in text


@patch("notifier._send")
def test_notify_auth_expired_states_no_retry_budget_consumed(mock_send):
    """The message must make the "still scheduled, no budget burned" guarantee
    explicit — that is the whole point of the dedicated notice over the raw
    per-attempt notify_error()."""
    notifier.notify_auth_expired("claude")

    text = mock_send.call_args[0][0]
    assert "Retry-Budget" in text


@patch("notifier._send")
def test_notify_auth_expired_respects_notify_on_error_flag(mock_send, monkeypatch):
    monkeypatch.setattr(notifier, "NOTIFY_ON_ERROR", False)

    notifier.notify_auth_expired("claude")

    mock_send.assert_not_called()
