"""Phase 4 — Synthesis + Status-Tuple-Computation (Plan §2.4, §2.5, I5).

Workflow:

  1. Author LLM call drafts ``proof.md`` from the framing + Phase-2 plan +
     Phase-3 execution results. The prompt fixes the document structure
     (mandatory limitations section + falsification test catalog).
  2. Substantive validation: the produced markdown is checked for
        - mandatory "Was diese Investigation NICHT beweist" section at the top,
        - all five Limitations categories (Plan §2.4),
        - minimum 2 sentences per category,
        - no black-list phrases like "nicht relevant", "vernachlässigbar".
     A validation failure raises ``SynthesisValidationError`` — the caller
     can retry with a sharper prompt or abort the run.
  3. ``compute_status_tuple`` runs the Conjunction-Logik from Plan §2.5:
     ``methodological_rigor=MEDIUM`` iff every K5 sub-condition is met,
     otherwise ``LOW``. HIGH never appears in the schema (Plan §0.2 K15).

The output draft lives at ``{run_dir}/draft/proof.md`` until Phase 8 (user
approval) moves it to ``{run_dir}/proof.md``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from config import (
    TOOL_SI_LIMITATIONS_BLACKLIST,
    TOOL_SI_LIMITATIONS_MIN_SENTENCES_PER_CATEGORY,
    TOOL_SI_LIMITATIONS_REQUIRED_CATEGORIES,
    TOOL_SI_PHASE4_TIMEOUT_SEC,
)
from providers.base import BaseProvider
from tools.personas import AUTHOR
from tools.scientific_investigation_phase2 import (
    InvestigationPlan,
    Phase2Result,
)
from tools.scientific_investigation_phase3 import Phase3Result
from tools.scientific_investigation_phases import (
    FramingResult,
    PreRegResult,
)

logger = logging.getLogger(__name__)

# Plan §0.2 K15 — HIGH is structurally not achievable on same-stack setups.
Rigor = Literal["MEDIUM", "LOW"]
EvidenceBasis = Literal[
    "llm_generated_user_reviewed", "mixed", "llm_generated_only"
]
ResidualRisk = Literal["LOW", "MEDIUM", "HIGH"]
CriteriaStatus = Literal[
    "defined_criteria_all_within_tolerance",
    "defined_criteria_some_outside_tolerance",
    "defined_criteria_all_outside_tolerance",
    "criteria_not_testable",
]


class SynthesisValidationError(ValueError):
    """Raised when the produced proof.md fails the mandatory checks."""


# ── Prompt ─────────────────────────────────────────────────────────────────


_SYNTHESIS_PROMPT = """\
{persona_system_prompt}

## Aufgabe

Drafte das Investigation-Proof-Dokument für die Forschungsfrage. Verwende \
EXAKT die unten vorgegebene Struktur — Reviewer und mechanische Validatoren \
prüfen jede Sektion.

## Framing

- **Frage:** {question}
- **Hypothese:** {hypothesis}
- **Bias-Statement:** {bias_statement}
- **Disziplin:** {discipline}

## Pre-Registration-Schwellen

{thresholds_block}

## Phase-3-Sub-Task-Ergebnisse

{phase3_block}

## Output-Format (Pflicht — exakt diese Struktur, sonst Validierung schlägt fehl)

