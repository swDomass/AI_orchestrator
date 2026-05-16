"""I5 tests for scientific-investigation Phase 4: synthesis + validator + status-tuple."""

from __future__ import annotations

import pytest

from providers.base import RunResult
from tools.scientific_investigation_phase2 import (
    InvestigationPlan,
    Phase2Result,
    SubTask,
)
from tools.scientific_investigation_phase3 import Phase3Result, SubTaskResult
from tools.scientific_investigation_phase4 import (
    StatusTuple,
    SynthesisResult,
    _count_sentences,
    _parse_limitation_subsections,
    compute_status_tuple,
    phase_synthesis,
    validate_synthesis_output,
)
from tools.scientific_investigation_phases import (
    FramingResult,
    PreRegResult,
    Threshold,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


class _ScriptedProvider:
    name = "claude"

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls: list[str] = []

    def run(self, task, **kwargs):
        self.calls.append(task)
        if not self._outputs:
            return RunResult(success=False, error="empty queue")
        return RunResult(success=True, output=self._outputs.pop(0))


def _make_inputs():
    framing = FramingResult(
        question="Q", hypothesis="H", bias_statement="B",
        discipline="engineering", framing_text="text",
    )
    prereg = PreRegResult(
        thresholds=[
            Threshold("F1", "d", "5%", "norm_reference", "DIN-X §1 5%"),
        ],
        discipline_warning=False,
        discipline_warning_approved=False,
        prereg_hash="h",
    )
    plan = InvestigationPlan(
        sub_tasks=[SubTask("S1", "T", "d", ["F1"], "data_analysis", "o")],
        raw_yaml="raw",
    )
    phase2 = Phase2Result(
        plan=plan, findings_by_iteration=[], iterations_used=1, converged=True,
    )
    phase3 = Phase3Result(
        sub_task_results=[
            SubTaskResult(
                sub_task=plan.sub_tasks[0], success=True,
                output="measured bias 2%", duration_sec=1.0,
            ),
        ],
        total_duration_sec=1.0,
        crosscheck_tiers_per_subtask={"S1": ["T3"]},
    )
    return framing, prereg, phase2, phase3


_GOOD_PROOF = """\
# Investigation Proof — Q

## Was diese Investigation NICHT beweist

Diese Investigation hat folgende strukturelle Limitations:

### 1. Multi-LLM-Korpus-Überlappungs-Restrisiko
Konkret in dieser Investigation: alle Claude-Modelle ziehen aus überlappenden Trainingsdaten.
Dadurch ist eine versteckte Konsens-Bias zwischen Author und Reviewer nicht ausgeschlossen.

### 2. Self-Reporting-Bias bei Persona-Klassifikation
Die DA-Persona basiert auf Claude und teilt damit den Stack des Authors.
Eine echte kontradiktorische Bewertung wäre erst durch externe Hardware-Trennung gegeben.

### 3. Disziplin-Restriktion / externe Schwellen-Verfügbarkeit
Die Pre-Reg-Schwelle F1 referenziert DIN-EN-60068-2 und ist damit extern verankert.
Weitere mögliche Schwellen wurden bewusst aus Scope-Gründen weggelassen.

### 4. Cross-Investigation-Cherry-Picking-Möglichkeit
Keine prior-runs mit Cosine-Similarity > 0.7 wurden gefunden.
Damit besteht kein Risiko der unbewussten Replikation eines früheren Ergebnisses.

### 5. LLM-Drift-Reproduzierbarkeits-Restrisiko
Das genutzte Modell ist claude-opus-4-7. Anthropic kann die Version ohne Vorwarnung ändern.
Eine spätere Replikation kann daher leicht abweichende Ergebnisse liefern.

## Ergebnis-Synthese

Bias gemessen mit 2 % unter Schwelle 5 % — within tolerance.
"""


# ── _parse_limitation_subsections ──────────────────────────────────────────


def test_parse_limitation_subsections_finds_all_five():
    sections = _parse_limitation_subsections(_GOOD_PROOF)
    assert len(sections) == 5
    assert "Multi-LLM" in sections[0]["title"]
    assert "LLM-Drift" in sections[4]["title"]


def test_count_sentences_handles_basic_text():
    assert _count_sentences("First. Second. Third.") == 3
    assert _count_sentences("Only one sentence here.") == 1
    assert _count_sentences("") == 0


# ── validate_synthesis_output ──────────────────────────────────────────────


def test_validator_passes_good_proof():
    errors = validate_synthesis_output(_GOOD_PROOF)
    assert errors == []


def test_validator_rejects_missing_section():
    bad = "# Investigation Proof\n\n## Ergebnis\n\nBlah.\n"
    errors = validate_synthesis_output(bad)
    assert any("FEHLT: Pflicht-Sektion" in e for e in errors)


def test_validator_rejects_missing_category():
    # Remove §4 (Cherry-Picking) from the good proof
    truncated = _GOOD_PROOF.replace(
        "### 4. Cross-Investigation-Cherry-Picking-Möglichkeit\n"
        "Keine prior-runs mit Cosine-Similarity > 0.7 wurden gefunden.\n"
        "Damit besteht kein Risiko der unbewussten Replikation eines früheren Ergebnisses.\n\n",
        "",
    )
    errors = validate_synthesis_output(truncated)
    assert any("Cherry-Picking" in e for e in errors) or any("NUR" in e for e in errors)


def test_validator_rejects_too_few_sentences():
    # Replace §1 body with a single sentence
    truncated = _GOOD_PROOF.replace(
        "Konkret in dieser Investigation: alle Claude-Modelle ziehen aus überlappenden Trainingsdaten.\n"
        "Dadurch ist eine versteckte Konsens-Bias zwischen Author und Reviewer nicht ausgeschlossen.",
        "Single sentence only.",
    )
    errors = validate_synthesis_output(truncated)
    assert any("Multi-LLM" in e and "mind. 2" in e for e in errors)


def test_validator_rejects_blacklist_phrase():
    bad = _GOOD_PROOF.replace(
        "Konkret in dieser Investigation: alle Claude-Modelle ziehen aus überlappenden Trainingsdaten.",
        "Diese Limitation ist vernachlässigbar im Kontext.",
    )
    errors = validate_synthesis_output(bad)
    assert any("vernachlässigbar" in e for e in errors)


# ── compute_status_tuple ──────────────────────────────────────────────────


def _full_medium_inputs():
    return dict(
        preregistration_thresholds_sources=[
            {"source": "norm_reference"}, {"source": "paper_reference"},
        ],
        crosscheck_tiers_per_subtask={"S1": ["T2"], "S2": ["T2", "T3"]},
        adversarial_search_audit={"diversity_pass": True, "tool_call_pass": True},
        cross_provider_da_active=True,
        engineering_reviewer_status="passed",
        investigation_user_approval="approved",
        criteria_test_status="defined_criteria_all_within_tolerance",
    )


def test_status_tuple_medium_iff_all_conditions_met():
    st = compute_status_tuple(**_full_medium_inputs())
    assert st.methodological_rigor == "MEDIUM"


def test_status_tuple_downgrades_when_user_not_approved():
    inputs = _full_medium_inputs()
    inputs["investigation_user_approval"] = "rejected"
    assert compute_status_tuple(**inputs).methodological_rigor == "LOW"


def test_status_tuple_downgrades_when_engineering_reviewer_needs_revision():
    inputs = _full_medium_inputs()
    inputs["engineering_reviewer_status"] = "needs_revision"
    assert compute_status_tuple(**inputs).methodological_rigor == "LOW"


def test_status_tuple_downgrades_when_subtask_lacks_t2():
    inputs = _full_medium_inputs()
    inputs["crosscheck_tiers_per_subtask"] = {"S1": ["T3"], "S2": ["T2"]}
    assert compute_status_tuple(**inputs).methodological_rigor == "LOW"


def test_status_tuple_downgrades_when_cross_provider_da_inactive():
    inputs = _full_medium_inputs()
    inputs["cross_provider_da_active"] = False
    assert compute_status_tuple(**inputs).methodological_rigor == "LOW"


def test_status_tuple_no_high_value_possible():
    """K15: HIGH must not be expressible. Even with everything 'beyond passing'."""
    inputs = _full_medium_inputs()
    st = compute_status_tuple(**inputs)
    assert st.methodological_rigor in ("MEDIUM", "LOW")


def test_status_tuple_evidence_basis_llm_only_when_no_t2():
    inputs = _full_medium_inputs()
    inputs["crosscheck_tiers_per_subtask"] = {"S1": ["T3"]}
    st = compute_status_tuple(**inputs)
    assert st.evidence_basis == "llm_generated_only"


def test_status_tuple_evidence_basis_mixed_when_both_tiers_present():
    inputs = _full_medium_inputs()
    inputs["crosscheck_tiers_per_subtask"] = {"S1": ["T2"], "S2": ["T3"]}
    st = compute_status_tuple(**inputs)
    assert st.evidence_basis == "mixed"


def test_status_tuple_residual_risk_high_when_many_telegrams():
    inputs = _full_medium_inputs()
    inputs["preregistration_thresholds_sources"] = [
        {"source": "telegram_approval"} for _ in range(3)
    ]
    st = compute_status_tuple(**inputs)
    assert st.residual_risk == "HIGH"


# ── phase_synthesis ───────────────────────────────────────────────────────


def test_phase_synthesis_writes_draft(tmp_path):
    rd = tmp_path / "run"
    (rd / "draft").mkdir(parents=True)
    framing, prereg, phase2, phase3 = _make_inputs()
    provider = _ScriptedProvider([_GOOD_PROOF])
    synth = phase_synthesis(framing, prereg, phase2, phase3, provider,
                            run_dir=rd, run_id="r1")
    assert isinstance(synth, SynthesisResult)
    assert synth.draft_path.is_file()
    assert synth.draft_path.read_text("utf-8").strip().startswith("# Investigation Proof")


def test_phase_synthesis_reports_validation_errors(tmp_path):
    rd = tmp_path / "run"
    (rd / "draft").mkdir(parents=True)
    framing, prereg, phase2, phase3 = _make_inputs()
    provider = _ScriptedProvider(["# Proof\n\nIncomplete.\n"])
    synth = phase_synthesis(framing, prereg, phase2, phase3, provider,
                            run_dir=rd, run_id="r1")
    assert synth.is_valid() is False
    assert any("Pflicht-Sektion" in e for e in synth.validation_errors)


def test_phase_synthesis_strips_code_fence(tmp_path):
    rd = tmp_path / "run"
    (rd / "draft").mkdir(parents=True)
    framing, prereg, phase2, phase3 = _make_inputs()
    wrapped = "```markdown\n" + _GOOD_PROOF + "\n```"
    provider = _ScriptedProvider([wrapped])
    synth = phase_synthesis(framing, prereg, phase2, phase3, provider,
                            run_dir=rd, run_id="r1")
    assert not synth.proof_md_text.startswith("```")
    assert synth.is_valid() is True


def test_phase_synthesis_raises_on_llm_failure(tmp_path):
    rd = tmp_path / "run"
    (rd / "draft").mkdir(parents=True)
    framing, prereg, phase2, phase3 = _make_inputs()

    class _Broken:
        name = "claude"

        def run(self, *a, **kw):
            return RunResult(success=False, error="boom")

    with pytest.raises(RuntimeError, match="boom"):
        phase_synthesis(framing, prereg, phase2, phase3, _Broken(),
                        run_dir=rd, run_id="r1")
