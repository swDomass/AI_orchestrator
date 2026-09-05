"""Tests for provider-bound model alias resolution in config.py."""

from config import (
    CLAUDE_MODEL_ALIASES,
    CODEX_MODEL_ALIASES,
    GEMINI_MODEL_ALIASES,
    OPENCODE_MODEL_ALIASES,
    OPENROUTER_MODEL_ALIASES,
    is_known_model_tag,
    model_id_for_provider,
)


def test_claude_tags_resolve_for_claude():
    assert model_id_for_provider("claude_haiku", "claude") == CLAUDE_MODEL_ALIASES["claude_haiku"]
    assert model_id_for_provider("claude_sonnet", "claude") == CLAUDE_MODEL_ALIASES["claude_sonnet"]
    assert model_id_for_provider("claude_opus", "claude") == CLAUDE_MODEL_ALIASES["claude_opus"]


def test_gemini_tags_resolve_for_gemini():
    assert model_id_for_provider("gemini_flash", "gemini") == "gemini-3.5-flash"
    assert model_id_for_provider("gemini_pro", "gemini") == "gemini-3.1-pro-preview"
    assert model_id_for_provider("gemini_flash_lite", "gemini") == "gemini-3.5-flash-lite"


def test_codex_tags_resolve_for_codex():
    assert model_id_for_provider("codex_mini", "codex") == "gpt-5.6-luna"
    assert model_id_for_provider("codex_5", "codex") == "gpt-5.6-sol"
    assert model_id_for_provider("codex_5_4", "codex") == "gpt-5.6-terra"


def test_vibe_tags_resolve_for_vibe():
    # Values are vibe's own config aliases (`active_model`), not raw model names —
    # the provider passes them through VIBE_ACTIVE_MODEL and vibe resolves them.
    assert model_id_for_provider("vibe_medium", "vibe") == "mistral-medium-3.5"
    assert model_id_for_provider("vibe_small", "vibe") == "devstral-small"


def test_vibe_tags_do_not_leak_to_other_providers():
    assert model_id_for_provider("vibe_medium", "claude") is None
    assert model_id_for_provider("claude_opus", "vibe") is None


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


def test_gemini_aliases_match_current_ids():
    # Drift-checked 2026-07-23 against Google's deprecations page + the live
    # /v1beta/models listing: still no GA pro model, so pro stays on the 3.1 preview.
    # gemini-3.1-flash-lite is deprecated (shutdown 2027-05-07) → replaced by its named
    # successor gemini-3.5-flash-lite (GA 2026-07-21). gemini-3.5-flash is not
    # deprecated and stays; gemini-3.6-flash (GA 2026-07-21) is a watch item.
    assert GEMINI_MODEL_ALIASES["gemini_pro"] == "gemini-3.1-pro-preview"
    assert GEMINI_MODEL_ALIASES["gemini_flash"] == "gemini-3.5-flash"
    assert GEMINI_MODEL_ALIASES["gemini_flash_lite"] == "gemini-3.5-flash-lite"


def test_codex_aliases_match_verified_model_cache():
    # GPT-5.6 family (2026-07-09) replaced gpt-5.5/gpt-5.4; Codex CLI 0.145.0 migrated
    # its bundled selections to Terra/Luna. All three IDs probed live on 2026-07-23.
    assert CODEX_MODEL_ALIASES["codex_mini"] == "gpt-5.6-luna"
    assert CODEX_MODEL_ALIASES["codex_5"] == "gpt-5.6-sol"
    assert CODEX_MODEL_ALIASES["codex_5_4"] == "gpt-5.6-terra"


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


# ---------------------------------------------------------------------------
# opencode aliases (tag-activated third external voice, Stufe 2, pay-per-token
# but capped via its own OpenRouter key — see limits.py)
# ---------------------------------------------------------------------------


def test_opencode_tags_resolve_for_opencode():
    assert model_id_for_provider("opencode_deepseek", "opencode") == OPENCODE_MODEL_ALIASES["opencode_deepseek"]
    assert model_id_for_provider("opencode_deepseek_long", "opencode") == OPENCODE_MODEL_ALIASES["opencode_deepseek_long"]
    assert model_id_for_provider("opencode_glm", "opencode") == OPENCODE_MODEL_ALIASES["opencode_glm"]


def test_opencode_aliases_are_zdr_review_variants():
    # Handpicked ZDR aliases only — zdr-auto-* (managed by oc_sync_zdr_aliases.py,
    # order changes over time) are deliberately not valid tag targets.
    assert OPENCODE_MODEL_ALIASES["opencode_deepseek"] == "openrouter/zdr-review"
    assert OPENCODE_MODEL_ALIASES["opencode_deepseek_long"] == "openrouter/zdr-review-long"
    assert OPENCODE_MODEL_ALIASES["opencode_glm"] == "openrouter/zdr-review-alt"


def test_opencode_tags_do_not_leak_to_other_providers():
    assert model_id_for_provider("opencode_deepseek", "claude") is None
    assert model_id_for_provider("claude_opus", "opencode") is None


def test_opencode_aliases_blocked_on_other_providers():
    assert model_id_for_provider("opencode_deepseek", "openrouter") is None
    assert model_id_for_provider("opencode_glm", "codex") is None


def test_native_aliases_blocked_on_opencode():
    assert model_id_for_provider("or_glm", "opencode") is None
    assert model_id_for_provider("vibe_medium", "opencode") is None


def test_is_known_model_tag_recognises_opencode():
    assert is_known_model_tag("opencode_deepseek") is True
    assert is_known_model_tag("opencode_deepseek_long") is True
    assert is_known_model_tag("opencode_glm") is True
    assert is_known_model_tag("opencode_kaputt") is False


def test_opencode_alias_count():
    # Sanity: drift-detection — if the dict shrinks/grows unexpectedly the
    # tag map in dispatcher.py probably needs updating too (same pattern as
    # test_openrouter_alias_count above).
    assert len(OPENCODE_MODEL_ALIASES) == 3
