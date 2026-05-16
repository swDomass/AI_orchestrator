"""Phase 7 — Engineering-Reviewer with rework loop (Plan §2.7, I7).

A cross-provider LLM reads the synthesis output produced in Phase 4 (plus
the mechanical + heuristic reports from Phase 5) and surfaces findings.

  * BLOCKER finding → Author rewrites the affected sections, loop step 1.
  * HINT finding    → recorded for the decision-log but does not block.

Termination:
  * Convergence — no BLOCKER findings remain → status="passed".
  * Cap reached (default ``TOOL_SI_PHASE7_MAX_REWORK_ITERATIONS``)
    → status="needs_revision". The investigation continues to Phase 8
    but the Status-Tuple downgrade is automatic (status != "passed").

Provider selection (Plan §2.7):
  * If ``engineering_reviewer_provider_name`` (from the
    ``#engineering_reviewer:<alias>`` tag) is supplied, use that.
  * Otherwise pick any provider *different from* the primary that the
    injected ``provider_lookup`` can resolve.
  * Fallback to primary with a logged warning — status stays "passed"
    only when a real cross-provider review happened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from config import (
    TOOL_SI_PHASE7_MAX_REWORK_ITERATIONS,
    TOOL_SI_PHASE7_TIMEOUT_SEC,
)
from providers.base import BaseProvider
from tools.crosschecks import audit_trail
from tools.personas import AUTHOR
from tools.scientific_investigation_phase2 import _parse_review_findings

logger = logging.getLogger(__name__)


ReviewerStatus = Literal["passed", "needs_revision", "skipped"]
ProviderLookup = Callable[[str], BaseProvider | None]

# Audit-trail entry type for engineering reviewer outcomes.
AUDIT_TYPE = "execution_sub_task"  # reuses the existing type so we don't grow the enum


# ── Dataclasses ──────────────────────────────────────────────────────────


@dataclass
class EngineeringFinding:
    severity: Literal["BLOCKER", "HINT"]
    section: str
    issue: str
    suggestion: str

    @classmethod
    def from_p_severity(cls, p_finding) -> "EngineeringFinding":
        sev: Literal["BLOCKER", "HINT"] = (
            "BLOCKER" if p_finding.severity == "P1" else "HINT"
        )
        return cls(
            severity=sev,
            section=p_finding.sub_id,
            issue=p_finding.issue,
            suggestion=p_finding.suggestion,
        )


@dataclass
class Phase7Result:
    status: ReviewerStatus
    iterations_used: int
    final_proof_md: str
    findings_by_iteration: list[list[EngineeringFinding]] = field(default_factory=list)
    reviewer_provider_name: str = ""
    cross_provider_satisfied: bool = False

    def open_blockers(self) -> list[EngineeringFinding]:
        if not self.findings_by_iteration:
            return []
        return [
            f for f in self.findings_by_iteration[-1] if f.severity == "BLOCKER"
        ]


# ── Prompts ───────────────────────────────────────────────────────────────


_REVIEWER_PROMPT = """\
Du bist Engineering-Reviewer (4. Persona neben Author, DA, Methodiker). Du \
reviewst das Synthese-Output AUS PHASE 4 mit einem aggressiven Engineering-\
Profil:

1. **Bilanz-Konsistenz**: stimmen die Zahlen über alle Sub-Tasks?
2. **Plausibilität gegen Literatur**: sind die Werte in publizierten Korridoren?
3. **Substanz der Pflicht-Prosa-Limitations-Sektion**: sind die Beschreibungen \
   spezifisch zu DIESER Investigation, oder Boilerplate?
4. **Crosscheck-Konsistenz**: passen die Tier-Zuweisungen zu den Crosscheck-\
   Inhalten?
5. **Pre-Reg-Schwellen vs. Falsifikations-Test-Ergebnis**: keine Verschiebung \
   nachträglich?

## Synthese-Dokument

{proof_md}

## Phase-5-Bericht (mechanisch + heuristisch)

{phase5_summary}

Antworte NUR mit einem YAML-Block (zwischen ```yaml und ```). Schema:

```yaml
findings:
  - severity: P1   # P1 → BLOCKER (Author muss überarbeiten), P3 → HINT (nur Log)
    sub_id: SECTION_NAME   # z.B. "Falsifikations-Test-Katalog" oder "Limitations §2"
    issue: <Konkretes Problem, max. 3 Sätze>
    suggestion: <Konkreter Verbesserungsvorschlag, max. 3 Sätze>
```

WICHTIG:
- Nutze nur P1 (BLOCKER) und P3 (HINT). P2 ist hier nicht definiert.
- Boilerplate-Findings ("könnte ausführlicher sein") sind nicht erwünscht.
- Bei keinen substantiellen Findings: schreibe `findings: []`.
"""


_REWORK_PROMPT = """\
{persona_system_prompt}