```markdown
# Investigation Proof — {question_short}

## Was diese Investigation NICHT beweist

Diese Investigation hat folgende strukturelle Limitations:

### 1. Multi-LLM-Korpus-Überlappungs-Restrisiko
<MINDESTENS 2 Sätze. Beschreibe konkret, wie der Same-Stack-Bias in DIESER \
Investigation wirkt. Keine Boilerplate-Phrasen wie "nicht relevant".>

### 2. Self-Reporting-Bias bei Persona-Klassifikation
<MINDESTENS 2 Sätze. Bezug auf die genutzten Personas und ihren Same-Stack-Bias.>

### 3. Disziplin-Restriktion / externe Schwellen-Verfügbarkeit
<MINDESTENS 2 Sätze. Welche Pre-Reg-Schwellen haben externe Quelle, welche nicht.>

### 4. Cross-Investigation-Cherry-Picking-Möglichkeit
<MINDESTENS 2 Sätze. Wurden verwandte Pre-Investigations gefunden? Wie wird damit umgegangen?>

### 5. LLM-Drift-Reproduzierbarkeits-Restrisiko
<MINDESTENS 2 Sätze. Welches Modell? Was passiert wenn Provider die Version ändert?>

## Falsifikations-Test-Katalog

| Pre-Reg | Schwellwert | Gemessen | Status |
| --- | --- | --- | --- |
<eine Zeile pro Pre-Reg-Schwelle, Status ∈ {{passed, failed, not_checkable}}>

## Ergebnis-Synthese

<Eigentliche Auswertung der Sub-Task-Outputs. Sachlich, ergebnisoffen, \
INCONCLUSIVE ist ein gültiges Ergebnis. Keine Marketing-Sprache.>

## Quellen-Liste

<Liste aller im Run benutzten externen Quellen — Normen, Paper-DOIs, \
Telegram-approvals — in der Reihenfolge des Pre-Reg.>
```

