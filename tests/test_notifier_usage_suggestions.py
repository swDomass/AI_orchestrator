"""Tests for usage suggestion notifications."""

from types import SimpleNamespace
from unittest.mock import patch

import notifier


def _mk(rank: int, label: str):
    return SimpleNamespace(rank=rank, label=label)


@patch("notifier._send")
def test_notify_usage_suggestions_uses_dynamic_pick_range(mock_send):
    suggestions = [_mk(1, "Skill: foo"), _mk(2, "Git: bar")]

    notifier.notify_usage_suggestions(
        suggestions=suggestions,
        remaining_pct=55.0,
        resets_in_sec=600,
    )

    text = mock_send.call_args[0][0]
    assert "/pick 1-2" in text
    assert "1. Skill: foo" in text
    assert "2. Git: bar" in text


@patch("notifier._send")
def test_notify_usage_suggestions_uses_pick_one_for_single_suggestion(mock_send):
    suggestions = [_mk(1, "Skill: solo")]

    notifier.notify_usage_suggestions(
        suggestions=suggestions,
        remaining_pct=40.0,
        resets_in_sec=300,
    )

    text = mock_send.call_args[0][0]
    assert "/pick 1 " in text
    assert "/pick 1-1" not in text


@patch("notifier._send")
def test_notify_usage_suggestions_no_double_newlines(mock_send):
    suggestions = [_mk(1, "Skill: test")]

    notifier.notify_usage_suggestions(
        suggestions=suggestions,
        remaining_pct=45.0,
        resets_in_sec=600,
    )

    text = mock_send.call_args[0][0]
    # Should not contain triple newlines (which would indicate double \n bug)
    assert "\n\n\n" not in text
