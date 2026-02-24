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

from limits import AllLimits
from providers.base import BaseProvider
from providers import ClaudeProvider, GeminiProvider, CodexProvider

# Tag in task text to force a specific provider
_TAG_MAP = {
    "#claude": "claude",
    "#gemini": "gemini",
    "#codex": "codex",
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


def select_provider(task: str, limits: AllLimits, exclude: set[str] | None = None) -> BaseProvider | None:
    """
    Returns the best available provider for this task, or None if all are blocked.
    If the task contains a #provider tag, that provider is tried first.
    """
    # Check for explicit provider tag
    forced = next(
        (_providers[v] for tag, v in _TAG_MAP.items() if tag in task.lower()),
        None
    )

    order = _PRIORITY[:]
    if forced:
        # Move forced provider to front
        order = [forced.name] + [n for n in order if n != forced.name]

    excluded = exclude or set()

    for name in order:
        if name in excluded:
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

