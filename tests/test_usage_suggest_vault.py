"""Tests for the vault-task suggestion strategy in usage_suggester."""

import re
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

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


# ------------------------------------------------------------------
# _filter_vault_task
# ------------------------------------------------------------------

class TestFilterVaultTask:
    def test_keeps_rolle_arbeit(self):
        s = _make_suggester()
        assert s._filter_vault_task("Do stuff #Rolle/arbeit") is not None

    def test_keeps_rolle_ich(self):
        s = _make_suggester()
        assert s._filter_vault_task("Learn Python #Rolle/ich") is not None

    def test_rejects_no_rolle(self):
        s = _make_suggester()
        assert s._filter_vault_task("Random task without role") is None

    def test_rejects_rolle_haus(self):
        s = _make_suggester()
        assert s._filter_vault_task("Fix sink #Rolle/haus") is None

    def test_rejects_rolle_fam(self):
        s = _make_suggester()
        assert s._filter_vault_task("Family dinner #Rolle/Fam") is None

    def test_rejects_rolle_whitelady(self):
        s = _make_suggester()
        assert s._filter_vault_task("Invoice #Rolle/literal:YourOrg") is None

    def test_rejects_rolle_unternehmungen(self):
        s = _make_suggester()
        assert s._filter_vault_task("Hiking #Rolle/unternehmungen") is None

    def test_rejects_wait(self):
        s = _make_suggester()
        assert s._filter_vault_task("Blocked task #Rolle/arbeit #wait") is None

    def test_rejects_habit(self):
        s = _make_suggester()
        assert s._filter_vault_task("Daily standup #Rolle/arbeit #habit/daily") is None

    def test_rejects_recurrence_emoji(self):
        s = _make_suggester()
        assert s._filter_vault_task("Weekly review #Rolle/arbeit \U0001f501") is None

    def test_rejects_dauer_proj(self):
        s = _make_suggester()
        assert s._filter_vault_task("Big project #Rolle/arbeit #Dauer/proj") is None

    def test_rejects_dauer_d(self):
        s = _make_suggester()
        assert s._filter_vault_task("Full day task #Rolle/arbeit #Dauer/d") is None


# ------------------------------------------------------------------
# _heuristic_score
# ------------------------------------------------------------------

class TestHeuristicScore:
    def test_urgent_1(self):
        s = _make_suggester()
        score = s._heuristic_score("Task #Urgent/1 #Rolle/arbeit")
        assert score == pytest.approx(2.0)

    def test_urgent_2(self):
        s = _make_suggester()
        assert s._heuristic_score("Task #Urgent/2") == pytest.approx(1.5)

    def test_urgent_3(self):
        s = _make_suggester()
        assert s._heuristic_score("Task #Urgent/3") == pytest.approx(1.0)

    def test_urgent_4(self):
        s = _make_suggester()
        assert s._heuristic_score("Task #Urgent/4") == pytest.approx(0.3)

    def test_dauer_15min(self):
        s = _make_suggester()
        assert s._heuristic_score("Task #Dauer/15min") == pytest.approx(1.0)

    def test_dauer_30min(self):
        s = _make_suggester()
        assert s._heuristic_score("Task #Dauer/30min") == pytest.approx(0.8)

    def test_dauer_1h(self):
        s = _make_suggester()
        assert s._heuristic_score("Task #Dauer/1h") == pytest.approx(0.5)

    def test_dauer_2h(self):
        s = _make_suggester()
        assert s._heuristic_score("Task #Dauer/2h") == pytest.approx(0.3)

    def test_overdue_date(self):
        s = _make_suggester()
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        score = s._heuristic_score(f"Task \U0001f4c5 {yesterday}")
        assert score == pytest.approx(1.5)

    def test_future_date_no_bonus(self):
        s = _make_suggester()
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        score = s._heuristic_score(f"Task \U0001f4c5 {tomorrow}")
        assert score == pytest.approx(0.0)

    def test_combined_scores(self):
        s = _make_suggester()
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        text = f"Task #Urgent/1 #Dauer/15min \U0001f4c5 {yesterday}"
        score = s._heuristic_score(text)
        assert score == pytest.approx(2.0 + 1.0 + 1.5)

    def test_score_never_negative(self):
        s = _make_suggester()
        # Only low-priority emoji, no positives
        score = s._heuristic_score("Task \U0001f53d")
        assert score >= 0.0

    def test_priority_up_emoji(self):
        s = _make_suggester()
        score = s._heuristic_score("Task \u23eb")  # ⏫
        assert score == pytest.approx(1.0)


