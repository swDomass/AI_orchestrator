"""
Selects the best available provider for a given task.

Routing priority:
  1. Claude  - best quality, default choice
  2. Codex   - fallback, good for code tasks

A provider is skipped if:
  - cclimits shows < 5% remaining capacity
  - It is in cooldown (unreachable / error within last 30 min)

OpenRouter (pay-per-token) and Vibe (Mistral, pay-per-token) are registered but
never part of that chain — they run only when a task tags them explicitly.

Gemini was removed from the chain on 2026-08-15 (IneligibleTierError + data
protection). The provider stays registered so tagged/legacy paths still resolve,
but nothing routes there by default.
"""

import re
from typing import Callable

import config
from limits import AllLimits, ProviderLimits, is_transient_token_refresh
from providers.base import BaseProvider
from providers import (
    ClaudeProvider,
    CodexProvider,
    GeminiProvider,
    OpenRouterProvider,
    VibeProvider,
)

# Tag in task text to force a specific provider.
# Model-specific tags also select their owning provider.
# OpenRouter tags only resolve when OPENROUTER_API_KEY is set (see _providers
# below); without a key, a tagged task falls through to the default chain.
_TAG_MAP = {
    "#claude":            "claude",
    "#claude_haiku":      "claude",
    "#claude_sonnet":     "claude",
    "#claude_opus":       "claude",
    "#gemini":            "gemini",
    "#gemini_pro":        "gemini",
    "#gemini_flash":      "gemini",
    "#gemini_flash_lite": "gemini",
    "#codex":             "codex",
    "#codex_5":           "codex",
    "#codex_5_4":         "codex",
    "#codex_mini":        "codex",
    "#vibe":              "vibe",
    "#vibe_medium":       "vibe",
    "#vibe_small":        "vibe",
    "#openrouter":        "openrouter",
    "#or_minimax_free":   "openrouter",
    "#or_deepseek_free":  "openrouter",
    "#or_qwen_free":      "openrouter",
    "#or_nemotron_free":  "openrouter",
    "#or_glm":            "openrouter",
    "#or_kimi":           "openrouter",
    "#or_qwen":           "openrouter",
    "#or_deepseek":       "openrouter",
    "#or_minimax":        "openrouter",
}

_TAG_RE_BY_PROVIDER = {
    tag: re.compile(rf"(?<!\S){re.escape(tag)}(?![\w-])")
    for tag in _TAG_MAP
}

# Singleton provider instances (carry cooldown state across calls).
# OpenRouter and Vibe are registered conditionally: without an API key resp.
# without the `vibe` binary on PATH, tagged tasks fall through to the default
# chain (Claude/Gemini/Codex) automatically.
_providers: dict[str, BaseProvider] = {
    "claude": ClaudeProvider(),
    "gemini": GeminiProvider(),
    "codex": CodexProvider(),
}
if config.OPENROUTER_API_KEY:
    _providers["openrouter"] = OpenRouterProvider()
if VibeProvider.is_available():
    _providers["vibe"] = VibeProvider()

# Priority order — OpenRouter and Vibe are intentionally absent so they NEVER
# enter the default fallback chain. Activation requires an explicit
# #openrouter/#or_* resp. #vibe/#vibe_* tag (or #second_opinion:vibe).
# Gemini removed 2026-08-15: an exhausted Claude used to hand untagged tasks
# straight to it (fallback slot 2) although it cannot execute them
# (IneligibleTierError) — plus data protection.
_PRIORITY = ["claude", "codex"]

# Providers whose whole point is that they do NOT write. Falling back from one of
# these to the default chain would silently swap a non-writing reviewer for a
# file-writing executor — a wider blast radius than the task asked for. For
# OpenRouter the same fallback is harmless (executor → executor); here it is not.
# So: an explicit tag for a reviewer-only provider that isn't registered yields
# no provider at all, and the task is parked instead of quietly escalated.
_REVIEWER_ONLY = {"vibe"}

