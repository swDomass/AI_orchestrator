"""Tests for the Round-Table dialogue phase + ToolTracer integration in
the deep-security-audit tool.

Round-Table is opt-in via #roundtable tag. It runs between Phase 6 (last
agent) and Phase 7 (CISO synthesis) and produces 6 additional subprocess
calls — one per persona — each seeing the OTHER 5's findings.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    from providers.base import RunResult
    from tools.deep_security_audit import (
        DeepSecurityAuditTool,
        _AGENTS,
        _ROUNDTABLE_PROMPT,
        _clean_tags,
        _wants_fix,
        _wants_roundtable,
    )


# ── Helpers (mirror conventions from test_deep_security_audit.py) ────


class _ScriptedProvider:
    def __init__(self, name: str, outputs: list[str], supports_sessions: bool = False):
        self.name = name
        self._outputs = list(outputs)
        self.calls: list[dict] = []
        self.supports_sessions = supports_sessions

    def run(self, task, cwd=None, timeout=0, read_only=False, **kwargs):
        self.calls.append({
            "task": task, "cwd": cwd, "timeout": timeout, "read_only": read_only,
        })
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))


def _noop(*_args, **_kwargs):
    pass


@pytest.fixture
def _patch(monkeypatch):
    monkeypatch.setattr("tools.deep_security_audit.notify_tool_done", _noop)
    monkeypatch.setattr("tools.deep_security_audit.notify_tool_progress", _noop)
    monkeypatch.setattr("tools.deep_security_audit.is_cached_provider_available", lambda _name: True)


# ── Tag Parsing ─────────────────────────────────────────────────────


class TestRoundtableTagParsing:

    def test_wants_roundtable_default_off(self):
        assert _wants_roundtable("Audit the repo") is False

    def test_wants_roundtable_with_tag(self):
        assert _wants_roundtable("Audit #roundtable cwd:/d/proj") is True

    def test_wants_roundtable_case_insensitive(self):
        assert _wants_roundtable("Audit #Roundtable") is True
        assert _wants_roundtable("Audit #ROUNDTABLE") is True

    def test_wants_roundtable_word_boundary(self):
        # tag must not match inside other identifiers
        assert _wants_roundtable("Audit foo#roundtable") is False
        assert _wants_roundtable("Audit #roundtable_extended") is False

    def test_clean_tags_removes_roundtable(self):
        assert "#roundtable" not in _clean_tags("Audit #roundtable cwd:/d/p")

    def test_clean_tags_removes_both_tags(self):
        cleaned = _clean_tags("Audit #roundtable #no-fix cwd:/d/p")
        assert "#roundtable" not in cleaned
        assert "#no-fix" not in cleaned
        assert "Audit" in cleaned
        assert "cwd:/d/p" in cleaned

    def test_clean_tags_no_tags_unchanged(self):
        assert _clean_tags("Audit the repo") == "Audit the repo"


# ── Prompt Constant ─────────────────────────────────────────────────


class TestRoundtablePrompt:

    def test_prompt_has_all_required_placeholders(self):
        assert "{agent_title}" in _ROUNDTABLE_PROMPT
        assert "{own_findings}" in _ROUNDTABLE_PROMPT
        assert "{other_findings}" in _ROUNDTABLE_PROMPT
        assert "{task}" in _ROUNDTABLE_PROMPT

    def test_prompt_mentions_four_categories(self):
        for keyword in ("AGREE", "REBUT", "EXTEND", "GAPS"):
            assert keyword in _ROUNDTABLE_PROMPT

    def test_prompt_enforces_read_only(self):
        # Round-Table is a read-only phase
        assert "read-only" in _ROUNDTABLE_PROMPT.lower() or "Do NOT modify" in _ROUNDTABLE_PROMPT


# ── Full sequential run WITH round-table ─────────────────────────────


class TestRoundtableFullRun:

    def test_six_extra_calls_when_roundtable_set(self, tmp_path, _patch):
        """13 subprocess calls expected: 6 agents + 6 round-table + 1 synthesis."""
        outputs = (
            [f"Agent {i} findings" for i in range(6)]
            + [f"RT response {i}" for i in range(6)]
            + ["CISO Synthesis"]
        )
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        result = tool.run(
            "Audit the repo #roundtable #no-fix",
            provider,
            cwd=str(tmp_path),
        )
        assert result.success is True
        assert len(provider.calls) == 13

    def test_roundtable_calls_inject_other_findings(self, tmp_path, _patch):
        """Each round-table prompt must contain at least one OTHER persona's findings."""
        outputs = (
            [f"Findings-of-{a.key}" for a in _AGENTS]
            + [f"RT-{a.key}" for a in _AGENTS]
            + ["Synthesis"]
        )
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #roundtable #no-fix", provider, cwd=str(tmp_path))

        # Calls 6..11 are the round-table calls (index 6 to 11 inclusive)
        for rt_idx, agent in enumerate(_AGENTS):
            call_idx = 6 + rt_idx
            prompt = provider.calls[call_idx]["task"]
            # Own findings recap must be present
            assert f"Findings-of-{agent.key}" in prompt
            # At least one OTHER persona's findings must be injected
            other_keys = [a.key for a in _AGENTS if a.key != agent.key]
            assert any(f"Findings-of-{k}" in prompt for k in other_keys), \
                f"RT call for {agent.key} missing peer findings"

    def test_roundtable_calls_are_read_only(self, tmp_path, _patch):
        outputs = (
            [f"a{i}" for i in range(6)] + [f"rt{i}" for i in range(6)] + ["synth"]
        )
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #roundtable #no-fix", provider, cwd=str(tmp_path))

        # round-table calls = indices 6..11
        for idx in range(6, 12):
            assert provider.calls[idx]["read_only"] is True, f"RT call {idx} was not read_only"

    def test_per_persona_roundtable_files_written(self, tmp_path, _patch):
        outputs = (
            [f"a{i}" for i in range(6)] + [f"rt{i}" for i in range(6)] + ["synth"]
        )
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #roundtable #no-fix", provider, cwd=str(tmp_path))

        docs = tmp_path / "docs"
        rt_files = list(docs.glob("deep-security-audit-*-roundtable-*.md"))
        assert len(rt_files) == 6
        keys = {a.key for a in _AGENTS}
        for f in rt_files:
            assert any(k in f.name for k in keys)

    def test_combined_report_includes_roundtable_section(self, tmp_path, _patch):
        outputs = (
            [f"a{i}" for i in range(6)] + [f"rt{i}" for i in range(6)] + ["synth"]
        )
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #roundtable #no-fix", provider, cwd=str(tmp_path))

        # The combined (non-partial, non-per-agent) report should include the
        # new "Round-Table Dialogue" header.
        docs = tmp_path / "docs"
        candidates = [
            f for f in docs.glob("deep-security-audit-*.md")
            if "-partial" not in f.name and "-roundtable-" not in f.name
            and "-audit-only" not in f.name
            and not any(a.key in f.name for a in _AGENTS)
        ]
        assert candidates, "combined report not found"
        text = candidates[0].read_text(encoding="utf-8")
        assert "Round-Table Dialogue" in text

    def test_synthesis_prompt_receives_roundtable_block(self, tmp_path, _patch):
        outputs = (
            [f"Findings-{i}" for i in range(6)]
            + [f"RT-response-{i}" for i in range(6)]
            + ["synth"]
        )
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #roundtable #no-fix", provider, cwd=str(tmp_path))

        synthesis_prompt = provider.calls[12]["task"]
        assert "Round-Table Dialogue" in synthesis_prompt
        # At least one round-table response must appear in the synthesis prompt
        assert any(f"RT-response-{i}" in synthesis_prompt for i in range(6))