# ------------------------------------------------------------------
# _scan_vault_tasks
# ------------------------------------------------------------------

class TestScanVaultTasks:
    def test_scans_files_and_filters(self, tmp_path):
        task_file = tmp_path / "01_Tasks" / "01_Tasks_Lake.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text(
            "# Tasks\n"
            "- [ ] Write docs #Rolle/arbeit #Dauer/15min\n"
            "- [x] Done task #Rolle/arbeit\n"
            "- [ ] Fix sink #Rolle/haus\n"
            "- [ ] No role task\n"
            "- [ ] Learn stuff #Rolle/ich #Urgent/2\n",
            encoding="utf-8",
        )

        s = _make_suggester()
        with (
            patch.object(us, "VAULT_PATH", tmp_path),
            patch.object(us, "USAGE_SUGGEST_VAULT_TASK_DIRS", ["01_Tasks/01_Tasks_Lake.md"]),
        ):
            results = s._scan_vault_tasks()

        # Should find "Write docs" and "Learn stuff", not done/haus/no-role
        assert len(results) == 2
        texts = [r[0] for r in results]
        assert any("Write docs" in t for t in texts)
        assert any("Learn stuff" in t for t in texts)

    def test_scans_directory_recursively(self, tmp_path):
        proj_dir = tmp_path / "01_Tasks" / "01_Projekte" / "proj1"
        proj_dir.mkdir(parents=True)
        (proj_dir / "tasks.md").write_text(
            "- [ ] Implement feature #Rolle/arbeit\n",
            encoding="utf-8",
        )

        s = _make_suggester()
        with (
            patch.object(us, "VAULT_PATH", tmp_path),
            patch.object(us, "USAGE_SUGGEST_VAULT_TASK_DIRS", ["01_Tasks/01_Projekte"]),
        ):
            results = s._scan_vault_tasks()

        assert len(results) == 1
        assert "Implement feature" in results[0][0]

    def test_missing_path_is_skipped(self, tmp_path):
        s = _make_suggester()
        with (
            patch.object(us, "VAULT_PATH", tmp_path),
            patch.object(us, "USAGE_SUGGEST_VAULT_TASK_DIRS", ["nonexistent.md"]),
        ):
            results = s._scan_vault_tasks()

        assert results == []

    def test_scan_ignores_malformed_suggested_hashes_file(self, tmp_path):
        task_file = tmp_path / "01_Tasks" / "01_Tasks_Lake.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text("- [ ] Write docs #Rolle/arbeit\n", encoding="utf-8")
        bad_hashes = tmp_path / "99_System" / "AI" / "memory" / "suggested_tasks.json"
        bad_hashes.parent.mkdir(parents=True)
        bad_hashes.write_text('["broken"]', encoding="utf-8")

        s = _make_suggester()
        with (
            patch.object(us, "VAULT_PATH", tmp_path),
            patch.object(us, "USAGE_SUGGEST_VAULT_TASK_DIRS", ["01_Tasks/01_Tasks_Lake.md"]),
            patch.object(us, "_SUGGESTED_HASHES_FILE", bad_hashes),
        ):
            results = s._scan_vault_tasks()

        assert len(results) == 1
        assert "Write docs" in results[0][0]

    def test_scan_uses_filtered_text_for_history_and_scoring(self, tmp_path):
        task_file = tmp_path / "01_Tasks" / "01_Tasks_Lake.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text("- [ ] Write docs #Rolle/arbeit #Urgent/1\n", encoding="utf-8")

        s = _make_suggester()
        with (
            patch.object(us, "VAULT_PATH", tmp_path),
            patch.object(us, "USAGE_SUGGEST_VAULT_TASK_DIRS", ["01_Tasks/01_Tasks_Lake.md"]),
            patch.object(us.UsageSuggester, "_filter_vault_task", return_value="Write docs"),
            patch.object(us.UsageSuggester, "_is_recently_suggested", return_value=False) as recent_mock,
            patch.object(us.UsageSuggester, "_heuristic_score", return_value=1.25) as score_mock,
        ):
            results = s._scan_vault_tasks()

        recent_mock.assert_called_once_with("Write docs", ANY, ANY)
        score_mock.assert_called_once_with("Write docs", file_is_recent=ANY)
        assert results == [("Write docs", 1.25)]


