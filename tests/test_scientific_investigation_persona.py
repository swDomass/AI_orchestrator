"""I2 tests for scientific-investigation: Persona-Allocation, PolicyEngine
bypass routing, Decision-Log + Cherry-Picking-Detector.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from providers.base import RunResult
from tools.crosschecks import audit_trail, bypass_counter, similarity_index
from tools.crosschecks.cherrypicking_detector import (
    build_cherrypicking_block,
    build_persona_allocation_block,
    write_decision_log,
)
from tools.personas import ALL_PHASE2_PERSONAS, AUTHOR, DEVILS_ADVOCATE, METHODIKER
from tools.personas.base import PersonaAllocation
from tools.scientific_investigation import ScientificInvestigationTool
from tools.scientific_investigation_approvals import reset_manager_for_tests
from tools.scientific_investigation_phases import phase_persona_allocation


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_manager():
    reset_manager_for_tests()
    yield
    reset_manager_for_tests()


class _ScriptedProvider:
    name = "claude"
    supports_sessions = False

    _DEFAULT_FRAMING = """```yaml
question: q
hypothesis: h
bias_statement: bias-line
discipline: engineering
framing_text: engineering investigation about diffusion bias under load
```"""
    _DEFAULT_PREREG = """```yaml
thresholds:
  - criterion_id: F1
    description: t
    threshold_value: 5%
    source: norm_reference
    reference: DIN-EN-60068-2 §4.3 Toleranz 5% bei 800K
```"""
    _DEFAULT_AUTHOR_PLAN = """```yaml
sub_tasks:
  - sub_id: S1
    title: Tolerance check
    description: Run measurement series
    addresses_criteria: [F1]
    type: data_analysis
    expected_output: bias_at_800K
```"""
    _DEFAULT_REVIEW_EMPTY = """```yaml
findings: []
```"""

    def __init__(self, outputs: list[str] | None = None, name: str = "claude"):
        self.name = name
        self.calls: list[str] = []
        if outputs is None:
            outputs = [
                self._DEFAULT_FRAMING,
                self._DEFAULT_PREREG,
                self._DEFAULT_AUTHOR_PLAN,
                self._DEFAULT_REVIEW_EMPTY,
                self._DEFAULT_REVIEW_EMPTY,
                "# Investigation Proof\n\nStub synthesis from scripted provider.\n",
                self._DEFAULT_REVIEW_EMPTY,
            ]
        self._outputs = list(outputs)

    def run(self, task: str, **kwargs) -> RunResult:
        self.calls.append(task)
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))


def _patch_notifier(monkeypatch):
    from tools.scientific_investigation_phase3 import SubTaskResult

    monkeypatch.setattr(
        "tools.scientific_investigation.notify_tool_done",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr("dispatcher.get_provider_by_name", lambda name: None)

    def _stub(sub_task, *, sub_state_cwd, env, provider, timeout):
        return SubTaskResult(
            sub_task=sub_task, success=True,
            output=f"stub: {sub_task.sub_id}", duration_sec=0.0,
        )

    monkeypatch.setattr(
        "tools.scientific_investigation_phase3.default_devloop_executor", _stub,
    )
    monkeypatch.setattr(
        "tools.scientific_investigation_phase4.validate_synthesis_output",
        lambda _text: [],
    )
    from tools.scientific_investigation_phase8 import Phase8Result as _P8R

    def _approve_immediately(*, summary, run_dir, run_id, notify_callable, timeout_sec):
        draft = summary.draft_path
        final = run_dir / "proof.md"
        if draft.exists():
            try:
                draft.replace(final)
            except OSError:
                final = None
        else:
            final = None
        return _P8R(state="approved", telegram_msg_id="stub-msg-id",
                    approver="stub-user", reason="", final_proof_path=final)

    monkeypatch.setattr(
        "tools.scientific_investigation.phase_final_approval", _approve_immediately,
    )


def _make_lookup(*available_names: str):
    """Provider lookup that pretends ``available_names`` exist (returns dummies)."""
    def _lookup(name: str):
        if name in available_names:
            mock = MagicMock()
            mock.name = name
            return mock
        return None
    return _lookup


# ── Persona definitions ─────────────────────────────────────────────────────


def test_three_phase2_personas_defined():
    assert ALL_PHASE2_PERSONAS == (AUTHOR, DEVILS_ADVOCATE, METHODIKER)


def test_persona_provider_preferences():
    assert AUTHOR.provider_preference == "primary"
    assert DEVILS_ADVOCATE.provider_preference == "cross"
    assert METHODIKER.provider_preference == "any_external"


def test_personas_have_non_empty_system_prompts():
    for p in ALL_PHASE2_PERSONAS:
        assert len(p.system_prompt) > 50


# ── phase_persona_allocation ───────────────────────────────────────────────


def test_persona_allocation_assigns_cross_when_available(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    primary = _ScriptedProvider(name="claude")
    allocations = phase_persona_allocation(
        primary,
        run_dir=rd,
        run_id="run-1",
        cross_provider_none=False,
        provider_lookup=_make_lookup("claude", "gemini", "codex"),
    )
    assert len(allocations) == 3
    assert allocations[0].persona is AUTHOR
    assert allocations[0].provider_name == "claude"
    assert allocations[0].cross_provider_satisfied is False
    # DA should be cross — not claude
    assert allocations[1].persona is DEVILS_ADVOCATE
    assert allocations[1].provider_name != "claude"
    assert allocations[1].cross_provider_satisfied is True
    # Methodiker should also be cross AND distinct from DA
    assert allocations[2].persona is METHODIKER
    assert allocations[2].provider_name not in ("claude", allocations[1].provider_name)
    assert allocations[2].cross_provider_satisfied is True


def test_persona_allocation_falls_back_when_cross_provider_none(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    primary = _ScriptedProvider(name="claude")
    allocations = phase_persona_allocation(
        primary,
        run_dir=rd,
        run_id="run-1",
        cross_provider_none=True,
        provider_lookup=_make_lookup("claude", "gemini", "codex"),
    )
    # All three personas use the primary, none satisfies cross-provider.
    assert all(a.provider_name == "claude" for a in allocations)
    assert all(a.cross_provider_satisfied is False for a in allocations)


def test_persona_allocation_methodiker_falls_back_when_no_extra_provider(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    primary = _ScriptedProvider(name="claude")
    # Only one cross-provider available — DA takes it, Methodiker (any_external)
    # falls back to primary.
    allocations = phase_persona_allocation(
        primary,
        run_dir=rd,
        run_id="run-1",
        cross_provider_none=False,
        provider_lookup=_make_lookup("claude", "gemini"),
    )
    assert allocations[1].provider_name == "gemini"  # DA
    assert allocations[1].cross_provider_satisfied is True
    assert allocations[2].provider_name == "claude"  # Methodiker fallback
    assert allocations[2].cross_provider_satisfied is False


def test_persona_allocation_writes_audit_entries(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    primary = _ScriptedProvider(name="claude")
    phase_persona_allocation(
        primary, run_dir=rd, run_id="r-1",
        cross_provider_none=False,
        provider_lookup=_make_lookup("claude", "gemini", "codex"),
    )
    entries = audit_trail.load_audit_entries(rd, entry_type="persona_allocation")
    assert len(entries) == 3
    roles = {e["role"] for e in entries}
    assert roles == {"author", "devils_advocate", "methodiker"}
    # Audit must record the primary AND the chosen provider
    for e in entries:
        assert e["primary_provider"] == "claude"
        assert "provider" in e


# ── Bypass → PolicyEngine routing ──────────────────────────────────────────


def test_bypass_under_limit_records_audit_without_policy(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider()
    result = tool.run(
        "investigate #cross-provider:none", provider, cwd=str(tmp_path),
    )
    assert result.success is True
    run_dir = next((tmp_path / "docs").glob("scientific-investigation-*"))
    entries = audit_trail.load_audit_entries(run_dir, entry_type="cross_provider_bypass")
    assert len(entries) == 1
    assert entries[0]["policy_routed"] is False


def test_bypass_over_limit_routes_to_policy_engine(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    # Pre-fill counter
    for i in range(3):
        bypass_counter.record_bypass(tmp_path, run_id=f"prior-{i}")

    fake_engine = MagicMock()
    fake_engine.request_approval.return_value = "approved"
    monkeypatch.setattr("policy.get_engine", lambda: fake_engine)

    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider()
    result = tool.run(
        "investigate #cross-provider:none", provider, cwd=str(tmp_path),
    )
    assert result.success is True  # PolicyEngine approved → run continues
    fake_engine.request_approval.assert_called_once()
    run_dir = next((tmp_path / "docs").glob("scientific-investigation-*"))
    entries = audit_trail.load_audit_entries(run_dir, entry_type="cross_provider_bypass")
    assert len(entries) == 1
    assert entries[0]["policy_routed"] is True
    assert entries[0]["policy_response"] == "approved"


def test_bypass_over_limit_policy_denied_aborts(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    for i in range(3):
        bypass_counter.record_bypass(tmp_path, run_id=f"prior-{i}")

    fake_engine = MagicMock()
    fake_engine.request_approval.return_value = "denied"
    monkeypatch.setattr("policy.get_engine", lambda: fake_engine)

    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider(outputs=[])  # framing would explode if reached
    result = tool.run(
        "investigate #cross-provider:none", provider, cwd=str(tmp_path),
    )
    assert result.success is False
    assert result.error_code == "policy_denied"


def test_bypass_over_limit_policy_timeout_aborts(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    for i in range(3):
        bypass_counter.record_bypass(tmp_path, run_id=f"prior-{i}")

    fake_engine = MagicMock()
    fake_engine.request_approval.return_value = "timeout"
    monkeypatch.setattr("policy.get_engine", lambda: fake_engine)

    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider(outputs=[])
    result = tool.run(
        "investigate #cross-provider:none", provider, cwd=str(tmp_path),
    )
    assert result.success is False
    assert result.error_code == "policy_denied"


# ── cherry-picking detector ────────────────────────────────────────────────


def test_cherrypicking_block_empty_when_no_hits(tmp_path):
    block = build_cherrypicking_block(
        tmp_path, framing_text="completely different unique text",
        run_id="r-1",
    )
    assert "Keine vorherigen Investigations" in block


def test_cherrypicking_block_lists_hits(tmp_path):
    similarity_index.append_investigation(
        tmp_path, run_id="prior",
        framing_text="engineering analysis 800K toleranz diffusion",
        embedding_model="m",
    )
    block = build_cherrypicking_block(
        tmp_path,
        framing_text="engineering analysis 800K toleranz diffusion",
        run_id="r-2",
    )
    assert "prior" in block
    assert "Cosine-Similarity" in block
    assert "Embedding-Modell" in block  # K11 visibility


def test_cherrypicking_block_excludes_self(tmp_path):
    similarity_index.append_investigation(
        tmp_path, run_id="r-self",
        framing_text="alpha beta gamma delta",
        embedding_model="m",
    )
    block = build_cherrypicking_block(
        tmp_path, framing_text="alpha beta gamma delta",
        run_id="r-self",
    )
    assert "Keine vorherigen Investigations" in block


# ── persona-allocation block ────────────────────────────────────────────────


def test_persona_allocation_block_renders_table():
    allocations = [
        PersonaAllocation(persona=AUTHOR, provider_name="claude", cross_provider_satisfied=False),
        PersonaAllocation(persona=DEVILS_ADVOCATE, provider_name="gemini", cross_provider_satisfied=True),
    ]
    block = build_persona_allocation_block(allocations)
    assert "Author" in block
    assert "Devil's Advocate" in block
    assert "claude" in block
    assert "gemini" in block
    assert "✓" in block  # cross-provider satisfied marker


# ── decision-log writer ────────────────────────────────────────────────────


def test_write_decision_log_creates_file(tmp_path):
    out = write_decision_log(
        tmp_path, run_id="r-1",
        sections=[
            ("Phase 1", "persona content"),
            ("Phase 6", ""),
        ],
    )
    text = out.read_text(encoding="utf-8")
    assert "# Decision Log — r-1" in text
    assert "## Phase 1" in text
    assert "persona content" in text
    assert "## Phase 6" in text
    assert "noch nichts dokumentiert" in text  # placeholder for empty body


# ── Tool-level integration: I2 happy path ──────────────────────────────────


def test_tool_run_i2_creates_decision_log_and_state(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider()
    result = tool.run("investigate diffusion", provider, cwd=str(tmp_path))
    assert result.success is True
    assert result.error_code == "pipeline_complete"
    assert result.iterations == 8
    run_dir = next((tmp_path / "docs").glob("scientific-investigation-*"))
    assert (run_dir / "decision_log.md").is_file()
    state = json.loads(
        next((tmp_path / ".scientific-investigation").glob("*/state.json"))
        .read_text("utf-8")
    )
    assert state["phase"] == "phase8_done"
    assert len(state["personas"]) == 3


def test_tool_run_i2_emits_persona_allocation_audit(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider()
    tool.run("investigate diffusion", provider, cwd=str(tmp_path))
    run_dir = next((tmp_path / "docs").glob("scientific-investigation-*"))
    persona_entries = audit_trail.load_audit_entries(
        run_dir, entry_type="persona_allocation",
    )
    assert len(persona_entries) == 3


def test_tool_run_i2_decision_log_includes_persona_block(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider()
    tool.run("investigate diffusion", provider, cwd=str(tmp_path))
    run_dir = next((tmp_path / "docs").glob("scientific-investigation-*"))
    text = (run_dir / "decision_log.md").read_text("utf-8")
    assert "Persona-Allocation" in text
    assert "Cherry-Picking-Detection" in text
    assert "Author" in text
