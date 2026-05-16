"""I3 tests for scientific-investigation Phase 2: multi-persona review loop.

Covers:
  * Author plan YAML parsing + sequence validation.
  * DA + Methodiker findings parsing (including empty lists).
  * Convergence on no-P1 findings.
  * P1 finding triggers Author rework, loops until cap.
  * Max-iterations cap leaves the result non-converged with findings intact.
  * Cross-provider routing actually invokes the per-persona provider.
  * Write helpers produce sensible markdown output.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from providers.base import RunResult
from tools.personas import AUTHOR, DEVILS_ADVOCATE, METHODIKER
from tools.personas.base import PersonaAllocation
from tools.scientific_investigation_phase2 import (
    InvestigationPlan,
    Phase2Result,
    ReviewFinding,
    SubTask,
    _parse_investigation_plan,
    _parse_review_findings,
    phase_investigation_plan_review,
    write_investigation_plan_md,
    write_review_findings_md,
)
from tools.scientific_investigation_phases import FramingResult, PreRegResult, Threshold


# ── Helpers ──────────────────────────────────────────────────────────────────


class _QueueProvider:
    name = "claude"
    supports_sessions = False

    def __init__(self, outputs: list[str]):
        self._outputs = list(outputs)
        self.calls: list[str] = []

    def run(self, task: str, **kwargs) -> RunResult:
        self.calls.append(task)
        if not self._outputs:
            return RunResult(success=False, error="queue empty")
        return RunResult(success=True, output=self._outputs.pop(0))


def _make_framing():
    return FramingResult(
        question="Q",
        hypothesis="H",
        bias_statement="B",
        discipline="engineering",
        framing_text="engineering eval long enough",
    )


def _make_prereg():
    return PreRegResult(
        thresholds=[
            Threshold("F1", "desc", "5%", "norm_reference", "DIN-EN §1 5%"),
            Threshold("F2", "desc", "10mm", "norm_reference", "DIN-EN §2 10mm"),
        ],
        discipline_warning=False,
        discipline_warning_approved=False,
        prereg_hash="abc",
    )


def _make_allocations(provider_name: str = "claude"):
    return [
        PersonaAllocation(persona=AUTHOR, provider_name=provider_name, cross_provider_satisfied=False),
        PersonaAllocation(persona=DEVILS_ADVOCATE, provider_name=provider_name, cross_provider_satisfied=False),
        PersonaAllocation(persona=METHODIKER, provider_name=provider_name, cross_provider_satisfied=False),
    ]


_PLAN_OK = """```yaml
sub_tasks:
  - sub_id: S1
    title: Tolerance check at 800K
    description: Run measurement series and compare against DIN limit
    addresses_criteria: [F1]
    type: data_analysis
    expected_output: bias measurement
  - sub_id: S2
    title: Geometric check
    description: Measure dimensional tolerance
    addresses_criteria: [F2]
    type: data_analysis
    expected_output: dimensional report
```"""

_REVIEW_EMPTY = "```yaml\nfindings: []\n```"


def _make_p1_finding_yaml(sub_id: str = "S1") -> str:
    return f"""```yaml
findings:
  - severity: P1
    sub_id: {sub_id}
    issue: Sub-Task adressiert F1 nicht wirklich — Messmethodik fehlt
    suggestion: Methodik-Beschreibung ergaenzen, z.B. Messgeraet und Versuchsaufbau
```"""


# ── Plan parsing ────────────────────────────────────────────────────────────


def test_parse_investigation_plan_happy_path():
    plan = _parse_investigation_plan(_PLAN_OK)
    assert isinstance(plan, InvestigationPlan)
    assert len(plan.sub_tasks) == 2
    assert plan.sub_tasks[0].sub_id == "S1"
    assert plan.sub_tasks[0].addresses_criteria == ["F1"]
    assert plan.sub_tasks[1].addresses_criteria == ["F2"]


def test_parse_investigation_plan_rejects_non_sequential_ids():
    bad = """```yaml
sub_tasks:
  - sub_id: S1
    title: x
    description: d
    addresses_criteria: [F1]
    type: t
    expected_output: o
  - sub_id: S3
    title: y
    description: d
    addresses_criteria: [F2]
    type: t
    expected_output: o
```"""
    with pytest.raises(ValueError, match="must be S2"):
        _parse_investigation_plan(bad)


def test_parse_investigation_plan_rejects_missing_addresses_criteria():
    bad = """```yaml
sub_tasks:
  - sub_id: S1
    title: x
    description: d
    addresses_criteria: []
    type: t
    expected_output: o
```"""
    with pytest.raises(ValueError, match="Pre-Reg-Mapping ist Pflicht"):
        _parse_investigation_plan(bad)


def test_parse_investigation_plan_rejects_too_many_sub_tasks():
    block = "sub_tasks:\n" + "".join(
        f"""  - sub_id: S{i}
    title: t{i}
    description: d
    addresses_criteria: [F1]
    type: t
    expected_output: o
