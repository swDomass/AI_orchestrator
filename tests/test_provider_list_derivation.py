"""Regression tests for the provider-list derivation (Stufe 1):

queue_manager.PROVIDER_TAG_RE and profiles._KNOWN_PROVIDERS are both derived from
dispatcher._TAG_MAP instead of hand-copied lists, so a provider added to _TAG_MAP
(vibe, openrouter, and any future provider) is picked up automatically instead of
silently falling through as a stray tag in the prompt / a silently-replaced profile.

Machart: tests/test_queue_manager_regressions.py::test_model_tag_re_covers_every_dispatcher_alias
(drift guard for MODEL_TAG_RE) and tests/test_profiles.py (profile-config fixtures).
"""

import logging
from types import SimpleNamespace

import pytest

import dispatcher
import orchestrator
import queue_manager
from dispatcher import _TAG_MAP, _selection_order, profile_dead_end_reason
from profiles import (
    _DEFAULT_PROVIDERS,
    _KNOWN_PROVIDERS,
    ProfileConfig,
    _build_profile_config,
)
from providers.openrouter import OpenRouterProvider
from providers.vibe import VibeProvider
from queue_linter import lint_queue


def test_provider_tag_re_matches_every_dispatcher_provider():
    """A blank #<provider> tag for every value in dispatcher._TAG_MAP is recognized."""
    provider_names = set(_TAG_MAP.values())
    assert len(provider_names) >= 5  # claude, gemini, codex, vibe, openrouter
    for name in provider_names:
        assert queue_manager.PROVIDER_TAG_RE.search(f"Task #{name} hier"), (
            f"PROVIDER_TAG_RE did not match bare #{name}"
        )


def test_strip_metadata_tags_removes_vibe_and_openrouter():
    """Regression for the exact DONE scenario from auftrag.md: #vibe/#openrouter used
    to survive strip_metadata_tags() as literal prompt text."""
    assert queue_manager.strip_metadata_tags("Mach X #vibe #openrouter") == "Mach X"


def test_provider_tag_re_does_not_eat_model_alias_tags():
    """Regression anchor for the `\\b` boundary (R1 in research-and-plan.md): the
    provider-only regex must not swallow part of a longer model-alias tag that starts
    with a provider name, or MODEL_TAG_RE never gets a chance to match the rest.

    Note: research-and-plan.md's example list names "#vibe_large", which is not
    actually in dispatcher._TAG_MAP (only vibe_medium/vibe_small are) — using
    vibe_medium here instead so the second assertion (full-pipeline stripping via
    MODEL_TAG_RE) exercises a real, known alias."""
    text = "#claude_opus #or_glm #vibe_medium #codex_5"
    assert queue_manager.PROVIDER_TAG_RE.sub("", text) == text

    # strip_metadata_tags() runs PROVIDER_TAG_RE before MODEL_TAG_RE, so the full
    # pipeline must remove these tags completely (via MODEL_TAG_RE), not leave a
    # "_opus"-style remainder behind.
    stripped = queue_manager.strip_metadata_tags(f"Run it {text} now")
    assert stripped == "Run it now"


def test_provider_tag_re_requires_a_left_boundary():
    """PROVIDER_TAG_RE must not fire mid-word — `\\b` only ever guarded the RIGHT
    edge of the tag, so text with no whitespace before the '#' used to be cut too,
    even though dispatcher.has_explicit_provider_tag() (which uses `(?<!\\S)` via
    _TAG_RE_BY_PROVIDER) never treated that text as a tag in the first place.
    Table from findings-r1.md, reproduced directly against the compiled regex."""
    cases = [
        "Implement C#vibe bridge",
        "Task #vibe-medium",
        "Besuche https://x/#openrouter heute",
        "Ticket ABC#codex-42 pruefen",
    ]
    for text in cases:
        assert queue_manager.PROVIDER_TAG_RE.sub("", text) == text, (
            f"PROVIDER_TAG_RE incorrectly matched inside: {text!r}"
        )


def test_provider_tag_re_still_strips_a_properly_bounded_tag():
    """Gegenprobe to the boundary test above: a tag with real whitespace/start-of-
    string on both sides is still recognized and stripped."""
    assert queue_manager.PROVIDER_TAG_RE.sub("", "Normal #vibe raus") == "Normal  raus"
    assert queue_manager.strip_metadata_tags("Normal #vibe raus") == "Normal raus"


