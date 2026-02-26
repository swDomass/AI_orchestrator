import pytest
from pathlib import Path
from profiles import ProfileConfig, load_profile, _build_profile_config

def test_build_profile_config_defaults():
    data = {}
    cfg = _build_profile_config("test", data)
    assert cfg.name == "test"
    assert cfg.providers == ["claude", "gemini", "codex"]
    assert cfg.allowed_roots == []
    assert cfg.allowed_skills == []
    assert cfg.denied_skills == []
    assert cfg.timeout_minutes == 0
    assert cfg.sandbox == "off"
    assert cfg.safety_level == "standard"

def test_build_profile_config_custom():
    data = {
        "name": "Custom Name",
        "providers": ["gemini", "unknown", "claude"],
        "allowed_roots": ["/tmp"],
        "allowed_skills": ["skill1"],
        "denied_skills": ["skill2"],
        "timeout_minutes": 10,
        "sandbox": "rw",
        "safety_level": "strict"
    }
    cfg = _build_profile_config("test", data)
    assert cfg.name == "Custom Name"
    # "unknown" should be filtered out
    assert cfg.providers == ["gemini", "claude"]
    assert cfg.allowed_roots == ["/tmp"]
    assert cfg.allowed_skills == ["skill1"]
    assert cfg.denied_skills == ["skill2"]
    assert cfg.timeout_minutes == 10
    assert cfg.sandbox == "rw"
    assert cfg.safety_level == "strict"

def test_load_profile_not_found(tmp_path):
    # Should return None if no profile file is found
    cfg = load_profile("nonexistent", tmp_path)
    assert cfg is None

def test_load_profile_from_vault(tmp_path):
    profiles_dir = tmp_path / "99_System" / "AI" / "profiles"
    profiles_dir.mkdir(parents=True)
    profile_file = profiles_dir / "work.yaml"
    profile_file.write_text("""providers:
  - gemini
timeout_minutes: 5""", encoding="utf-8")
    
    cfg = load_profile("work", tmp_path)
    assert cfg is not None
    assert cfg.name == "work"
    assert cfg.providers == ["gemini"]
    assert cfg.timeout_minutes == 5