# ── Default behaviour (no #roundtable tag) ───────────────────────────


class TestRoundtableDefaultOff:

    def test_without_tag_no_roundtable_calls(self, tmp_path, _patch):
        """Plain audit without #roundtable: 6 agents + 1 synthesis = 7 calls."""
        outputs = [f"a{i}" for i in range(6)] + ["synth"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #no-fix", provider, cwd=str(tmp_path))
        assert len(provider.calls) == 7

    def test_synthesis_prompt_omits_roundtable_block(self, tmp_path, _patch):
        outputs = [f"a{i}" for i in range(6)] + ["synth"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        synthesis_prompt = provider.calls[6]["task"]
        # CISO prompt template has {roundtable_block} placeholder — must be
        # replaced with empty string, NOT left as the placeholder.
        assert "{roundtable_block}" not in synthesis_prompt
        assert "Round-Table Dialogue" not in synthesis_prompt


# ── Capability switch: #roundtable forces sequential mode ────────────


class TestRoundtableForcesSequential:

    def test_session_provider_with_roundtable_uses_sequential(self, tmp_path, _patch, monkeypatch):
        """When provider supports_sessions AND #roundtable is set, the tool
        must fall back to _run_sequential_mode (not _run_subagent_mode).
        Detection: sequential mode makes 13 distinct provider calls; subagent
        mode would make just 1 master call."""
        monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)
        outputs = (
            [f"a{i}" for i in range(6)] + [f"rt{i}" for i in range(6)] + ["synth"]
        )
        provider = _ScriptedProvider("claude", outputs, supports_sessions=True)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #roundtable #no-fix", provider, cwd=str(tmp_path))
        # Must have made many calls, not just 1 master call
        assert len(provider.calls) == 13

    def test_session_provider_without_roundtable_uses_subagent(self, tmp_path, _patch, monkeypatch):
        """Without #roundtable, session-capable provider should pick subagent
        mode (1 master subprocess for audit+synthesis, then optional fix)."""
        monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)
        outputs = ["Master audit + synthesis output"]
        provider = _ScriptedProvider("claude", outputs, supports_sessions=True)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #no-fix", provider, cwd=str(tmp_path))
        # Subagent mode: only 1 call (master orchestrates internally)
        assert len(provider.calls) == 1


