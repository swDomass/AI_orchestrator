"""
Selects the best available provider for a given task.

Routing priority:
  1. Claude  - best quality, default choice
  2. Gemini  - fallback, great for long context (CLI picks tier internally)
  3. Codex   - fallback, good for code tasks

A provider is skipped if:
  - cclimits shows < 5% remaining capacity
  - It is in cooldown (unreachable / error within last 30 min)

OpenRouter (pay-per-token) and Vibe (Mistral, pay-per-token) are registered but
never part of that chain — they run only when a task tags them explicitly.
"""

import re

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
_PRIORITY = ["claude", "gemini", "codex"]

# Providers whose whole point is that they do NOT write. Falling back from one of
# these to the default chain would silently swap a non-writing reviewer for a
# file-writing executor — a wider blast radius than the task asked for. For
# OpenRouter the same fallback is harmless (executor → executor); here it is not.
# So: an explicit tag for a reviewer-only provider that isn't registered yields
# no provider at all, and the task is parked instead of quietly escalated.
_REVIEWER_ONLY = {"vibe"}


def _limits_ok(name: str, limits: AllLimits) -> bool:
    # OpenRouter is pay-per-token and has no subscription quota tracked by
    # cclimits — treat it as always available. Rate-limit recovery happens
    # via the provider's own cooldown on HTTP 429.
    if name in ("openrouter", "vibe"):
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

    # Tool Policy Layering: filter allowed providers for this tool
    allowed_by_policy = None
    
    # 1. Task level (#tool_providers:p1,p2)
    try:
        from queue_manager import extract_tool_providers
        allowed_by_policy = extract_tool_providers(task)
    except (ImportError, ValueError, AttributeError):
        pass
    
    # 2. Profile level (profile.tool_providers)
    if allowed_by_policy is None:
        if profile and tool_name and hasattr(profile, "tool_providers"):
            allowed_by_policy = profile.tool_providers.get(tool_name)
    
    # 3. Global level (policy.yaml)
    if allowed_by_policy is None:
        try:
            from policy import get_engine
            allowed_by_policy = get_engine().get_allowed_providers(tool_name)
        except (ImportError, ValueError, AttributeError):
            pass

    # Profile provider order overrides _PRIORITY
    if profile and getattr(profile, "providers", None):
        base_order = [p for p in profile.providers if p in _providers]
    else:
        base_order = _PRIORITY[:]

    # Filter base_order by policy if applicable
    if allowed_by_policy:
        base_order = [p for p in base_order if p in allowed_by_policy]

    if forced:
        if strict:
            # Strict mode: only try the forced provider, no fallback
            order = [forced.name]
        else:
            # Move forced provider to front within the allowed order
            order = [forced.name] + [n for n in base_order if n != forced.name]
    else:
        order = base_order

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

    Used by tools that need cross-provider support (e.g. critical-review multi-pass).
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