# Providers billed per token with NO cost ceiling anywhere in this codebase:
# _limits_ok() below returns True for them unconditionally (there is no cclimits
# quota to poll), and nothing else caps them either. The subscription providers
# (claude, codex) are capped by their own quota and cost nothing extra when they
# run — so "spent money while nobody was watching" is a risk that exists for
# exactly this set.
#
# Single source of truth for that property: _limits_ok() reads it, and
# _allows() below fails CLOSED for these when no policy could be loaded. Adding a
# pay-per-token provider means adding it here once, not in two places that can
# drift apart.
_UNCAPPED_PROVIDERS = frozenset({"openrouter", "vibe"})


def _sanitize_allowed(allowed) -> list[str] | None:
    """Coerce whatever the policy engine yielded into ``list[str] | None``.

    ``tool_providers:`` is raw YAML — nothing validates its shape on load, so a
    hand-edited or half-synced file can hand back a number, a mapping, or a
    string where a list belongs. Downstream this list is only ever membership-
    tested (``p in allowed``), which raises TypeError on a non-container and
    would take the whole nightly run down over a typo.

    Anything unusable degrades to None = "no usable restriction", which routes
    into _allows() and therefore stays fail-open for capped providers and
    fail-closed for the uncapped ones. Non-string entries are dropped rather
    than stringified, so `[claude, 5]` allows claude and ignores the 5.
    """
    if allowed is None:
        return None
    if isinstance(allowed, str):
        # `dev-loop: claude` (scalar instead of a list) — the obvious intent.
        # Note PolicyEngine.get_allowed_providers() already list()s its entry, so
        # a scalar normally arrives here pre-exploded into characters; this
        # branch catches the paths that hand the raw scalar through.
        return [allowed]
    if not isinstance(allowed, (list, tuple, set, frozenset)):
        return None
    names = [p.strip() for p in allowed if isinstance(p, str) and p.strip()]
    return names or None


def _policy_allowed_providers(tool_name: str | None) -> list[str] | None:
    """Global tool-provider policy for *tool_name* (policy.yaml), or None.

    None means "no restriction configured" — see _allows() for what callers do
    with that (fail-open for capped providers, fail-closed for uncapped ones).
    A missing, unreadable, malformed or half-synced policy.yaml all land here as
    None rather than as an exception: this runs unattended, so a broken config
    file must degrade, not crash.
    """
    try:
        from policy import get_engine
        allowed = get_engine().get_allowed_providers(tool_name)
    except (ImportError, ValueError, AttributeError, TypeError):
        return None
    return _sanitize_allowed(allowed)


def _allowed_by_policy(
    task: str = "",
    profile=None,  # ProfileConfig | None
    tool_name: str | None = None,
) -> list[str] | None:
    """Resolve the allowed-provider list through the three policy layers.

    1. Task level  (``#tool_providers:p1,p2`` in the queue line)
    2. Profile level (``profile.tool_providers[tool_name]``)
    3. Global level (``tool_providers:`` in policy.yaml)

    Returns None when no layer restricts anything.
    """
    if task:
        try:
            from queue_manager import extract_tool_providers
            allowed = extract_tool_providers(task)
            if allowed is not None:
                return allowed
        except (ImportError, ValueError, AttributeError):
            pass

    if profile and tool_name and hasattr(profile, "tool_providers"):
        allowed = profile.tool_providers.get(tool_name)
        if allowed is not None:
            return allowed

    return _policy_allowed_providers(tool_name)


def _allows(provider_name: str, allowed: list[str] | None) -> bool:
    """Decide one provider against an already-resolved allow-list.

    ``allowed`` is None/empty when no policy layer restricts anything — which is
    also what a MISSING, unreadable or syntactically broken policy.yaml produces
    (PolicyEngine swallows both: _reload_if_changed returns early on a missing
    file, _load_rules_locked logs and returns on a parse error, so
    get_allowed_providers() reports "no restriction" either way). The file lives
    in a OneDrive-synced folder, so a half-written or conflicted state is a real
    operating condition, not a thought experiment.

    That case is split, deliberately asymmetrically:

    * fail-OPEN for the capped providers (claude, codex, gemini) — a policy file
      that failed to load must not take the whole orchestrator offline at 03:00.
    * fail-CLOSED for _UNCAPPED_PROVIDERS — those are pay-per-token with no cost
      ceiling anywhere (_limits_ok returns True for them unconditionally), so a
      lost policy file would otherwise turn "barred from unattended runs" into
      "billable and unsupervised". Losing them costs a second opinion; keeping
      them costs money nobody authorised.

    An explicit allow-list that names an uncapped provider still permits it —
    that is a deliberate authorisation (policy.yaml entry, profile, or a
    ``#tool_providers:`` tag on the queue line), not an accident.
    """
    if not allowed:
        return provider_name not in _UNCAPPED_PROVIDERS
    return provider_name in allowed


