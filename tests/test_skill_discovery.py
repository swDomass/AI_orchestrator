from skills.discovery import parse_skill_md


def test_parse_skill_md_accepts_empty_frontmatter(tmp_path):
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\n---\nPrompt body\n", encoding="utf-8")

    skill = parse_skill_md(skill_file)

    assert skill is not None
    assert skill.name == "demo-skill"
    assert skill.description == ""
    assert skill.prompt == "Prompt body"