class TestSuggestedHashesPersistence:
    def test_load_suggested_hashes_filters_invalid_entries(self, tmp_path):
        hashes_file = tmp_path / "suggested_tasks.json"
        hashes_file.write_text(
            '{"good": 123.5, "also_good": "456", "bad": "nope", "nullish": null}',
            encoding="utf-8",
        )

        s = _make_suggester()
        with patch.object(us, "_SUGGESTED_HASHES_FILE", hashes_file):
            loaded = s._load_suggested_hashes()

        assert loaded == {"good": 123.5, "also_good": 456.0}

    def test_save_suggested_hashes_replaces_existing_file_atomically(self, tmp_path):
        hashes_file = tmp_path / "suggested_tasks.json"
        hashes_file.write_text('{"old": 1}', encoding="utf-8")

        s = _make_suggester()
        with patch.object(us, "_SUGGESTED_HASHES_FILE", hashes_file):
            s._save_suggested_hashes({"new": 2.0})

        assert hashes_file.read_text(encoding="utf-8") == '{"new": 2.0}'
        assert not hashes_file.with_suffix(".tmp").exists()

    def test_record_suggestions_only_persists_vault_tasks(self, tmp_path):
        hashes_file = tmp_path / "suggested_tasks.json"
        s = _make_suggester()
        suggestions = [
            us.Suggestion(rank=1, label="Vault", task_text="Review docs #Rolle/arbeit", source="vault", score=1.0),
            us.Suggestion(rank=2, label="Retry", task_text="Review docs #Rolle/arbeit", source="retry", score=0.6),
            us.Suggestion(rank=3, label="Skill", task_text="Run skill", source="skill", score=1.5),
        ]

        with patch.object(us, "_SUGGESTED_HASHES_FILE", hashes_file):
            s._record_suggestions(suggestions)
            loaded = s._load_suggested_hashes()

        task_hash = s._task_hash("Review docs #Rolle/arbeit")
        assert len(loaded) == 1
        assert task_hash in loaded
        assert isinstance(loaded[task_hash], float)


# ------------------------------------------------------------------
# _assess_autonomy
# ------------------------------------------------------------------

class TestAssessAutonomy:
    def test_parses_llm_response(self):
        """Test LLM response parsing with mocked provider."""
        s = _make_suggester()
        candidates = [
            ("Write unit tests #Rolle/arbeit", 2.0),
            ("Buy groceries #Rolle/ich", 1.0),
        ]

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = "1: 9\n2: 1\n"

        mock_provider = MagicMock()
        mock_provider.run.return_value = mock_result

        mock_limits = MagicMock()

        with (
            patch("limits.get_limits", return_value=mock_limits),
            patch("dispatcher.select_provider", return_value=mock_provider),
        ):
            scores = s._assess_autonomy(candidates)

        assert scores[0] == pytest.approx(9.0)
        assert scores[1] == pytest.approx(1.0)

    def test_returns_empty_when_no_provider(self):
        s = _make_suggester()
        candidates = [("Some task #Rolle/arbeit", 1.0)]

        mock_limits = MagicMock()

        with (
            patch("limits.get_limits", return_value=mock_limits),
            patch("dispatcher.select_provider", return_value=None),
        ):
            scores = s._assess_autonomy(candidates)

        assert scores == {}

    def test_returns_empty_on_provider_failure(self):
        s = _make_suggester()
        candidates = [("Some task #Rolle/arbeit", 1.0)]

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.output = ""

        mock_provider = MagicMock()
        mock_provider.run.return_value = mock_result

        mock_limits = MagicMock()

        with (
            patch("limits.get_limits", return_value=mock_limits),
            patch("dispatcher.select_provider", return_value=mock_provider),
        ):
            scores = s._assess_autonomy(candidates)

        assert scores == {}

    def test_returns_empty_on_exception(self):
        s = _make_suggester()
        candidates = [("Some task #Rolle/arbeit", 1.0)]

        with patch("limits.get_limits", side_effect=Exception("boom")):
            scores = s._assess_autonomy(candidates)

        assert scores == {}

    def test_empty_candidates(self):
        s = _make_suggester()
        scores = s._assess_autonomy([])
        assert scores == {}


# ------------------------------------------------------------------
# _suggest_vault_tasks (integration)
# ------------------------------------------------------------------

