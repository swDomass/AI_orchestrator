"""Phase 2 — Investigation-Plan + Multi-Persona-Review (Plan §1.1, I3).

Workflow (sequential, Plan §9 I3):

    1. Author drafts the investigation plan (sub-task list).
    2. DA reviews adversarially → P1/P2/P3 findings.
    3. Methodiker reviews methodology → P1/P2/P3 findings.
    4. If any P1 findings exist: Author reworks; loop step 1.
    5. Else: convergence — write investigation_plan.md + review_findings.md.

Termination: convergence (no P1 findings) OR
``TOOL_SI_PHASE2_MAX_ITERATIONS`` reached (then result.converged = False,
findings carry forward into the decision-log so the user sees the open
gap).

All persona calls flow through the providers selected by
``phase_persona_allocation`` — the lookup is injected so tests can swap
the real dispatcher.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from config import (
    TOOL_SI_PHASE2_AUTHOR_TIMEOUT_SEC,
    TOOL_SI_PHASE2_MAX_ITERATIONS,
    TOOL_SI_PHASE2_REVIEW_TIMEOUT_SEC,
)
from providers.base import BaseProvider
from tools.personas import AUTHOR, DEVILS_ADVOCATE, METHODIKER
from tools.personas.base import PersonaAllocation
from tools.scientific_investigation_phases import (
    FramingResult,
    PreRegResult,
    _extract_yaml_block,
    _parse_yaml_minimal,
)

logger = logging.getLogger(__name__)


Severity = Literal["P1", "P2", "P3"]


# ── Prompts ────────────────────────────────────────────────────────────────


_AUTHOR_PLAN_PROMPT = """\
{persona_system_prompt}

## Aufgabe

Erstelle einen Investigation-Plan für die folgende Forschungsfrage. Der Plan \
listet die Sub-Tasks auf, die in Phase 3 ausgeführt werden — jeder Sub-Task \
muss eine konkrete, ausführbare Aktion beschreiben (Datenanalyse, Crosscheck-\
Code, Literatur-Recherche, etc.).

## Framing

- **Frage:** {question}
- **Hypothese:** {hypothesis}
- **Bias-Statement:** {bias_statement}
- **Disziplin:** {discipline}

## Pre-Registration-Schwellen

{thresholds_block}

Antworte NUR mit einem YAML-Block (zwischen ```yaml und ```), ohne erklärenden \
Text. Schema:

```yaml
sub_tasks:
  - sub_id: S1
    title: <Kurztitel max. 80 Zeichen>
    description: <Was wird konkret gemacht, max. 4 Sätze>
    addresses_criteria: [F1, F2]   # welche Pre-Reg-Schwellen werden adressiert
    type: <eines von: data_analysis | crosscheck_code | literature_search | computation>
    expected_output: <Was wird produziert, 1-2 Sätze>
  - sub_id: S2
    ...
```

WICHTIG:
- Mindestens 1, höchstens 8 Sub-Tasks.
- `sub_id` sequenziell S1, S2, ... — keine Lücken.
- Jeder Sub-Task muss mindestens ein `addresses_criteria` haben (das Mapping \
  zu Pre-Reg-Schwellen ist Pflicht — sonst keine Falsifikations-Verbindung).
- Keine Verschiebung der Pre-Reg-Schwellen. Wenn eine Schwelle nicht \
  adressierbar ist, mache das in einem eigenen Sub-Task explizit.
"""


_AUTHOR_REWORK_PROMPT = """\
{persona_system_prompt}

## Reviewer-Findings zum vorherigen Plan

{findings_block}

## Vorheriger Plan

```yaml
{previous_plan_yaml}
```

## Aufgabe

Überarbeite den Plan so, dass ALLE P1-Findings adressiert sind. P2-Findings \
sollten möglichst adressiert sein. P3-Findings sind optional.

Antworte NUR mit einem YAML-Block (zwischen ```yaml und ```) im gleichen \
Schema wie vorher (`sub_tasks` mit S1, S2, ...). Wenn du Sub-Tasks streichst \
oder hinzufügst, dokumentiere das NICHT im YAML-Block selbst — der Reviewer \
sieht den Diff.
"""


_DA_REVIEW_PROMPT = """\
{persona_system_prompt}

