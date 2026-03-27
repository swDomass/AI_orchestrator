"""Tests for the multi-agent deep security audit tool."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Mock config._load_dotenv before importing modules that depend on config
with patch("config._load_dotenv"):
    from providers.base import RunResult
    from tools.base_tool import ToolResult
    from tools.deep_security_audit import (
        DeepSecurityAuditTool,
        _AGENTS,
        _clean_tags,
        _wants_fix,
    )


# ── Helpers ────────────────────────────────────────────────────────────


class _ScriptedProvider:
    """Provider that returns pre-scripted outputs in FIFO order."""

    def __init__(self, name: str, outputs: list[str]):
        self.name = name
        self._outputs = list(outputs)
        self.calls: list[dict] = []

    def run(self, task: str, cwd: str | None = None, timeout: int = 0,
            read_only: bool = False) -> RunResult:
        self.calls.append({"task": task, "cwd": cwd, "timeout": timeout, "read_only": read_only})
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))


class _ErrorProvider:
    """Provider that always returns an error."""

    def __init__(self, name: str, error: str = "provider_error"):
        self.name = name
        self._error = error
        self.calls: list[dict] = []

    def run(self, task: str, cwd: str | None = None, timeout: int = 0,
            read_only: bool = False) -> RunResult:
        self.calls.append({"task": task, "cwd": cwd, "timeout": timeout, "read_only": read_only})
        return RunResult(success=False, output="", error=self._error)


def _noop(*_args, **_kwargs):
    pass


@pytest.fixture
def _patch(monkeypatch):
    """Suppress notifications and external calls."""
    monkeypatch.setattr("tools.deep_security_audit.notify_tool_done", _noop)
    monkeypatch.setattr("tools.deep_security_audit.notify_tool_progress", _noop)
    monkeypatch.setattr("tools.deep_security_audit.is_cached_provider_available", lambda _name: True)


# ── Tag Parsing Tests ──────────────────────────────────────────────────


class TestTagParsing:

    def test_wants_fix_default(self):
        assert _wants_fix("Deep security audit of the repo") is True

    def test_wants_fix_with_no_fix_tag(self):
        assert _wants_fix("Audit #no-fix cwd:/d/proj") is False

    def test_wants_fix_case_insensitive(self):
        assert _wants_fix("Audit #No-Fix cwd:/d/proj") is False
        assert _wants_fix("Audit #NO-FIX cwd:/d/proj") is False

    def test_clean_tags_removes_no_fix(self):
        assert "#no-fix" not in _clean_tags("Audit #no-fix cwd:/d/proj")

    def test_clean_tags_normalizes_whitespace(self):
        assert _clean_tags("Audit #no-fix cwd:/foo") == "Audit cwd:/foo"

    def test_clean_tags_preserves_other(self):
        cleaned = _clean_tags("Audit the auth module #no-fix")
        assert "Audit the auth module" in cleaned


# ── Tool Class Attributes ──────────────────────────────────────────────


class TestToolAttributes:

    def test_name(self):
        tool = DeepSecurityAuditTool()
        assert tool.name == "deep-security-audit"

    def test_read_only_false(self):
        tool = DeepSecurityAuditTool()
        assert tool.read_only is False

    def test_description_mentions_multi_agent(self):
        tool = DeepSecurityAuditTool()
        assert "multi-agent" in tool.description.lower() or "Multi-agent" in tool.description

    def test_six_agents_defined(self):
        assert len(_AGENTS) == 6


# ── Agent Definitions ──────────────────────────────────────────────────


class TestAgentDefinitions:

    def test_all_agents_have_unique_keys(self):
        keys = [a.key for a in _AGENTS]
        assert len(keys) == len(set(keys))

    def test_all_agents_have_title(self):
        for agent in _AGENTS:
            assert len(agent.title) > 5, f"Agent {agent.key} has no title"

    def test_all_agents_have_persona(self):
        for agent in _AGENTS:
            assert len(agent.persona) > 20, f"Agent {agent.key} has no persona"

    def test_all_agents_have_checklist(self):
        for agent in _AGENTS:
            assert len(agent.checklist) > 50, f"Agent {agent.key} has no checklist"

    def test_expected_agent_keys(self):
        keys = {a.key for a in _AGENTS}
        expected = {"pentester", "architect", "code_auditor", "supply_chain", "data_privacy", "forensics"}
        assert keys == expected


# ── Full Audit Run (no fix) ────────────────────────────────────────────


class TestFullAuditNoFix:

    def test_all_agents_plus_synthesis(self, tmp_path, _patch):
        """7 calls: 6 agents + 1 synthesis."""
        outputs = [f"Agent {i} findings" for i in range(6)]
        outputs.append("CISO Synthesis report")
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        result = tool.run(
            "Audit the repo #no-fix",
            provider,
            cwd=str(tmp_path),
        )
        assert result.success is True
        assert result.iterations == 7  # 6 agents + synthesis
        assert len(provider.calls) == 7

    def test_all_agents_read_only(self, tmp_path, _patch):
        outputs = [f"Agent {i}" for i in range(6)] + ["Synthesis"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        # All 6 agents + synthesis should be read_only
        for i, call in enumerate(provider.calls):
            assert call["read_only"] is True, f"Call {i} was not read_only"

    def test_combined_report_written(self, tmp_path, _patch):
        outputs = [f"Agent {i}" for i in range(6)] + ["Synthesis"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        docs = tmp_path / "docs"
        combined = list(docs.glob("deep-security-audit-*[!-].md"))
        # Filter out partial/agent/audit-only files
        combined = [f for f in combined if "-partial" not in f.name
                    and "-audit-only" not in f.name
                    and not any(a.key in f.name for a in _AGENTS)]
        assert len(combined) >= 1

    def test_individual_agent_reports_saved(self, tmp_path, _patch):
        outputs = [f"Agent {i} output" for i in range(6)] + ["Synthesis"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        docs = tmp_path / "docs"
        for agent in _AGENTS:
            agent_files = list(docs.glob(f"*-{agent.key}.md"))
            assert len(agent_files) == 1, f"Missing report for {agent.key}"

    def test_synthesis_contains_agent_outputs(self, tmp_path, _patch):
        outputs = [f"UNIQUE_FINDING_{i}_XYZ" for i in range(6)] + ["Synthesis"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        # Synthesis prompt (call 7) should contain agent outputs
        synth_prompt = provider.calls[6]["task"]
        for i in range(6):
            assert f"UNIQUE_FINDING_{i}_XYZ" in synth_prompt


# ── Full Audit Run (with fix) ──────────────────────────────────────────


class TestFullAuditWithFix:

    def test_eight_phases_with_fix(self, tmp_path, _patch):
        """8 calls: 6 agents + synthesis + fix."""
        outputs = [f"Agent {i}" for i in range(6)]
        outputs.append("CISO Synthesis")
        outputs.append("Fixes applied")
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        result = tool.run(
            "Audit the repo",
            provider,
            cwd=str(tmp_path),
        )
        assert result.success is True
        assert result.iterations == 8
        assert len(provider.calls) == 8

    def test_fix_phase_not_read_only(self, tmp_path, _patch):
        outputs = [f"Agent {i}" for i in range(6)] + ["Synthesis", "Fixes"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit", provider, cwd=str(tmp_path))

        # Fix phase (last call) should NOT be read_only
        assert provider.calls[7]["read_only"] is False

    def test_report_includes_fixes(self, tmp_path, _patch):
        outputs = [f"Agent {i}" for i in range(6)] + ["Synthesis", "FIX_OUTPUT_MARKER"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit", provider, cwd=str(tmp_path))

        docs = tmp_path / "docs"
        combined = [f for f in docs.glob("deep-security-audit-*.md")
                    if "-partial" not in f.name and "-audit-only" not in f.name
                    and not any(a.key in f.name for a in _AGENTS)]
        assert len(combined) >= 1
        content = combined[0].read_text(encoding="utf-8")
        assert "FIX_OUTPUT_MARKER" in content
        assert "Fixes Applied" in content


# ── Token Aggregation ──────────────────────────────────────────────────


class TestTokenAggregation:

    def test_tokens_summed_across_all_phases(self, tmp_path, _patch):
        call_count = [0]

        def mock_run(task, cwd=None, timeout=0, read_only=False):
            call_count[0] += 1
            return RunResult(
                success=True, output=f"output {call_count[0]}",
                input_tokens=100, output_tokens=50,
            )

        provider = SimpleNamespace(name="claude", run=mock_run)
        tool = DeepSecurityAuditTool()
        result = tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        # 7 calls: 6 agents + 1 synthesis
        assert result.input_tokens == 700
        assert result.output_tokens == 350

    def test_tokens_include_fix_phase(self, tmp_path, _patch):
        call_count = [0]

        def mock_run(task, cwd=None, timeout=0, read_only=False):
            call_count[0] += 1
            return RunResult(
                success=True, output=f"output {call_count[0]}",
                input_tokens=100, output_tokens=50,
            )

        provider = SimpleNamespace(name="claude", run=mock_run)
        tool = DeepSecurityAuditTool()
        result = tool.run("Audit", provider, cwd=str(tmp_path))

        # 8 calls: 6 agents + synthesis + fix
        assert result.input_tokens == 800
        assert result.output_tokens == 400


# ── Partial Failure (some agents fail) ─────────────────────────────────


class TestPartialAgentFailure:

    def test_continues_after_agent_failure(self, tmp_path, _patch):
        """If one agent errors, remaining agents still run."""
        call_count = [0]

        def mock_run(task, cwd=None, timeout=0, read_only=False):
            call_count[0] += 1
            # Agent 3 fails
            if call_count[0] == 3:
                return RunResult(success=False, error="timeout")
            return RunResult(success=True, output=f"output {call_count[0]}")

        provider = SimpleNamespace(name="claude", run=mock_run)
        tool = DeepSecurityAuditTool()
        result = tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        # Should still succeed (synthesis ran)
        assert result.success is True
        assert call_count[0] == 7  # All 6 agents + synthesis

    def test_all_agents_fail_returns_error(self, tmp_path, _patch):
        provider = _ErrorProvider("claude", "timeout")
        tool = DeepSecurityAuditTool()
        result = tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        assert result.success is False
        assert "Alle Agenten fehlgeschlagen" in result.error

    def test_synthesis_fails_returns_error(self, tmp_path, _patch):
        """Synthesis failure returns success=False with error from synthesis."""
        call_count = [0]

        def mock_run(task, cwd=None, timeout=0, read_only=False):
            call_count[0] += 1
            if call_count[0] <= 6:
                return RunResult(success=True, output=f"agent output {call_count[0]}")
            # Synthesis call fails
            return RunResult(success=False, output="", error="synthesis_timeout")

        provider = SimpleNamespace(name="claude", run=mock_run)
        tool = DeepSecurityAuditTool()
        result = tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        assert result.success is False
        assert "CISO-Synthese fehlgeschlagen" in result.error
        assert call_count[0] == 7  # All 6 agents + 1 synthesis attempt


class TestFixPhaseError:

    def test_fix_error_propagates_to_result(self, tmp_path, _patch):
        """Fix phase error makes success=False with error in ToolResult."""
        call_count = [0]

        def mock_run(task, cwd=None, timeout=0, read_only=False):
            call_count[0] += 1
            if call_count[0] <= 7:
                return RunResult(success=True, output=f"output {call_count[0]}")
            # Fix phase fails
            return RunResult(success=False, output="", error="fix_provider_error")

        provider = SimpleNamespace(name="claude", run=mock_run)
        tool = DeepSecurityAuditTool()
        result = tool.run("Audit", provider, cwd=str(tmp_path))

        assert result.success is False
        assert result.error == "fix_provider_error"

    def test_fix_error_in_output_summary(self, tmp_path, _patch):
        """Fix phase error appears in output_summary string."""
        call_count = [0]

        def mock_run(task, cwd=None, timeout=0, read_only=False):
            call_count[0] += 1
            if call_count[0] <= 7:
                return RunResult(success=True, output=f"output {call_count[0]}")
            return RunResult(success=False, output="", error="fix_timeout")

        provider = SimpleNamespace(name="claude", run=mock_run)
        tool = DeepSecurityAuditTool()
        result = tool.run("Audit", provider, cwd=str(tmp_path))

        assert "Fix-Fehler" in result.output
        assert "fix_timeout" in result.output

    def test_report_written_even_on_fix_error(self, tmp_path, _patch):
        """Combined report is saved even when fix phase fails."""
        call_count = [0]

        def mock_run(task, cwd=None, timeout=0, read_only=False):
            call_count[0] += 1
            if call_count[0] <= 7:
                return RunResult(success=True, output=f"output {call_count[0]}")
            return RunResult(success=False, output="", error="fix_error")

        provider = SimpleNamespace(name="claude", run=mock_run)
        tool = DeepSecurityAuditTool()
        tool.run("Audit", provider, cwd=str(tmp_path))

        docs = tmp_path / "docs"
        combined = [f for f in docs.glob("deep-security-audit-*.md")
                    if "-partial" not in f.name and "-audit-only" not in f.name
                    and not any(a.key in f.name for a in _AGENTS)]
        assert len(combined) >= 1


# ── Capacity Exhaustion ────────────────────────────────────────────────


class TestCapacityExhaustion:

    def test_capacity_exhausted_before_start(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tools.deep_security_audit.notify_tool_done", _noop)
        monkeypatch.setattr("tools.deep_security_audit.notify_tool_progress", _noop)
        monkeypatch.setattr("tools.deep_security_audit.is_cached_provider_available", lambda _: False)

        provider = _ScriptedProvider("claude", ["should not run"])
        tool = DeepSecurityAuditTool()
        result = tool.run("Audit", provider, cwd=str(tmp_path))

        assert result.success is False
        assert result.error_code == "capacity_exhausted"
        assert result.retryable is True
        assert len(provider.calls) == 0

    def test_capacity_exhausted_mid_agents(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tools.deep_security_audit.notify_tool_done", _noop)
        monkeypatch.setattr("tools.deep_security_audit.notify_tool_progress", _noop)

        available_count = [0]
        def mock_available(name):
            available_count[0] += 1
            return available_count[0] <= 3  # Available for first 3 agents only

        monkeypatch.setattr("tools.deep_security_audit.is_cached_provider_available", mock_available)

        outputs = [f"Agent {i}" for i in range(3)]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        result = tool.run("Audit", provider, cwd=str(tmp_path))

        assert result.success is False
        assert result.error_code == "capacity_exhausted"
        assert result.retryable is True
        assert len(provider.calls) == 3  # Only first 3 agents ran

    def test_capacity_exhausted_saves_partial(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tools.deep_security_audit.notify_tool_done", _noop)
        monkeypatch.setattr("tools.deep_security_audit.notify_tool_progress", _noop)

        available_count = [0]
        def mock_available(name):
            available_count[0] += 1
            return available_count[0] <= 2

        monkeypatch.setattr("tools.deep_security_audit.is_cached_provider_available", mock_available)

        outputs = ["Agent 1 output", "Agent 2 output"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit", provider, cwd=str(tmp_path))

        docs = tmp_path / "docs"
        partial = list(docs.glob("*-partial.md"))
        assert len(partial) == 1

    def test_capacity_exhausted_before_synthesis(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tools.deep_security_audit.notify_tool_done", _noop)
        monkeypatch.setattr("tools.deep_security_audit.notify_tool_progress", _noop)

        # Available for all 6 agents, but not for synthesis
        available_count = [0]
        def mock_available(name):
            available_count[0] += 1
            return available_count[0] <= 6

        monkeypatch.setattr("tools.deep_security_audit.is_cached_provider_available", mock_available)

        outputs = [f"Agent {i}" for i in range(6)]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        result = tool.run("Audit", provider, cwd=str(tmp_path))

        assert result.success is False
        assert result.error_code == "capacity_exhausted"
        assert result.retryable is True


# ── Output Truncation ──────────────────────────────────────────────────


class TestOutputTruncation:

    def test_agent_output_truncated_in_synthesis(self, tmp_path, _patch, monkeypatch):
        monkeypatch.setattr("tools.deep_security_audit.TOOL_DSA_MAX_AGENT_OUTPUT_CHARS", 50)

        long_output = "X" * 200
        outputs = [long_output] + [f"Agent {i}" for i in range(1, 6)] + ["Synthesis"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        synth_prompt = provider.calls[6]["task"]
        assert "X" * 200 not in synth_prompt
        assert "...[truncated]" in synth_prompt


# ── Edge Cases ─────────────────────────────────────────────────────────


class TestEdgeCases:

    def test_kwargs_accepted(self, tmp_path, _patch):
        outputs = [f"a{i}" for i in range(6)] + ["synth"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        result = tool.run(
            "Audit #no-fix", provider, cwd=str(tmp_path),
            pass_providers={}, unknown_kwarg="ignored",
        )
        assert result.success is True

    def test_no_cwd_defaults_to_dot(self, _patch):
        outputs = [f"a{i}" for i in range(6)] + ["synth"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        result = tool.run("Audit #no-fix", provider)
        assert result.success is True

    def test_each_agent_gets_distinct_persona(self, tmp_path, _patch):
        outputs = [f"Agent {i}" for i in range(6)] + ["Synthesis"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        # Each agent prompt should mention its title
        for i, agent in enumerate(_AGENTS):
            assert agent.title in provider.calls[i]["task"]