class TestSuggestVaultTasks:
    def test_returns_suggestions_with_llm(self, tmp_path):
        task_file = tmp_path / "tasks.md"
        task_file.write_text(
            "- [ ] Write unit tests for parser #Rolle/arbeit #Urgent/1 #Dauer/15min\n"
            "- [ ] Review PR #Rolle/arbeit #Dauer/30min\n",
            encoding="utf-8",
        )

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = "1: 8\n2: 7\n"

        mock_provider = MagicMock()
        mock_provider.run.return_value = mock_result

        mock_limits = MagicMock()

        s = _make_suggester()
        with (
            patch.object(us, "VAULT_PATH", tmp_path),
            patch.object(us, "USAGE_SUGGEST_VAULT_TASK_DIRS", ["tasks.md"]),
            patch("limits.get_limits", return_value=mock_limits),
            patch("dispatcher.select_provider", return_value=mock_provider),
        ):
            results = s._suggest_vault_tasks()

        assert len(results) == 2
        assert all(r.source == "vault" for r in results)
        assert all(0.5 <= r.score <= 2.5 for r in results)

    def test_returns_suggestions_without_llm(self, tmp_path):
        """Fallback: no provider available, uses heuristic only."""
        task_file = tmp_path / "tasks.md"
        task_file.write_text(
            "- [ ] Write docs #Rolle/arbeit #Urgent/1 #Dauer/15min\n",
            encoding="utf-8",
        )

        mock_limits = MagicMock()

        s = _make_suggester()
        with (
            patch.object(us, "VAULT_PATH", tmp_path),
            patch.object(us, "USAGE_SUGGEST_VAULT_TASK_DIRS", ["tasks.md"]),
            patch("limits.get_limits", return_value=mock_limits),
            patch("dispatcher.select_provider", return_value=None),
        ):
            results = s._suggest_vault_tasks()

        # Should still return results (heuristic-only), no LLM filter
        assert len(results) >= 1
        assert results[0].source == "vault"

    def test_filters_low_autonomy_tasks(self, tmp_path):
        task_file = tmp_path / "tasks.md"
        task_file.write_text(
            "- [ ] Call dentist #Rolle/ich #Dauer/15min\n",
            encoding="utf-8",
        )

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = "1: 1\n"  # Very low autonomy score

        mock_provider = MagicMock()
        mock_provider.run.return_value = mock_result

        mock_limits = MagicMock()

        s = _make_suggester()
        with (
            patch.object(us, "VAULT_PATH", tmp_path),
            patch.object(us, "USAGE_SUGGEST_VAULT_TASK_DIRS", ["tasks.md"]),
            patch("limits.get_limits", return_value=mock_limits),
            patch("dispatcher.select_provider", return_value=mock_provider),
        ):
            results = s._suggest_vault_tasks()

        # LLM scored < 3, should be filtered out
        assert len(results) == 0

    def test_max_3_suggestions(self, tmp_path):
        task_file = tmp_path / "tasks.md"
        lines = [
            f"- [ ] Task {i} #Rolle/arbeit #Urgent/1 #Dauer/15min\n"
            for i in range(10)
        ]
        task_file.write_text("".join(lines), encoding="utf-8")

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = "\n".join(f"{i+1}: 8" for i in range(10))

        mock_provider = MagicMock()
        mock_provider.run.return_value = mock_result

        mock_limits = MagicMock()

        s = _make_suggester()
        with (
            patch.object(us, "VAULT_PATH", tmp_path),
            patch.object(us, "USAGE_SUGGEST_VAULT_TASK_DIRS", ["tasks.md"]),
            patch("limits.get_limits", return_value=mock_limits),
            patch("dispatcher.select_provider", return_value=mock_provider),
        ):
            results = s._suggest_vault_tasks()

        assert len(results) <= 3


# ------------------------------------------------------------------
# _gather_suggestions includes vault tasks
# ------------------------------------------------------------------

class TestGatherSuggestionsIncludesVault:
    def test_vault_tasks_in_candidates(self):
        s = _make_suggester()
        vault_suggestion = us.Suggestion(
            rank=0, label="Vault: Test", task_text="test", source="vault", score=2.0,
        )
        with (
            patch.object(s, "_suggest_skills", return_value=[]),
            patch.object(s, "_suggest_git_changes", return_value=[]),
            patch.object(s, "_suggest_failed_retries", return_value=[]),
            patch.object(s, "_suggest_vault_tasks", return_value=[vault_suggestion]),
        ):
            results = s._gather_suggestions()

        assert len(results) == 1
        assert results[0].source == "vault"
        assert results[0].rank == 1