def test_provider_tag_re_leaves_claude_opus_intact():
    """Gegenprobe: the tightened boundary must not touch the pre-existing
    `\\b`-equivalent protection against eating '#claude_opus'."""
    assert queue_manager.PROVIDER_TAG_RE.sub("", "#claude_opus") == "#claude_opus"


def test_profile_keeps_known_non_default_provider(caplog):
    """providers: [claude, vibe] keeps vibe instead of falling back to the default
    three-provider list (DONE scenario #2 from auftrag.md). The actual guarantee
    here is that a *known* non-default name draws no warning at all — nailed down
    with `not caplog.records` rather than leaving the fixture unused."""
    with caplog.at_level(logging.WARNING, logger="profiles"):
        cfg = _build_profile_config("t", {"providers": ["claude", "vibe"]})
    assert cfg.providers == ["claude", "vibe"]
    assert not caplog.records


def test_profile_drops_unknown_provider_with_warning(caplog):
    """providers: [claude, quatsch] keeps claude, drops quatsch, and logs a WARNING
    naming it — dropping used to be silent (DONE scenario #3 from auftrag.md).
    Pinned down precisely: exactly one record, at WARNING, naming both the profile
    and the dropped name — not just "some message somewhere mentions quatsch"."""
    with caplog.at_level(logging.WARNING, logger="profiles"):
        cfg = _build_profile_config("prof-t", {"providers": ["claude", "quatsch"]})
    assert cfg.providers == ["claude"]
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert "prof-t" in record.message
    assert "quatsch" in record.message


def test_profile_all_unknown_falls_back_to_default_with_warning(caplog):
    """providers: [quatsch] falls back to the existing default list, but the fallback
    is now logged instead of silent. Same precision as the single-drop test above."""
    with caplog.at_level(logging.WARNING, logger="profiles"):
        cfg = _build_profile_config("prof-t", {"providers": ["quatsch"]})
    assert cfg.providers == ["claude", "gemini", "codex"]
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert "prof-t" in record.message
    assert "quatsch" in record.message


def test_known_providers_drift_guard():
    """profiles._KNOWN_PROVIDERS tracks dispatcher._TAG_MAP.values() exactly, so a
    silently shrunk _TAG_MAP (or a re-hand-copied _KNOWN_PROVIDERS) is caught here.

    The second assertion catches the other half: _DEFAULT_PROVIDERS is a separate,
    NOT-derived constant (see its comment in profiles.py), so nothing else forces it
    to stay a subset of _KNOWN_PROVIDERS. If a future cleanup retires a provider from
    _TAG_MAP (e.g. gemini) without also updating _DEFAULT_PROVIDERS, the two fallback
    paths in _build_profile_config (omitted `providers:` vs. every entry discarded)
    would start disagreeing silently — this line fails loudly instead."""
    assert set(_TAG_MAP.values()) == _KNOWN_PROVIDERS
    assert len(_KNOWN_PROVIDERS) >= 5
    assert set(_DEFAULT_PROVIDERS) <= _KNOWN_PROVIDERS


# ---------------------------------------------------------------------------
# queue_linter._check_model_tag — second consumer of PROVIDER_TAG_RE. It builds
# `explicit_providers` via PROVIDER_TAG_RE.finditer() (detection, not stripping),
# so vibe/openrouter becoming visible to the regex is a behavior change for the
# linter too, previously uncovered by any test (tests/test_queue_linter.py:133/141
# only exercise #gemini/#claude as the explicit-provider tag).
# ---------------------------------------------------------------------------


def test_vibe_tag_flags_mismatched_claude_model_alias():
    """#vibe #claude_opus is a real contradiction: resolve_forced_provider() walks
    _TAG_MAP in insertion order, so #claude_opus wins and the task runs on claude
    despite the #vibe tag — the linter must flag it."""
    content = "## Queue\n- [ ] Task #vibe #claude_opus\n"
    findings = lint_queue(content)
    assert "model_provider_mismatch" in {f.code for f in findings}


def test_openrouter_tag_matching_or_alias_passes():
    """#openrouter #or_glm agree (or_glm's owning provider IS openrouter) — no
    mismatch finding."""
    content = "## Queue\n- [ ] Task #openrouter #or_glm\n"
    findings = lint_queue(content)
    assert "model_provider_mismatch" not in {f.code for f in findings}


# ---------------------------------------------------------------------------
# P3 (optional, taken): orchestrator.py's policy-dead-end message must not blame
# tool_providers-Policy when the real cause is a profile naming a provider that
# isn't registered in this process at all (missing CLI/API key) — now reachable
# because profiles legitimately can name vibe/openrouter after this diff, where
# before `known = {claude, gemini, codex}` made it structurally impossible.
# ---------------------------------------------------------------------------


