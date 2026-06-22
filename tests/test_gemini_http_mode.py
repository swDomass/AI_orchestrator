"""HTTP-API-mode behaviour for Gemini across dispatcher + limits.

When GEMINI_API_KEY is set the provider talks to the Gemini REST API, which has
no pollable subscription quota. These tests pin the resulting cross-module
contract: Gemini is treated as available (cooldown-driven) and the dead
CLI/OAuth refresh + cclimits quota are bypassed.

The autouse conftest fixture clears GEMINI_API_KEY by default, so each test here
sets it explicitly to opt into HTTP mode.
"""

import dispatcher
import limits
from limits import AllLimits, ProviderLimits


# --------------------------------------------------------------- dispatcher


def test_limits_ok_gemini_available_with_key_even_if_quota_empty(monkeypatch):
    monkeypatch.setattr("config.GEMINI_API_KEY", "k")
    all_limits = AllLimits(
        claude=ProviderLimits(available=False),
        gemini=ProviderLimits(available=False, error="token expired"),
        codex=ProviderLimits(available=False),
    )
    assert dispatcher._limits_ok("gemini", all_limits) is True


def test_limits_ok_gemini_follows_quota_without_key(monkeypatch):
    monkeypatch.setattr("config.GEMINI_API_KEY", "")
    all_limits = AllLimits(gemini=ProviderLimits(available=False))
    assert dispatcher._limits_ok("gemini", all_limits) is False
    all_limits = AllLimits(gemini=ProviderLimits(available=True))
    assert dispatcher._limits_ok("gemini", all_limits) is True


# --------------------------------------------------------------- limits


def test_needs_token_refresh_skipped_for_gemini_with_key(monkeypatch):
    monkeypatch.setattr("config.GEMINI_API_KEY", "k")
    expired = {"gemini": {"status": "error", "token_status": "expired"}}
    assert limits._needs_token_refresh(expired, "gemini") is False


def test_needs_token_refresh_active_for_gemini_without_key(monkeypatch):
    monkeypatch.setattr("config.GEMINI_API_KEY", "")
    expired = {"gemini": {"status": "error", "token_status": "expired"}}
    assert limits._needs_token_refresh(expired, "gemini") is True


def test_claude_refresh_unaffected_by_gemini_key(monkeypatch):
    """The Gemini short-circuit must not touch Claude's refresh detection."""
    monkeypatch.setattr("config.GEMINI_API_KEY", "k")
    expired = {"claude": {"status": "error", "token_status": "expired"}}
    assert limits._needs_token_refresh(expired, "claude") is True


def test_http_override_replaces_gemini_when_key_set(monkeypatch):
    monkeypatch.setattr("config.GEMINI_API_KEY", "k")
    result = AllLimits(gemini=ProviderLimits(available=False, error="token expired"))
    out = limits._apply_gemini_http_override(result)
    assert out.gemini.available is True
    assert out.gemini.remaining_pct == 100.0
    assert out.gemini.error == ""


def test_http_override_noop_without_key(monkeypatch):
    monkeypatch.setattr("config.GEMINI_API_KEY", "")
    result = AllLimits(gemini=ProviderLimits(available=False, error="token expired"))
    out = limits._apply_gemini_http_override(result)
    assert out.gemini.available is False
    assert out.gemini.error == "token expired"


def test_providers_with_429_excludes_gemini_with_key(monkeypatch):
    """A cclimits 429 for Gemini must be ignored in HTTP mode so it doesn't drive
    retry sleeps / fallback / notifications for a provider that bypasses cclimits."""
    raw = {
        "claude": {"status": "error", "error": "HTTP 429"},
        "gemini": {"status": "error", "error": "HTTP 429"},
        "codex": {"status": "ok"},
    }
    monkeypatch.setattr("config.GEMINI_API_KEY", "k")
    assert limits._providers_with_429(raw) == {"claude"}
    monkeypatch.setattr("config.GEMINI_API_KEY", "")
    assert limits._providers_with_429(raw) == {"claude", "gemini"}