def _effective_allowed(allowed: list[str] | None) -> list[str]:
    """The allow-list to SHOW a human, resolving the implicit one.

    When no policy loaded there is no list to print, yet the effective rule is
    not "everything" (see _allows). Report what actually remains reachable so a
    log line never claims a provider was allowed when it was not.
    """
    if allowed:
        return list(allowed)
    return sorted(n for n in _providers if n not in _UNCAPPED_PROVIDERS)


def policy_allows_provider(provider_name: str, tool_name: str | None) -> bool:
    """True when the global tool policy permits *provider_name* for *tool_name*.

    The single gate every tool-internal provider lookup goes through. Without it
    a tool that resolves its own provider (second opinion, pass 2, cross-provider
    persona allocation) bypasses policy.yaml entirely — which is how OpenRouter
    and Vibe kept being reachable in unattended runs even after they were barred
    there (neither has a cost ceiling: _limits_ok returns True for both).

    A missing/broken policy fails open for capped providers and closed for
    uncapped ones — see _allows() for why the two halves differ.
    """
    return _allows(provider_name, _policy_allowed_providers(tool_name))


def get_provider_for_tool(name: str, tool_name: str | None) -> BaseProvider | None:
    """Policy-aware ``get_provider_by_name()``.

    Returns None both when the provider is unknown/unregistered AND when the tool
    policy bars it, so callers keep their existing "None → skip / fall back"
    handling. Use ``policy_allows_provider()`` first when the two cases need to be
    told apart in a log line.
    """
    if not policy_allows_provider(name, tool_name):
        return None
    # Deliberately routed through the module-level get_provider_by_name rather
    # than _providers directly: it stays the single registry access point, so a
    # test that stubs it out (hermeticity patches do exactly that) still governs
    # what tools can resolve.
    return get_provider_by_name(name)


def policy_provider_lookup(tool_name: str | None) -> "Callable[[str], BaseProvider | None]":
    """Return a ``(name) -> provider | None`` lookup bound to *tool_name*'s policy.

    Drop-in replacement for the ``provider_lookup`` callables that the
    cross-provider allocation phases (brainstorm personas, scientific-investigation
    personas, Phase 7 reviewer) default to. Candidate tuples in those modules stay
    as they are — a barred provider simply resolves to None and the loop moves on,
    which preserves the cross-provider diversity logic instead of hardcoding it.
    """
    def _lookup(name: str) -> BaseProvider | None:
        return get_provider_for_tool(name, tool_name)
    return _lookup


def forced_provider_policy_violation(
    task: str,
    *,
    tool_name: str | None = None,
    force_name: str | None = None,
    profile=None,  # ProfileConfig | None
) -> tuple[str, list[str]] | None:
    """Return ``(provider_name, allowed)`` when the task forces a provider the tool
    policy bars — otherwise None.

    Lets the orchestrator tell "policy rejected the #tag" apart from "everything is
    capacity-exhausted", which both surface as ``select_provider() -> None``. The
    former is terminal (retrying cannot change a policy), the latter is not.
    """
    forced = resolve_forced_provider(task, force_name)
    if forced is None:
        return None
    allowed = _allowed_by_policy(task, profile, tool_name)
    if not allowed or forced.name in allowed:
        return None
    return forced.name, list(allowed)