## Investigation-Framing

- **Frage:** {question}
- **Hypothese:** {hypothesis}
- **Bias-Statement:** {bias_statement}

## Zu reviewender Plan (vom Author)

```yaml
{plan_yaml}
```

## Aufgabe

Attackiere den Plan adversarial. Finde Schwachstellen, die später zu falschen \
positiven oder negativen Ergebnissen führen würden:

- Welche Bias-Quellen sind nicht adressiert?
- Welche alternative Erklärung könnte das Ergebnis liefern, ohne dass die \
  Hypothese stimmt?
- Sind die Sub-Tasks unabhängig genug, oder zirkulär?
- Welche kritischen Quellen/Normen werden ignoriert?

Antworte NUR mit einem YAML-Block (zwischen ```yaml und ```). Schema:

```yaml
findings:
  - severity: P1   # P1 = Blocker, P2 = Substantielle Lücke, P3 = Hinweis
    sub_id: S2     # bezogen auf welchen Sub-Task (oder "ALL" für plan-übergreifend)
    issue: <Konkretes Problem, max. 3 Sätze>
    suggestion: <Konkreter Verbesserungsvorschlag, max. 3 Sätze>
  - ...
```

WICHTIG:
- Keine Boilerplate-Findings ("könnte ausführlicher sein"). Jedes Finding muss \
  ein konkretes Problem mit konkretem Vorschlag sein.
- Wenn du keine P1/P2-Findings hast und nur P3 oder gar keine, schreibe einen \
  leeren `findings: []`. Das ist ein gültiges Ergebnis.
"""


_METHODIKER_REVIEW_PROMPT = """\
{persona_system_prompt}

## Investigation-Framing

- **Frage:** {question}
- **Pre-Reg-Schwellen:** {thresholds_summary}

## Zu reviewender Plan (vom Author)

```yaml
{plan_yaml}
```

## Aufgabe

Prüfe die Methodik des Plans — NICHT die Schlussfolgerung. Konkret:

- Adressiert jeder Sub-Task echt eine Pre-Reg-Schwelle (`addresses_criteria` \
  ist sauber)? Oder gibt es Sub-Tasks, die "nice to have" sind aber keine \
  Falsifikations-Verbindung haben?
- Sind die Pre-Reg-Schwellen alle abgedeckt? Welche Schwelle hat KEINEN \
  zugewiesenen Sub-Task?
- Sind die Crosschecks unabhängig (T2-Pfad realistisch erreichbar)?
- Ist die Adversarial-Citation-Search ausreichend geplant?

Antworte NUR mit einem YAML-Block (zwischen ```yaml und ```). Gleiches \
Schema wie DA:

```yaml
findings:
  - severity: P1
    sub_id: ALL
    issue: <...>
    suggestion: <...>
```

WICHTIG: Eine ungedeckte Pre-Reg-Schwelle ist immer mindestens P2 (besser P1).
"""


# ── Dataclasses ────────────────────────────────────────────────────────────


@dataclass
class SubTask:
    sub_id: str
    title: str
    description: str
    addresses_criteria: list[str]
    type: str
    expected_output: str


@dataclass
class InvestigationPlan:
    sub_tasks: list[SubTask]
    raw_yaml: str

    def as_yaml(self) -> str:
        """Re-serialize for round-trip into the next iteration's prompt."""
        return self.raw_yaml


@dataclass
class ReviewFinding:
    severity: Severity
    sub_id: str
    issue: str
    suggestion: str
    reviewer: str  # persona role: "devils_advocate" | "methodiker"


@dataclass
class Phase2Result:
    plan: InvestigationPlan
    findings_by_iteration: list[list[ReviewFinding]]
    iterations_used: int
    converged: bool

    def latest_findings(self) -> list[ReviewFinding]:
        return self.findings_by_iteration[-1] if self.findings_by_iteration else []

    def has_open_p1(self) -> bool:
        return any(f.severity == "P1" for f in self.latest_findings())


# ── Parsing helpers ────────────────────────────────────────────────────────


_VALID_SEVERITIES = frozenset({"P1", "P2", "P3"})


