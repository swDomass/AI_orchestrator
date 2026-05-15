"""Tests for dispatcher.select_provider() routing logic."""

from unittest.mock import patch, PropertyMock
from types import SimpleNamespace

import pytest

from dispatcher import select_provider, has_explicit_provider_tag, _providers


def _make_limits(claude_avail=True, gemini_avail=True, codex_avail=True,
                 claude_pct=50.0, gemini_pct=50.0, codex_pct=50.0):
    """Build a mock AllLimits object."""
    return SimpleNamespace(
        claude=SimpleNamespace(available=claude_avail, remaining_pct=claude_pct, error=None,
                               windows={}),
        gemini=SimpleNamespace(available=gemini_avail, remaining_pct=gemini_pct, error=None,
                               windows={}),
        codex=SimpleNamespace(available=codex_avail, remaining_pct=codex_pct, error=None,
                              windows={}),
    )


def test_default_priority_selects_claude():
    limits = _make_limits()
    provider = select_provider("Fix a bug", limits)
    assert provider is not None
    assert provider.name == "claude"


def test_fallback_to_gemini_when_claude_unavailable():
    limits = _make_limits(claude_avail=False)
    provider = select_provider("Fix a bug", limits)
    assert provider is not None
    assert provider.name == "gemini"


def test_fallback_to_codex_when_claude_and_gemini_unavailable():
    limits = _make_limits(claude_avail=False, gemini_avail=False)
    provider = select_provider("Fix a bug", limits)
    assert provider is not None
    assert provider.name == "codex"


def test_returns_none_when_all_unavailable():
    limits = _make_limits(claude_avail=False, gemini_avail=False, codex_avail=False)
    provider = select_provider("Fix a bug", limits)
    assert provider is None


def test_forced_provider_via_tag():
    limits = _make_limits()
    provider = select_provider("Fix a bug #gemini", limits)
    assert provider is not None
    assert provider.name == "gemini"


def test_forced_provider_with_strict_no_fallback():
    limits = _make_limits(gemini_avail=False)
    provider = select_provider("Fix a bug", limits, force_name="gemini", strict=True)
    assert provider is None  # strict: no fallback


def test_has_explicit_provider_tag_detects_claude():
    assert has_explicit_provider_tag("Fix bug #claude") is True


def test_has_explicit_provider_tag_false_on_plain_text():
    assert has_explicit_provider_tag("Fix the login bug") is False


def test_exclude_provider():
    limits = _make_limits()
    provider = select_provider("Fix bug", limits, exclude={"claude"})
    assert provider is not None
    assert provider.name == "gemini"


def test_profile_provider_order():
    limits = _make_limits()
    profile = SimpleNamespace(providers=["codex", "gemini", "claude"],
                              tool_providers={}, allowed_skills=[], denied_skills=[])
    provider = select_provider("Fix bug", limits, profile=profile)
    assert provider is not None
    assert provider.name == "codex"


def test_gemini_flash_tag_selects_gemini():
    limits = _make_limits()
    provider = select_provider("Iterate #gemini_flash", limits)
    assert provider is not None
    assert provider.name == "gemini"


def test_gemini_pro_tag_selects_gemini():
    limits = _make_limits()
    provider = select_provider("Review #gemini_pro", limits)
    assert provider is not None
    assert provider.name == "gemini"


def test_codex_mini_tag_selects_codex():
    limits = _make_limits()
    provider = select_provider("Run #codex_mini", limits)
    assert provider is not None
    assert provider.name == "codex"


def test_has_explicit_provider_tag_detects_new_model_tags():
    assert has_explicit_provider_tag("Do thing #gemini_flash") is True
    assert has_explicit_provider_tag("Do thing #gemini_pro") is True
    assert has_explicit_provider_tag("Do thing #codex_mini") is True


# ---------------------------------------------------------------------------
# OpenRouter routing — never in fallback chain, only via explicit tag
# ---------------------------------------------------------------------------


@pytest.fixture
def with_openrouter():
    """Register OpenRouter in dispatcher._providers for the duration of a test."""
    import dispatcher
    from providers.openrouter import OpenRouterProvider

    had_it = "openrouter" in dispatcher._providers
    if not had_it:
        dispatcher._providers["openrouter"] = OpenRouterProvider()
    yield dispatcher._providers["openrouter"]
    if not had_it:
        dispatcher._providers.pop("openrouter", None)


@pytest.fixture
def without_openrouter():
    """Ensure OpenRouter is NOT in dispatcher._providers for the duration of a test."""
    import dispatcher

    saved = dispatcher._providers.pop("openrouter", None)
    yield
    if saved is not None:
        dispatcher._providers["openrouter"] = saved


def test_openrouter_not_in_default_fallback_chain(with_openrouter):
    """Untagged tasks must never route to OpenRouter, even when it's registered."""
    limits = _make_limits()
    provider = select_provider("Fix a bug", limits)
    assert provider is not None
    assert provider.name != "openrouter"


def test_openrouter_not_selected_when_all_others_unavailable(with_openrouter):
    """OpenRouter must NOT step in as a fallback when claude/gemini/codex are blocked."""
    limits = _make_limits(claude_avail=False, gemini_avail=False, codex_avail=False)
    provider = select_provider("Fix a bug", limits)
    assert provider is None  # explicitly do NOT fall through to openrouter


def test_openrouter_tag_selects_openrouter_when_registered(with_openrouter):
    limits = _make_limits()
    provider = select_provider("Check models #openrouter", limits)
    assert provider is not None
    assert provider.name == "openrouter"


def test_or_minimax_free_tag_selects_openrouter(with_openrouter):
    limits = _make_limits()
    provider = select_provider("Daily summary #or_minimax_free", limits)
    assert provider is not None
    assert provider.name == "openrouter"


def test_or_paid_flagship_tags_select_openrouter(with_openrouter):
    """All paid-flagship or_* tags resolve to openrouter."""
    limits = _make_limits()
    for tag in ("#or_glm", "#or_kimi", "#or_qwen", "#or_deepseek", "#or_minimax"):
        provider = select_provider(f"Task {tag}", limits)
        assert provider is not None, f"No provider returned for {tag}"
        assert provider.name == "openrouter", f"{tag} did not route to openrouter"


def test_or_tag_falls_back_when_openrouter_unregistered(without_openrouter):
    """Without OPENROUTER_API_KEY (unregistered), tagged tasks fall through to claude."""
    limits = _make_limits()
    provider = select_provider("Daily summary #or_minimax_free", limits)
    assert provider is not None
    assert provider.name == "claude"


def test_has_explicit_provider_tag_detects_openrouter_tags():
    assert has_explicit_provider_tag("Check #openrouter") is True
    assert has_explicit_provider_tag("Check #or_minimax_free") is True
    assert has_explicit_provider_tag("Check #or_glm") is True


def test_limits_ok_returns_true_for_openrouter():
    """OpenRouter is pay-per-token — no quota gating via cclimits."""
    from dispatcher import _limits_ok
    limits = _make_limits(claude_avail=False, gemini_avail=False, codex_avail=False)
    assert _limits_ok("openrouter", limits) is True


def test_limits_ok_still_checks_native_providers():
    """Special-case for openrouter must not break native provider gating."""
    from dispatcher import _limits_ok
    limits = _make_limits(claude_avail=False)
    assert _limits_ok("claude", limits) is False
    assert _limits_ok("gemini", limits) is True
