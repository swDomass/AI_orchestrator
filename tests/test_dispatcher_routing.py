"""Tests for dispatcher.select_provider() routing logic."""

from unittest.mock import patch, PropertyMock
from types import SimpleNamespace

import pytest

import limits
from dispatcher import (
    select_provider,
    has_explicit_provider_tag,
    resolve_forced_provider,
    force_refresh_can_unblock,
    _providers,
    _selection_order,
)


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


def test_fallback_to_codex_when_claude_unavailable():
    """Gemini left the chain 2026-08-15 — codex is the only fallback left."""
    limits = _make_limits(claude_avail=False)
    provider = select_provider("Fix a bug", limits)
    assert provider is not None
    assert provider.name == "codex"


def test_gemini_never_in_default_chain():
    """Even with gemini healthy and claude exhausted, nothing routes there."""
    limits = _make_limits(claude_avail=False, gemini_avail=True, codex_avail=False)
    provider = select_provider("Fix a bug", limits)
    assert provider is None


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
    assert provider.name == "codex"


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


def test_resolve_forced_provider_via_model_tag():
    p = resolve_forced_provider("Morning brief #claude_sonnet")
    assert p is not None and p.name == "claude"


def test_resolve_forced_provider_via_force_name():
    p = resolve_forced_provider("Plain task", force_name="gemini")
    assert p is not None and p.name == "gemini"


def test_resolve_forced_provider_none_for_plain_task():
    assert resolve_forced_provider("Fix the login bug") is None


def test_force_refresh_can_unblock_strict_ignores_unrelated_expired_provider():
    """Codex P2: strict #claude_sonnet with claude GENUINELY exhausted (reset known)
    while an unrelated provider's token is expired must NOT trigger a force_refresh —
    a refresh can't unblock the only routable provider."""
    all_limits = limits.AllLimits(
        claude=limits.ProviderLimits(available=False, remaining_pct=0.0, resets_in_sec=3600),
        gemini=limits.ProviderLimits(available=False, error="token expired"),
        codex=limits.ProviderLimits(available=False, error="token expired"),
    )
    assert force_refresh_can_unblock(
        "Morning brief #claude_sonnet", all_limits, strict=True
    ) is False


def test_force_refresh_can_unblock_strict_true_when_forced_provider_expired():
    all_limits = limits.AllLimits(
        claude=limits.ProviderLimits(available=False, error="token expired"),
        gemini=limits.ProviderLimits(available=True, remaining_pct=100.0),
        codex=limits.ProviderLimits(available=True, remaining_pct=100.0),
    )
    assert force_refresh_can_unblock(
        "Morning brief #claude_sonnet", all_limits, strict=True
    ) is True


def test_force_refresh_can_unblock_non_strict_checks_any_provider():
    """Non-forced task: claude exhausted but gemini expired → a refresh could open
    the gemini fallback, so it SHOULD be attempted."""
    all_limits = limits.AllLimits(
        claude=limits.ProviderLimits(available=False, remaining_pct=0.0, resets_in_sec=3600),
        gemini=limits.ProviderLimits(available=False, error="token expired"),
        codex=limits.ProviderLimits(available=False, remaining_pct=0.0, resets_in_sec=3600),
    )
    assert force_refresh_can_unblock("Fix a bug", all_limits, strict=False) is True


def test_force_refresh_can_unblock_false_when_nothing_transient():
    all_limits = limits.AllLimits(
        claude=limits.ProviderLimits(available=False, remaining_pct=0.0, resets_in_sec=3600),
        gemini=limits.ProviderLimits(available=False, remaining_pct=0.0, resets_in_sec=3600),
        codex=limits.ProviderLimits(available=False, remaining_pct=0.0, resets_in_sec=3600),
    )
    assert force_refresh_can_unblock("Fix a bug", all_limits, strict=False) is False


# ---------------------------------------------------------------------------
# Vibe routing — same opt-in contract as OpenRouter: registered but never a fallback
# ---------------------------------------------------------------------------