def _parse_investigation_plan(raw_output: str) -> InvestigationPlan:
    yaml_text = _extract_yaml_block(raw_output)
    parsed = _parse_yaml_minimal(yaml_text)
    if not isinstance(parsed, dict) or "sub_tasks" not in parsed:
        raise ValueError(f"investigation plan missing 'sub_tasks': {yaml_text[:200]}")
    raw_subs = parsed.get("sub_tasks") or []
    if not isinstance(raw_subs, list) or not raw_subs:
        raise ValueError("investigation plan must list at least one sub_task")
    if len(raw_subs) > 8:
        raise ValueError(f"investigation plan has {len(raw_subs)} sub-tasks — max 8 allowed")
    sub_tasks: list[SubTask] = []
    for idx, raw in enumerate(raw_subs, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"sub_task #{idx} not a mapping: {raw!r}")
        sub_id = str(raw.get("sub_id") or "").strip()
        if sub_id != f"S{idx}":
            raise ValueError(
                f"sub_task #{idx} sub_id {sub_id!r} — must be S{idx}"
            )
        criteria_raw = raw.get("addresses_criteria") or []
        if isinstance(criteria_raw, str):
            # Mini-YAML may produce "[F1, F2]" as a string when the value is
            # written inline. Be lenient.
            criteria_raw = [
                c.strip().strip("[]").strip()
                for c in criteria_raw.split(",")
                if c.strip().strip("[]").strip()
            ]
        if not isinstance(criteria_raw, list) or not criteria_raw:
            raise ValueError(
                f"sub_task {sub_id} missing addresses_criteria — Pre-Reg-Mapping ist Pflicht"
            )
        criteria = [str(c).strip() for c in criteria_raw if str(c).strip()]
        sub_tasks.append(SubTask(
            sub_id=sub_id,
            title=str(raw.get("title", "")).strip(),
            description=str(raw.get("description", "")).strip(),
            addresses_criteria=criteria,
            type=str(raw.get("type", "")).strip(),
            expected_output=str(raw.get("expected_output", "")).strip(),
        ))
    return InvestigationPlan(sub_tasks=sub_tasks, raw_yaml=yaml_text)


def _parse_review_findings(raw_output: str, reviewer: str) -> list[ReviewFinding]:
    """Parse a reviewer's findings YAML. Tolerates an empty list."""
    yaml_text = _extract_yaml_block(raw_output)
    parsed = _parse_yaml_minimal(yaml_text)
    if not isinstance(parsed, dict):
        raise ValueError(f"review output not a mapping: {yaml_text[:200]}")
    raw_findings = parsed.get("findings", []) or []
    # Mini-YAML returns top-level scalar list-keys as [] only when the key
    # itself was followed by ":" and nothing — that's fine, we treat as empty.
    if isinstance(raw_findings, list) and not raw_findings:
        return []
    if not isinstance(raw_findings, list):
        raise ValueError("review findings must be a list")
    out: list[ReviewFinding] = []
    for idx, raw in enumerate(raw_findings, start=1):
        if not isinstance(raw, dict):
            logger.warning("review finding #%d not a mapping, skipping: %r", idx, raw)
            continue
        severity = str(raw.get("severity", "")).strip().upper()
        if severity not in _VALID_SEVERITIES:
            logger.warning(
                "review finding #%d invalid severity %r, defaulting to P3",
                idx, severity,
            )
            severity = "P3"
        out.append(ReviewFinding(
            severity=severity,  # type: ignore[arg-type]
            sub_id=str(raw.get("sub_id") or "ALL").strip(),
            issue=str(raw.get("issue", "")).strip(),
            suggestion=str(raw.get("suggestion", "")).strip(),
            reviewer=reviewer,
        ))
    return out


def _thresholds_block(prereg: PreRegResult) -> str:
    return "\n".join(
        f"- {t.criterion_id}: {t.description} (Schwelle: {t.threshold_value}, "
        f"Quelle: `{t.source}`)"
        for t in prereg.thresholds
    )


def _thresholds_summary(prereg: PreRegResult) -> str:
    return ", ".join(t.criterion_id for t in prereg.thresholds)


