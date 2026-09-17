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
                 claude_pct=50.0, gemini_pct=50.0, codex_pct=50.0,
                 opencode_avail=True, opencode_pct=50.0):
    """Build a mock AllLimits object.

    opencode defaults to available=True — unlike openrouter/vibe it is NOT in
    _UNCAPPED_PROVIDERS, so _limits_ok("opencode", ...) reaches the real
    getattr(limits, "opencode").available branch instead of short-circuiting to
    True. Without this field any test that routes an #opencode tag through a
    registered-and-available provider would AttributeError here.
    """
    return SimpleNamespace(
        claude=SimpleNamespace(available=claude_avail, remaining_pct=claude_pct, error=None,
                               windows={}),
        gemini=SimpleNamespace(available=gemini_avail, remaining_pct=gemini_pct, error=None,
                               windows={}),
        codex=SimpleNamespace(available=codex_avail, remaining_pct=codex_pct, error=None,
                              windows={}),
        opencode=SimpleNamespace(available=opencode_avail, remaining_pct=opencode_pct, error=None,
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


# Since 2026-09-17 a bare uncapped tag needs an explicit authorisation: the
# suite-wide policy is empty (no allow-list resolved), and the forced branch now
# fails closed there like _allows() does. The documented route is the task tag.
_OR_OK = "#tool_providers:openrouter,claude,codex"
_VIBE_OK = "#tool_providers:vibe,claude,codex"


def test_openrouter_tag_selects_openrouter_when_registered(with_openrouter):
    limits = _make_limits()
    provider = select_provider(f"Check models #openrouter {_OR_OK}", limits)
    assert provider is not None
    assert provider.name == "openrouter"


def test_or_minimax_free_tag_selects_openrouter(with_openrouter):
    limits = _make_limits()
    provider = select_provider(f"Daily summary #or_minimax_free {_OR_OK}", limits)
    assert provider is not None
    assert provider.name == "openrouter"


def test_or_paid_flagship_tags_select_openrouter(with_openrouter):
    """All paid-flagship or_* tags resolve to openrouter."""
    limits = _make_limits()
    for tag in ("#or_glm", "#or_kimi", "#or_qwen", "#or_deepseek", "#or_minimax"):
        provider = select_provider(f"Task {tag} {_OR_OK}", limits)
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


# with_vibe / without_vibe live in tests/conftest.py, next to the opencode pair —
# three test files need them and vibe is registered conditionally, so a local copy
# is one more place for the two to drift apart.


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
        provider = select_provider(f"Second opinion {tag} {_VIBE_OK}", limits_)
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


# ---------------------------------------------------------------------------
# opencode routing — capped-but-opt-in: registered conditionally like Vibe,
# but NOT in _UNCAPPED_PROVIDERS (its cap is pollable, see limits.py) and NOT
# reviewer-only (it writes) — still in _NO_FALLBACK_PROVIDERS because the tag
# itself is the ask (avoid Claude quota, or the only ZDR path for customer
# code), not because of blast radius.
# ---------------------------------------------------------------------------


# ``with_opencode`` / ``without_opencode`` now live in tests/conftest.py — the
# queue-linter policy tests need the same registration guarantee, and two copies
# of a fixture whose whole job is hermeticity is exactly how one copy drifts.


def test_opencode_not_in_default_fallback_chain(with_opencode):
    """Untagged tasks must never route to opencode, even when it's registered —
    Stufe 3 (_PRIORITY membership) is explicitly not built."""
    limits_ = _make_limits()
    provider = select_provider("Fix a bug", limits_)
    assert provider is not None
    assert provider.name != "opencode"


def test_opencode_not_selected_when_all_others_unavailable(with_opencode):
    """opencode must NOT step in as a last resort when claude/codex are blocked —
    only an explicit tag can reach it."""
    limits_ = _make_limits(claude_avail=False, gemini_avail=False, codex_avail=False)
    assert select_provider("Fix a bug", limits_) is None


def test_opencode_tags_select_opencode_when_registered(with_opencode):
    limits_ = _make_limits()
    for tag in ("#opencode", "#opencode_deepseek", "#opencode_deepseek_long", "#opencode_glm"):
        provider = select_provider(f"Second opinion {tag}", limits_)
        assert provider is not None, f"No provider returned for {tag}"
        assert provider.name == "opencode", f"{tag} did not route to opencode"


def test_opencode_tag_does_not_degrade_into_another_provider(without_opencode):
    """Explicitly asking for opencode must never be silently answered by
    Claude/Codex — the tag itself is the ask (avoid Claude quota, or the only
    ZDR path). Without the CLI/config the task is parked, not escalated."""
    limits_ = _make_limits()
    assert select_provider("Second opinion #opencode", limits_) is None
    assert select_provider("Second opinion #opencode_deepseek", limits_) is None


def test_unregistered_opencode_does_not_park_untagged_tasks(without_opencode):
    """The guard is scoped to tasks that actually tag opencode."""
    limits_ = _make_limits()
    provider = select_provider("Fix a bug", limits_)
    assert provider is not None
    assert provider.name == "claude"


def test_explicit_executor_tag_still_wins_alongside_opencode(without_opencode):
    """#claude next to an inert #opencode is an explicit choice, not an escalation."""
    limits_ = _make_limits()
    provider = select_provider("Review this #claude #opencode", limits_)
    assert provider is not None
    assert provider.name == "claude"


def test_has_explicit_provider_tag_detects_opencode_tags():
    assert has_explicit_provider_tag("Review #opencode") is True
    assert has_explicit_provider_tag("Review #opencode_deepseek") is True
    assert has_explicit_provider_tag("Review #opencode_deepseek_long") is True
    assert has_explicit_provider_tag("Review #opencode_glm") is True


def test_opencode_bare_tag_not_confused_with_model_alias(with_opencode):
    r"""#opencode is a literal prefix of #opencode_deepseek — each must resolve to
    itself. Regression for the boundary MODEL_TAG_RE/_TAG_RE_BY_PROVIDER rely on
    ((?![\w-]) on the right, (?<!\S) on the left) — same class of bug as
    codex_5 vs codex_5_4."""
    from dispatcher import resolve_forced_provider

    bare = resolve_forced_provider("Task #opencode")
    aliased = resolve_forced_provider("Task #opencode_deepseek")
    assert bare is not None and bare.name == "opencode"
    assert aliased is not None and aliased.name == "opencode"

    limits_ = _make_limits()
    from config import model_id_for_provider
    # The bare tag must NOT force a model — only the aliased tag should.
    assert model_id_for_provider("opencode", "opencode") is None
    assert model_id_for_provider("opencode_deepseek", "opencode") is not None


def test_limits_ok_checks_opencode_capacity_unlike_openrouter_and_vibe():
    """opencode is NOT in _UNCAPPED_PROVIDERS — its own AllLimits.opencode.available
    must actually be consulted, unlike the unconditional True for openrouter/vibe."""
    from dispatcher import _limits_ok
    available = _make_limits(opencode_avail=True)
    unavailable = _make_limits(opencode_avail=False)
    assert _limits_ok("opencode", available) is True
    assert _limits_ok("opencode", unavailable) is False


def test_opencode_not_in_priority():
    """Stufe 3 (_PRIORITY membership) is explicitly not built — the hang cause
    against opencode is unmeasured, and _PRIORITY is exactly where an unattended
    run would hit it without a tag asking for it."""
    from dispatcher import _PRIORITY
    assert "opencode" not in _PRIORITY


def test_opencode_not_in_uncapped_providers():
    """opencode IS pay-per-token but its cap is pollable (own OpenRouter key,
    live-polled $/day limit via openrouter_budget.fetch_budget()) — unlike
    openrouter/vibe it does not need the unconditional _limits_ok() True nor the
    fail-closed _allows() default, so it must stay out of this set."""
    from dispatcher import _UNCAPPED_PROVIDERS
    assert "opencode" not in _UNCAPPED_PROVIDERS
    assert _UNCAPPED_PROVIDERS == frozenset({"openrouter", "vibe"})


def test_opencode_in_no_fallback_providers():
    from dispatcher import _NO_FALLBACK_PROVIDERS
    assert "opencode" in _NO_FALLBACK_PROVIDERS
    assert "vibe" in _NO_FALLBACK_PROVIDERS


def test_park_log_names_the_specific_no_fallback_provider(without_vibe, without_opencode, capsys):
    """The park log line must name which provider is missing — 'vibe fehlt' and
    'opencode fehlt' need to be distinguishable in an unattended 03:00 log."""
    limits_ = _make_limits()

    select_provider("Second opinion #vibe", limits_)
    vibe_out = capsys.readouterr().out
    assert "vibe" in vibe_out
    assert "opencode" not in vibe_out

    select_provider("Second opinion #opencode", limits_)
    opencode_out = capsys.readouterr().out
    assert "opencode" in opencode_out


def test_park_log_also_reaches_the_logger_not_only_stdout(without_opencode, caplog):
    """capsys alone pins the WRONG channel for the promise this line makes.

    The line exists to be readable "in an unattended run", and run_orchestrator.ps1
    starts --watch with no stdout redirection — so a print-only version is exactly
    absent where it was needed, and the morning shows the generic "alle Provider
    voll" message instead. Same trap CLAUDE.md documents for
    queue_manager.collect_file_context.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="dispatcher"):
        select_provider("Second opinion #opencode", _make_limits())

    assert any("opencode" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Forced-tag branch fails closed under a missing policy (2026-09-17)
#
# Until then _selection_order() / forced_provider_policy_violation() only
# checked `allowed and name not in allowed`, so with NO allow-list resolved a
# bare #openrouter/#vibe tag was prepended and started — the one path the
# fail-closed rule of dispatcher._allows() never reached.
# ---------------------------------------------------------------------------

@pytest.fixture
def missing_policy(tmp_path, monkeypatch):
    """A PolicyEngine pointed at a vault with NO policy.yaml on disk."""
    import policy as policy_module

    engine = policy_module.PolicyEngine(vault_path=tmp_path / "no_policy_vault")
    monkeypatch.setattr(policy_module, "_engine", engine)
    assert not engine.config_path.exists()
    return engine


@pytest.mark.parametrize("tag", ["#openrouter", "#or_glm", "#or_minimax_free"])
@pytest.mark.parametrize("strict", [False, True])
def test_bare_openrouter_tag_is_barred_under_missing_policy(
    with_openrouter, missing_policy, tag, strict,
):
    assert select_provider(f"Task {tag}", _make_limits(), strict=strict) is None


@pytest.mark.parametrize("tag", ["#vibe", "#vibe_medium"])
@pytest.mark.parametrize("strict", [False, True])
def test_bare_vibe_tag_is_barred_under_missing_policy(with_vibe, missing_policy, tag, strict):
    assert select_provider(f"Review {tag}", _make_limits(), strict=strict) is None


def test_selection_order_is_empty_for_uncapped_forced_tag_without_allow_list(
    with_vibe, with_openrouter, monkeypatch,
):
    """Same case via the patched resolver — `allowed is None`, not just 'no file'."""
    import dispatcher

    monkeypatch.setattr(dispatcher, "_allowed_by_policy", lambda *a, **kw: None)
    assert _selection_order("x #vibe", None, None, False, None) == ([], None)
    assert _selection_order("x #openrouter", None, None, False, None) == ([], None)


def test_forced_violation_reports_uncapped_tag_under_missing_policy(
    with_vibe, with_openrouter, missing_policy,
):
    from dispatcher import forced_provider_policy_violation

    for tag, name in (("#vibe", "vibe"), ("#or_kimi", "openrouter")):
        violation = forced_provider_policy_violation(f"Task {tag}")
        assert violation is not None, tag
        got_name, shown = violation
        assert got_name == name
        # The effective list, never an empty "erlaubt:" and never an uncapped name.
        assert "claude" in shown and "codex" in shown
        assert "vibe" not in shown and "openrouter" not in shown


@pytest.mark.parametrize("tag,name", [("#claude", "claude"), ("#codex", "codex")])
def test_capped_forced_tags_unchanged_under_missing_policy(missing_policy, tag, name):
    from dispatcher import forced_provider_policy_violation

    order, allowed = _selection_order(f"Task {tag}", None, None, False, None)
    assert allowed is None
    assert order[0] == name
    assert forced_provider_policy_violation(f"Task {tag}") is None
    assert select_provider(f"Task {tag}", _make_limits()).name == name


def test_opencode_forced_tag_stays_fail_open_under_missing_policy(with_opencode, missing_policy):
    """opencode is capped (pollable OpenRouter key budget) — not in _UNCAPPED_PROVIDERS."""
    from dispatcher import forced_provider_policy_violation

    order, _ = _selection_order("Task #opencode", None, None, False, None)
    assert order[0] == "opencode"
    assert forced_provider_policy_violation("Task #opencode") is None


def test_explicit_task_authorisation_still_routes_uncapped_tag(
    with_vibe, with_openrouter, missing_policy,
):
    from dispatcher import forced_provider_policy_violation

    assert select_provider(f"Review #vibe {_VIBE_OK}", _make_limits()).name == "vibe"
    assert select_provider(f"Task #or_glm {_OR_OK}", _make_limits()).name == "openrouter"
    assert forced_provider_policy_violation(f"Review #vibe {_VIBE_OK}") is None


def test_barred_uncapped_tag_logs_effective_allow_list(with_vibe, missing_policy, capsys):
    assert select_provider("Review #vibe", _make_limits()) is None
    out = capsys.readouterr().out
    assert "[policy] Provider 'vibe'" in out
    assert "kein Fallback" in out
    line = next(ln for ln in out.splitlines() if "[policy]" in ln)
    erlaubt = line.split("erlaubt:")[1].split(")")[0]
    assert "claude" in erlaubt and "vibe" not in erlaubt
