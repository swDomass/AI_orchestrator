"""
Progressive skill loading — keep an always-present INDEX of skill summaries
and pull the full SKILL.md body only when the task tag matches.

INDEX format
------------

The index is a Markdown block, ~30 tokens per skill::

    ## Available skills (always-present index)
    - `dev-loop` — Iterative Research→Plan→Execute→Review dev workflow
      Tags: dev, refactor, fix
    - `review-loop` — Iterative code review with P1/P2/P3 findings
      Tags: review, quality
    ...

It is built once per process (mtime-cached) and reused on every prompt
assembly so we don't read every SKILL.md on every task.

Lazy section loading
--------------------

When a tool body is injected, the caller may pass ``phase=...`` to limit the
injected text to the matching ``## <Phase>`` section. Falls back to the full
body when the section is missing.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from skills.discovery import SkillConfig, discover_skills

logger = logging.getLogger(__name__)

_INDEX_LOCK = threading.Lock()
_index_cache: tuple[str, frozenset[tuple[Path, float]]] | None = None  # (text, fingerprint)


def _fingerprint(skills: dict[str, SkillConfig]) -> frozenset[tuple[Path, float]]:
    """Stable fingerprint of skill SKILL.md mtimes — used to detect changes."""
    out: list[tuple[Path, float]] = []
    for cfg in skills.values():
        skill_md = cfg.path / "SKILL.md" if cfg.path else None
        if skill_md and skill_md.exists():
            try:
                out.append((skill_md, skill_md.stat().st_mtime))
            except OSError:
                continue
    return frozenset(out)


def build_index(
    *,
    cwd: Path | None = None,
    vault_path: Path | None = None,
    max_skills: int = 50,
) -> str:
    """Return the always-present skill index block (sorted by name, capped)."""
    skills = discover_skills(cwd=cwd, vault_path=vault_path)
    if not skills:
        return ""

    fp = _fingerprint(skills)
    global _index_cache
    with _INDEX_LOCK:
        if _index_cache is not None and _index_cache[1] == fp:
            return _index_cache[0]

    lines = ["## Available skills (always-present index)"]
    for name in sorted(skills.keys())[:max_skills]:
        cfg = skills[name]
        desc = (cfg.description or "").strip().replace("\n", " ")
        if not desc:
            desc = "(no description)"
        tags_block = ", ".join(cfg.tags) if cfg.tags else ""
        if tags_block:
            lines.append(f"- `{name}` — {desc}\n  Tags: {tags_block}")
        else:
            lines.append(f"- `{name}` — {desc}")

    text = "\n".join(lines)
    with _INDEX_LOCK:
        _index_cache = (text, fp)
    return text


def invalidate_index() -> None:
    """Drop cached index (test helper or after SKILL.md edits)."""
    global _index_cache
    with _INDEX_LOCK:
        _index_cache = None


# ---------------------------------------------------------------------------
# Lazy section extraction
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class SkillSection:
    heading_level: int
    heading_text: str
    body: str


def list_sections(skill_body: str) -> list[SkillSection]:
    """Return one SkillSection per heading (anything from H1 to H6)."""
    sections: list[SkillSection] = []
    matches = list(_HEADING_RE.finditer(skill_body))
    if not matches:
        return sections

    for i, m in enumerate(matches):
        level = len(m.group(1))
        heading = m.group(2).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(skill_body)
        body = skill_body[body_start:body_end].strip()
        sections.append(SkillSection(level, heading, body))
    return sections


def extract_section(skill_body: str, section_name: str) -> str:
    """Return ``## <section_name>`` body, or empty string if not found.

    Matching is case-insensitive on heading text (after stripping the leading
    ``#`` markers). The first heading at level 1-3 whose text contains
    ``section_name`` wins.
    """
    if not section_name:
        return ""
    needle = section_name.lower().strip()
    for sec in list_sections(skill_body):
        if sec.heading_level > 3:
            continue
        if needle in sec.heading_text.lower():
            return f"## {sec.heading_text}\n{sec.body}".strip()
    return ""


def progressive_body(
    skill: SkillConfig,
    *,
    phase: str | None = None,
) -> str:
    """Return the skill body, optionally narrowed to a phase section.

    Falls back to the full body when ``phase`` is None or no matching section
    is found — so callers can safely opt in without risking empty injection.
    """
    if not skill or not skill.prompt:
        return ""
    if phase:
        section = extract_section(skill.prompt, phase)
        if section:
            return section
    return skill.prompt