def _selection_order(
    task: str,
    profile,  # ProfileConfig | None
    force_name: str | None,
    strict: bool,
    tool_name: str | None,
) -> tuple[list[str], list[str] | None]:
    """The provider names select_provider() will walk, plus the resolved allow-list.

    Capacity, cooldown and ``exclude`` are deliberately NOT applied here: those
    are the temporary reasons a provider drops out, and they are what the caller
    loops over. What IS applied is every permanent reason — the policy layers,
    the profile's provider list, and registration.

    So an EMPTY order means "nothing may be routed to, ever", which is a
    different animal from "everything is busy right now": no quota reset and no
    cooldown expiry can turn it non-empty. Shared with policy_dead_end() so the
    two can never disagree about what select_provider() would have tried.
    """
    forced = resolve_forced_provider(task, force_name)
    allowed = _allowed_by_policy(task, profile, tool_name)

    # A forced provider the policy bars stops the selection outright — no
    # fallback (see select_provider), so nothing is routable.
    if forced and allowed and forced.name not in allowed:
        return [], allowed

    # Profile provider order overrides _PRIORITY. Unlike _PRIORITY (which never
    # contains openrouter/vibe), a profile's `providers:` list legitimately can —
    # so it must clear _allows() here too, not just the `if allowed:` filter
    # below. That filter alone is fail-OPEN when allowed is None (see _allows()),
    # which is correct for _PRIORITY's capped members but was silently routing
    # an uncapped profile provider around the fail-closed gate: `known =
    # {claude, gemini, codex}` used to make this structurally impossible before
    # profiles.py started deriving providers from dispatcher._TAG_MAP.
    if profile and getattr(profile, "providers", None):
        base_order = [p for p in profile.providers if p in _providers and _allows(p, allowed)]
    else:
        base_order = _PRIORITY[:]

    if allowed:
        base_order = [p for p in base_order if p in allowed]

    if forced:
        if strict:
            # Strict mode: only the forced provider, no fallback
            return [forced.name], allowed
        return [forced.name] + [n for n in base_order if n != forced.name], allowed

    return base_order, allowed


def policy_dead_end(
    task: str,
    *,
    tool_name: str | None = None,
    force_name: str | None = None,
    strict: bool = False,
    profile=None,  # ProfileConfig | None
) -> list[str] | None:
    """Return the effective allow-list when NO provider is routable at all — else None.

    Separates the two states that both surface as ``select_provider() -> None``:

    * no provider **available** — capacity exhausted, cooldown running. Temporary;
      parking the task until the quota resets is the right answer.
    * no provider **allowed** — the policy/profile layers leave an empty order.
      Permanent. The orchestrator used to read this as the first case and park the
      task waiting for a quota reset that could never lift a policy restriction,
      so the task sat in the queue forever, silently, every poll.

    ``forced_provider_policy_violation()`` already covers the same dead end for a
    task that carries a #provider tag; this covers the untagged case (a policy
    naming only providers that are not in the fallback chain, or a profile whose
    provider list and the policy do not intersect).
    """
    # A task tagging an unregistered reviewer-only provider is parked on purpose
    # (see _REVIEWER_ONLY) — not a policy dead end, so leave that path alone.
    if resolve_forced_provider(task, force_name) is None and _tags_unregistered_reviewer_only(task):
        return None
    order, allowed = _selection_order(task, profile, force_name, strict, tool_name)
    if order:
        return None
    return _effective_allowed(allowed)