@pytest.fixture
def without_vibe():
    saved = dispatcher._providers.pop("vibe", None)
    yield
    if saved is not None:
        dispatcher._providers["vibe"] = saved


@pytest.fixture
def with_vibe():
    had_it = "vibe" in dispatcher._providers
    if not had_it:
        dispatcher._providers["vibe"] = VibeProvider()
    yield
    if not had_it:
        dispatcher._providers.pop("vibe", None)


@pytest.fixture
def with_openrouter():
    """Register OpenRouter for the duration of the test.

    dispatcher registers it conditionally (`if config.OPENROUTER_API_KEY:`), so
    without this fixture these tests pass only on a machine that happens to carry a
    paid key: on a fresh clone openrouter lands in the `unregistered` bucket instead
    of `uncapped_barred` and the assertions flip. Same hermeticity rule the repo
    already applies in the other direction (`conftest._isolate_gemini_api_key`).
    The constructor runs fine without a key — nothing is called on it here.
    """
    had_it = "openrouter" in dispatcher._providers
    if not had_it:
        dispatcher._providers["openrouter"] = OpenRouterProvider()
    yield
    if not had_it:
        dispatcher._providers.pop("openrouter", None)


def test_dead_end_reason_none_without_a_profile():
    assert profile_dead_end_reason("Do it", tool_name="dev-loop", profile=None) is None
    assert profile_dead_end_reason(
        "Do it", tool_name="dev-loop", profile=SimpleNamespace(providers=[])
    ) is None


def test_dead_end_reason_none_when_one_entry_is_routable(with_vibe):
    """claude is registered AND allowed, so the profile is not why anything failed —
    the caller must keep its policy wording rather than blame the profile."""
    profile = SimpleNamespace(providers=["claude", "vibe"])
    assert profile_dead_end_reason("Do it", tool_name="dev-loop", profile=profile) is None


def test_dead_end_reason_splits_unregistered_from_barred(without_vibe, with_openrouter):
    """A profile can hit both causes in one run: vibe is not registered here, while
    openrouter is registered but barred by the fail-closed gate (uncapped, no policy).
    The old predicate collapsed both into a single "all unregistered" boolean."""
    profile = SimpleNamespace(providers=["vibe", "openrouter"])
    unregistered, uncapped_barred, policy_barred = profile_dead_end_reason(
        "Do it", tool_name="dev-loop", profile=profile
    )
    assert unregistered == ["vibe"]
    assert uncapped_barred == ["openrouter"]
    assert policy_barred == []


def test_dead_end_reason_none_when_only_an_allow_list_excludes_them():
    """Every entry registered but missing from an explicit allow-list: that IS the
    policy, and the caller's own message already says so *and* prints the list. A
    profile-flavoured message here would replace an accurate sentence with a vaguer
    one — the earlier draft of this fix wrongly called claude/gemini/codex
    "pay-per-token, kein Kostendeckel" on exactly this path."""
    profile = SimpleNamespace(providers=["claude", "codex"])
    reason = profile_dead_end_reason(
        "Do it #tool_providers:claudee", tool_name="dev-loop", profile=profile
    )
    assert reason is None


def test_dead_end_message_names_the_missing_cli_not_the_policy(without_vibe):
    """Cause 1 — the provider is not registered at all. _allows() never even gets a
    say (`p in _providers` empties the order first), so pointing at policy.yaml
    sends the reader to a file that cannot fix it."""
    profile = ProfileConfig(name="only-vibe", providers=["vibe"])
    order, allowed = _selection_order("Do it", profile, None, False, "dev-loop")
    assert order == []  # confirms this really is the dead end under test

    msg = orchestrator._policy_dead_end_message(
        dispatcher._effective_allowed(allowed), "dev-loop", "Do it", profile
    )
    assert "only-vibe" in msg
    assert "nicht registriert" in msg
    assert "die tool_providers-Policy erlaubt" not in msg