WICHTIG:
- Jede Limitations-Subsection MUSS mindestens 2 Sätze haben.
- Boilerplate-Phrasen ("nicht relevant", "vernachlässigbar", "minimal", \
"trivial", "ignorierbar") sind verboten und führen zu Rejection.
- Falsifikations-Test-Katalog enthält EINE Zeile pro Pre-Reg-Schwelle — \
keine zusätzlichen, keine ausgelassenen.
"""


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass
class StatusTuple:
    methodological_rigor: Rigor
    residual_risk: ResidualRisk
    evidence_basis: EvidenceBasis
    criteria_test_status: CriteriaStatus

    def as_dict(self) -> dict:
        return {
            "methodological_rigor": self.methodological_rigor,
            "residual_risk": self.residual_risk,
            "evidence_basis": self.evidence_basis,
            "criteria_test_status": self.criteria_test_status,
        }


@dataclass
class SynthesisResult:
    proof_md_text: str
    draft_path: Path
    validation_errors: list[str] = field(default_factory=list)

    def is_valid(self) -> bool:
        return not self.validation_errors


# ── Substantive validator ─────────────────────────────────────────────────


_HEADER_RE = re.compile(r"^## Was diese Investigation NICHT beweist", re.MULTILINE)
_SUBSECTION_RE = re.compile(r"^###\s+(\d+)\.\s+(.+?)$", re.MULTILINE)
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _count_sentences(body: str) -> int:
    """Count sentence-ending punctuation. Crude but matches the prompt's
    "mind. 2 Sätze" instruction faithfully enough for substantive validation.
    """
    return len(_SENTENCE_END_RE.findall(body))


def _parse_limitation_subsections(content: str) -> list[dict]:
    """Return a list of ``{title, body}`` for ### sections inside the
    mandatory limitations block. Only sections numbered 1..5 are considered.
    """
    # Find the mandatory section start.
    m = _HEADER_RE.search(content)
    if not m:
        return []
    section_start = m.start()
    # Truncate at the next H2 (## ...) AFTER our header.
    next_h2 = re.search(r"^## ", content[m.end():], re.MULTILINE)
    if next_h2:
        section_end = m.end() + next_h2.start()
    else:
        section_end = len(content)
    block = content[section_start:section_end]
    out: list[dict] = []
    matches = list(_SUBSECTION_RE.finditer(block))
    for i, sub in enumerate(matches):
        body_start = sub.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(block)
        body = block[body_start:body_end].strip()
        out.append({"title": sub.group(2).strip(), "body": body})
    return out


def validate_synthesis_output(proof_md: str) -> list[str]:
    """Return a list of substantive validation errors. Empty list = pass.

    Checks (Plan §2.4):
      * Mandatory section header present near the top.
      * All five required categories present.
      * Each category has at least ``TOOL_SI_LIMITATIONS_MIN_SENTENCES_PER_CATEGORY`` sentences.
      * No blacklisted phrase appears within any category body.
    """
    errors: list[str] = []
    head = proof_md[:3000]
    if not _HEADER_RE.search(head):
        errors.append("FEHLT: Pflicht-Sektion '## Was diese Investigation NICHT beweist' am Anfang")

    sections = _parse_limitation_subsections(proof_md)
    if len(sections) < 5:
        errors.append(
            f"NUR {len(sections)} Limitations-Subsektionen, alle 5 sind Pflicht"
        )

    section_titles_lower = [_normalize(s["title"]).lower() for s in sections]
    for category in TOOL_SI_LIMITATIONS_REQUIRED_CATEGORIES:
        cat_lower = category.lower()
        if not any(cat_lower in title for title in section_titles_lower):
            errors.append(f"FEHLT: Kategorie '{category}'")
            continue
        idx = next(
            i for i, t in enumerate(section_titles_lower) if cat_lower in t
        )
        body = sections[idx]["body"]
        sentence_count = _count_sentences(body)
        if sentence_count < TOOL_SI_LIMITATIONS_MIN_SENTENCES_PER_CATEGORY:
            errors.append(
                f"Kategorie '{category}' nur {sentence_count} Satz/Sätze — "
                f"mind. {TOOL_SI_LIMITATIONS_MIN_SENTENCES_PER_CATEGORY} verlangt"
            )
        body_lower = body.lower()
        for blacklisted in TOOL_SI_LIMITATIONS_BLACKLIST:
            if blacklisted.lower() in body_lower:
                errors.append(
                    f"Kategorie '{category}' enthält Black-List-Phrase "
                    f"'{blacklisted}' — substantielle Behandlung verlangt"
                )

    return errors


# ── Status-tuple computation (Conjunction-Logik per Plan §2.5) ───────────


def compute_status_tuple(
    *,
    preregistration_thresholds_sources: list[dict],
    crosscheck_tiers_per_subtask: dict[str, list[str]],
    adversarial_search_audit: dict,
    cross_provider_da_active: bool,
    engineering_reviewer_status: str,
    investigation_user_approval: str,
    criteria_test_status: CriteriaStatus,
) -> StatusTuple:
    """Compute the deterministic Status-Tuple.

    ``methodological_rigor=MEDIUM`` IFF (Plan §2.5):
      * all pre-reg thresholds have external sources (norm/paper/telegram_approval),
      * each sub-task has at least one T2 crosscheck,
      * adversarial search audit passes (diversity AND tool_call),
      * cross-provider DA was active for this run,
      * engineering-reviewer status == "passed" (Phase 7),
      * investigation_user_approval == "approved" (Phase 8).

    Otherwise: ``LOW``. HIGH never appears in the schema.

    ``residual_risk`` is a side dimension: counts T3-only sub-tasks +
    telegram-only pre-reg sources, then bins into LOW/MEDIUM/HIGH.

    ``evidence_basis`` summarizes the tier mix.
    """
    all_prereg_external = all(
        (s.get("source") or "") in (
            "norm_reference", "paper_reference", "telegram_approval"
        )
        for s in preregistration_thresholds_sources
    )
    at_least_one_T2_per_subtask = bool(crosscheck_tiers_per_subtask) and all(
        "T2" in tiers for tiers in crosscheck_tiers_per_subtask.values()
    )
    adversarial_pass = bool(
        adversarial_search_audit.get("diversity_pass")
        and adversarial_search_audit.get("tool_call_pass")
    )
    medium_conditions = [
        all_prereg_external,
        at_least_one_T2_per_subtask,
        adversarial_pass,
        cross_provider_da_active,
        engineering_reviewer_status == "passed",
        investigation_user_approval == "approved",
    ]
    rigor: Rigor = "MEDIUM" if all(medium_conditions) else "LOW"

    all_tiers = [t for tiers in crosscheck_tiers_per_subtask.values() for t in tiers]
    if all_tiers and all(t == "T2" for t in all_tiers):
        evidence: EvidenceBasis = "llm_generated_user_reviewed"
    elif "T2" in all_tiers:
        evidence = "mixed"
    else:
        evidence = "llm_generated_only"

    risk_factors = sum(
        1 for tiers in crosscheck_tiers_per_subtask.values()
        if not any(t == "T2" for t in tiers)
    ) + sum(
        1 for s in preregistration_thresholds_sources
        if (s.get("source") or "") == "telegram_approval"
    )
    if risk_factors >= 3:
        residual_risk: ResidualRisk = "HIGH"
    elif risk_factors >= 1:
        residual_risk = "MEDIUM"
    else:
        residual_risk = "LOW"

    return StatusTuple(
        methodological_rigor=rigor,
        residual_risk=residual_risk,
        evidence_basis=evidence,
        criteria_test_status=criteria_test_status,
    )


# ── Phase 4 runner ────────────────────────────────────────────────────────


def _phase3_block(phase3: Phase3Result) -> str:
    lines: list[str] = []
    for r in phase3.sub_task_results:
        mark = "OK" if r.success else "FAIL"
        lines.append(
            f"- **{r.sub_task.sub_id}** ({mark}, type={r.sub_task.type}): "
            f"{r.sub_task.title}. Output-Auszug: "
            f"{(r.output or '').strip().splitlines()[0][:200] if r.output else '(leer)'}"
        )
    return "\n".join(lines) if lines else "*(keine Sub-Task-Ergebnisse)*"


def _thresholds_block(prereg: PreRegResult) -> str:
    return "\n".join(
        f"- {t.criterion_id}: Schwellwert {t.threshold_value} "
        f"(Quelle: `{t.source}`)"
        for t in prereg.thresholds
    )


def phase_synthesis(
    framing: FramingResult,
    prereg: PreRegResult,
    phase2: Phase2Result,
    phase3: Phase3Result,
    provider: BaseProvider,
    *,
    run_dir: Path,
    run_id: str,
    timeout_sec: int | None = None,
) -> SynthesisResult:
    """Run Phase 4 synthesis. Produces draft/proof.md.

    Phase 8 (final approval) is responsible for moving the file from
    ``draft/`` to the run-dir root. Phase 4 NEVER touches the production
    location.

    The validator's errors are stored in ``SynthesisResult.validation_errors``;
    the caller (tool.run) decides whether to abort or continue with a
    LOW-cap status. The draft is always written so the user can inspect
    what the LLM produced even when validation failed.
    """
    if timeout_sec is None:
        timeout_sec = TOOL_SI_PHASE4_TIMEOUT_SEC

    prompt = _SYNTHESIS_PROMPT.format(
        persona_system_prompt=AUTHOR.system_prompt,
        question=framing.question,
        question_short=framing.question[:60],
        hypothesis=framing.hypothesis,
        bias_statement=framing.bias_statement,
        discipline=framing.discipline,
        thresholds_block=_thresholds_block(prereg),
        phase3_block=_phase3_block(phase3),
    )
    result = provider.run(prompt, timeout=timeout_sec, read_only=True)
    if not getattr(result, "success", False):
        raise RuntimeError(
            f"Phase 4 synthesis LLM call failed: {getattr(result, 'error', 'unknown')}"
        )
    proof_md = (result.output or "").strip()
    if not proof_md:
        raise RuntimeError("Phase 4 synthesis returned empty output")

    # Strip a leading ```markdown ... ``` fence if present (LLMs sometimes
    # wrap the entire document in a code block despite the prompt).
    if proof_md.startswith("```"):
        first_nl = proof_md.find("\n")
        if first_nl != -1:
            proof_md = proof_md[first_nl + 1:]
        if proof_md.endswith("```"):
            proof_md = proof_md[: -3]
        proof_md = proof_md.strip()

    draft_dir = run_dir / "draft"
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_path = draft_dir / "proof.md"
    draft_path.write_text(proof_md, encoding="utf-8")

    errors = validate_synthesis_output(proof_md)
    if errors:
        logger.warning(
            "Phase 4 synthesis validation failures (%d): %s",
            len(errors), "; ".join(errors[:3]),
        )
    return SynthesisResult(
        proof_md_text=proof_md,
        draft_path=draft_path,
        validation_errors=errors,
    )
