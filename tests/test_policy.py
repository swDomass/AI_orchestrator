import pytest
from pathlib import Path
from policy import PolicyEngine, TIER_AUTO, TIER_APPROVE, TIER_DENY, PolicyRule

def test_policy_rule_exact_match():
    rule = PolicyRule(pattern="git commit", message="git commit matched", tier=TIER_AUTO)
    assert rule.matches("git commit -m 'test'") is True
    assert rule.matches("git status") is False

def test_policy_rule_regex_match():
    rule = PolicyRule(pattern="git push.*main", message="pushing to main", tier=TIER_DENY)
    assert rule.matches("git push origin main") is True
    assert rule.matches("git push origin develop") is False

def test_policy_engine_classification(tmp_path):
    policy_file = tmp_path / "99_System" / "AI" / "policy.yaml"
    policy_file.parent.mkdir(parents=True)
    policy_file.write_text(
        """auto:
  - "pytest"
approve:
  - pattern: "git push"
    message: "pushing to remote"
deny:
  - "rm -rf /"
""",
        encoding="utf-8"
    )
    
    engine = PolicyEngine(vault_path=tmp_path)
    
    tier, reasons = engine.check_task("pytest")
    assert tier == TIER_AUTO
    assert reasons == ["pytest"]
    
    tier, reasons = engine.check_task("git push origin master")
    assert tier == TIER_APPROVE
    assert reasons == ["pushing to remote"]
    
    tier, reasons = engine.check_task("rm -rf /tmp")
    assert tier == TIER_DENY
    assert reasons == ["rm -rf /"]
    
    tier, reasons = engine.check_task("ls -la")
    assert tier == TIER_AUTO
    assert reasons == []

def test_preapprovals():
    engine = PolicyEngine(vault_path=Path("/tmp"))
    assert engine.is_preapproved("push") is False
    engine.add_preapproval("push")
    assert engine.is_preapproved("push") is True
    assert engine.is_preapproved("PUSH") is True # Case insensitive

def test_policy_engine_multi_match(tmp_path):
    policy_file = tmp_path / "99_System" / "AI" / "policy.yaml"
    policy_file.parent.mkdir(parents=True)
    policy_file.write_text(
        """approve:
  - pattern: "git push"
    message: "push"
  - pattern: "npm publish"
    message: "publish"
""",
        encoding="utf-8"
    )
    engine = PolicyEngine(vault_path=tmp_path)
    tier, reasons = engine.check_task("git push and npm publish")
    assert tier == TIER_APPROVE
    assert set(reasons) == {"push", "publish"}
