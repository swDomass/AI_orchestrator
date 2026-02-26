from pathlib import Path
from typing import Optional
from skills.discovery import SkillConfig, discover_skills

_cache: dict[str, SkillConfig] = {}


def load_skill(name: str, cwd: Path | None = None, vault_path: Path | None = None) -> Optional[SkillConfig]:
    cache_key = f"{name}::{cwd}::{vault_path}"
    if cache_key not in _cache:
        skills = discover_skills(cwd=cwd, vault_path=vault_path)
        if name in skills:
            _cache[cache_key] = skills[name]
    return _cache.get(cache_key)


def invalidate_cache() -> None:
    _cache.clear()
