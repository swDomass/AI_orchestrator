import re
from pathlib import Path

from policy import (
    TIER_APPROVE,
    TIER_AUTO,
    TIER_DENY,
    PolicyEngine,
    PolicyRule,
    reason_matches_preapproval,
)


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

def test_policy_engine_matches_case_insensitively(tmp_path):
    """Pins the IGNORECASE semantics PolicyRule.search() inherited from matches()."""
    policy_file = tmp_path / "99_System" / "AI" / "policy.yaml"
    policy_file.parent.mkdir(parents=True)
    policy_file.write_text(
        """approve:
  - pattern: "git push"
    message: "pushing to remote"
""",
        encoding="utf-8"
    )
    engine = PolicyEngine(vault_path=tmp_path)

    tier, reasons = engine.check_task("GIT PUSH origin x")
    assert tier == TIER_APPROVE
    assert reasons == ["pushing to remote"]

def test_policy_rule_invalid_regex_falls_back_to_literal_match():
    """An unparseable pattern is matched as escaped literal text, not dropped."""
    rule = PolicyRule(pattern="a(b", message="literal a(b", tier=TIER_APPROVE)
    try:
        matched = rule.matches("a(b")
    except re.error as exc:  # no fallback at all: the pattern error escapes
        matched = exc
    assert matched is True, f"escape fallback did not match the literal text: {matched!r}"
    assert rule.matches("ab") is False

def test_preapprovals():
    engine = PolicyEngine(vault_path=Path("/tmp"))
    assert engine.is_preapproved("push") is False
    engine.add_preapproval("push")
    assert engine.is_preapproved("push") is True
    assert engine.is_preapproved("PUSH") is True # Case insensitive
    assert engine.is_preapproved("git push to remote") is True


def test_reason_matches_preapproval_category_tokens():
    assert reason_matches_preapproval("git push to remote", "push") is True
    assert reason_matches_preapproval("npm publish package", "publish") is True
    assert reason_matches_preapproval("git push to remote", "publish") is False

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