def test_dead_end_message_names_the_fail_closed_gate_not_the_policy(with_vibe):
    """Cause 2 — vibe IS registered; the fail-closed gate for uncapped providers is
    why the order is empty. The previous predicate answered False here and produced a
    message claiming a tool_providers-policy existed (it was synthesised by
    _effective_allowed) and that claude/codex/gemini were unregistered (they are not)."""
    profile = ProfileConfig(name="only-vibe", providers=["vibe"])
    order, allowed = _selection_order("Do it", profile, None, False, "dev-loop")
    assert order == []
    assert allowed is None  # no tool_providers: section — the normal install state

    msg = orchestrator._policy_dead_end_message(
        dispatcher._effective_allowed(allowed), "dev-loop", "Do it", profile
    )
    assert "only-vibe" in msg
    assert "ohne ausdrückliche Freigabe" in msg
    assert "#tool_providers:vibe" in msg
    assert "die tool_providers-Policy erlaubt" not in msg
    # The old message's two false claims must be gone.
    assert "nicht registriert" not in msg


def test_dead_end_message_keeps_policy_wording_when_the_profile_is_not_the_cause():
    """No profile in play -> the pre-existing message is unchanged; the new
    parameters are additive, not a rewrite of the untagged case."""
    msg = orchestrator._policy_dead_end_message(["claude", "codex"], "dev-loop", "Do it")
    assert "die tool_providers-Policy erlaubt" in msg
    assert "claude, codex" in msg


def test_dead_end_message_renders_both_causes_and_the_real_allow_list(without_vibe):
    """The mixed case: one entry unregistered, one excluded by a REAL allow-list.

    This is the only path on which the message still points at policy.yaml, and
    it was provably untested — a mutation that dropped the policy_barred branch
    left the whole suite green (2291 passed). Guard all three moving parts: both
    cause sentences, the allow-list printed verbatim, and the closing hint naming
    policy.yaml rather than the profile alone.

    The first argument mirrors production, where policy_dead_end() hands over
    `_effective_allowed(allowed)` — not the raw value the two neighbouring tests
    pass — so a regression in what `listed` renders cannot hide behind a fixture.
    """
    profile = ProfileConfig(name="mixed", providers=["vibe", "claude"])
    task = "Do it #tool_providers:codex"

    unregistered, uncapped_barred, policy_barred = profile_dead_end_reason(
        task, tool_name="dev-loop", profile=profile
    )
    assert unregistered == ["vibe"]        # CLI not on PATH here
    assert uncapped_barred == []           # an allow-list exists, so nothing is fail-closed
    assert policy_barred == ["claude"]     # registered, but the tag's list says codex

    msg = orchestrator._policy_dead_end_message(
        dispatcher._effective_allowed(["codex"]), "dev-loop", task, profile
    )
    assert "['vibe'] ist in diesem Prozess nicht registriert" in msg
    assert "['claude'] steht nicht in der tool_providers-Allow-Liste [codex]" in msg
    assert "policy.yaml oder Profil prüfen" in msg


def test_uncapped_advice_names_every_barred_provider(with_vibe, with_openrouter):
    """Following the advice has to actually unblock the run.

    Must render a MULTI-element uncapped_barred: with a single entry
    `','.join(x)` and `x[0]` produce the same string, so a one-provider profile
    cannot tell the fix from the bug it replaced — measured, a mutation back to
    `uncapped_barred[0]` left the full suite green (2293 passed). Both providers
    are registered via fixtures so the bucket does not depend on whether this
    machine carries an OPENROUTER_API_KEY.
    """
    profile = ProfileConfig(name="two-uncapped", providers=["vibe", "openrouter"])
    unregistered, uncapped_barred, policy_barred = profile_dead_end_reason(
        "Do it", tool_name="dev-loop", profile=profile
    )
    assert unregistered == []
    assert uncapped_barred == ["vibe", "openrouter"]
    assert policy_barred == []

    msg = orchestrator._policy_dead_end_message(
        dispatcher._effective_allowed(None), "dev-loop", "Do it", profile
    )
    assert "#tool_providers:vibe,openrouter" in msg
    # The body names policy.yaml as a remedy, so the closing hint must not send the
    # reader to the profile alone.
    assert "policy.yaml oder Profil prüfen" in msg


def test_dead_end_message_points_at_the_profile_only_when_nothing_else_helps(without_vibe):
    """The one case where neither policy.yaml nor the queue line can fix it: the
    provider is simply not installed here. Pins the negative direction of `where`,
    which a hardwired "policy.yaml oder Profil prüfen" would otherwise satisfy."""
    profile = ProfileConfig(name="only-vibe", providers=["vibe"])
    msg = orchestrator._policy_dead_end_message(
        dispatcher._effective_allowed(None), "dev-loop", "Do it", profile
    )
    assert "nicht registriert" in msg
    assert "Task abgebrochen (Profil prüfen)." in msg
    assert "policy.yaml" not in msg
