import pytest
from pathlib import Path
from limits import AllLimits, ProviderLimits
from dispatcher import select_provider
from policy import PolicyEngine, get_engine
import policy as policy_module

@pytest.fixture
def mock_limits():
    return AllLimits(
        claude=ProviderLimits(available=True, remaining_pct=100.0),
        gemini=ProviderLimits(available=True, remaining_pct=100.0),
        codex=ProviderLimits(available=True, remaining_pct=100.0)
    )

def _make_engine(tmp_path, global_yaml: str) -> PolicyEngine:
    """Create a PolicyEngine with a custom global policy YAML."""
    policy_file = tmp_path / "99_System" / "AI" / "policy.yaml"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(global_yaml, encoding="utf-8")
    return PolicyEngine(vault_path=tmp_path)

def test_tool_provider_filtering(tmp_path, mock_limits, monkeypatch):
    """Verify that select_provider respects the tool-provider policy.

    The policy filters the *priority chain* — it cannot add a provider that is not
    in it. `_PRIORITY` is ["claude", "codex"] since Gemini left the chain on
    2026-08-15, so a policy naming only a non-chain provider yields None rather
    than routing there (this test asserted `gemini` until then and went red with
    that removal).
    """
    yaml_content = """
tool_providers:
  review-loop: [codex]
  test-loop: [claude, codex]
  gemini-only: [gemini]
  default: [claude, codex]
"""
    engine = _make_engine(tmp_path, yaml_content)

    # Mock the singleton engine
    monkeypatch.setattr(policy_module, "_engine", engine)

    # Test review-loop (should only allow codex)
    p = select_provider("Run review", mock_limits, tool_name="review-loop")
    assert p.name == "codex"

    # Test test-loop (should allow claude first)
    p = select_provider("Run tests", mock_limits, tool_name="test-loop")
    assert p.name == "claude"

    # Test unknown tool (should use default)
    p = select_provider("Unknown", mock_limits, tool_name="unknown-tool")
    assert p.name == "claude"

    # A policy entry naming only an out-of-chain provider selects nothing.
    assert select_provider("Anything", mock_limits, tool_name="gemini-only") is None

def test_tool_provider_fallback_blocked(tmp_path, mock_limits, monkeypatch):
    """Verify that fallback is restricted to the allowed providers."""
    yaml_content = """
tool_providers:
  review-loop: [codex]
"""
    engine = _make_engine(tmp_path, yaml_content)
    monkeypatch.setattr(policy_module, "_engine", engine)

    # Excluding the single allowed provider must NOT fall back to claude:
    # 1. allowed_by_policy = [codex]
    # 2. base_order = [claude, codex] filtered to [codex]
    # 3. exclude = {codex}
    # 4. Result is None
    p = select_provider("Run review", mock_limits, tool_name="review-loop", exclude={"codex"})
    assert p is None


def test_forced_tag_rejected_when_policy_bars_it(tmp_path, mock_limits, monkeypatch):
    """A #provider tag must not smuggle a barred provider past the policy filter.

    The forced provider used to be prepended AFTER filtering, which made the
    filter a no-op for exactly the providers it exists to bar (openrouter/vibe —
    dispatcher._limits_ok returns True for both unconditionally, so there is no
    cost ceiling either). Rejection must be a clean None, never a silent reroute
    to another provider.
    """
    engine = _make_engine(tmp_path, "tool_providers:\n  dev-loop: [claude, codex]\n")
    monkeypatch.setattr(policy_module, "_engine", engine)

    assert select_provider("Do it #gemini", mock_limits, tool_name="dev-loop") is None
    # Not rerouted to claude — the barred tag stops the selection outright.
    assert select_provider("Do it", mock_limits, tool_name="dev-loop").name == "claude"


def test_forced_provider_policy_violation_reports_reason(tmp_path, mock_limits, monkeypatch):
    """The orchestrator needs to tell "policy said no" from "no capacity"."""
    from dispatcher import forced_provider_policy_violation

    engine = _make_engine(tmp_path, "tool_providers:\n  dev-loop: [claude, codex]\n")
    monkeypatch.setattr(policy_module, "_engine", engine)

    violation = forced_provider_policy_violation("Do it #gemini", tool_name="dev-loop")
    assert violation == ("gemini", ["claude", "codex"])

    # An allowed tag and an untagged task are both non-violations.
    assert forced_provider_policy_violation("Do it #codex", tool_name="dev-loop") is None
    assert forced_provider_policy_violation("Do it", tool_name="dev-loop") is None


