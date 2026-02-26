import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

@dataclass
class SkillConfig:
    name: str
    path: Path
    description: str = ""
    version: str = "1.0"
    requires: Dict[str, List[str]] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)
    prompt: str = ""

def parse_skill_md(file_path: Path) -> Optional[SkillConfig]:
    """Parse SKILL.md file and return SkillConfig."""
    if not file_path.exists():
        return None
    
    try:
        content = file_path.read_text(encoding="utf-8")
        if content.startswith("---"):
            _, frontmatter, body = content.split("---", 2)
            data = yaml.safe_load(frontmatter)
            if data is None:
                data = {}
            elif not isinstance(data, dict):
                data = {}
            
            return SkillConfig(
                name=data.get("name", file_path.parent.name),
                path=file_path.parent,
                description=data.get("description", ""),
                version=str(data.get("version", "1.0")),
                requires=data.get("requires", {}),
                tags=data.get("tags", []),
                config=data.get("config", {}),
                prompt=body.strip()
            )
        else:
            # No frontmatter, just body
            return SkillConfig(
                name=file_path.parent.name,
                path=file_path.parent,
                prompt=content.strip()
            )
    except Exception as e:
        print(f"Error parsing {file_path}: {e}")
        return None

def discover_skills(cwd: Optional[Path] = None, vault_path: Optional[Path] = None) -> Dict[str, SkillConfig]:
    """
    Discover skills in all 4 locations with precedence:
    1. Task CWD: <cwd>/.orchestrator/skills/<name>/SKILL.md
    2. Repo-local: ./skills/<name>/SKILL.md
    3. Vault: 99_System/AI/Skills/<name>/SKILL.md
    4. Bundled: ./tools/<name>/SKILL.md
    """
    skills: Dict[str, SkillConfig] = {}
    
    # 4. Bundled (current tools directory)
    bundled_dir = Path(__file__).parent.parent / "tools"
    _scan_dir(bundled_dir, skills)
    
    # 3. Vault
    if vault_path:
        vault_skills_dir = vault_path / "99_System" / "AI" / "Skills"
        _scan_dir(vault_skills_dir, skills)
        
    # 2. Repo-local
    repo_skills_dir = Path(__file__).parent.parent / "skills"
    _scan_dir(repo_skills_dir, skills)
    
    # 1. Task CWD
    if cwd:
        cwd_skills_dir = cwd / ".orchestrator" / "skills"
        _scan_dir(cwd_skills_dir, skills)
        
    return skills

def _scan_dir(base_dir: Path, skills_dict: Dict[str, SkillConfig]):
    """Scan a directory for skills and add to dict (overwriting existing)."""
    if not base_dir.exists() or not base_dir.is_dir():
        return
        
    for item in base_dir.iterdir():
        if item.is_dir():
            skill_md = item / "SKILL.md"
            if skill_md.exists():
                skill = parse_skill_md(skill_md)
                if skill:
                    if skill.name in skills_dict:
                        print(f"  [skills] '{skill.name}' shadows existing skill from lower-priority location")
                    skills_dict[skill.name] = skill