def profile_dead_end_reason(
    task: str,
    *,
    tool_name: str | None = None,
    profile=None,  # ProfileConfig | None
) -> tuple[list[str], list[str], list[str]] | None:
    """Why the *profile* left nothing routable — or None when the honest answer is
    "the policy did", which the caller already words correctly.

    Returns ``(unregistered, uncapped_barred, policy_barred)``. Three buckets,
    because ``_allows()`` says False for two unrelated reasons and a message that
    merges them states a cause it cannot know:

    * ``unregistered`` — named in ``providers:`` but not registered in this
      process at all (CLI missing, API key unset). No edit to policy.yaml changes
      this, so a message pointing there sends the reader to the wrong file.
    * ``uncapped_barred`` — registered, but in ``_UNCAPPED_PROVIDERS`` with **no**
      allow-list resolved. That is the fail-CLOSED default from ``_allows()``, and
      it says nothing about policy.yaml's contents: ``get_allowed_providers()``
      returns None whenever no ``tool_providers:`` section exists, the normal
      state of an install that never configured one. The fix is an explicit
      authorisation, not a correction.
    * ``policy_barred`` — registered, and an allow-list exists that does not name
      it. This one genuinely IS the policy, and it is the case the caller's
      pre-existing message already describes accurately, including printing the
      list. Reported here only so a mixed profile can be described completely.

    Returns None unless at least one of the first two buckets is non-empty:
    when every entry fell to an explicit allow-list, there is nothing the profile
    wording adds and the caller keeps its own — which also keeps that message's
    contract of naming what the policy *does* allow.

    The gate is ``not any(registered and allowed)``. A single entry that is both
    means the empty order came from somewhere else, so the profile is not to blame.
    Capacity and cooldown are deliberately not consulted: ``_selection_order()``
    does not apply them either (see its docstring) — they are the temporary
    reasons, and this function only explains permanent ones.

    This replaced a narrower predicate that asked only "is NONE of them
    registered?". It covered the missing-CLI case and reported everything else as
    a tool_providers policy problem — naming an allow-list ``_effective_allowed()``
    had synthesised, and calling providers unregistered that were registered all
    along.
    """
    if not profile or not getattr(profile, "providers", None):
        return None
    allowed = _allowed_by_policy(task, profile, tool_name)
    if any(p in _providers and _allows(p, allowed) for p in profile.providers):
        return None
    unregistered = [p for p in profile.providers if p not in _providers]
    registered_barred = [
        p for p in profile.providers if p in _providers and not _allows(p, allowed)
    ]
    if allowed:
        uncapped_barred: list[str] = []
        policy_barred = registered_barred
    else:
        # No allow-list resolved -> _allows() only ever says False for the
        # uncapped set (it fails OPEN for the capped providers), so everything
        # in registered_barred got there via the fail-closed default.
        uncapped_barred = registered_barred
        policy_barred = []
    if not unregistered and not uncapped_barred:
        return None
    return unregistered, uncapped_barred, policy_barred


def _limits_ok(name: str, limits: AllLimits) -> bool:
    # OpenRouter and Vibe are pay-per-token and have no subscription quota
    # tracked by cclimits — treat them as always available. Rate-limit recovery
    # happens via the provider's own cooldown on HTTP 429. This unconditional
    # True is exactly what makes them uncapped, which is why _allows() fails
    # closed for the same set — one constant, so the two cannot drift.
    if name in _UNCAPPED_PROVIDERS:
        return True
    # Gemini in HTTP-API mode (GEMINI_API_KEY set) has no pollable subscription
    # quota either — the consumer CLI/OAuth endpoint cclimits reads is dead. Treat
    # it as available; rate-limit recovery is cooldown-driven, exactly like OpenRouter.
    if name == "gemini" and config.GEMINI_API_KEY:
        return True
    return getattr(limits, name).available


def has_explicit_provider_tag(task: str) -> bool:
    """Return True if the task text contains an explicit provider/model tag."""
    task_lower = task.lower()
    return any(_TAG_RE_BY_PROVIDER[tag].search(task_lower) for tag in _TAG_MAP)


def resolve_forced_provider(task: str, force_name: str | None = None) -> BaseProvider | None:
    """Return the provider a task explicitly forces — via *force_name* or a
    #provider/#model tag — or None if none is forced / the tagged provider isn't
    registered (e.g. openrouter without an API key). Shared by select_provider()
    and force_refresh_can_unblock() so both agree on which provider is forced."""
    if force_name and force_name in _providers:
        return _providers[force_name]
    task_lower = task.lower()
    for tag, provider_name in _TAG_MAP.items():
        if _TAG_RE_BY_PROVIDER[tag].search(task_lower):
            provider = _providers.get(provider_name)
            if provider is not None:
                return provider
    return None


def _tags_unregistered_reviewer_only(task: str) -> bool:
    """True when the task tags a reviewer-only provider that is not registered.

    That is the one case where falling through to the default chain would hand a
    deliberately non-writing review job to a file-writing executor. Everything
    else (unknown tag, unregistered OpenRouter) keeps the existing fall-through.
    """
    task_lower = task.lower()
    for tag, provider_name in _TAG_MAP.items():
        if provider_name not in _REVIEWER_ONLY or provider_name in _providers:
            continue
        if _TAG_RE_BY_PROVIDER[tag].search(task_lower):
            return True
    return False


