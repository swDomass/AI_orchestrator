from pathlib import Path
from typing import Optional
from skills.discovery import SkillConfig, discover_skills

_cache: dict[str, tuple[float, SkillConfig]] = {}  # key → (mtime, config)


def load_skill(name: str, cwd: Path | None = None, vault_path: Path | None = None) -> Optional[SkillConfig]:
    cache_key = f"{name}::{cwd}::{vault_path}"
    if cache_key in _cache:
        cached_mtime, cached_cfg = _cache[cache_key]
        # Check if the SKILL.md file still has the same mtime
        skill_md = cached_cfg.path / "SKILL.md" if cached_cfg.path else None
        if skill_md and skill_md.exists():
            try:
                current_mtime = skill_md.stat().st_mtime
                if current_mtime == cached_mtime:
                    return cached_cfg
            except OSError:
                pass
        elif skill_md and not skill_md.exists():
            del _cache[cache_key]

    skills = discover_skills(cwd=cwd, vault_path=vault_path)
    if name in skills:
        cfg = skills[name]
        mtime = 0.0
        skill_md = cfg.path / "SKILL.md" if cfg.path else None
        if skill_md and skill_md.exists():
            try:
                mtime = skill_md.stat().st_mtime
            except OSError:
                pass
        _cache[cache_key] = (mtime, cfg)
        return cfg
    return None


def invalidate_cache() -> None:
    _cache.clear()
