"""Tests for provider-bound model alias resolution in config.py."""

from config import (
    CLAUDE_MODEL_ALIASES,
    CODEX_MODEL_ALIASES,
    GEMINI_MODEL_ALIASES,
    is_known_model_tag,
    model_id_for_provider,
)


def test_claude_tags_resolve_for_claude():
    assert model_id_for_provider("claude_haiku", "claude") == CLAUDE_MODEL_ALIASES["claude_haiku"]
    assert model_id_for_provider("claude_sonnet", "claude") == CLAUDE_MODEL_ALIASES["claude_sonnet"]
    assert model_id_for_provider("claude_opus", "claude") == CLAUDE_MODEL_ALIASES["claude_opus"]


def test_gemini_tags_resolve_for_gemini():
    assert model_id_for_provider("gemini_flash", "gemini") == "gemini-3-flash-preview"
    assert model_id_for_provider("gemini_pro", "gemini") == "gemini-3-pro-preview"


def test_codex_tags_resolve_for_codex():
    assert model_id_for_provider("codex_mini", "codex") == "gpt-5.4-mini"


def test_cross_provider_mismatch_returns_none():
    # Claude tag on Gemini provider → None (prevents --model claude-opus-4-6 on gemini CLI)
    assert model_id_for_provider("claude_opus", "gemini") is None
    assert model_id_for_provider("gemini_flash", "claude") is None
    assert model_id_for_provider("codex_mini", "gemini") is None


def test_none_tag_returns_none():
    assert model_id_for_provider(None, "claude") is None
    assert model_id_for_provider(None, "gemini") is None


def test_unknown_provider_returns_none():
    assert model_id_for_provider("claude_opus", "unknown") is None


def test_is_known_model_tag_matches_all_providers():
    assert is_known_model_tag("claude_haiku") is True
    assert is_known_model_tag("gemini_flash") is True
    assert is_known_model_tag("codex_mini") is True


def test_is_known_model_tag_rejects_unknown():
    assert is_known_model_tag("totally_made_up") is False
    assert is_known_model_tag(None) is False
    assert is_known_model_tag("") is False


def test_gemini_aliases_match_verified_preview_ids():
    assert GEMINI_MODEL_ALIASES["gemini_pro"] == "gemini-3-pro-preview"
    assert GEMINI_MODEL_ALIASES["gemini_flash"] == "gemini-3-flash-preview"


def test_codex_aliases_match_verified_model_cache():
    assert CODEX_MODEL_ALIASES["codex_mini"] == "gpt-5.4-mini"