def _findings_block(findings: list[ReviewFinding]) -> str:
    if not findings:
        return "*(Keine Findings)*"
    lines: list[str] = []
    for f in findings:
        lines.append(
            f"- **[{f.severity}]** ({f.reviewer}, sub={f.sub_id}) {f.issue}\n"
            f"    *Vorschlag:* {f.suggestion}"
        )
    return "\n".join(lines)


# ── Persona-run helpers ────────────────────────────────────────────────────


ProviderLookup = Callable[[str], BaseProvider | None]


def _resolve_persona_provider(
    allocations: list[PersonaAllocation],
    role: str,
    primary_provider: BaseProvider,
    lookup: ProviderLookup,
) -> BaseProvider:
    """Return the concrete provider for a persona role.

    Falls back to ``primary_provider`` if the allocation's provider can no
    longer be resolved (rare: dispatcher reconfigured mid-run). The fallback
    case is logged.
    """
    alloc = next((a for a in allocations if a.persona.role == role), None)
    if alloc is None:
        return primary_provider
    if alloc.provider_name == primary_provider.name:
        return primary_provider
    provider = lookup(alloc.provider_name)
    if provider is None:
        logger.warning(
            "Phase 2: provider %r for persona %s not resolvable, "
            "falling back to primary %s",
            alloc.provider_name, role, primary_provider.name,
        )
        return primary_provider
    return provider


def _run_persona_call(
    provider: BaseProvider,
    prompt: str,
    timeout_sec: int,
    label: str,
) -> str:
    """Wraps provider.run() with logging + error normalisation."""
    result = provider.run(prompt, timeout=timeout_sec, read_only=True)
    if not getattr(result, "success", False):
        raise RuntimeError(
            f"Phase 2 {label} failed: {getattr(result, 'error', 'unknown')}"
        )
    return (result.output or "").strip()


# ── Main loop ──────────────────────────────────────────────────────────────


def phase_investigation_plan_review(
    framing: FramingResult,
    prereg: PreRegResult,
    allocations: list[PersonaAllocation],
    primary_provider: BaseProvider,
    *,
    run_dir: Path,
    run_id: str,
    provider_lookup: ProviderLookup | None = None,
    max_iterations: int | None = None,
) -> Phase2Result:
    """Run Phase 2: investigation plan + multi-persona review loop.

    Returns a ``Phase2Result``. The caller is responsible for writing the
    final ``investigation_plan.md`` and ``review_findings.md`` via the
    accompanying helpers (``write_investigation_plan_md``,
    ``write_review_findings_md``) — keeping the loop pure makes it easier
    to test.
    """
    if provider_lookup is None:
        from dispatcher import get_provider_by_name as _lookup
        provider_lookup = _lookup
    if max_iterations is None:
        max_iterations = TOOL_SI_PHASE2_MAX_ITERATIONS

    author_provider = _resolve_persona_provider(
        allocations, "author", primary_provider, provider_lookup,
    )
    da_provider = _resolve_persona_provider(
        allocations, "devils_advocate", primary_provider, provider_lookup,
    )
    methodiker_provider = _resolve_persona_provider(
        allocations, "methodiker", primary_provider, provider_lookup,
    )

    findings_by_iteration: list[list[ReviewFinding]] = []
    current_plan: InvestigationPlan | None = None

    for iteration in range(1, max_iterations + 1):
        # ── Author draft / rework ──────────────────────────────────────────
        if current_plan is None:
            prompt = _AUTHOR_PLAN_PROMPT.format(
                persona_system_prompt=AUTHOR.system_prompt,
                question=framing.question,
                hypothesis=framing.hypothesis,
                bias_statement=framing.bias_statement,
                discipline=framing.discipline,
                thresholds_block=_thresholds_block(prereg),
            )
            label = f"author plan (iter {iteration})"
        else:
            prompt = _AUTHOR_REWORK_PROMPT.format(
                persona_system_prompt=AUTHOR.system_prompt,
                findings_block=_findings_block(findings_by_iteration[-1]),
                previous_plan_yaml=current_plan.raw_yaml,
            )
            label = f"author rework (iter {iteration})"
        plan_output = _run_persona_call(
            author_provider, prompt,
            TOOL_SI_PHASE2_AUTHOR_TIMEOUT_SEC, label,
        )
        current_plan = _parse_investigation_plan(plan_output)

        # ── DA review ──────────────────────────────────────────────────────
        da_prompt = _DA_REVIEW_PROMPT.format(
            persona_system_prompt=DEVILS_ADVOCATE.system_prompt,
            question=framing.question,
            hypothesis=framing.hypothesis,
            bias_statement=framing.bias_statement,
            plan_yaml=current_plan.raw_yaml,
        )
        da_output = _run_persona_call(
            da_provider, da_prompt,
            TOOL_SI_PHASE2_REVIEW_TIMEOUT_SEC,
            f"DA review (iter {iteration})",
        )
        da_findings = _parse_review_findings(da_output, reviewer="devils_advocate")

        # ── Methodiker review ──────────────────────────────────────────────
        meth_prompt = _METHODIKER_REVIEW_PROMPT.format(
            persona_system_prompt=METHODIKER.system_prompt,
            question=framing.question,
            thresholds_summary=_thresholds_summary(prereg),
            plan_yaml=current_plan.raw_yaml,
        )
        meth_output = _run_persona_call(
            methodiker_provider, meth_prompt,
            TOOL_SI_PHASE2_REVIEW_TIMEOUT_SEC,
            f"Methodiker review (iter {iteration})",
        )
        meth_findings = _parse_review_findings(meth_output, reviewer="methodiker")

        iter_findings = [*da_findings, *meth_findings]
        findings_by_iteration.append(iter_findings)

        # Convergence: no P1 findings → done.
        if not any(f.severity == "P1" for f in iter_findings):
            return Phase2Result(
                plan=current_plan,
                findings_by_iteration=findings_by_iteration,
                iterations_used=iteration,
                converged=True,
            )

    # Cap hit. Return non-converged result; caller decides how to expose this.
    return Phase2Result(
        plan=current_plan,  # type: ignore[arg-type]  # non-None guaranteed by loop body
        findings_by_iteration=findings_by_iteration,
        iterations_used=max_iterations,
        converged=False,
    )