def test_task_level_tool_providers_tag_overrides_global_policy(tmp_path, mock_limits, monkeypatch):
    """Layer 1 (#tool_providers: in the queue line) still wins over policy.yaml."""
    engine = _make_engine(tmp_path, "tool_providers:\n  dev-loop: [claude]\n")
    monkeypatch.setattr(policy_module, "_engine", engine)

    p = select_provider(
        "Do it #tool_providers:codex", mock_limits, tool_name="dev-loop",
    )
    assert p.name == "codex"


def test_policy_allows_provider_fails_open_for_capped_providers_only(tmp_path, monkeypatch):
    """No tool_providers section → capped providers allowed, UNCAPPED ones barred.

    Replaces an earlier test that asserted a blanket `is True` for every provider.
    That blanket fail-open was the bug: policy.yaml sits in a OneDrive-synced
    folder, and a missing / half-written / conflicted file makes
    get_allowed_providers() report "no restriction" — indistinguishable from
    "deliberately unrestricted". Under the old rule an unattended 03:00 run could
    then reach openrouter and vibe, which are pay-per-token and have NO cost
    ceiling anywhere (dispatcher._limits_ok returns True for both
    unconditionally). Fail-open stays for claude/codex/gemini so a lost policy
    file cannot take the whole orchestrator offline; it is dropped for exactly
    the set that can spend money unsupervised.
    """
    from dispatcher import policy_allows_provider

    engine = _make_engine(tmp_path, "auto:\n  - pytest\n")
    monkeypatch.setattr(policy_module, "_engine", engine)

    # Capped/subscription providers: still fail-open.
    assert policy_allows_provider("claude", "dev-loop") is True
    assert policy_allows_provider("codex", "dev-loop") is True
    assert policy_allows_provider("gemini", None) is True

    # Uncapped pay-per-token providers: fail-closed.
    assert policy_allows_provider("openrouter", "dev-loop") is False
    assert policy_allows_provider("vibe", None) is False


def test_policy_allows_provider_fails_closed_when_policy_file_is_missing(tmp_path, monkeypatch):
    """The OneDrive case: no policy.yaml on disk at all, not merely no section."""
    from dispatcher import policy_allows_provider
    from policy import PolicyEngine

    # No file written — PolicyEngine tolerates that silently.
    monkeypatch.setattr(policy_module, "_engine", PolicyEngine(vault_path=tmp_path))

    assert policy_allows_provider("openrouter", "dev-loop") is False
    assert policy_allows_provider("vibe", "review-loop") is False
    assert policy_allows_provider("claude", "dev-loop") is True


def test_policy_allows_provider_fails_closed_when_policy_yaml_is_corrupt(tmp_path, monkeypatch):
    """A half-synced / syntactically broken file must not read as 'no restriction'."""
    from dispatcher import policy_allows_provider

    engine = _make_engine(tmp_path, "tool_providers: [unclosed\n  :::bad")
    monkeypatch.setattr(policy_module, "_engine", engine)

    assert policy_allows_provider("openrouter", "dev-loop") is False
    assert policy_allows_provider("vibe", "review-loop") is False
    # The orchestrator stays operational on the capped providers.
    assert policy_allows_provider("claude", "dev-loop") is True
    assert policy_allows_provider("codex", "dev-loop") is True


def test_explicit_allow_list_still_authorises_an_uncapped_provider(tmp_path, monkeypatch):
    """Fail-closed applies to the ABSENCE of a policy, not to a policy that says yes.

    Naming vibe/openrouter in policy.yaml (or in a `#tool_providers:` queue tag) is
    a deliberate authorisation and must keep working — otherwise the fix would
    hardcode a ban instead of a default.
    """
    from dispatcher import policy_allows_provider

    engine = _make_engine(
        tmp_path, "tool_providers:\n  review-loop: [claude, vibe]\n  default: [claude]\n"
    )
    monkeypatch.setattr(policy_module, "_engine", engine)

    assert policy_allows_provider("vibe", "review-loop") is True
    assert policy_allows_provider("vibe", "dev-loop") is False  # default: [claude]


def test_uncapped_set_is_exactly_the_set_without_a_cost_ceiling(tmp_path, monkeypatch):
    """The fail-closed set is derived from _limits_ok, not typed out twice.

    _limits_ok returning True unconditionally IS the definition of "no cost
    ceiling". If a provider is added to that special case without being added to
    _UNCAPPED_PROVIDERS, it would spend money unsupervised on a lost policy file
    — this pins the two together.
    """
    import dispatcher
    from limits import AllLimits, ProviderLimits

    exhausted = AllLimits(
        claude=ProviderLimits(available=False, remaining_pct=0.0),
        gemini=ProviderLimits(available=False, remaining_pct=0.0),
        codex=ProviderLimits(available=False, remaining_pct=0.0),
    )
    for name in dispatcher._UNCAPPED_PROVIDERS:
        assert dispatcher._limits_ok(name, exhausted) is True, name
        assert dispatcher._allows(name, None) is False, name


