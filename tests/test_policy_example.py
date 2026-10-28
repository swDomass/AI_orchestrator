"""policy.example.yaml: the shipped example must load, mean what its comments say,
and pass the doctor's own validators. Everything runs against tmp_path - nothing
reads or writes the real vault."""

import shutil
from pathlib import Path

import pytest
import yaml

import dispatcher
import doctor
import policy as policy_module
from policy import TIER_APPROVE, TIER_AUTO, TIER_DENY, PolicyEngine
from tools.registry import list_tools

EXAMPLE = Path(__file__).resolve().parent.parent / "policy.example.yaml"


def _data() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Real PolicyEngine over a byte copy of the example, installed as the singleton
    so dispatcher.policy_allows_provider reads it."""
    target = tmp_path / "99_System" / "AI" / "policy.yaml"
    target.parent.mkdir(parents=True)
    shutil.copyfile(EXAMPLE, target)
    eng = PolicyEngine(vault_path=tmp_path)
    monkeypatch.setattr(policy_module, "_engine", eng)
    return eng


def test_example_file_exists():
    assert EXAMPLE.is_file()


def test_example_loads_without_error(engine):
    # get_tool_phase raises PolicyUnreadableError / a section parser's exception
    # when the file does not load; a clean answer proves all sections parsed.
    assert engine.get_tool_phase("dev-loop", "plan_approval", "x") == "auto"


# -- tool_providers ----------------------------------------------------------

def test_every_registered_tool_has_an_entry_and_no_tool_is_invented():
    listed = set(_data()["tool_providers"]) - {"default"}
    assert listed == set(list_tools())


@pytest.mark.parametrize("tool", sorted(list_tools()))
def test_each_tool_allows_claude_and_bars_the_switched_off_providers(engine, tool):
    assert dispatcher.policy_allows_provider("claude", tool) is True
    for barred in ("gemini", "openrouter", "vibe", "opencode"):
        assert dispatcher.policy_allows_provider(barred, tool) is False, barred


def test_codex_allowed_everywhere_except_test_loop(engine):
    for tool in list_tools():
        assert dispatcher.policy_allows_provider("codex", tool) is (tool != "test-loop")


def test_default_entry_is_the_only_place_opencode_is_allowed(engine):
    assert engine.get_allowed_providers("not-a-registered-tool") == ["claude", "codex", "opencode"]
    assert dispatcher.policy_allows_provider("opencode", "not-a-registered-tool") is True
    assert dispatcher.policy_allows_provider("gemini", "not-a-registered-tool") is False


# -- rule classes --------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "git push origin main", "GIT PUSH origin feature", "npm publish --access public",
])
def test_approve_rules(engine, text):
    assert engine.check_task(text)[0] == TIER_APPROVE


@pytest.mark.parametrize("text", [
    "rm -rf /", "git push --force origin main", "DROP TABLE users", "format C:",
    "mkfs.ext4 /dev/sda1",
])
def test_deny_rules_win_over_approve(engine, text):
    assert engine.check_task(text)[0] == TIER_DENY


@pytest.mark.parametrize("text", [
    "Create hello.md with one sentence describing this orchestrator",
    "pytest -q",
    "git commit -m 'x'",
])
def test_ordinary_tasks_are_auto(engine, text):
    assert engine.check_task(text)[0] == TIER_AUTO


# -- contracts and phases ------------------------------------------------------

def test_contracts_pass_the_doctor_validator():
    contracts = _data()["tool_contracts"]
    assert contracts
    assert doctor._validate_tool_contracts(contracts) == []
    assert set(contracts) <= set(list_tools())


def test_contracts_reach_the_engine_with_positive_budgets(engine):
    c = engine.get_tool_contract("dev-loop")
    assert c.max_iterations and c.max_runtime_sec and c.max_files_touched


def test_phase_switches_are_auto(engine):
    assert engine.get_tool_phase("dev-loop", "plan_approval", "approve") == "auto"
    assert engine.get_tool_phase("review-loop", "verification", "approve") == "auto"
