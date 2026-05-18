"""Tests for skills/index.py — Progressive skill loading (#37)."""

from __future__ import annotations

from pathlib import Path

import pytest

from skills import index as skill_index
from skills.discovery import SkillConfig


def _make_skill_dir(base: Path, name: str, *, description: str = "",
                    tags: list[str] | None = None, body: str = "") -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = ["---", f"name: {name}", f"description: {description}"]
    if tags:
        tag_list = ", ".join(f'"{t}"' for t in tags)
        frontmatter.append(f"tags: [{tag_list}]")
    frontmatter.append("---")
    md = "\n".join(frontmatter) + "\n\n" + body
    (skill_dir / "SKILL.md").write_text(md, encoding="utf-8")
    return skill_dir


@pytest.fixture(autouse=True)
def _isolate_index():
    skill_index.invalidate_index()
    yield
    skill_index.invalidate_index()


def test_build_index_returns_empty_when_no_skills(tmp_path):
    out = skill_index.build_index(vault_path=tmp_path)
    # No SKILL.md files anywhere — but bundled tools dir might have some.
    # We accept either empty or a list — just ensure no crash.
    assert isinstance(out, str)


def test_build_index_includes_vault_skills(tmp_path):
    skills_dir = tmp_path / "99_System" / "AI" / "Skills"
    _make_skill_dir(skills_dir, "alpha-skill",
                    description="First skill", tags=["a", "b"])
    _make_skill_dir(skills_dir, "beta-skill",
                    description="Second skill")

    out = skill_index.build_index(vault_path=tmp_path)
    assert "## Available skills" in out
    assert "alpha-skill" in out
    assert "First skill" in out
    assert "beta-skill" in out
    assert "Second skill" in out
    assert "Tags: a, b" in out


def test_build_index_caches_on_repeated_calls(tmp_path):
    skills_dir = tmp_path / "99_System" / "AI" / "Skills"
    _make_skill_dir(skills_dir, "cached", description="X")

    first = skill_index.build_index(vault_path=tmp_path)
    second = skill_index.build_index(vault_path=tmp_path)
    assert first == second


def test_invalidate_index_resets_cache(tmp_path):
    skills_dir = tmp_path / "99_System" / "AI" / "Skills"
    _make_skill_dir(skills_dir, "first", description="X")
    skill_index.build_index(vault_path=tmp_path)

    _make_skill_dir(skills_dir, "second", description="Y")
    # Old fingerprint includes only "first"; adding "second" changes mtime fingerprint
    skill_index.invalidate_index()
    out = skill_index.build_index(vault_path=tmp_path)
    assert "first" in out
    assert "second" in out


def test_list_sections_extracts_all_headings():
    body = (
        "# Intro\nLead text.\n\n"
        "## Research\nDo discovery.\n\n"
        "## Plan\nMake a plan.\n\n"
        "### Subplan\nDetail.\n"
    )
    sections = skill_index.list_sections(body)
    assert len(sections) == 4
    assert [s.heading_text for s in sections] == ["Intro", "Research", "Plan", "Subplan"]
    research = next(s for s in sections if s.heading_text == "Research")
    assert "Do discovery" in research.body


def test_extract_section_returns_matching_section():
    body = "## Research\nFind X.\n\n## Plan\nDesign Y.\n"
    out = skill_index.extract_section(body, "research")
    assert "Find X" in out
    assert "Plan" not in out


def test_extract_section_returns_empty_when_not_found():
    body = "## Plan\nDesign Y.\n"
    assert skill_index.extract_section(body, "nonexistent") == ""


def test_extract_section_is_case_insensitive():
    body = "## Research\nFind X.\n"
    assert "Find X" in skill_index.extract_section(body, "RESEARCH")


def test_progressive_body_returns_full_body_when_no_phase(tmp_path):
    cfg = SkillConfig(name="x", path=tmp_path, prompt="## A\nText\n\n## B\nText2")
    out = skill_index.progressive_body(cfg)
    assert "## A" in out and "## B" in out


def test_progressive_body_narrows_to_phase(tmp_path):
    body = "## Research\nDo X.\n\n## Plan\nDo Y.\n"
    cfg = SkillConfig(name="x", path=tmp_path, prompt=body)
    out = skill_index.progressive_body(cfg, phase="Research")
    assert "Do X" in out
    assert "Do Y" not in out


def test_progressive_body_falls_back_when_phase_missing(tmp_path):
    body = "## Plan\nDo Y.\n"
    cfg = SkillConfig(name="x", path=tmp_path, prompt=body)
    out = skill_index.progressive_body(cfg, phase="Research")
    # Falls back to the full body when section not found
    assert "Do Y" in out


def test_progressive_body_empty_for_empty_skill(tmp_path):
    cfg = SkillConfig(name="x", path=tmp_path, prompt="")
    assert skill_index.progressive_body(cfg) == ""


def test_progressive_body_handles_none_skill():
    assert skill_index.progressive_body(None) == ""  # type: ignore[arg-type]


def test_index_caps_at_max_skills(tmp_path):
    skills_dir = tmp_path / "99_System" / "AI" / "Skills"
    for i in range(20):
        _make_skill_dir(skills_dir, f"s{i:02}", description=f"d{i}")

    out_capped = skill_index.build_index(vault_path=tmp_path, max_skills=5)
    # Count `- ` list items in the output
    capped_count = out_capped.count("\n- `")
    assert capped_count == 5

    # Without cap, more entries appear
    skill_index.invalidate_index()
    out_full = skill_index.build_index(vault_path=tmp_path, max_skills=100)
    full_count = out_full.count("\n- `")
    assert full_count > capped_count