@pytest.fixture
def with_vibe():
    """Register Vibe in dispatcher._providers regardless of local CLI presence."""
    import dispatcher
    from providers.vibe import VibeProvider

    had_it = "vibe" in dispatcher._providers
    if not had_it:
        dispatcher._providers["vibe"] = VibeProvider()
    yield dispatcher._providers["vibe"]
    if not had_it:
        dispatcher._providers.pop("vibe", None)


@pytest.fixture
def without_vibe():
    import dispatcher

    saved = dispatcher._providers.pop("vibe", None)
    yield
    if saved is not None:
        dispatcher._providers["vibe"] = saved


def test_vibe_not_in_default_fallback_chain(with_vibe):
    """Untagged tasks must never route to Vibe — it is a reviewer, not an executor."""
    limits_ = _make_limits()
    provider = select_provider("Fix a bug", limits_)
    assert provider is not None
    assert provider.name != "vibe"


def test_vibe_not_selected_when_all_others_unavailable(with_vibe):
    """Vibe must NOT step in as a last resort when claude/gemini/codex are blocked."""
    limits_ = _make_limits(claude_avail=False, gemini_avail=False, codex_avail=False)
    assert select_provider("Fix a bug", limits_) is None


def test_vibe_tags_select_vibe_when_registered(with_vibe):
    limits_ = _make_limits()
    for tag in ("#vibe", "#vibe_medium", "#vibe_small"):
        provider = select_provider(f"Second opinion {tag}", limits_)
        assert provider is not None, f"No provider returned for {tag}"
        assert provider.name == "vibe", f"{tag} did not route to vibe"


def test_vibe_tag_does_not_degrade_into_an_executor(without_vibe):
    """Explicitly asking for the non-writing reviewer must never be answered with
    a file-writing executor. Without the CLI the task is parked, not escalated —
    unlike an unregistered #or_* tag, where executor → executor is harmless."""
    limits_ = _make_limits()
    assert select_provider("Second opinion #vibe", limits_) is None
    assert select_provider("Second opinion #vibe_small", limits_) is None


def test_unregistered_vibe_does_not_park_untagged_tasks(without_vibe):
    """The guard is scoped to tasks that actually tag vibe."""
    limits_ = _make_limits()
    provider = select_provider("Fix a bug", limits_)
    assert provider is not None
    assert provider.name == "claude"


def test_explicit_executor_tag_still_wins_alongside_vibe(without_vibe):
    """#claude next to an inert #vibe is an explicit choice, not an escalation."""
    limits_ = _make_limits()
    provider = select_provider("Review this #claude #vibe", limits_)
    assert provider is not None
    assert provider.name == "claude"


def test_has_explicit_provider_tag_detects_vibe_tags():
    assert has_explicit_provider_tag("Review #vibe") is True
    assert has_explicit_provider_tag("Review #vibe_medium") is True


def test_limits_ok_returns_true_for_vibe():
    """Pay-per-token via Mistral's API — no cclimits quota to gate on."""
    from dispatcher import _limits_ok
    limits_ = _make_limits(claude_avail=False, gemini_avail=False, codex_avail=False)
    assert _limits_ok("vibe", limits_) is True


def test_profile_provider_order_fails_closed_on_uncapped_provider(with_vibe):
    """A profile naming vibe/openrouter must clear the same fail-closed gate as the
    global/task-level policy layers — the profile branch of _selection_order() used
    to only check registration (`p in _providers`), never _allows(), so a profile
    `providers: [claude, vibe]` reached vibe whenever no tool_providers policy was
    configured (allowed=None), which is the normal state of an installation without
    a configured ceiling, not a corruption case. _isolate_policy_engine (conftest)
    points at an empty vault, so `allowed` here is None exactly like that scenario.
    """
    profile = SimpleNamespace(providers=["claude", "vibe"],
                              tool_providers={}, allowed_skills=[], denied_skills=[])
    order, allowed = _selection_order("Do it", profile, None, False, None)
    assert allowed is None
    assert order == ["claude"]
