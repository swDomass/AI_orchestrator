"""Tests for the usage_suggester module."""

import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import usage_suggester as us


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the module-level singleton between tests."""
    us._suggester = None
    yield
    us._suggester = None


def _make_suggester():
    return us.get_suggester()


class TestSuggestionDataclass:
    def test_fields(self):
        s = us.Suggestion(rank=1, label="Test", task_text="do stuff", source="skill", score=2.0)
        assert s.rank == 1
        assert s.source == "skill"


class TestGetSuggester:
    def test_singleton(self):
        a = us.get_suggester()
        b = us.get_suggester()
        assert a is b


class TestCheckAndSuggest:
    def test_returns_none_when_queue_not_empty(self):
        suggester = _make_suggester()
        result = suggester.check_and_suggest(lambda: ["task1"])
        assert result is None

    def test_returns_none_on_cooldown(self):
        suggester = _make_suggester()
        suggester._last_triggered = datetime.now()
        result = suggester.check_and_suggest(lambda: [])
        assert result is None

    @patch.object(us.UsageSuggester, "_get_claude_limits", return_value=(None, 0))
    def test_returns_none_when_limits_unavailable(self, _mock):
        suggester = _make_suggester()
        result = suggester.check_and_suggest(lambda: [])
        assert result is None

    @patch.object(us.UsageSuggester, "_get_claude_limits", return_value=(10.0, 300))
    def test_returns_none_when_remaining_too_low(self, _mock):
        suggester = _make_suggester()
        result = suggester.check_and_suggest(lambda: [])
        assert result is None

    @patch.object(us.UsageSuggester, "_get_claude_limits", return_value=(50.0, 7200))
    def test_returns_none_when_reset_too_far(self, _mock):
        suggester = _make_suggester()
        result = suggester.check_and_suggest(lambda: [])
        assert result is None

    @patch.object(us.UsageSuggester, "_get_claude_limits", return_value=(50.0, 600))
    @patch.object(us.UsageSuggester, "_gather_suggestions", return_value=[])
    def test_returns_none_when_no_suggestions(self, _g, _l):
        suggester = _make_suggester()
        result = suggester.check_and_suggest(lambda: [])
        assert result is None

    @patch("notifier.notify_usage_suggestions")
    @patch("queue_manager.append_task", return_value=True)
    @patch("notifier.send_message")
    @patch.object(us.UsageSuggester, "_get_claude_limits", return_value=(50.0, 600))
    @patch.object(us.UsageSuggester, "_get_seven_day_pace", return_value=None)
    @patch.object(us.UsageSuggester, "_gather_suggestions")
    def test_pick_adds_task(self, mock_gather, _pace, _lim, _send, _append, _notify):
        suggestions = [
            us.Suggestion(rank=1, label="Skill: foo", task_text="run foo", source="skill", score=2.0),
        ]
        mock_gather.return_value = suggestions

        suggester = _make_suggester()

        # Simulate /pick 1 arriving shortly after suggestions are sent
        def _auto_respond():
            import time
            time.sleep(0.1)
            suggester.respond("1")

        t = threading.Thread(target=_auto_respond, daemon=True)
        t.start()

        result = suggester.check_and_suggest(lambda: [])
        assert result == "picked: Skill: foo"

    @patch("queue_manager.append_task", return_value=True)
    @patch("notifier.send_message")
    @patch.object(us.UsageSuggester, "_get_claude_limits", return_value=(50.0, 600))
    @patch.object(us.UsageSuggester, "_get_seven_day_pace", return_value=None)
    @patch.object(us.UsageSuggester, "_gather_suggestions")
    def test_pick_immediately_after_notify(self, mock_gather, _pace, _lim, _send, _append):
        suggestions = [
            us.Suggestion(rank=1, label="Skill: foo", task_text="run foo", source="skill", score=2.0),
        ]
        mock_gather.return_value = suggestions
        suggester = _make_suggester()

        with patch("notifier.notify_usage_suggestions", side_effect=lambda *_, **__: suggester.respond("1")):
            result = suggester.check_and_suggest(lambda: [])

        assert result == "picked: Skill: foo"

    @patch("queue_manager.append_task", return_value=True)
    @patch("notifier.send_message")
    @patch.object(us.UsageSuggester, "_get_claude_limits", return_value=(50.0, 600))
    @patch.object(us.UsageSuggester, "_get_seven_day_pace", return_value=None)
    @patch.object(us.UsageSuggester, "_gather_suggestions")
    def test_pick_processed_when_wait_reports_timeout_but_response_exists(self, mock_gather, _pace, _lim, _send, _append):
        suggestions = [
            us.Suggestion(rank=1, label="Skill: foo", task_text="run foo", source="skill", score=2.0),
        ]
        mock_gather.return_value = suggestions
        suggester = _make_suggester()

        with (
            patch("notifier.notify_usage_suggestions", side_effect=lambda *_, **__: suggester.respond("1")),
            patch.object(threading.Event, "wait", return_value=False),
        ):
            result = suggester.check_and_suggest(lambda: [])

        assert result == "picked: Skill: foo"

    @patch("notifier.notify_usage_suggestions")
    @patch.object(us.UsageSuggester, "_get_claude_limits", return_value=(50.0, 600))
    @patch.object(us.UsageSuggester, "_get_seven_day_pace", return_value=None)
    @patch.object(us.UsageSuggester, "_gather_suggestions")
    def test_decline(self, mock_gather, _pace, _lim, _notify):
        suggestions = [
            us.Suggestion(rank=1, label="Skill: foo", task_text="run foo", source="skill", score=2.0),
        ]
        mock_gather.return_value = suggestions

        suggester = _make_suggester()

        def _auto_respond():
            import time
            time.sleep(0.1)
            suggester.respond("decline")

        t = threading.Thread(target=_auto_respond, daemon=True)
        t.start()

        result = suggester.check_and_suggest(lambda: [])
        assert result == "declined"


class TestSevenDayPaceGuard:
    """Tests for the 7-day pace guard in check_and_suggest."""

    @patch.object(us.UsageSuggester, "_get_claude_limits", return_value=(50.0, 600))
    @patch.object(
        us.UsageSuggester,
        "_get_seven_day_pace",
        return_value={"pace_factor": 2.6, "days_remaining": 3.0, "status": "critical"},
    )
    @patch.object(us.UsageSuggester, "_gather_suggestions")
    def test_suppressed_when_seven_day_over_pace(self, mock_gather, _pace, _lim):
        """pace_factor=2.6 (> max 2.5) with 3 days remaining → suppressed before gathering."""
        suggester = _make_suggester()
        result = suggester.check_and_suggest(lambda: [])
        assert result is None
        mock_gather.assert_not_called()

    @patch.object(us.UsageSuggester, "_get_claude_limits", return_value=(50.0, 600))
    @patch.object(
        us.UsageSuggester,
        "_get_seven_day_pace",
        return_value={"pace_factor": 3.0, "days_remaining": 0.1, "status": "critical"},
    )
    @patch.object(us.UsageSuggester, "_gather_suggestions", return_value=[])
    def test_not_suppressed_at_end_of_window(self, _gather, _pace, _lim):
        """pace_factor=3.0 but only 0.1 days remaining → proceeds (no suggestions though)."""
        suggester = _make_suggester()
        result = suggester.check_and_suggest(lambda: [])
        # Not suppressed by pace guard, but no suggestions → None
        assert result is None
        # Verify _gather_suggestions was called (pace guard didn't stop it)
        _gather.assert_called_once()

    @patch.object(us.UsageSuggester, "_get_claude_limits", return_value=(50.0, 600))
    @patch.object(
        us.UsageSuggester,
        "_get_seven_day_pace",
        return_value={"pace_factor": 1.1, "days_remaining": 3.0, "status": "ok"},
    )
    @patch.object(us.UsageSuggester, "_gather_suggestions", return_value=[])
    def test_not_suppressed_when_pace_ok(self, _gather, _pace, _lim):
        """pace_factor=1.1 (below 2.0 limit) → proceeds normally."""
        suggester = _make_suggester()
        result = suggester.check_and_suggest(lambda: [])
        _gather.assert_called_once()

    @patch.object(us.UsageSuggester, "_get_claude_limits", return_value=(50.0, 600))
    @patch.object(us.UsageSuggester, "_get_seven_day_pace", return_value=None)
    @patch.object(us.UsageSuggester, "_gather_suggestions", return_value=[])
    def test_not_suppressed_when_no_seven_day_data(self, _gather, _pace, _lim):
        """_get_seven_day_pace returns None → no suppression."""
        suggester = _make_suggester()
        result = suggester.check_and_suggest(lambda: [])
        _gather.assert_called_once()


class TestRespond:
    def test_respond_sets_event(self):
        suggester = _make_suggester()
        event = threading.Event()
        suggester._suggestion_event = event
        assert suggester.respond("1")
        assert event.is_set()
        assert suggester._suggestion_response == "1"

    def test_respond_without_event_returns_false(self):
        suggester = _make_suggester()
        assert not suggester.respond("1")

class TestHasPendingSuggestion:
    def test_false_when_no_event(self):
        suggester = _make_suggester()
        assert not suggester.has_pending_suggestion()

    def test_true_when_event_set(self):
        suggester = _make_suggester()
        suggester._suggestion_event = threading.Event()
        assert suggester.has_pending_suggestion()


class TestPendingSuggestionCount:
    def test_zero_without_pending_event(self):
        suggester = _make_suggester()
        suggester._pending_suggestions = [
            us.Suggestion(rank=1, label="x", task_text="y", source="skill", score=1.0),
        ]
        assert suggester.pending_suggestion_count() == 0

    def test_count_with_pending_event(self):
        suggester = _make_suggester()
        suggester._suggestion_event = threading.Event()
        suggester._pending_suggestions = [
            us.Suggestion(rank=1, label="a", task_text="a", source="skill", score=1.0),
            us.Suggestion(rank=2, label="b", task_text="b", source="git", score=0.9),
        ]
        assert suggester.pending_suggestion_count() == 2


class TestSuggestGitChanges:
    @patch("subprocess.run")
    def test_scores_by_change_count(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="M file1.py\nM file2.py\n",
        )
        suggester = _make_suggester()
        # Root itself is a git repo (has .git dir)
        mock_root = MagicMock()
        mock_root.is_dir.return_value = True
        mock_root.name = "proj"
        mock_git_dir = MagicMock()
        mock_git_dir.exists.return_value = True
        mock_root.__truediv__ = lambda self, key: mock_git_dir if key == ".git" else MagicMock()
        with patch("usage_suggester.ALLOWED_CWD_ROOTS", [mock_root]):
            results = suggester._suggest_git_changes()

        assert len(results) == 1
        assert results[0].source == "git"
        assert results[0].score == pytest.approx(0.9, abs=0.01)

    @patch("subprocess.run")
    def test_scans_child_dirs_for_git_repos(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="M file1.py\n",
        )
        suggester = _make_suggester()
        # Root is NOT a git repo, but has a child that is
        mock_child = MagicMock()
        mock_child.is_dir.return_value = True
        mock_child.name = "child_repo"
        child_git = MagicMock()
        child_git.exists.return_value = True
        mock_child.__truediv__ = lambda self, key: child_git if key == ".git" else MagicMock()

        mock_root = MagicMock()
        mock_root.is_dir.return_value = True
        mock_root.name = "parent"
        root_git = MagicMock()
        root_git.exists.return_value = False
        mock_root.__truediv__ = lambda self, key: root_git if key == ".git" else MagicMock()
        mock_root.iterdir.return_value = [mock_child]

        with patch("usage_suggester.ALLOWED_CWD_ROOTS", [mock_root]):
            results = suggester._suggest_git_changes()

        assert len(results) == 1
        assert "child_repo" in results[0].label
