"""I6+I7+I8 tests for scientific-investigation Phase 5a/5b/7/8."""

from __future__ import annotations

import json
import threading
import time

import pytest

from providers.base import RunResult
from tools.crosschecks import audit_trail
from tools.scientific_investigation_approvals import (
    INVESTIGATION_CRITERION,
    get_manager,
    reset_manager_for_tests,
)
from tools.scientific_investigation_phase2 import ReviewFinding
from tools.scientific_investigation_phase5 import (
    Phase5aReport,
    Phase5bReport,
    parse_threshold,
    phase_heuristic_review,
    phase_mechanical_falsification_check,
    write_phase5_report_md,
)
from tools.scientific_investigation_phase7 import (
    EngineeringFinding,
    Phase7Result,
    phase_engineering_reviewer,
    write_phase7_report_md,
)
from tools.scientific_investigation_phase8 import (
    Phase8Summary,
    extract_top_limitations,
    phase_final_approval,
)
from tools.scientific_investigation_phases import PreRegResult, Threshold


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_manager():
    reset_manager_for_tests()
    yield
    reset_manager_for_tests()


class _ScriptedProvider:
    name = "claude"

    def __init__(self, outputs, name="claude"):
        self.name = name
        self._outputs = list(outputs)
        self.calls: list[str] = []

    def run(self, task, **kwargs):
        self.calls.append(task)
        if not self._outputs:
            return RunResult(success=False, error="empty queue")
        return RunResult(success=True, output=self._outputs.pop(0))


def _make_prereg(thresholds=None):
    if thresholds is None:
        thresholds = [
            Threshold("F1", "d1", "5%", "norm_reference", "DIN-X §1 5%"),
            Threshold("F2", "d2", "10mm", "norm_reference", "DIN-X §2 10mm"),
        ]
    return PreRegResult(
        thresholds=thresholds, discipline_warning=False,
        discipline_warning_approved=False, prereg_hash="h",
    )


# ────────────────────────────────────────────────────────────────────────────
# Phase 5a — mechanical falsification check
# ────────────────────────────────────────────────────────────────────────────


def test_parse_threshold_percent():
    val, kind = parse_threshold("5%")
    assert val == 5.0 and kind == "percent"


def test_parse_threshold_absolute_with_unit():
    val, kind = parse_threshold("10mm")
    assert val == 10.0 and kind == "absolute"


def test_parse_threshold_unknown_returns_none():
    val, kind = parse_threshold("between 5 and 7")
    assert val is None and kind == "unknown"


