"""Phase 5a (mechanical falsification check) + Phase 5b (heuristic LLM review).

Plan §2.5:
  * Phase 5a is *deterministic*: read pre-reg thresholds from
    ``audit/approvals.jsonl`` and compare against the measured values the
    sub-tasks produced. Status per criterion ∈
    {passed, failed, not_checkable}. Aggregate maps to the
    ``criteria_test_status`` field of the StatusTuple.
  * Phase 5b is *heuristic*: a cross-provider LLM reads the synthesised
    proof.md plus the falsification report and surfaces P1/P2/P3 findings
    that the mechanical check would miss (logic gaps, statistical
    misinterpretation, etc.). The LLM never modifies the proof — its
    output flows into the decision-log and the engineering-reviewer
    prompt in Phase 7.

Mechanical-check measurement provenance
----------------------------------------
Sub-tasks can emit measurements in two ways:
  1. As a structured ``MeasurementMap`` dict ``{criterion_id: value}``
     passed in by the caller (typical when the sub-task executor knows
     how to extract numbers from its dev-loop output).
  2. As a JSON sidecar file written by the sub-task itself at
     ``{sub_state_cwd}/measurements.json`` with the same dict shape —
     ``_load_measurements_from_state`` walks the run's state dir and
     merges them.

Whichever path the caller uses, the report carries the raw value, the
parsed threshold, and a binary pass/fail outcome so the audit can be
re-derived offline from the run dir alone.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from config import TOOL_SI_PHASE5B_TIMEOUT_SEC
from providers.base import BaseProvider
from tools.crosschecks import audit_trail
from tools.scientific_investigation_phase2 import _parse_review_findings
from tools.scientific_investigation_phase4 import CriteriaStatus
from tools.scientific_investigation_phases import PreRegResult, Threshold

logger = logging.getLogger(__name__)

CheckStatus = Literal["passed", "failed", "not_checkable"]

# Type alias — `{criterion_id: numeric value}` produced by a sub-task.
MeasurementMap = dict[str, float]


# ── Dataclasses ───────────────────────────────────────────────────────────


@dataclass
class CriterionCheck:
    criterion_id: str
    threshold_raw: str
    threshold_numeric: float | None
    threshold_kind: Literal["percent", "absolute", "unknown"]
    measured: float | None
    status: CheckStatus
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "criterion_id": self.criterion_id,
            "threshold_raw": self.threshold_raw,
            "threshold_numeric": self.threshold_numeric,
            "threshold_kind": self.threshold_kind,
            "measured": self.measured,
            "status": self.status,
            "note": self.note,
        }


@dataclass
class Phase5aReport:
    checks: list[CriterionCheck] = field(default_factory=list)

    @property
    def aggregate_status(self) -> CriteriaStatus:
        if not self.checks:
            return "criteria_not_testable"
        statuses = [c.status for c in self.checks]
        if all(s == "passed" for s in statuses):
            return "defined_criteria_all_within_tolerance"
        if all(s == "failed" for s in statuses):
            return "defined_criteria_all_outside_tolerance"
        if any(s == "passed" for s in statuses) and any(s == "failed" for s in statuses):
            return "defined_criteria_some_outside_tolerance"
        # Only not_checkable items left.
        return "criteria_not_testable"


@dataclass
class Phase5bReport:
    findings: list  # list[ReviewFinding] from phase2
    raw_output: str
    reviewer_provider: str

    def has_open_p1(self) -> bool:
        return any(f.severity == "P1" for f in self.findings)


# ── Threshold parsing ─────────────────────────────────────────────────────


_PERCENT_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*%\s*$")
_ABSOLUTE_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*([a-zA-Z%]*)\s*$")


def parse_threshold(raw: str) -> tuple[float | None, Literal["percent", "absolute", "unknown"]]:
    """Return ``(value, kind)``.

    * ``"5%"``     → ``(5.0, "percent")``
    * ``"10mm"``   → ``(10.0, "absolute")``
    * ``"7.2"``    → ``(7.2, "absolute")``
    * ``"between 5 and 7"`` → ``(None, "unknown")`` (caller marks not_checkable)
    """
    if not isinstance(raw, str):
        return None, "unknown"
    raw = raw.strip()
    m = _PERCENT_RE.match(raw)
    if m:
        try:
            return float(m.group(1)), "percent"
        except ValueError:
            return None, "unknown"
    m = _ABSOLUTE_RE.match(raw)
    if m:
        try:
            return float(m.group(1)), "absolute"
        except ValueError:
            return None, "unknown"
    return None, "unknown"


def evaluate_threshold(measured: float, threshold: float, kind: str) -> CheckStatus:
    """A measurement passes if ``|measured| <= threshold`` (tolerance band).

    Percent vs absolute is treated the same way numerically — the caller is
    expected to report measured values in the same unit as the threshold
    (so a 5% tolerance threshold sees a "1.3" measured value as 1.3%).
    """
    try:
        if abs(measured) <= threshold:
            return "passed"
        return "failed"
    except (TypeError, ValueError):
        return "not_checkable"


# ── Measurement collection ───────────────────────────────────────────────


def _load_measurements_from_state(root_cwd: Path, run_id: str) -> MeasurementMap:
    """Merge ``{sub_state_cwd}/measurements.json`` files across all sub-tasks.

    Tolerant: missing/empty/malformed files are skipped with a warning.
    Later sub-task files overwrite earlier ones when keys collide — sub-
    tasks producing multiple criteria should each contribute distinct keys.
    """
    base = root_cwd / ".scientific-investigation" / run_id / "sub-tasks"
    out: MeasurementMap = {}
    if not base.exists():
        return out
    for sub_dir in sorted(base.iterdir()):
        meas_file = sub_dir / "measurements.json"
        if not meas_file.is_file():
            continue
        try:
            data = json.loads(meas_file.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "phase5a: %s unreadable, skipping: %s", meas_file, exc,
            )
            continue
        if not isinstance(data, dict):
            continue
        for k, v in data.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                logger.warning(
                    "phase5a: measurement %s=%r in %s is not numeric, skipping",
                    k, v, meas_file,
                )
    return out


# ── Phase 5a runner ──────────────────────────────────────────────────────


def phase_mechanical_falsification_check(
    prereg: PreRegResult,
    *,
    run_dir: Path,
    root_cwd: Path,
    run_id: str,
    measurements: MeasurementMap | None = None,
) -> Phase5aReport:
    """Compare each pre-reg threshold against the measured value.

    ``measurements`` (when not None) takes precedence over disk-loaded
    measurements — tests pass an explicit dict. Production callers can
    omit the kwarg and rely on the sub-task JSON sidecars.
    """
    measured_map: MeasurementMap = dict(measurements) if measurements is not None else {}
    if not measured_map:
        measured_map = _load_measurements_from_state(root_cwd, run_id)

    checks: list[CriterionCheck] = []
    for t in prereg.thresholds:
        value, kind = parse_threshold(t.threshold_value)
        measured = measured_map.get(t.criterion_id)
        if value is None or measured is None:
            checks.append(CriterionCheck(
                criterion_id=t.criterion_id,
                threshold_raw=t.threshold_value,
                threshold_numeric=value,
                threshold_kind=kind,
                measured=measured,
                status="not_checkable",
                note=(
                    "Schwelle nicht parsbar" if value is None
                    else f"Keine Messung für {t.criterion_id}"
                ),
            ))
            continue
        status = evaluate_threshold(measured, value, kind)
        checks.append(CriterionCheck(
            criterion_id=t.criterion_id,
            threshold_raw=t.threshold_value,
            threshold_numeric=value,
            threshold_kind=kind,
            measured=measured,
            status=status,
        ))

    # Persist the report as a sidecar file inside the audit dir — the
    # synthesis prompt + later phases can read it back without re-running.
    report_path = run_dir / "audit" / "phase5a_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {"checks": [c.as_dict() for c in checks]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return Phase5aReport(checks=checks)


# ── Phase 5b heuristic review ────────────────────────────────────────────


_REVIEW_PROMPT = """\
Du bist ein cross-provider Heuristik-Reviewer. Du erhältst das Synthese-\
Dokument einer Investigation und den mechanischen Falsifikations-Bericht. \
Suche nach Logik-Lücken, statistischen Fehlinterpretationen, fehlenden \
Sensitivitäts-Analysen — Dinge, die der mechanische Check NICHT findet.