""" for i in range(1, 10)  # 9 sub-tasks
    )
    with pytest.raises(ValueError, match="max 8 allowed"):
        _parse_investigation_plan(f"```yaml\n{block}```")


# ── Findings parsing ──────────────────────────────────────────────────────


def test_parse_review_findings_empty_list():
    findings = _parse_review_findings(_REVIEW_EMPTY, reviewer="devils_advocate")
    assert findings == []


def test_parse_review_findings_single_p1():
    findings = _parse_review_findings(
        _make_p1_finding_yaml(), reviewer="devils_advocate",
    )
    assert len(findings) == 1
    assert findings[0].severity == "P1"
    assert findings[0].sub_id == "S1"
    assert findings[0].reviewer == "devils_advocate"


def test_parse_review_findings_invalid_severity_defaults_to_p3():
    text = """```yaml
findings:
  - severity: X9
    sub_id: S1
    issue: i
    suggestion: s
```"""
    findings = _parse_review_findings(text, reviewer="methodiker")
    assert findings[0].severity == "P3"


# ── phase_investigation_plan_review: convergence ──────────────────────────


def test_phase2_converges_on_empty_findings(tmp_path):
    provider = _QueueProvider([_PLAN_OK, _REVIEW_EMPTY, _REVIEW_EMPTY])
    rd = tmp_path / "rd"
    rd.mkdir()
    result = phase_investigation_plan_review(
        _make_framing(),
        _make_prereg(),
        _make_allocations(),
        provider,
        run_dir=rd,
        run_id="r1",
        provider_lookup=lambda _: None,
    )
    assert result.converged is True
    assert result.iterations_used == 1
    assert len(result.plan.sub_tasks) == 2
    assert result.latest_findings() == []


def test_phase2_rework_after_p1_then_converges(tmp_path):
    # Iter 1: DA finds P1 → rework triggered
    # Iter 2: rework plan + clean reviews → converge
    provider = _QueueProvider([
        _PLAN_OK,                       # author iter 1
        _make_p1_finding_yaml("S1"),    # DA iter 1
        _REVIEW_EMPTY,                  # methodiker iter 1
        _PLAN_OK,                       # author rework iter 2
        _REVIEW_EMPTY,                  # DA iter 2
        _REVIEW_EMPTY,                  # methodiker iter 2
    ])
    rd = tmp_path / "rd"
    rd.mkdir()
    result = phase_investigation_plan_review(
        _make_framing(),
        _make_prereg(),
        _make_allocations(),
        provider,
        run_dir=rd,
        run_id="r1",
        provider_lookup=lambda _: None,
        max_iterations=3,
    )
    assert result.converged is True
    assert result.iterations_used == 2
    assert len(result.findings_by_iteration) == 2
    assert any(f.severity == "P1" for f in result.findings_by_iteration[0])
    assert result.findings_by_iteration[1] == []


def test_phase2_cap_reached_returns_non_converged(tmp_path):
    # Every iteration produces a P1 finding → never converges.
    # max_iterations=2 to keep the test short.
    outputs = []
    for _ in range(2):
        outputs.extend([_PLAN_OK, _make_p1_finding_yaml("S1"), _REVIEW_EMPTY])
    provider = _QueueProvider(outputs)
    rd = tmp_path / "rd"
    rd.mkdir()
    result = phase_investigation_plan_review(
        _make_framing(),
        _make_prereg(),
        _make_allocations(),
        provider,
        run_dir=rd,
        run_id="r1",
        provider_lookup=lambda _: None,
        max_iterations=2,
    )
    assert result.converged is False
    assert result.iterations_used == 2
    assert result.has_open_p1() is True


def test_phase2_propagates_author_call_failure(tmp_path):
    """If the author LLM call fails, we surface that as RuntimeError."""
    class _BrokenProvider:
        name = "claude"
        supports_sessions = False

        def run(self, task, **kwargs):
            return RunResult(success=False, error="capacity exhausted")

    rd = tmp_path / "rd"
    rd.mkdir()
    with pytest.raises(RuntimeError, match="capacity exhausted"):
        phase_investigation_plan_review(
            _make_framing(),
            _make_prereg(),
            _make_allocations(),
            _BrokenProvider(),
            run_dir=rd,
            run_id="r1",
            provider_lookup=lambda _: None,
        )


# ── Provider routing via persona allocation ────────────────────────────────


def test_phase2_routes_each_persona_to_its_provider(tmp_path):
    """Author hits primary; DA + Methodiker hit their allocated providers."""
    author_provider = _QueueProvider([_PLAN_OK])
    da_provider = _QueueProvider([_REVIEW_EMPTY])
    meth_provider = _QueueProvider([_REVIEW_EMPTY])

    def lookup(name: str):
        return {"gemini": da_provider, "codex": meth_provider}.get(name)

    allocations = [
        PersonaAllocation(AUTHOR, "claude", cross_provider_satisfied=False),
        PersonaAllocation(DEVILS_ADVOCATE, "gemini", cross_provider_satisfied=True),
        PersonaAllocation(METHODIKER, "codex", cross_provider_satisfied=True),
    ]
    rd = tmp_path / "rd"
    rd.mkdir()
    result = phase_investigation_plan_review(
        _make_framing(),
        _make_prereg(),
        allocations,
        author_provider,
        run_dir=rd,
        run_id="r1",
        provider_lookup=lookup,
    )
    assert result.converged is True
    assert len(author_provider.calls) == 1  # only the plan
    assert len(da_provider.calls) == 1
    assert len(meth_provider.calls) == 1


def test_phase2_falls_back_to_primary_when_lookup_returns_none(tmp_path):
    primary = _QueueProvider([_PLAN_OK, _REVIEW_EMPTY, _REVIEW_EMPTY])
    allocations = [
        PersonaAllocation(AUTHOR, "claude", cross_provider_satisfied=False),
        PersonaAllocation(DEVILS_ADVOCATE, "gemini", cross_provider_satisfied=True),
        PersonaAllocation(METHODIKER, "codex", cross_provider_satisfied=True),
    ]
    rd = tmp_path / "rd"
    rd.mkdir()
    result = phase_investigation_plan_review(
        _make_framing(),
        _make_prereg(),
        allocations,
        primary,
        run_dir=rd,
        run_id="r1",
        provider_lookup=lambda _: None,  # nothing resolvable
    )
    assert result.converged is True
    assert len(primary.calls) == 3  # all three persona calls went to primary


# ── Output writers ─────────────────────────────────────────────────────────


def test_write_investigation_plan_md_renders_status(tmp_path):
    plan = InvestigationPlan(
        sub_tasks=[
            SubTask("S1", "Title", "desc", ["F1"], "data_analysis", "out"),
        ],
        raw_yaml="sub_tasks:\n  - sub_id: S1",
    )
    path = write_investigation_plan_md(
        tmp_path, plan=plan, converged=True, iterations=2,
    )
    text = path.read_text("utf-8")
    assert "Investigation Plan" in text
    assert "converged" in text
    assert "Iterationen:** 2" in text
    assert "S1: Title" in text
    assert "F1" in text


def test_write_investigation_plan_md_shows_warning_when_non_converged(tmp_path):
    plan = InvestigationPlan(
        sub_tasks=[SubTask("S1", "T", "d", ["F1"], "x", "o")],
        raw_yaml="sub_tasks: [...]",
    )
    text = write_investigation_plan_md(
        tmp_path, plan=plan, converged=False, iterations=3,
    ).read_text("utf-8")
    assert "cap reached" in text or "⚠️" in text


def test_write_review_findings_md_renders_per_iteration(tmp_path):
    findings_by_iteration = [
        [ReviewFinding("P1", "S1", "issue1", "sugg1", "devils_advocate")],
        [],
    ]
    text = write_review_findings_md(
        tmp_path, findings_by_iteration=findings_by_iteration,
    ).read_text("utf-8")
    assert "Iteration 1" in text
    assert "Iteration 2" in text
    assert "issue1" in text
    assert "konvergiert" in text  # empty-list placeholder for iteration 2