@pytest.mark.parametrize("allowed_name", [
    "claudee",   # typo — matches no registered provider at all
    "gemini",    # registered, but not in _PRIORITY (left the chain 2026-08-15)
])
def test_policy_dead_end_flags_a_policy_that_allows_nothing_routable(
    tmp_path, mock_limits, monkeypatch, allowed_name,
):
    """No tag, and the policy names nothing the chain can actually reach.

    select_provider() returns None here exactly like "everything is busy", and the
    orchestrator used to read it that way: park the task and wait for a quota
    reset. No quota reset can lift a policy restriction, so the task waited
    forever. policy_dead_end() is what tells the two apart.

    Both parametrisations are dead ends only because no profile is passed (the
    chain is then `_PRIORITY` = claude, codex). With the default profile, whose
    providers are ["claude", "gemini", "codex"], the gemini case is NOT a dead end
    — see the orchestrator-level test for that distinction.
    """
    from dispatcher import policy_dead_end, select_provider

    engine = _make_engine(tmp_path, f"tool_providers:\n  dev-loop: [{allowed_name}]\n")
    monkeypatch.setattr(policy_module, "_engine", engine)

    # The ambiguous symptom...
    assert select_provider("Do it", mock_limits, tool_name="dev-loop") is None
    # ...resolved: permanent, and it reports what the policy actually allows.
    assert policy_dead_end("Do it", tool_name="dev-loop") == [allowed_name]


@pytest.mark.parametrize("yaml_text", [
    "tool_providers:\n  dev-loop: 5\n",              # scalar where a list belongs
    "tool_providers:\n  dev-loop: {a: b}\n",         # mapping
    "tool_providers:\n  dev-loop: []\n",             # empty list
    "tool_providers:\n  dev-loop: [null, 7]\n",      # no usable names
])
def test_malformed_tool_providers_entry_degrades_instead_of_crashing(
    tmp_path, mock_limits, monkeypatch, yaml_text,
):
    """A junk `tool_providers:` entry must not take the nightly run down.

    Nothing validates that section's shape on load, and downstream it is only
    membership-tested (`p in allowed`) — which raises TypeError on a non-container.
    Unusable input degrades to "no usable restriction", which still means
    fail-closed for the uncapped providers.
    """
    from dispatcher import policy_allows_provider, policy_dead_end, select_provider

    engine = _make_engine(tmp_path, yaml_text)
    monkeypatch.setattr(policy_module, "_engine", engine)

    assert select_provider("Do it", mock_limits, tool_name="dev-loop").name == "claude"
    assert policy_dead_end("Do it", tool_name="dev-loop") is None
    assert policy_allows_provider("claude", "dev-loop") is True
    assert policy_allows_provider("vibe", "dev-loop") is False


def test_scalar_tool_providers_entry_is_not_exploded_into_characters(tmp_path, mock_limits, monkeypatch):
    """`dev-loop: claude` (no brackets) must mean claude, never c/l/a/u/d/e.

    PolicyEngine used to `list()` the entry unconditionally, so the scalar form —
    a natural thing to hand-write — silently became a six-character allow-list
    that matched no provider at all and barred the tool from everything.
    """
    import dispatcher

    engine = _make_engine(tmp_path, "tool_providers:\n  dev-loop: claude\n")
    monkeypatch.setattr(policy_module, "_engine", engine)

    assert engine.get_allowed_providers("dev-loop") == ["claude"]
    assert select_provider("Do it", mock_limits, tool_name="dev-loop").name == "claude"

    # Unit level, both layers (engine-side and the dispatcher's defence in depth).
    assert policy_module._coerce_provider_list("claude") == ["claude"]
    assert policy_module._coerce_provider_list({"a": "b"}) is None
    assert policy_module._coerce_provider_list(["claude", 5, "  codex  "]) == ["claude", "codex"]
    assert dispatcher._sanitize_allowed("claude") == ["claude"]
    assert dispatcher._sanitize_allowed(["claude", 5, "  codex  "]) == ["claude", "codex"]
    assert dispatcher._sanitize_allowed(object()) is None


def test_tool_providers_section_that_is_not_a_mapping_is_ignored(tmp_path, mock_limits, monkeypatch):
    """`tool_providers:` as a list instead of a mapping must not gate anything."""
    engine = _make_engine(tmp_path, "tool_providers:\n  - claude\n  - codex\n")
    monkeypatch.setattr(policy_module, "_engine", engine)

    assert engine.get_allowed_providers("dev-loop") is None
    assert select_provider("Do it", mock_limits, tool_name="dev-loop").name == "claude"