# ── Output writers ─────────────────────────────────────────────────────────


def write_investigation_plan_md(
    run_dir: Path,
    *,
    plan: InvestigationPlan,
    converged: bool,
    iterations: int,
) -> Path:
    """Render investigation_plan.md."""
    rows: list[str] = []
    for s in plan.sub_tasks:
        criteria = ", ".join(s.addresses_criteria)
        rows.append(f"### {s.sub_id}: {s.title}")
        rows.append(f"- **Beschreibung:** {s.description}")
        rows.append(f"- **Typ:** `{s.type}`")
        rows.append(f"- **Adressiert:** {criteria}")
        rows.append(f"- **Erwartetes Output:** {s.expected_output}")
        rows.append("")
    status = "✅ converged" if converged else "⚠️ cap reached — open P1 findings"
    body = (
        f"# Investigation Plan (Phase 2)\n\n"
        f"**Status:** {status}  \n"
        f"**Iterationen:** {iterations}\n\n"
        f"## Sub-Tasks\n\n"
        + "\n".join(rows)
        + "\n## Raw-YAML (für Phase 3 Execution)\n\n"
        f"```yaml\n{plan.raw_yaml}\n```\n"
    )
    out = run_dir / "investigation_plan.md"
    out.write_text(body, encoding="utf-8")
    return out


def write_review_findings_md(
    run_dir: Path,
    *,
    findings_by_iteration: list[list[ReviewFinding]],
) -> Path:
    """Render review_findings.md (audit-friendly log of every iteration)."""
    parts: list[str] = ["# Phase 2 Review Findings\n"]
    if not findings_by_iteration:
        parts.append("*(Keine Iterationen ausgeführt.)*\n")
    for i, findings in enumerate(findings_by_iteration, start=1):
        parts.append(f"## Iteration {i}\n")
        if not findings:
            parts.append("*(Keine Findings — konvergiert.)*\n")
        else:
            for f in findings:
                parts.append(
                    f"- **[{f.severity}]** *{f.reviewer}* (sub_id={f.sub_id}) "
                    f"{f.issue}\n"
                    f"    *Vorschlag:* {f.suggestion}\n"
                )
        parts.append("")
    out = run_dir / "review_findings.md"
    out.write_text("\n".join(parts), encoding="utf-8")
    return out
