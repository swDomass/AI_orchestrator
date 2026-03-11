"""
Execution profiles for the AI Orchestrator.

Profiles are YAML files that bundle provider order, allowed roots,
skill allow/deny lists, timeouts, and safety settings.

Tag syntax in queue:
    - [ ] Task description #agent:work
    - [ ] Another task #agent:personal

Search order:
    1. vault/99_System/AI/profiles/<name>.yaml
    2. ./profiles/<name>.yaml  (repo-local)
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_profile_cache: dict[str, tuple[float, "ProfileConfig | None"]] = {}


@dataclass
class ProfileConfig:
    name: str
    providers: list[str] = field(default_factory=lambda: ["claude", "gemini", "codex"])
    allowed_roots: list[str] = field(default_factory=list)
    allowed_skills: list[str] = field(default_factory=list)   # empty = allow all
    denied_skills: list[str] = field(default_factory=list)
    timeout_minutes: int = 0   # 0 = use TASK_TIMEOUT_SEC default
    sandbox: str = "off"       # off | ro | rw
    safety_level: str = "standard"  # strict | standard | yolo
    policy: dict = field(default_factory=dict)   # {"auto": [...], "approve": [...], "deny": [...]}
    tool_providers: dict[str, list[str]] = field(default_factory=dict)  # {"tool_name": ["p1", "p2"]}


def get_default_profile() -> ProfileConfig:
    return ProfileConfig(name="default")


def _load_yaml_safe(path: Path) -> dict | None:
    """Load YAML file, return None on error."""
    try:
        import yaml  # pyyaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return None
        return data
    except Exception as e:
        logger.warning("profiles: could not load %s: %s", path, e)
        return None


def load_profile(name: str, vault_path: Path) -> "ProfileConfig | None":
    """Load a named profile from vault or repo-local directory.

    Returns None if the profile file is not found.
    Uses mtime-based caching so edits are picked up without restart.
    """
    from config import PROFILES_DIR

    candidates: list[Path] = [
        vault_path / "99_System" / "AI" / "profiles" / f"{name}.yaml",
        PROFILES_DIR / f"{name}.yaml",
        Path(__file__).parent / "profiles" / f"{name}.yaml",
    ]

    # Deduplicate while preserving order
    seen: set[str] = set()
    search_paths: list[Path] = []
    for p in candidates:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            search_paths.append(p)

    for path in search_paths:
        if not path.exists():
            continue

        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue

        cached_mtime, cached_cfg = _profile_cache.get(str(path), (0.0, None))
        if cached_mtime == mtime and cached_cfg is not None:
            return cached_cfg

        data = _load_yaml_safe(path)
        if data is None:
            continue

        cfg = _build_profile_config(name, data)
        _profile_cache[str(path)] = (mtime, cfg)
        logger.debug("profiles: loaded '%s' from %s", name, path)
        return cfg

    return None


def _build_profile_config(name: str, data: dict) -> ProfileConfig:
    """Build a ProfileConfig from a YAML dict, applying defaults."""
    def _list(key: str) -> list:
        v = data.get(key, [])
        return list(v) if v else []

    providers = _list("providers") or ["claude", "gemini", "codex"]
    # Validate provider names
    known = {"claude", "gemini", "codex"}
    providers = [p for p in providers if p in known] or ["claude", "gemini", "codex"]

    return ProfileConfig(
        name=data.get("name", name),
        providers=providers,
        allowed_roots=_list("allowed_roots"),
        allowed_skills=_list("allowed_skills"),
        denied_skills=_list("denied_skills"),
        timeout_minutes=int(data.get("timeout_minutes", 0)),
        sandbox=str(data.get("sandbox", "off")),
        safety_level=str(data.get("safety_level", "standard")),
        policy=data.get("policy") or {},
        tool_providers=data.get("tool_providers") or {},
    )