def test_phase5a_within_tolerance_passes(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    report = phase_mechanical_falsification_check(
        _make_prereg(), run_dir=rd, root_cwd=tmp_path, run_id="r1",
        measurements={"F1": 2.0, "F2": 3.0},
    )
    assert all(c.status == "passed" for c in report.checks)
    assert report.aggregate_status == "defined_criteria_all_within_tolerance"


def test_phase5a_outside_tolerance_fails(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    report = phase_mechanical_falsification_check(
        _make_prereg(), run_dir=rd, root_cwd=tmp_path, run_id="r1",
        measurements={"F1": 8.0, "F2": 20.0},
    )
    assert all(c.status == "failed" for c in report.checks)
    assert report.aggregate_status == "defined_criteria_all_outside_tolerance"


def test_phase5a_mixed_status(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    report = phase_mechanical_falsification_check(
        _make_prereg(), run_dir=rd, root_cwd=tmp_path, run_id="r1",
        measurements={"F1": 2.0, "F2": 20.0},
    )
    assert report.aggregate_status == "defined_criteria_some_outside_tolerance"


def test_phase5a_missing_measurement_marks_not_checkable(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    report = phase_mechanical_falsification_check(
        _make_prereg(), run_dir=rd, root_cwd=tmp_path, run_id="r1",
        measurements={"F1": 2.0},
    )
    statuses = [c.status for c in report.checks]
    assert "passed" in statuses and "not_checkable" in statuses


def test_phase5a_loads_measurements_from_sub_task_state(tmp_path):
    """When no measurements kwarg is given, sub_state_cwd/measurements.json wins."""
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    sub_dir = tmp_path / ".scientific-investigation" / "r1" / "sub-tasks" / "S1"
    sub_dir.mkdir(parents=True)
    (sub_dir / "measurements.json").write_text(
        json.dumps({"F1": 1.5, "F2": 8.0}), encoding="utf-8",
    )
    report = phase_mechanical_falsification_check(
        _make_prereg(), run_dir=rd, root_cwd=tmp_path, run_id="r1",
    )
    assert all(c.status == "passed" for c in report.checks)


def test_phase5a_writes_sidecar(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    phase_mechanical_falsification_check(
        _make_prereg(), run_dir=rd, root_cwd=tmp_path, run_id="r1",
        measurements={"F1": 2.0, "F2": 3.0},
    )
    sidecar = rd / "audit" / "phase5a_report.json"
    assert sidecar.is_file()
    data = json.loads(sidecar.read_text("utf-8"))
    assert "checks" in data and len(data["checks"]) == 2


# ────────────────────────────────────────────────────────────────────────────
# Phase 5b — heuristic LLM review
# ────────────────────────────────────────────────────────────────────────────


def test_phase5b_returns_findings_from_yaml():
    provider = _ScriptedProvider([
        """```yaml
findings:
  - severity: P1
    sub_id: ALL
    issue: Statistic mismatch in section A
    suggestion: Recompute using paired t-test
```"""
    ])
    phase5a = Phase5aReport()
    report = phase_heuristic_review("# Proof", phase5a, provider)
    assert isinstance(report, Phase5bReport)
    assert report.has_open_p1()
    assert report.findings[0].issue.startswith("Statistic mismatch")


def test_phase5b_empty_findings_when_no_issues():
    provider = _ScriptedProvider(["```yaml\nfindings: []\n```"])
    report = phase_heuristic_review("# Proof", Phase5aReport(), provider)
    assert report.findings == []
    assert not report.has_open_p1()


def test_phase5b_raises_on_llm_failure():
    class _Broken:
        name = "claude"

        def run(self, *a, **kw):
            return RunResult(success=False, error="capacity")

    with pytest.raises(RuntimeError, match="capacity"):
        phase_heuristic_review("# Proof", Phase5aReport(), _Broken())


def test_write_phase5_report_md(tmp_path):
    from tools.scientific_investigation_phase5 import CriterionCheck
    phase5a = Phase5aReport(checks=[
        CriterionCheck("F1", "5%", 5.0, "percent", 2.0, "passed"),
    ])
    text = write_phase5_report_md(tmp_path, phase5a=phase5a, phase5b=None).read_text("utf-8")
    assert "Phase 5a" in text and "F1" in text and "passed" in text
    assert "Phase 5b nicht ausgeführt" in text


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 — engineering reviewer
# ────────────────────────────────────────────────────────────────────────────


def test_phase7_passes_on_empty_findings(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    primary = _ScriptedProvider(["```yaml\nfindings: []\n```"])
    result = phase_engineering_reviewer(
        "# Proof", {"checks": []}, [], primary,
        run_dir=rd, run_id="r1",
        provider_lookup=lambda _: None,  # forces fallback to primary
    )
    assert result.status == "passed"
    assert result.iterations_used == 1
    assert result.cross_provider_satisfied is False


def test_phase7_reworks_on_blocker_then_converges(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    # Iter 1: BLOCKER → rework → Iter 2: clean
    primary = _ScriptedProvider([
        "```yaml\nfindings:\n  - severity: P1\n    sub_id: S1\n    issue: bilanz fehler\n    suggestion: korrigieren\n```",
        "# Proof reworked\n",  # author rework output
        "```yaml\nfindings: []\n```",  # iter 2 review
    ])
    result = phase_engineering_reviewer(
        "# Proof", {"checks": []}, [], primary,
        run_dir=rd, run_id="r1",
        provider_lookup=lambda _: None,
        max_iterations=3,
    )
    assert result.status == "passed"
    assert result.iterations_used == 2
    assert result.final_proof_md.startswith("# Proof reworked")


def test_phase7_cap_reached_returns_needs_revision(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    # Every iter: BLOCKER. Use 2-iter cap to keep test short.
    outputs = []
    for _ in range(2):
        outputs.extend([
            "```yaml\nfindings:\n  - severity: P1\n    sub_id: ALL\n    issue: x\n    suggestion: y\n```",
            "# Proof rework attempt\n",
        ])
    primary = _ScriptedProvider(outputs)
    result = phase_engineering_reviewer(
        "# Proof", {"checks": []}, [], primary,
        run_dir=rd, run_id="r1",
        provider_lookup=lambda _: None, max_iterations=2,
    )
    assert result.status == "needs_revision"
    assert len(result.open_blockers()) >= 1


def test_phase7_uses_explicit_reviewer_provider(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    reviewer = _ScriptedProvider(["```yaml\nfindings: []\n```"], name="openrouter")
    primary = _ScriptedProvider([])  # nothing should be called
    result = phase_engineering_reviewer(
        "# Proof", {"checks": []}, [], primary,
        run_dir=rd, run_id="r1",
        explicit_reviewer_name="openrouter",
        provider_lookup=lambda n: reviewer if n == "openrouter" else None,
    )
    assert result.cross_provider_satisfied is True
    assert result.reviewer_provider_name == "openrouter"
    assert len(reviewer.calls) == 1
    assert len(primary.calls) == 0


def test_phase7_writes_audit_entries(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    primary = _ScriptedProvider(["```yaml\nfindings: []\n```"])
    phase_engineering_reviewer(
        "# Proof", {"checks": []}, [], primary,
        run_dir=rd, run_id="r1",
        provider_lookup=lambda _: None,
    )
    entries = audit_trail.load_audit_entries(rd, entry_type="execution_sub_task")
    assert any(e.get("summary", {}).get("phase") == "engineering_reviewer" for e in entries)


def test_write_phase7_report_md(tmp_path):
    res = Phase7Result(
        status="passed", iterations_used=1, final_proof_md="x",
        findings_by_iteration=[[EngineeringFinding("HINT", "S1", "i", "s")]],
        reviewer_provider_name="claude", cross_provider_satisfied=False,
    )
    text = write_phase7_report_md(tmp_path, phase7=res).read_text("utf-8")
    assert "Phase 7" in text and "passed" in text and "[HINT]" in text


# ────────────────────────────────────────────────────────────────────────────
# Phase 8 — final approval gate
# ────────────────────────────────────────────────────────────────────────────


def _make_summary(tmp_path):
    draft = tmp_path / "draft" / "proof.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("# Proof\n\nContent.", encoding="utf-8")
    return Phase8Summary(
        question="Q?",
        methodological_rigor="MEDIUM",
        residual_risk="LOW",
        evidence_basis="mixed",
        criteria_test_status="defined_criteria_all_within_tolerance",
        top_limitations=["L1", "L2", "L3"],
        draft_path=draft,
        run_id="r1",
    )


def test_phase8_telegram_text_contains_run_id_and_status():
    summary = Phase8Summary(
        question="Q", methodological_rigor="MEDIUM", residual_risk="LOW",
        evidence_basis="mixed", criteria_test_status="x",
        top_limitations=["a", "b", "c"], draft_path=__import__("pathlib").Path("x"),
        run_id="r-007",
    )
    text = summary.to_telegram_text()
    assert "r-007" in text
    assert "MEDIUM" in text
    assert "/approve r-007" in text
    assert "/reject r-007" in text


def test_phase8_approved_moves_draft_to_final(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    summary = _make_summary(rd)
    mgr = get_manager()

    def responder():
        for _ in range(200):
            if mgr.has_pending("r1", INVESTIGATION_CRITERION):
                break
            time.sleep(0.01)
        mgr.respond(
            run_id="r1", criterion_id=INVESTIGATION_CRITERION,
            response="approved", telegram_msg_id="msg-99",
            approver="dominik",
        )

    threading.Thread(target=responder, daemon=True).start()
    result = phase_final_approval(
        summary=summary, run_dir=rd, run_id="r1",
        notify_callable=lambda text: "msg-99",
        timeout_sec=2.0,
    )
    assert result.state == "approved"
    assert result.final_proof_path == rd / "proof.md"
    assert result.final_proof_path.is_file()
    assert not summary.draft_path.exists()
    # Audit entry written
    entries = audit_trail.load_audit_entries(rd, entry_type="investigation_approval")
    assert len(entries) == 1
    assert entries[0]["user_response"] == "approved"


def test_phase8_rejected_keeps_draft(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    summary = _make_summary(rd)
    mgr = get_manager()

    def responder():
        for _ in range(200):
            if mgr.has_pending("r1", INVESTIGATION_CRITERION):
                break
            time.sleep(0.01)
        mgr.respond(run_id="r1", criterion_id=INVESTIGATION_CRITERION,
                    response="rejected", reason="not ready")

    threading.Thread(target=responder, daemon=True).start()
    result = phase_final_approval(
        summary=summary, run_dir=rd, run_id="r1",
        notify_callable=lambda text: "",
        timeout_sec=2.0,
    )
    assert result.state == "rejected"
    assert result.final_proof_path is None
    assert summary.draft_path.exists()
    assert result.reason == "not ready"


def test_phase8_timeout_keeps_draft(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    summary = _make_summary(rd)
    # No responder thread → manager times out
    result = phase_final_approval(
        summary=summary, run_dir=rd, run_id="r1",
        notify_callable=lambda text: "",
        timeout_sec=0.05,
    )
    assert result.state == "timeout"
    assert summary.draft_path.exists()


def test_extract_top_limitations_first_sentence_each():
    proof = """\
# Proof

## Was diese Investigation NICHT beweist

### 1. Multi-LLM-Korpus
First sentence. Second sentence.

### 2. Self-Reporting
Only one here.

### 3. Disziplin
A line. Another line.
"""
    lims = extract_top_limitations(proof, max_count=3)
    assert lims[0].startswith("First sentence")
    assert "Only one here" in lims[1]
    assert lims[2].startswith("A line")
