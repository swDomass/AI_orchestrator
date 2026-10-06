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


# --- match_excerpts follows check_task's profile-before-global layering (Codex r1) ---

def _engine_with(tmp_path, policy_yaml):
    policy_file = tmp_path / "99_System" / "AI" / "policy.yaml"
    policy_file.parent.mkdir(parents=True)
    policy_file.write_text(policy_yaml, encoding="utf-8")
    return PolicyEngine(vault_path=tmp_path)


def test_match_excerpts_quotes_the_text_that_made_the_approval_necessary(tmp_path):
    """Profile AUTO rule and global APPROVE rule share the message "harmless". The
    excerpt must come from the text the global rule decided, not from the one the
    profile classified AUTO."""
    engine = _engine_with(
        tmp_path,
        'approve:\n  - pattern: "git push"\n    message: "harmless"\n',
    )
    profile = {"auto": ["harmless"]}
    texts = ["harmless: fetch documentation", "git push origin main"]
    assert [engine.check_task(t, profile_rules=profile) for t in texts] == [
        (TIER_AUTO, ["harmless"]), (TIER_APPROVE, ["harmless"]),
    ]

    excerpts = engine.match_excerpts(texts, ["harmless"], profile_rules=profile)

    assert excerpts == {"harmless": "git push origin main"}


def test_match_excerpts_skips_global_rules_on_texts_the_profile_decided(tmp_path, monkeypatch):
    """A pathological global regex must not run on a text whose classification the
    profile settled (measured before the fix: 1.6 s for n=24, check_task 0.000 s).
    Counted rather than timed: every PolicyRule.search call is recorded."""
    engine = _engine_with(
        tmp_path,
        'approve:\n  - pattern: "(a+)+$"\n    message: "nested quantifier"\n',
    )
    profile = {"auto": ["^a+!$"]}
    long_text = "a" * 24 + "!"
    texts = [long_text, "a"]
    calls = []
    original_search = PolicyRule.search

    def recording_search(rule, text):
        calls.append((rule.pattern, text))
        if (rule.pattern, text) == ("(a+)+$", long_text):
            return None  # never pay the backtracking, even under the reverted code
        return original_search(rule, text)

    monkeypatch.setattr(PolicyRule, "search", recording_search)

    excerpts = engine.match_excerpts(texts, ["nested quantifier"], profile_rules=profile)

    assert ("(a+)+$", long_text) not in calls, calls
    assert ("(a+)+$", "a") in calls
    assert excerpts == {"nested quantifier": "a"}
