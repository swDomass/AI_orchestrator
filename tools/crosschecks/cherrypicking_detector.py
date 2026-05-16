"""Cherry-Picking detector + decision-log writer (Plan §2.6, K11).

Two responsibilities — kept in one module because they share the same
input (the run's framing + similarity index) and write to adjacent files:

  * ``build_cherrypicking_block(...)`` — returns a Markdown section that
    documents prior investigations whose framing is suspiciously similar
    to the current run. Threshold defaults to
    ``TOOL_SI_CHERRYPICKING_SIMILARITY_THRESHOLD`` (0.7). The block is
    informational only — visibility, not enforcement (Plan §0.2 K11).

  * ``write_decision_log(run_dir, sections)`` — writes
    ``{run_dir}/decision_log.md`` from a list of named sections. Persona
    allocations, cherry-picking findings, status-tuple, and Phase-7
    engineering-reviewer findings all flow into this file in later
    increments. Today (I2) we only have personas + cherry-picking
    available, so the writer is generic.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from config import (
    TOOL_SI_CHERRYPICKING_SIMILARITY_THRESHOLD,
    TOOL_SI_EMBEDDING_MODEL,
)
from tools.crosschecks import similarity_index
from tools.personas.base import PersonaAllocation

logger = logging.getLogger(__name__)


def build_cherrypicking_block(
    root_cwd: Path,
    *,
    framing_text: str,
    run_id: str,
    threshold: float | None = None,
) -> str:
    """Return a Markdown block documenting similar prior investigations.

    Empty string when there are no hits — caller should still include the
    Cherry-Picking section header so the decision-log structure is stable.
    """
    if threshold is None:
        threshold = TOOL_SI_CHERRYPICKING_SIMILARITY_THRESHOLD
    hits = similarity_index.find_similar_investigations(
        root_cwd,
        framing_text=framing_text,
        threshold=threshold,
        exclude_run_id=run_id,
    )
    if not hits:
        return (
            "Keine vorherigen Investigations mit Cosine-Similarity ≥ "
            f"{threshold:.2f} gefunden.\n"
        )
    lines: list[str] = [
        f"Cosine-Similarity-Schwellenwert: **{threshold:.2f}**.",
        f"Embedding-Modell: `{TOOL_SI_EMBEDDING_MODEL}` (siehe manifest.json).",
        "",
        "| run_id | score | indexiert |",
        "| --- | --- | --- |",
    ]
    for h in hits:
        lines.append(
            f"| `{h['run_id']}` | {h['score']:.3f} | "
            f"{h.get('ts_utc', '?')} |"
        )
    lines.append("")
    lines.append(
        "**Hinweis (Sichtbarkeit, kein Hard-Block):** Wenn eine prior-"
        "Investigation hier auftaucht und das Ergebnis dieses Runs darauf "
        "aufbaut, dann begründen *warum* der neue Run nicht einfach den prior "
        "übernimmt — andernfalls Risiko von Cross-Investigation-Cherry-"
        "Picking."
    )
    return "\n".join(lines) + "\n"


def build_persona_allocation_block(
    allocations: list[PersonaAllocation],
) -> str:
    """Return a Markdown block summarizing the Phase-1 persona allocation."""
    if not allocations:
        return "Keine Personas alloziert.\n"
    lines = [
        "| Persona | Rolle | Provider | Cross-Provider |",
        "| --- | --- | --- | --- |",
    ]
    for a in allocations:
        cross = "✓" if a.cross_provider_satisfied else "—"
        lines.append(
            f"| {a.persona.name} | `{a.persona.role}` | "
            f"`{a.provider_name}` | {cross} |"
        )
    return "\n".join(lines) + "\n"


def write_decision_log(
    run_dir: Path,
    *,
    run_id: str,
    sections: Iterable[tuple[str, str]],
) -> Path:
    """Write ``decision_log.md`` from an ordered list of ``(heading, body)`` tuples.

    Heading levels are H2 ('## ' prefix). Empty bodies are kept (rendered as
    "*(noch nichts dokumentiert)*") so the structure is visible even when a
    later phase hasn't been run yet.
    """
    out = run_dir / "decision_log.md"
    parts: list[str] = [f"# Decision Log — {run_id}\n"]
    for heading, body in sections:
        body_text = body.strip() or "*(noch nichts dokumentiert)*"
        parts.append(f"## {heading}\n\n{body_text}\n")
    out.write_text("\n".join(parts), encoding="utf-8")
    return out