## Engineering-Reviewer-Findings (BLOCKER — Pflicht zur Korrektur)

{blockers_block}

## Vorheriger Synthese-Stand

{previous_proof}

## Aufgabe

Überarbeite das Synthese-Dokument so, dass ALLE oben gelisteten BLOCKER-\
Findings adressiert sind. Behalte die Pflicht-Struktur des proof.md \
(Limitations-Sektion am Anfang mit allen 5 Kategorien, Falsifikations-\
Test-Katalog als Tabelle, etc.) bei.

Antworte mit dem KOMPLETT überarbeiteten Markdown-Dokument — nicht nur \
mit Diffs oder Kommentaren.
"""


# ── Provider resolution ───────────────────────────────────────────────────


def _resolve_reviewer_provider(
    primary_provider: BaseProvider,
    explicit_name: str | None,
    provider_lookup: ProviderLookup,
) -> tuple[BaseProvider, bool, str]:
    """Pick the reviewer provider per Plan §2.7.

    Returns ``(provider, cross_provider_satisfied, name)``. When the
    explicit name resolves to the primary, ``cross_provider_satisfied`` is
    False — strict cross-provider semantics, no self-review counts.
    """
    if explicit_name:
        prov = provider_lookup(explicit_name)
        if prov is not None and getattr(prov, "name", "") != primary_provider.name:
            return prov, True, getattr(prov, "name", explicit_name)
        logger.warning(
            "Phase 7: explicit engineering_reviewer=%r not resolvable as a "
            "cross-provider; falling back to primary",
            explicit_name,
        )
        return primary_provider, False, primary_provider.name

    # No explicit reviewer — try the dispatcher's known names except the primary.
    for name in ("openrouter", "gemini", "codex", "claude"):
        if name == primary_provider.name:
            continue
        prov = provider_lookup(name)
        if prov is not None:
            return prov, True, name
    logger.warning(
        "Phase 7: no cross-provider available, using primary %s as reviewer",
        primary_provider.name,
    )
    return primary_provider, False, primary_provider.name


# ── Phase 7 runner ───────────────────────────────────────────────────────


def _phase5_summary(phase5a_dict: dict, phase5b_findings: list) -> str:
    parts = ["**Phase 5a** (mechanisch):"]
    for c in phase5a_dict.get("checks", []):
        parts.append(
            f"- {c['criterion_id']} ({c['status']}): "
            f"threshold={c.get('threshold_raw', '?')}, measured={c.get('measured', '?')}"
        )
    parts.append("")
    parts.append("**Phase 5b** (heuristisch):")
    if not phase5b_findings:
        parts.append("- *(keine Findings)*")
    else:
        for f in phase5b_findings:
            parts.append(f"- [{f.severity}] {f.sub_id}: {f.issue[:200]}")
    return "\n".join(parts)


def _blockers_block(findings: list[EngineeringFinding]) -> str:
    blockers = [f for f in findings if f.severity == "BLOCKER"]
    if not blockers:
        return "*(keine BLOCKER)*"
    return "\n".join(
        f"- **{f.section}**: {f.issue}\n    *Vorschlag:* {f.suggestion}"
        for f in blockers
    )


def phase_engineering_reviewer(
    proof_md: str,
    phase5a_dict: dict,
    phase5b_findings: list,
    primary_provider: BaseProvider,
    *,
    run_dir: Path,
    run_id: str,
    explicit_reviewer_name: str | None = None,
    provider_lookup: ProviderLookup | None = None,
    max_iterations: int | None = None,
    timeout_per_call: int | None = None,
) -> Phase7Result:
    """Run the engineering-reviewer rework loop.

    The reviewer call is read-only; the Author rework call writes the new
    proof.md draft back into the working text (returned as
    ``final_proof_md``). The caller persists the final text into
    ``run_dir/draft/proof.md``.

    On cap-reached the loop returns ``status="needs_revision"`` with the
    last-iteration findings still attached — Status-Tuple in I8 will
    interpret that as "MEDIUM not achievable".
    """
    if provider_lookup is None:
        from dispatcher import get_provider_by_name as _lookup
        provider_lookup = _lookup
    if max_iterations is None:
        max_iterations = TOOL_SI_PHASE7_MAX_REWORK_ITERATIONS
    if timeout_per_call is None:
        timeout_per_call = TOOL_SI_PHASE7_TIMEOUT_SEC

    reviewer, cross_ok, reviewer_name = _resolve_reviewer_provider(
        primary_provider, explicit_reviewer_name, provider_lookup,
    )
    phase5_summary = _phase5_summary(phase5a_dict, phase5b_findings)
    current_proof = proof_md
    findings_history: list[list[EngineeringFinding]] = []

    for iteration in range(1, max_iterations + 1):
        review_prompt = _REVIEWER_PROMPT.format(
            proof_md=current_proof,
            phase5_summary=phase5_summary,
        )
        review_result = reviewer.run(
            review_prompt, timeout=timeout_per_call, read_only=True,
        )
        if not getattr(review_result, "success", False):
            raise RuntimeError(
                f"Phase 7 review LLM call failed: "
                f"{getattr(review_result, 'error', 'unknown')}"
            )
        p_findings = _parse_review_findings(
            review_result.output or "", reviewer="engineering_reviewer",
        )
        eng_findings = [EngineeringFinding.from_p_severity(p) for p in p_findings]
        findings_history.append(eng_findings)

        blockers = [f for f in eng_findings if f.severity == "BLOCKER"]
        if not blockers:
            _emit_audit(run_dir, run_id, iteration, eng_findings, reviewer_name,
                        cross_ok, status="passed")
            return Phase7Result(
                status="passed",
                iterations_used=iteration,
                final_proof_md=current_proof,
                findings_by_iteration=findings_history,
                reviewer_provider_name=reviewer_name,
                cross_provider_satisfied=cross_ok,
            )

        # BLOCKER present → ask the Author (primary provider) to rework.
        rework_prompt = _REWORK_PROMPT.format(
            persona_system_prompt=AUTHOR.system_prompt,
            blockers_block=_blockers_block(eng_findings),
            previous_proof=current_proof,
        )
        rework_result = primary_provider.run(
            rework_prompt, timeout=timeout_per_call, read_only=True,
        )
        if not getattr(rework_result, "success", False):
            raise RuntimeError(
                f"Phase 7 rework LLM call failed: "
                f"{getattr(rework_result, 'error', 'unknown')}"
            )
        current_proof = (rework_result.output or "").strip() or current_proof
        _emit_audit(run_dir, run_id, iteration, eng_findings, reviewer_name,
                    cross_ok, status="rework")

    # Cap hit. Return whatever we have; status=needs_revision.
    _emit_audit(run_dir, run_id, max_iterations,
                findings_history[-1] if findings_history else [],
                reviewer_name, cross_ok, status="needs_revision")
    return Phase7Result(
        status="needs_revision",
        iterations_used=max_iterations,
        final_proof_md=current_proof,
        findings_by_iteration=findings_history,
        reviewer_provider_name=reviewer_name,
        cross_provider_satisfied=cross_ok,
    )


def _emit_audit(
    run_dir: Path, run_id: str, iteration: int,
    findings: list[EngineeringFinding], reviewer_name: str,
    cross_ok: bool, status: str,
) -> None:
    try:
        audit_trail.append_audit_entry(run_dir, {
            "type": AUDIT_TYPE,
            "run_id": run_id,
            "provider": reviewer_name,
            "summary": {
                "phase": "engineering_reviewer",
                "iteration": iteration,
                "status": status,
                "blocker_count": sum(1 for f in findings if f.severity == "BLOCKER"),
                "hint_count": sum(1 for f in findings if f.severity == "HINT"),
                "cross_provider_satisfied": cross_ok,
            },
        })
    except (OSError, ValueError) as exc:
        logger.warning("Phase 7 audit append failed: %s", exc)


# ── Output writer ────────────────────────────────────────────────────────


def write_phase7_report_md(
    run_dir: Path,
    *,
    phase7: Phase7Result,
) -> Path:
    """Render phase7_report.md summarising each iteration's findings."""
    parts: list[str] = ["# Phase 7 — Engineering-Reviewer\n"]
    parts.append(
        f"**Status:** `{phase7.status}`  \n"
        f"**Reviewer-Provider:** `{phase7.reviewer_provider_name}`  \n"
        f"**Cross-Provider erfüllt:** "
        f"{'✓' if phase7.cross_provider_satisfied else '—'}  \n"
        f"**Iterationen:** {phase7.iterations_used}\n"
    )
    parts.append("")
    for i, findings in enumerate(phase7.findings_by_iteration, start=1):
        parts.append(f"## Iteration {i}")
        if not findings:
            parts.append("*(keine Findings — passed)*")
        else:
            for f in findings:
                parts.append(
                    f"- **[{f.severity}]** {f.section}: {f.issue}\n"
                    f"    *Vorschlag:* {f.suggestion}"
                )
        parts.append("")
    out = run_dir / "phase7_report.md"
    out.write_text("\n".join(parts), encoding="utf-8")
    return out
