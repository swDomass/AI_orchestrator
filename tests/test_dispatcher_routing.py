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