## Synthese-Dokument (proof.md draft)

{proof_md}

## Phase-5a-Falsifikations-Bericht

{phase5a_json}

Antworte NUR mit einem YAML-Block (zwischen ```yaml und ```) — gleiches \
Schema wie Phase 2:

```yaml
findings:
  - severity: P1   # P1 = Blocker, P2 = Lücke, P3 = Hinweis
    sub_id: SECTION   # Sektion im proof.md ODER "ALL"
    issue: <Konkretes Problem>
    suggestion: <Konkreter Verbesserungsvorschlag>
```

Wenn du keine substantiellen Findings hast, schreibe `findings: []`.
"""


def phase_heuristic_review(
    proof_md: str,
    phase5a: Phase5aReport,
    reviewer_provider: BaseProvider,
    *,
    timeout_sec: int | None = None,
) -> Phase5bReport:
    """Run the cross-provider heuristic review against the synthesis.

    Caller picks ``reviewer_provider`` (typically NOT the primary that
    wrote the synthesis — defeats the cross-provider purpose). Falls back
    to primary silently when caller passes None? — no, we expect the caller
    to handle that; if they want the primary they pass it explicitly.
    """
    if timeout_sec is None:
        timeout_sec = TOOL_SI_PHASE5B_TIMEOUT_SEC
    prompt = _REVIEW_PROMPT.format(
        proof_md=proof_md,
        phase5a_json=json.dumps(
            {"checks": [c.as_dict() for c in phase5a.checks]},
            ensure_ascii=False, indent=2,
        ),
    )
    result = reviewer_provider.run(prompt, timeout=timeout_sec, read_only=True)
    if not getattr(result, "success", False):
        raise RuntimeError(
            f"Phase 5b review LLM call failed: {getattr(result, 'error', 'unknown')}"
        )
    raw = (result.output or "").strip()
    findings = _parse_review_findings(raw, reviewer="phase5b_heuristic")
    return Phase5bReport(
        findings=findings,
        raw_output=raw,
        reviewer_provider=getattr(reviewer_provider, "name", "unknown"),
    )


# ── Output writers ───────────────────────────────────────────────────────


def write_phase5_report_md(
    run_dir: Path,
    *,
    phase5a: Phase5aReport,
    phase5b: Phase5bReport | None,
) -> Path:
    """Render phase5_report.md combining mechanical + heuristic findings."""
    rows: list[str] = ["# Phase 5 — Falsifikations-Bericht\n"]
    rows.append("## Phase 5a — Mechanischer Check\n")
    rows.append(f"Aggregierter Status: **{phase5a.aggregate_status}**\n")
    rows.append("")
    rows.append("| Pre-Reg | Schwellwert | Gemessen | Status | Hinweis |")
    rows.append("| --- | --- | --- | --- | --- |")
    for c in phase5a.checks:
        rows.append(
            f"| {c.criterion_id} | {c.threshold_raw} | "
            f"{c.measured if c.measured is not None else '—'} | "
            f"`{c.status}` | {c.note or '—'} |"
        )
    rows.append("")
    rows.append("## Phase 5b — Heuristisches LLM-Review\n")
    if phase5b is None:
        rows.append("*(Phase 5b nicht ausgeführt — kein cross-provider Reviewer verfügbar.)*\n")
    elif not phase5b.findings:
        rows.append(
            f"*(Reviewer `{phase5b.reviewer_provider}` — keine substantiellen Findings.)*\n"
        )
    else:
        rows.append(f"**Reviewer-Provider:** `{phase5b.reviewer_provider}`\n")
        for f in phase5b.findings:
            rows.append(
                f"- **[{f.severity}]** sub_id={f.sub_id} — {f.issue}\n"
                f"    *Vorschlag:* {f.suggestion}"
            )
        rows.append("")
    out = run_dir / "phase5_report.md"
    out.write_text("\n".join(rows), encoding="utf-8")
    return out
