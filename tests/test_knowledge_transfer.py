"""Tests for tools/knowledge_transfer.py — pure functions plus the phase-failure
error classification (the only part of run() that decides queue behaviour)."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

with patch("config._load_dotenv"):
    from providers.base import RunResult
    from tools.knowledge_transfer import (
        KnowledgeTransferTool,
        _extract_note_title,
        _extract_topic,
        _make_slug,
        _scan_vault,
        _score_note,
    )


# ── _score_note ───────────────────────────────────────────────────────────────


class TestScoreNote:
    def test_longer_content_scores_higher(self):
        short = _score_note("note.md", "short", None)
        long = _score_note("note.md", "x" * 5000, None)
        assert long > short

    def test_wikilinks_boost_score(self):
        without = _score_note("note.md", "some content here", None)
        with_links = _score_note("note.md", "[[Link1]] [[Link2]] [[Link3]]", None)
        assert with_links > without

    def test_technical_keywords_boost_score(self):
        plain = _score_note("note.md", "this is just plain text", None)
        technical = _score_note("note.md", "algorithmus simulation analyse methode", None)
        assert technical > plain

    def test_topic_match_multiplies_score(self):
        no_match = _score_note("note.md", "general content here", "brakes")
        match_content = _score_note("note.md", "brakes content here", "brakes")
        match_filename = _score_note("brakes-analysis.md", "general content here", "brakes")
        assert match_content > no_match
        assert match_filename > no_match

    def test_topic_none_no_boost(self):
        score_none = _score_note("note.md", "content", None)
        # No crash, returns a float
        assert isinstance(score_none, float)

    def test_empty_content_scores_zero(self):
        score = _score_note("note.md", "", None)
        assert score == 0.0


# ── _extract_topic ────────────────────────────────────────────────────────────


class TestExtractTopic:
    def test_no_colon_returns_none(self):
        assert _extract_topic("Know-How Transfer #tool:knowledge-transfer") is None

    def test_extracts_after_colon(self):
        assert _extract_topic("Know-How Transfer: Bremsquitschen #tool:knowledge-transfer") == "Bremsquitschen"

    def test_extracts_multi_word_topic(self):
        assert _extract_topic("Transfer: FEM Bremse #tool:knowledge-transfer") == "FEM Bremse"

    def test_empty_after_colon_returns_none(self):
        # "Knowledge Transfer: #tool:knowledge-transfer" → after colon → "" (tags stripped)
        assert _extract_topic("Knowledge Transfer: #tool:knowledge-transfer") is None

    def test_strips_cwd_tag(self):
        result = _extract_topic("Transfer: Simulation cwd:/some/path #tool:knowledge-transfer")
        assert result == "Simulation"

    def test_strips_quoted_cwd_tag_with_spaces(self):
        result = _extract_topic(
            'Transfer: Simulation cwd:"D:\\My Repo\\Project Root" #tool:knowledge-transfer'
        )
        assert result == "Simulation"

    def test_no_tags_plain_colon(self):
        assert _extract_topic("Topic: MyTopic") == "MyTopic"


# ── _make_slug ────────────────────────────────────────────────────────────────


class TestMakeSlug:
    def test_spaces_to_hyphens(self):
        assert _make_slug("Hello World") == "Hello-World"

    def test_special_chars_removed(self):
        slug = _make_slug("Hello! World? (test)")
        assert "!" not in slug
        assert "?" not in slug
        assert "(" not in slug

    def test_max_50_chars(self):
        long_title = "A" * 100
        assert len(_make_slug(long_title)) <= 50

    def test_leading_trailing_hyphens_stripped(self):
        slug = _make_slug("  ---hello---  ")
        assert not slug.startswith("-")
        assert not slug.endswith("-")

    def test_empty_string(self):
        assert _make_slug("") == ""


# ── _extract_note_title ───────────────────────────────────────────────────────


class TestExtractNoteTitle:
    def test_extracts_emoji_title(self):
        note = "---\ntags: [test]\n---\n\n# 💡 My Brilliant Idea\n\nContent here."
        assert _extract_note_title(note) == "My Brilliant Idea"

    def test_extracts_plain_h1_fallback(self):
        note = "# Plain Title\n\nContent."
        assert _extract_note_title(note) == "Plain Title"

    def test_default_when_no_title(self):
        assert _extract_note_title("No heading here at all") == "Knowledge-Transfer-Idee"

    def test_takes_first_heading(self):
        note = "# 💡 First\n\n# Second\n"
        assert _extract_note_title(note) == "First"

    def test_strips_whitespace(self):
        note = "#   💡   Spaced Title   \n"
        assert _extract_note_title(note) == "Spaced Title"


# ── _scan_vault ───────────────────────────────────────────────────────────────


class TestScanVault:
    def test_missing_vault_returns_error(self, tmp_path):
        missing = tmp_path / "nonexistent"
        with patch("tools.knowledge_transfer.VAULT_PATH", missing):
            result = _scan_vault(None, 50_000)
        assert "nicht gefunden" in result or "Vault" in result

    def test_empty_vault_no_md_files(self, tmp_path):
        (tmp_path / "folder").mkdir()
        with patch("tools.knowledge_transfer.VAULT_PATH", tmp_path):
            result = _scan_vault(None, 50_000)
        assert "Keine Notizen" in result

    def test_skips_short_notes(self, tmp_path):
        (tmp_path / "tiny.md").write_text("short", encoding="utf-8")
        with patch("tools.knowledge_transfer.VAULT_PATH", tmp_path):
            result = _scan_vault(None, 50_000)
        assert "Keine Notizen" in result

    def test_includes_substantial_notes(self, tmp_path):
        (tmp_path / "real.md").write_text(
            "# My deep note\n\n" + "Some technical content. " * 20,
            encoding="utf-8",
        )
        with patch("tools.knowledge_transfer.VAULT_PATH", tmp_path):
            result = _scan_vault(None, 50_000)
        assert "real.md" in result

    def test_excludes_system_dirs(self, tmp_path):
        system = tmp_path / "99_System"
        system.mkdir()
        (system / "system_note.md").write_text("x" * 200, encoding="utf-8")
        with patch("tools.knowledge_transfer.VAULT_PATH", tmp_path):
            result = _scan_vault(None, 50_000)
        assert "system_note.md" not in result

    def test_topic_boosts_matching_notes(self, tmp_path):
        (tmp_path / "brakes.md").write_text(
            "# Bremsen\n\nBrakes analysis with simulation. " * 10,
            encoding="utf-8",
        )
        (tmp_path / "unrelated.md").write_text(
            "# Cooking\n\nRecipes and food preparation. " * 10,
            encoding="utf-8",
        )
        with patch("tools.knowledge_transfer.VAULT_PATH", tmp_path):
            result = _scan_vault("brakes", 50_000)
        # brakes.md should appear before unrelated.md (higher score = listed first)
        assert result.index("brakes.md") < result.index("unrelated.md")

    def test_respects_max_chars(self, tmp_path):
        for i in range(10):
            (tmp_path / f"note{i}.md").write_text("x" * 500, encoding="utf-8")
        with patch("tools.knowledge_transfer.VAULT_PATH", tmp_path):
            result = _scan_vault(None, 1_000)
        assert len(result) <= 1_100  # small buffer for block headers


# ── Phase-failure classification ──────────────────────────────────────────────


class _FailingAtPhase:
    """Succeeds for the first `fail_on - 1` calls, then fails with `error`."""

    name = "claude"
    supports_sessions = False

    def __init__(self, fail_on: int, error: str):
        self._fail_on = fail_on
        self._error = error
        self._calls = 0

    def run(self, prompt, cwd=None, timeout=0, **kwargs):
        self._calls += 1
        if self._calls == self._fail_on:
            return RunResult(success=False, error=self._error)
        return RunResult(success=True, output=f"# Ergebnis Phase {self._calls}\n\nInhalt.")


@pytest.fixture
def _kt_env(tmp_path, monkeypatch):
    """Vault with one substantial note, no notifier traffic, no inter-step sleep."""
    (tmp_path / "note.md").write_text(
        "# Bremsen\n\n" + "Technical simulation content. " * 20, encoding="utf-8",
    )
    monkeypatch.setattr("tools.knowledge_transfer.VAULT_PATH", tmp_path)
    monkeypatch.setattr("tools.knowledge_transfer.TOOL_INTER_STEP_SLEEP_SEC", 0)
    monkeypatch.setattr("tools.knowledge_transfer.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.knowledge_transfer.notify_tool_done", lambda *a, **kw: None)
    return tmp_path


class TestPhaseFailureClassification:
    """knowledge-transfer must classify via providers/base, not with `retryable=True`.

    All three phase-failure returns hardcoded `retryable=True` and set no
    `error_code`. Unconditional retry means an auth_error or a model refusal
    re-parks the task on every poll forever — unbounded, and invisible at 03:00
    because nobody is watching. The missing code also left the taxonomy guessing
    from free-form prose.
    """

    @pytest.mark.parametrize("fail_on, iterations", [(1, 1), (2, 2), (3, 3)])
    def test_transient_provider_error_stays_retryable(self, _kt_env, fail_on, iterations):
        provider = _FailingAtPhase(fail_on, "rate_limit: 429 upstream")
        result = KnowledgeTransferTool().run("Transfer test", provider, cwd=str(_kt_env))
        assert result.success is False
        assert result.iterations == iterations
        assert result.error_code == "rate_limit"
        assert result.retryable is True

    @pytest.mark.parametrize("fail_on", [1, 2, 3])
    def test_permanent_provider_error_is_not_retried(self, _kt_env, fail_on):
        provider = _FailingAtPhase(fail_on, "auth_error: token expired")
        result = KnowledgeTransferTool().run("Transfer test", provider, cwd=str(_kt_env))
        assert result.success is False
        assert result.error_code == "auth_error"
        assert result.retryable is False, (
            "an auth error requeues the task on every poll forever"
        )

    @pytest.mark.parametrize("fail_on", [1, 2, 3])
    def test_model_refusal_is_terminal(self, _kt_env, fail_on):
        provider = _FailingAtPhase(fail_on, "model_refusal: prompt blocked (SAFETY)")
        result = KnowledgeTransferTool().run("Transfer test", provider, cwd=str(_kt_env))
        assert result.error_code == "model_refusal"
        assert result.retryable is False

    def test_free_form_stderr_yields_no_error_code_and_no_retry(self, _kt_env):
        provider = _FailingAtPhase(1, "Traceback (most recent call last): boom")
        result = KnowledgeTransferTool().run("Transfer test", provider, cwd=str(_kt_env))
        assert result.error_code == ""       # prose never becomes a taxonomy code
        assert result.retryable is False
        assert "boom" in result.error        # ...but it stays visible in `error`