# ── Failed-agent skipping in round-table ─────────────────────────────


class TestRoundtableSkipsFailedAgents:

    def test_failed_agent_gets_skipped_placeholder(self, tmp_path, _patch, monkeypatch):
        """If an agent failed in Phase 1-6, its round-table response should
        be marked as skipped without spawning a subprocess for it."""
        call_log: list[str] = []

        def custom_run(task, cwd=None, timeout=0, read_only=False, **kwargs):
            call_log.append(task[:80])
            # Make agent #3 (idx 2 = "code_auditor") fail
            if len(call_log) == 3:
                return RunResult(success=False, error="agent_error")
            return RunResult(success=True, output=f"call-{len(call_log)}")

        provider = SimpleNamespace(name="claude", run=custom_run, supports_sessions=False)
        tool = DeepSecurityAuditTool()
        result = tool.run(
            "Audit #roundtable #no-fix",
            provider,
            cwd=str(tmp_path),
        )
        # Expected: 6 agent calls + 5 RT calls (skip the failed one) + 1 synth = 12
        assert len(call_log) == 12
        assert result.success is True


# ── ToolTracer integration ───────────────────────────────────────────


class TestToolTracerIntegration:

    def test_trace_file_written(self, tmp_path, _patch):
        outputs = [f"a{i}" for i in range(6)] + ["synth"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        traces = list((tmp_path / ".deep-security-audit" / "traces").glob("*.jsonl"))
        assert len(traces) == 1
        # First line should be a run_start event
        first_line = traces[0].read_text(encoding="utf-8").splitlines()[0]
        import json
        entry = json.loads(first_line)
        assert entry["action"] == "run_start"
        assert entry["tool"] == "deep-security-audit"

    def test_trace_contains_subprocess_results_for_each_agent(self, tmp_path, _patch):
        outputs = [f"a{i}" for i in range(6)] + ["synth"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        import json
        trace_file = next((tmp_path / ".deep-security-audit" / "traces").glob("*.jsonl"))
        events = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]

        subprocess_results = [e for e in events if e["action"] == "subprocess_result"]
        # 6 agents + 1 synthesis = 7 subprocess results
        assert len(subprocess_results) == 7

    def test_trace_ends_with_run_end(self, tmp_path, _patch):
        outputs = [f"a{i}" for i in range(6)] + ["synth"]
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #no-fix", provider, cwd=str(tmp_path))

        import json
        trace_file = next((tmp_path / ".deep-security-audit" / "traces").glob("*.jsonl"))
        events = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
        assert events[-1]["action"] == "run_end"
        assert events[-1]["details"]["success"] is True

    def test_trace_includes_roundtable_phase_when_enabled(self, tmp_path, _patch):
        outputs = (
            [f"a{i}" for i in range(6)] + [f"rt{i}" for i in range(6)] + ["synth"]
        )
        provider = _ScriptedProvider("claude", outputs)
        tool = DeepSecurityAuditTool()
        tool.run("Audit #roundtable #no-fix", provider, cwd=str(tmp_path))

        import json
        trace_file = next((tmp_path / ".deep-security-audit" / "traces").glob("*.jsonl"))
        events = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
        actions = [e["action"] for e in events]
        # roundtable phase_start emitted exactly once
        assert actions.count("phase_start") >= 1
        rt_phase_events = [e for e in events
                           if e["action"] == "phase_start"
                           and e["details"].get("phase") == "roundtable"]
        assert len(rt_phase_events) == 1