def select_provider(
    task: str,
    limits: AllLimits,
    exclude: set[str] | None = None,
    profile=None,  # ProfileConfig | None
    force_name: str | None = None,
    strict: bool = False,
    tool_name: str | None = None,
) -> BaseProvider | None:
    """
    Returns the best available provider for this task, or None if all are blocked.
    If 'force_name' is given or the task contains a #provider tag, that provider is tried first.
    If strict=True and a provider is forced (via tag or force_name), ONLY that provider is
    considered — no fallback to other providers.
    If a profile is given, its provider order overrides the default priority.
    If tool_name is given, allowed providers are filtered via PolicyEngine.
    """
    # Resolve an explicitly forced provider (via force_name or a #provider/#model
    # tag). Returns None when a tagged provider isn't registered (e.g. openrouter
    # without an API key), so the task falls through to the default chain.
    forced = resolve_forced_provider(task, force_name)

    # Reviewer-only providers do not degrade into executors — see _REVIEWER_ONLY.
    if forced is None and _tags_unregistered_reviewer_only(task):
        return None

    # Tool Policy Layering: task tag → profile → policy.yaml (see _allowed_by_policy),
    # profile provider order, strict/forced handling — all of it lives in
    # _selection_order() so policy_dead_end() judges the exact same order.
    order, allowed_by_policy = _selection_order(task, profile, force_name, strict, tool_name)

    # A forced provider (#openrouter / #vibe / force_name) used to be prepended
    # AFTER the policy filter, which made the filter a no-op for exactly the two
    # providers it exists to bar. Reject instead of quietly routing elsewhere: in
    # an unattended run a silent swap to a different provider is worse than a
    # clean stop, because nobody sees which model actually did the work.
    # (A forced provider that IS allowed always heads a non-empty order, so an
    # empty one here can only mean the policy barred it.)
    if forced and not order:
        print(
            f"  [policy] Provider '{forced.name}' ist für "
            f"{'Tool ' + tool_name if tool_name else 'diesen Task'} nicht zugelassen "
            f"(erlaubt: {', '.join(_effective_allowed(allowed_by_policy))}) — kein "
            f"Fallback auf einen anderen Provider."
        )
        return None

    excluded = exclude or set()

    for name in order:
        if name in excluded:
            continue
        if name not in _providers:
            continue
        provider = _providers[name]
        if provider.is_cooling_down():
            print(f"  [{name}] Cooldown aktiv, noch {provider.cooldown_remaining_str()}")
            continue
        if not _limits_ok(name, limits):
            lim = getattr(limits, name)
            print(f"  [{name}] Kein Capacity ({lim.remaining_pct:.1f}% remaining, error='{lim.error}')")
            continue
        return provider

    return None


def force_refresh_can_unblock(
    task: str,
    limits: AllLimits,
    *,
    force_name: str | None = None,
    strict: bool = False,
) -> bool:
    """True iff select_provider() returned None only because a provider this task can
    actually route to is mid OAuth-token-refresh (a transient "expired" snapshot) — so
    a synchronous limits force_refresh could plausibly turn that None into a provider.

    For a strict/forced task only the forced provider is routable, so an unrelated
    provider's expired token must NOT trigger a refresh (that would waste a synchronous
    refresh while the forced provider is genuinely capacity-exhausted). For non-forced
    tasks any provider counts, since a refresh could open up a fallback."""
    if strict:
        forced = resolve_forced_provider(task, force_name)
        if forced is not None:
            return is_transient_token_refresh(getattr(limits, forced.name, ProviderLimits()))
    return limits.has_transient_token_refresh()


def get_provider_by_name(name: str) -> BaseProvider | None:
    """Return a provider instance by name, or None if unknown.

    POLICY-BLIND on purpose — this is the raw registry lookup. Tool code must use
    ``get_provider_for_tool()`` / ``policy_provider_lookup()`` instead, otherwise the
    tool_providers policy is bypassed (that is how OpenRouter and Vibe stayed
    reachable in six tools after being barred from unattended runs).
    """
    return _providers.get(name)


def all_providers() -> list[BaseProvider]:
    return list(_providers.values())


def earliest_cooldown_reset() -> int | None:
    """Return seconds until the earliest provider cooldown ends, or None if none are in cooldown."""
    times = [
        int(p.cooldown_remaining()) for p in _providers.values()
        if p.is_cooling_down()
    ]
    if not times:
        return None
    return min(times)
