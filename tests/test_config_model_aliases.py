"""Tests for provider-bound model alias resolution in config.py."""

from config import (
    CLAUDE_MODEL_ALIASES,
    CODEX_MODEL_ALIASES,
    GEMINI_MODEL_ALIASES,
    OPENROUTER_MODEL_ALIASES,
    is_known_model_tag,
    model_id_for_provider,
)


def test_claude_tags_resolve_for_claude():
    assert model_id_for_provider("claude_haiku", "claude") == CLAUDE_MODEL_ALIASES["claude_haiku"]
    assert model_id_for_provider("claude_sonnet", "claude") == CLAUDE_MODEL_ALIASES["claude_sonnet"]
    assert model_id_for_provider("claude_opus", "claude") == CLAUDE_MODEL_ALIASES["claude_opus"]


def test_gemini_tags_resolve_for_gemini():
    assert model_id_for_provider("gemini_flash", "gemini") == "gemini-3-flash-preview"
    assert model_id_for_provider("gemini_pro", "gemini") == "gemini-3.1-pro-preview"
    assert model_id_for_provider("gemini_flash_lite", "gemini") == "gemini-3.1-flash-lite-preview"


def test_codex_tags_resolve_for_codex():
    assert model_id_for_provider("codex_mini", "codex") == "gpt-5.4-mini"
    assert model_id_for_provider("codex_5", "codex") == "gpt-5.5"
    assert model_id_for_provider("codex_5_4", "codex") == "gpt-5.4"


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
    # Verified against `cclimits --json` (2026-05-08): user's Gemini Code Assist tier
    # exposes gemini-3.1-pro-preview, gemini-3-flash-preview, gemini-3.1-flash-lite-preview.
    # gemini-3.1-flash (non-lite) does not exist, so gemini_flash maps to 3.0.
    assert GEMINI_MODEL_ALIASES["gemini_pro"] == "gemini-3.1-pro-preview"
    assert GEMINI_MODEL_ALIASES["gemini_flash"] == "gemini-3-flash-preview"
    assert GEMINI_MODEL_ALIASES["gemini_flash_lite"] == "gemini-3.1-flash-lite-preview"


def test_codex_aliases_match_verified_model_cache():
    assert CODEX_MODEL_ALIASES["codex_mini"] == "gpt-5.4-mini"
    assert CODEX_MODEL_ALIASES["codex_5"] == "gpt-5.5"
    assert CODEX_MODEL_ALIASES["codex_5_4"] == "gpt-5.4"


# ---------------------------------------------------------------------------
# OpenRouter aliases (HTTP provider, pay-per-token)
# ---------------------------------------------------------------------------


def test_openrouter_free_aliases_resolve_for_openrouter():
    assert model_id_for_provider("or_minimax_free", "openrouter") == "minimax/minimax-m2.5:free"
    assert model_id_for_provider("or_deepseek_free", "openrouter") == "deepseek/deepseek-v4-flash:free"
    assert model_id_for_provider("or_qwen_free", "openrouter") == "qwen/qwen3-coder:free"
    assert model_id_for_provider("or_nemotron_free", "openrouter") == "nvidia/nemotron-3-super-120b-a12b:free"


def test_openrouter_paid_aliases_resolve_for_openrouter():
    assert model_id_for_provider("or_glm", "openrouter") == "z-ai/glm-5"
    assert model_id_for_provider("or_kimi", "openrouter") == "moonshotai/kimi-k2.6"
    assert model_id_for_provider("or_qwen", "openrouter") == "qwen/qwen3-max"
    assert model_id_for_provider("or_deepseek", "openrouter") == "deepseek/deepseek-v4-pro"
    assert model_id_for_provider("or_minimax", "openrouter") == "minimax/minimax-m2.7"


def test_openrouter_aliases_blocked_on_other_providers():
    # An or_* tag must never resolve against claude/gemini/codex CLI
    assert model_id_for_provider("or_minimax_free", "claude") is None
    assert model_id_for_provider("or_glm", "gemini") is None
    assert model_id_for_provider("or_kimi", "codex") is None


def test_native_aliases_blocked_on_openrouter():
    # Claude/Gemini/Codex tags must never resolve against OpenRouter
    assert model_id_for_provider("claude_opus", "openrouter") is None
    assert model_id_for_provider("gemini_pro", "openrouter") is None
    assert model_id_for_provider("codex_5", "openrouter") is None


def test_is_known_model_tag_recognises_openrouter():
    assert is_known_model_tag("or_minimax_free") is True
    assert is_known_model_tag("or_glm") is True
    assert is_known_model_tag("or_unknown_made_up") is False


def test_openrouter_alias_count():
    # Sanity: drift-detection — if the dict shrinks/grows unexpectedly the
    # tag map in dispatcher.py probably needs updating too.
    assert len(OPENROUTER_MODEL_ALIASES) == 9