def test_policy_dead_end_is_none_while_a_provider_is_merely_busy(tmp_path, monkeypatch):
    """Capacity/cooldown must NOT be read as a dead end — parking is right there.

    policy_dead_end deliberately ignores limits and cooldowns: it answers "may
    anything be routed to", not "is anything free right now".
    """
    from limits import AllLimits, ProviderLimits
    from dispatcher import policy_dead_end, select_provider

    engine = _make_engine(tmp_path, "tool_providers:\n  dev-loop: [claude, codex]\n")
    monkeypatch.setattr(policy_module, "_engine", engine)

    exhausted = AllLimits(
        claude=ProviderLimits(available=False, remaining_pct=0.0),
        gemini=ProviderLimits(available=False, remaining_pct=0.0),
        codex=ProviderLimits(available=False, remaining_pct=0.0),
    )
    assert select_provider("Do it", exhausted, tool_name="dev-loop") is None
    assert policy_dead_end("Do it", tool_name="dev-loop") is None


def test_policy_dead_end_is_none_when_the_policy_permits_a_chain_provider(tmp_path, monkeypatch):
    from dispatcher import policy_dead_end

    engine = _make_engine(tmp_path, "tool_providers:\n  dev-loop: [claude, codex]\n")
    monkeypatch.setattr(policy_module, "_engine", engine)

    assert policy_dead_end("Do it", tool_name="dev-loop") is None
    assert policy_dead_end("Do it #codex", tool_name="dev-loop") is None


def test_policy_dead_end_covers_a_profile_that_cannot_intersect_the_policy(tmp_path, monkeypatch):
    """The other way to reach an empty order: profile order ∩ policy = {}."""
    from types import SimpleNamespace
    from dispatcher import policy_dead_end

    engine = _make_engine(tmp_path, "tool_providers:\n  dev-loop: [codex]\n")
    monkeypatch.setattr(policy_module, "_engine", engine)

    profile = SimpleNamespace(providers=["claude"], tool_providers={})
    assert policy_dead_end("Do it", tool_name="dev-loop", profile=profile) == ["codex"]
    # Same profile, a policy it can satisfy → not a dead end.
    engine2 = _make_engine(tmp_path / "b", "tool_providers:\n  dev-loop: [claude, codex]\n")
    monkeypatch.setattr(policy_module, "_engine", engine2)
    assert policy_dead_end("Do it", tool_name="dev-loop", profile=profile) is None


def test_policy_dead_end_leaves_the_unregistered_reviewer_park_alone(monkeypatch):
    """`#vibe` without the vibe binary is parked on purpose (_REVIEWER_ONLY).

    That is a registration state, not a policy verdict, so it must not be
    finalized as "policy allows nothing" — pinned so the new terminal path cannot
    quietly swallow it.
    """
    import dispatcher
    from dispatcher import policy_dead_end

    saved = dispatcher._providers.pop("vibe", None)
    try:
        assert policy_dead_end("Second opinion #vibe", tool_name="review-loop") is None
    finally:
        if saved is not None:
            dispatcher._providers["vibe"] = saved


def test_policy_dead_end_agrees_with_select_provider_on_the_forced_case(tmp_path, mock_limits, monkeypatch):
    """A barred #tag is a dead end too — the two reporters must not contradict."""
    from dispatcher import policy_dead_end, select_provider, forced_provider_policy_violation

    engine = _make_engine(tmp_path, "tool_providers:\n  dev-loop: [claude, codex]\n")
    monkeypatch.setattr(policy_module, "_engine", engine)

    assert select_provider("Do it #gemini", mock_limits, tool_name="dev-loop") is None
    assert forced_provider_policy_violation("Do it #gemini", tool_name="dev-loop") is not None
    assert policy_dead_end("Do it #gemini", tool_name="dev-loop") == ["claude", "codex"]


def test_get_provider_for_tool_blocks_barred_provider(tmp_path, monkeypatch):
    """The lookup every tool-internal provider resolution goes through."""
    from dispatcher import get_provider_for_tool, policy_provider_lookup

    engine = _make_engine(tmp_path, "tool_providers:\n  brainstorm: [claude]\n")
    monkeypatch.setattr(policy_module, "_engine", engine)

    assert get_provider_for_tool("claude", "brainstorm") is not None
    assert get_provider_for_tool("codex", "brainstorm") is None

    lookup = policy_provider_lookup("brainstorm")
    assert lookup("claude") is not None
    assert lookup("codex") is None
