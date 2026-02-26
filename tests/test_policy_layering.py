"""Tests for profile-level policy layering (Feature #10).

Layering hierarchy (highest → lowest priority):
    task #approve: tags  →  profile.policy  →  global policy.yaml
"""
import pytest
from pathlib import Path
import policy as policy_module
from policy import PolicyEngine, TIER_AUTO, TIER_APPROVE, TIER_DENY


def _make_engine(tmp_path, global_yaml: str) -> PolicyEngine:
    """Create a PolicyEngine with a custom global policy YAML."""
    policy_file = tmp_path / "99_System" / "AI" / "policy.yaml"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(global_yaml, encoding="utf-8")
    return PolicyEngine(vault_path=tmp_path)


def test_profile_auto_overrides_global_approve(tmp_path):
    """Profile AUTO for 'git push' beats global APPROVE for the same pattern."""
    engine = _make_engine(tmp_path, 'approve:\n  - "git push"\n')
    tier, msgs = engine.check_task(
        "git push origin main",
        profile_rules={"auto": ["git push"]},
    )
    assert tier == TIER_AUTO
    assert msgs == ["git push"]


def test_profile_auto_overrides_global_deny(tmp_path):
    """Profile AUTO for 'git push' beats global DENY for the same pattern."""
    engine = _make_engine(tmp_path, 'deny:\n  - "git push"\n')
    tier, msgs = engine.check_task(
        "git push origin main",
        profile_rules={"auto": ["git push"]},
    )
    assert tier == TIER_AUTO
    assert msgs == ["git push"]


def test_profile_deny_with_global_auto(tmp_path):
    """Profile DENY wins even when global would AUTO-approve."""
    engine = _make_engine(tmp_path, 'auto:\n  - "rm"\n')
    tier, msgs = engine.check_task(
        "rm /tmp/file.txt",
        profile_rules={"deny": ["rm"]},
    )
    assert tier == TIER_DENY
    assert msgs == ["rm"]


def test_no_profile_match_falls_back_to_global(tmp_path):
    """If profile has no matching rule the global result is used."""
    engine = _make_engine(tmp_path, 'approve:\n  - "npm publish"\n')
    # Profile only has a "git" rule — won't match "npm publish"
    tier, msgs = engine.check_task(
        "npm publish",
        profile_rules={"deny": ["git"]},
    )
    assert tier == TIER_APPROVE
    assert len(msgs) > 0


def test_empty_profile_policy_uses_global(tmp_path):
    """An empty profile_rules dict falls through to global rules."""
    engine = _make_engine(tmp_path, 'approve:\n  - "git push"\n')
    tier, msgs = engine.check_task("git push origin main", profile_rules={})
    assert tier == TIER_APPROVE


def test_none_profile_policy_uses_global(tmp_path):
    """profile_rules=None falls through to global rules (backwards compat)."""
    engine = _make_engine(tmp_path, 'deny:\n  - "rm -rf"\n')
    tier, msgs = engine.check_task("rm -rf /tmp", profile_rules=None)
    assert tier == TIER_DENY


def test_profile_policy_field_parsed_from_yaml(tmp_path):
    """ProfileConfig.policy is populated when the profile YAML has a policy section."""
    import yaml
    from profiles import _build_profile_config

    data = yaml.safe_load(
        """
name: work
providers: [claude, gemini]
policy:
  auto:
    - "git push"
    - "npm run"
  deny:
    - "rm -rf"
"""
    )
    cfg = _build_profile_config("work", data)

    assert cfg.policy == {
        "auto": ["git push", "npm run"],
        "deny": ["rm -rf"],
    }
    assert cfg.name == "work"
    assert cfg.providers == ["claude", "gemini"]


def test_profile_policy_cache_updates_when_same_dict_is_mutated(tmp_path):
    """Mutating the same profile_rules dict must not return a stale cached verdict."""
    engine = _make_engine(tmp_path, "")
    profile_rules = {"deny": ["git push"]}

    tier, _ = engine.check_task("git push origin main", profile_rules=profile_rules)
    assert tier == TIER_DENY

    profile_rules.clear()
    profile_rules["auto"] = ["git push"]

    tier, msgs = engine.check_task("git push origin main", profile_rules=profile_rules)
    assert tier == TIER_AUTO
    assert msgs == ["git push"]
