"""
Selects the best available provider for a given task.

Routing priority:
  1. Claude  - best quality, default choice
  2. Gemini  - fallback, great for long context (CLI picks tier internally)
  3. Codex   - fallback, good for code tasks

A provider is skipped if:
  - cclimits shows < 5% remaining capacity
  - It is in cooldown (unreachable / error within last 30 min)
"""

import re

from limits import AllLimits
from providers.base import BaseProvider
from providers import ClaudeProvider, GeminiProvider, CodexProvider

# Tag in task text to force a specific provider.
# Model-specific tags also select their owning provider.
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
}

_TAG_RE_BY_PROVIDER = {
    tag: re.compile(rf"(?<!\S){re.escape(tag)}(?![\w-])")
    for tag in _TAG_MAP
}

# Singleton provider instances (carry cooldown state across calls)
_providers: dict[str, BaseProvider] = {
    "claude": ClaudeProvider(),
    "gemini": GeminiProvider(),
    "codex": CodexProvider(),
}

# Priority order
_PRIORITY = ["claude", "gemini", "codex"]


def _limits_ok(name: str, limits: AllLimits) -> bool:
    return getattr(limits, name).available


def has_explicit_provider_tag(task: str) -> bool:
    """Return True if the task text contains an explicit provider/model tag."""
    task_lower = task.lower()
    return any(_TAG_RE_BY_PROVIDER[tag].search(task_lower) for tag in _TAG_MAP)


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
    # Check for explicit provider tag
    task_lower = task.lower()
    forced = None
    if force_name and force_name in _providers:
        forced = _providers[force_name]
    else:
        forced = next(
            (_providers[v] for tag, v in _TAG_MAP.items() if _TAG_RE_BY_PROVIDER[tag].search(task_lower)),
            None
        )

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
