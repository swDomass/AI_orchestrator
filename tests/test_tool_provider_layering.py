import pytest
from pathlib import Path
from limits import AllLimits, ProviderLimits
from dispatcher import select_provider
from policy import PolicyEngine, get_engine
import policy as policy_module

@pytest.fixture
def mock_limits():
    return AllLimits(
        claude=ProviderLimits(available=True, remaining_pct=100.0),
        gemini=ProviderLimits(available=True, remaining_pct=100.0),
        codex=ProviderLimits(available=True, remaining_pct=100.0)
    )

def _make_engine(tmp_path, global_yaml: str) -> PolicyEngine:
    """Create a PolicyEngine with a custom global policy YAML."""
    policy_file = tmp_path / "99_System" / "AI" / "policy.yaml"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(global_yaml, encoding="utf-8")
    return PolicyEngine(vault_path=tmp_path)

def test_tool_provider_filtering(tmp_path, mock_limits, monkeypatch):
    """Verify that select_provider respects the tool-provider policy."""
    yaml_content = """
tool_providers:
  review-loop: [gemini]
  test-loop: [claude, codex]
  default: [claude, gemini, codex]
"""
    engine = _make_engine(tmp_path, yaml_content)
    
    # Mock the singleton engine
    monkeypatch.setattr(policy_module, "_engine", engine)
    
    # Test review-loop (should only allow gemini)
    p = select_provider("Run review", mock_limits, tool_name="review-loop")
    assert p.name == "gemini"
    
    # Test test-loop (should allow claude first)
    p = select_provider("Run tests", mock_limits, tool_name="test-loop")
    assert p.name == "claude"
    
    # Test unknown tool (should use default)
    p = select_provider("Unknown", mock_limits, tool_name="unknown-tool")
    assert p.name == "claude"

def test_tool_provider_fallback_blocked(tmp_path, mock_limits, monkeypatch):
    """Verify that fallback is restricted to the allowed providers."""
    yaml_content = """
tool_providers:
  review-loop: [gemini]
"""
    engine = _make_engine(tmp_path, yaml_content)
    monkeypatch.setattr(policy_module, "_engine", engine)
    
    # Even if we exclude gemini, it shouldn't fall back to claude because claude is not in the allowed list for review-loop
    # Wait, the current logic is:
    # 1. allowed_by_policy = [gemini]
    # 2. base_order = [claude, gemini, codex] (filtered to [gemini])
    # 3. exclude = {gemini}
    # 4. Result should be None
    
    p = select_provider("Run review", mock_limits, tool_name="review-loop", exclude={"gemini"})
    assert p is None
