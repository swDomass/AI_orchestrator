"""Tests for tool_contracts in policy.yaml (P3)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from policy import PolicyEngine, ToolContract


@pytest.fixture(autouse=True)
def _mock_dotenv():
    with patch("config._load_dotenv"):
        yield


def _write_policy(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "99_System" / "AI" / "policy.yaml"
    f.parent.mkdir(parents=True)
    f.write_text(body, encoding="utf-8")
    return f


# ── PolicyEngine.get_tool_contract ────────────────────────────────────────────

def test_returns_default_contract_when_section_missing(tmp_path):
    """No tool_contracts section → returns an empty ToolContract (no None)."""
    _write_policy(tmp_path, "auto:\n  - pytest\n")
    engine = PolicyEngine(vault_path=tmp_path)
    c = engine.get_tool_contract("dev-loop")
    assert isinstance(c, ToolContract)
    assert c.tool_name == "dev-loop"
    assert c.max_iterations is None
    assert c.max_runtime_sec is None
    assert c.stop_conditions == ()
    assert c.reporting_path == "telegram+memory"


def test_returns_specific_contract(tmp_path):
    _write_policy(tmp_path, """
tool_contracts:
  dev-loop:
    budget:
      max_iterations: 20
      max_runtime_sec: 7200
      max_files_touched: 50
    stop_conditions:
      - reviews_pass
      - capacity_exhausted
    reporting_path: telegram+memory
""")
    engine = PolicyEngine(vault_path=tmp_path)
    c = engine.get_tool_contract("dev-loop")
    assert c.max_iterations == 20
    assert c.max_runtime_sec == 7200
    assert c.max_files_touched == 50
    assert c.stop_conditions == ("reviews_pass", "capacity_exhausted")
    assert c.reporting_path == "telegram+memory"


def test_default_section_falls_through_for_unknown_tool(tmp_path):
    _write_policy(tmp_path, """
tool_contracts:
  default:
    budget:
      max_iterations: 10
    reporting_path: telegram+memory
  dev-loop:
    budget:
      max_iterations: 20
""")
    engine = PolicyEngine(vault_path=tmp_path)
    # Specific entry wins
    assert engine.get_tool_contract("dev-loop").max_iterations == 20
    # Unknown tool falls through to default — and tool_name is rewritten
    c = engine.get_tool_contract("unknown-tool")
    assert c.max_iterations == 10
    assert c.tool_name == "unknown-tool"


def test_zero_or_negative_budget_treated_as_none(tmp_path):
    """Defensive: yaml values <= 0 are not valid budgets."""
    _write_policy(tmp_path, """
tool_contracts:
  review-loop:
    budget:
      max_iterations: 0
      max_runtime_sec: -100
""")
    engine = PolicyEngine(vault_path=tmp_path)
    c = engine.get_tool_contract("review-loop")
    assert c.max_iterations is None
    assert c.max_runtime_sec is None


def test_invalid_budget_types_treated_as_none(tmp_path):
    _write_policy(tmp_path, """
tool_contracts:
  review-loop:
    budget:
      max_iterations: "twenty"
      max_runtime_sec: null
""")
    engine = PolicyEngine(vault_path=tmp_path)
    c = engine.get_tool_contract("review-loop")
    assert c.max_iterations is None
    assert c.max_runtime_sec is None


def test_stop_conditions_string_normalized_to_single_element_tuple(tmp_path):
    _write_policy(tmp_path, """
tool_contracts:
  review-loop:
    stop_conditions: all_findings_resolved
""")
    engine = PolicyEngine(vault_path=tmp_path)
    c = engine.get_tool_contract("review-loop")
    assert c.stop_conditions == ("all_findings_resolved",)


def test_non_mapping_entry_logged_and_skipped(tmp_path, caplog):
    _write_policy(tmp_path, """
tool_contracts:
  dev-loop: "not a mapping"
""")
    with caplog.at_level("WARNING"):
        engine = PolicyEngine(vault_path=tmp_path)
    # Falls back to empty contract since entry was ignored
    c = engine.get_tool_contract("dev-loop")
    assert c.max_iterations is None
    assert any("not a mapping" in r.message or "tool_contracts" in r.message
               for r in caplog.records)


def test_list_tool_contracts_snapshot(tmp_path):
    _write_policy(tmp_path, """
tool_contracts:
  a:
    budget: {max_iterations: 5}
  b:
    budget: {max_runtime_sec: 60}
""")
    engine = PolicyEngine(vault_path=tmp_path)
    snap = engine.list_tool_contracts()
    assert set(snap.keys()) == {"a", "b"}
    assert snap["a"].max_iterations == 5
    assert snap["b"].max_runtime_sec == 60


# ── Doctor schema validation ──────────────────────────────────────────────────

class TestDoctorValidation:
    def test_no_section_no_warnings(self):
        from doctor import _validate_tool_contracts
        assert _validate_tool_contracts(None) == []
        assert _validate_tool_contracts({}) == []

    def test_top_level_must_be_mapping(self):
        from doctor import _validate_tool_contracts
        warnings = _validate_tool_contracts(["not", "a", "mapping"])
        assert len(warnings) == 1
        assert "must be a mapping" in warnings[0]

    def test_unknown_top_key(self):
        from doctor import _validate_tool_contracts
        warnings = _validate_tool_contracts({
            "dev-loop": {"buget": {"max_iterations": 10}},  # typo
        })
        assert any("unknown key 'buget'" in w for w in warnings)

    def test_unknown_budget_key(self):
        from doctor import _validate_tool_contracts
        warnings = _validate_tool_contracts({
            "dev-loop": {"budget": {"max_iterationz": 10}},
        })
        assert any("unknown budget key" in w for w in warnings)

    def test_negative_budget_flagged(self):
        from doctor import _validate_tool_contracts
        warnings = _validate_tool_contracts({
            "dev-loop": {"budget": {"max_iterations": -5}},
        })
        assert any("positive int" in w for w in warnings)

    def test_stop_conditions_must_be_list(self):
        from doctor import _validate_tool_contracts
        warnings = _validate_tool_contracts({
            "dev-loop": {"stop_conditions": "only-a-string"},
        })
        assert any("must be a list" in w for w in warnings)

    def test_clean_contract_no_warnings(self):
        from doctor import _validate_tool_contracts
        warnings = _validate_tool_contracts({
            "dev-loop": {
                "budget": {"max_iterations": 20, "max_runtime_sec": 7200},
                "stop_conditions": ["reviews_pass"],
                "reporting_path": "telegram+memory",
            },
        })
        assert warnings == []


# ── reload-on-change picks up contracts ───────────────────────────────────────

def test_reload_picks_up_new_contracts(tmp_path):
    f = _write_policy(tmp_path, "auto:\n  - pytest\n")
    engine = PolicyEngine(vault_path=tmp_path)
    assert engine.list_tool_contracts() == {}

    # Bump mtime to force reload (write same path with extra section)
    import time as _t
    _t.sleep(0.01)
    f.write_text(
        "auto:\n  - pytest\n"
        "tool_contracts:\n"
        "  review-loop:\n"
        "    budget:\n"
        "      max_iterations: 7\n",
        encoding="utf-8",
    )
    # Touch mtime explicitly in case the same-second write was a no-op
    import os
    new_mtime = f.stat().st_mtime + 1
    os.utime(f, (new_mtime, new_mtime))

    c = engine.get_tool_contract("review-loop")
    assert c.max_iterations == 7
