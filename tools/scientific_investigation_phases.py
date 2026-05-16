"""Phase 0 (Framing + Bias-Statement) and Phase 0.5 (Pre-Registration) for
the scientific-investigation tool — Increment I1.

Phase 0
-------
LLM reformulates the user task into:
  * ``question``           — precise version of the task.
  * ``hypothesis``         — what the user expects to find.
  * ``bias_statement``     — what would make the user *want* to find that.
  * ``discipline``         — engineering / natural-science / other.
  * ``framing_text``       — free-text framing used for similarity matching.

The framing is then matched against the cross-investigation similarity index.
Hits above ``TOOL_SI_CHERRYPICKING_SIMILARITY_THRESHOLD`` (default 0.7) are
written into ``plan.md`` as a visibility note (NOT a hard block — the user
decides whether the prior run informs the new one).

Phase 0.5
---------
LLM generates the Pre-Registration thresholds from the framing. Each
threshold carries a source:

  * ``norm_reference``      — DIN/EN/ISO/IEEE-style citation with snippet.
  * ``paper_reference``     — DOI + page/snippet.
  * ``telegram_approval``   — user must approve via Telegram.

For ``telegram_approval`` thresholds, the tool sends an approval request
via the notifier and blocks on the ``PreRegApprovalManager`` event. The
response (and the Telegram message ID) gets recorded as one
``preregistration_threshold`` audit entry per approved threshold.

Disziplin-Warnung — when ALL thresholds resolve via ``telegram_approval``
(no external norm/paper anywhere), the tool emits a second Telegram
notification: "Investigation ohne externe Normen — methodological_rigor
wird LOW. Trotzdem fortfahren?" — and refuses to continue without an
explicit ``approved`` response. The disziplin-warning entry is recorded
in the audit trail.

Hash-Lock
---------
After the Pre-Reg section is finalized, the tool computes a SHA256 over a
canonical representation of the threshold list and stores it in the run's
``plan.md`` frontmatter as ``prereg_hash:``. Later phases can re-compute
and detect tampering — visibility, not enforcement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import (
    TOOL_SI_CHERRYPICKING_SIMILARITY_THRESHOLD,
    TOOL_SI_EMBEDDING_MODEL,
)
from providers.base import BaseProvider
from tools.crosschecks import audit_trail, similarity_index
from tools.personas import ALL_PHASE2_PERSONAS, Persona, PersonaAllocation
from tools.scientific_investigation_approvals import get_manager

logger = logging.getLogger(__name__)


# ── Prompts ────────────────────────────────────────────────────────────────


_FRAMING_PROMPT = """\
Du bist Methodiker. Reformuliere die folgende Investigation präzise und \
analysiere mögliche kognitive Verzerrungen.

INVESTIGATION-IDEE: {task}

Antworte NUR mit einem YAML-Block (zwischen ```yaml und ```), ohne erklärenden Text. \
Schema:

```yaml
question: <Eine präzise formulierte Forschungsfrage, max. 2 Sätze>
hypothesis: <Was vermutet der User aktuell, max. 2 Sätze>
bias_statement: <Welche Erwartung/Wunsch könnte das Ergebnis verzerren, mind. 1 konkreter Satz>
discipline: <eines von: engineering | natural_science | social_science | unspecified>
framing_text: <Freitext-Zusammenfassung 3-5 Sätze, wird für Similarity-Matching genutzt>
```

WICHTIG:
- Verwende keine Boilerplate ("kein Bias erkennbar" etc) — wenn kein Bias offensichtlich \
ist, beschreibe konkret welche Forschungs-Position der User vermutlich vertritt.
- Disziplin nur dann engineering/natural_science wenn formalisierte Schwellen (Normen, \
peer-reviewed Toleranzen) verfügbar sind.
"""


_PREREG_PROMPT = """\
Du bist Methodiker. Generiere die Pre-Registration-Schwellen für die folgende Investigation.

FRAMING:
{framing}

Antworte NUR mit einem YAML-Block (zwischen ```yaml und ```), ohne erklärenden Text. \
Schema:

```yaml
thresholds:
  - criterion_id: F1
    description: <Was wird gemessen, max. 2 Sätze>
    threshold_value: <Konkreter numerischer Wert oder Toleranzband>
    source: norm_reference  # eines von: norm_reference | paper_reference | telegram_approval
    reference: <DIN/EN/ISO/IEEE-Nummer + Paragraph + Snippet ODER DOI + Seite + Snippet>
  - criterion_id: F2
    description: ...
    threshold_value: ...
    source: telegram_approval  # KEINE Norm/Paper verfügbar — User muss approven
    reference: ""
  ...
```

WICHTIG:
- Pro Schwelle EINE Quelle. Wenn KEINE belastbare Norm/Paper-Referenz existiert, MUSS \
``source`` auf ``telegram_approval`` gesetzt werden — nicht raten oder erfinden.
- ``reference`` muss bei norm/paper mindestens 20 Zeichen mit einer Zahl oder Toleranz \
enthalten. Bei ``telegram_approval`` leer.
- Mindestens 1, höchstens 5 Schwellen.
- ``criterion_id`` ist sequenziell F1, F2, ... — keine Lücken, keine Buchstaben.
"""


# ── Data classes ───────────────────────────────────────────────────────────


@dataclass
class FramingResult:
    question: str
    hypothesis: str
    bias_statement: str
    discipline: str
    framing_text: str
    similarity_hits: list[dict[str, Any]] = field(default_factory=list)

    def as_yaml_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "hypothesis": self.hypothesis,
            "bias_statement": self.bias_statement,
            "discipline": self.discipline,
            "framing_text": self.framing_text,
        }


@dataclass
class Threshold:
    criterion_id: str
    description: str
    threshold_value: str
    source: str  # norm_reference | paper_reference | telegram_approval
    reference: str = ""
    telegram_msg_id: str = ""
    approver: str = ""


@dataclass
class PreRegResult:
    thresholds: list[Threshold]
    discipline_warning: bool
    discipline_warning_approved: bool
    prereg_hash: str

    def all_external(self) -> bool:
        return all(
            t.source in ("norm_reference", "paper_reference") for t in self.thresholds
        )


# ── YAML parsing (no external dep — minimal subset) ────────────────────────


_YAML_BLOCK_RE = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)
_DOI_RE = re.compile(r"^10\.\d{4,9}/.+$")


def _extract_yaml_block(text: str) -> str:
    m = _YAML_BLOCK_RE.search(text)
    if m:
        return m.group(1).strip()
    # Fallback: assume the whole reply is the YAML body
    return text.strip()


def _parse_yaml_minimal(yaml_text: str) -> Any:
    """Tiny YAML subset parser sufficient for our two prompts.

    Supports:
      * top-level scalar key/value (string)
      * top-level list-of-mappings under a single key (``thresholds:``)
    Strings can be plain or quoted with single/double quotes.
    Indentation is two spaces. Comments after ``#`` are stripped if preceded
    by whitespace.

    Falls back to the real ``yaml`` module if available, but the project's
    constraint is stdlib + pyyaml — we use pyyaml when present.
    """
    try:
        import yaml  # type: ignore
        return yaml.safe_load(yaml_text)
    except ImportError:
        pass

    # Hand-written fallback. Only the shapes our prompts produce.
    out: dict[str, Any] = {}
    current_list: list[dict[str, Any]] | None = None
    current_item: dict[str, Any] | None = None
    for raw_line in yaml_text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # Strip trailing comments preceded by whitespace
        line = re.sub(r"\s+#.*$", "", line)
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0 and stripped.endswith(":"):
            key = stripped[:-1].strip()
            current_list = []
            out[key] = current_list
            current_item = None
            continue
        if indent == 0 and ":" in stripped:
            key, _, val = stripped.partition(":")
            out[key.strip()] = _strip_quotes(val.strip())
            current_list = None
            current_item = None
            continue
        if stripped.startswith("- ") and current_list is not None:
            current_item = {}
            current_list.append(current_item)
            stripped = stripped[2:]
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                current_item[key.strip()] = _strip_quotes(val.strip())
            continue
        if current_item is not None and ":" in stripped:
            key, _, val = stripped.partition(":")
            current_item[key.strip()] = _strip_quotes(val.strip())
    return out


def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


# ── Phase 0: Framing ────────────────────────────────────────────────────────


def phase_framing(
    task: str,
    provider: BaseProvider,
    *,
    run_dir: Path,
    root_cwd: Path,
    run_id: str,
    timeout_sec: int,
) -> FramingResult:
    """Run the framing LLM call, append to similarity index, write plan.md."""
    prompt = _FRAMING_PROMPT.format(task=task)
    result = provider.run(prompt, cwd=str(root_cwd), timeout=timeout_sec, read_only=True)
    if not getattr(result, "success", False):
        raise RuntimeError(
            f"framing LLM call failed: {getattr(result, 'error', 'unknown')}"
        )
    yaml_text = _extract_yaml_block(result.output or "")
    parsed = _parse_yaml_minimal(yaml_text)
    if not isinstance(parsed, dict):
        raise ValueError(f"framing output not a mapping: {yaml_text[:200]}")

    framing = FramingResult(
        question=str(parsed.get("question") or "").strip(),
        hypothesis=str(parsed.get("hypothesis") or "").strip(),
        bias_statement=str(parsed.get("bias_statement") or "").strip(),
        discipline=str(parsed.get("discipline") or "unspecified").strip(),
        framing_text=str(parsed.get("framing_text") or "").strip(),
    )
    if not framing.framing_text:
        raise ValueError("framing_text empty — LLM output incomplete")

    # Similarity check BEFORE appending the current framing so we don't match
    # ourselves.
    framing.similarity_hits = similarity_index.find_similar_investigations(
        root_cwd,
        framing_text=framing.framing_text,
        threshold=TOOL_SI_CHERRYPICKING_SIMILARITY_THRESHOLD,
        exclude_run_id=run_id,
    )
    similarity_index.append_investigation(
        root_cwd,
        run_id=run_id,
        framing_text=framing.framing_text,
        embedding_model=TOOL_SI_EMBEDDING_MODEL,
    )
    return framing


# ── Phase 0.5: Pre-Registration ─────────────────────────────────────────────


def phase_prereg(
    framing: FramingResult,
    provider: BaseProvider,
    *,
    run_dir: Path,
    run_id: str,
    timeout_sec: int,
    telegram_timeout_sec: int,
    notify_callable=None,
    discipline_warning_callable=None,
) -> PreRegResult:
    """Run the pre-registration LLM call, resolve sources, hash-lock the result.

    ``notify_callable(run_id, criterion_id, threshold)`` is invoked to send a
    Telegram approval request for each ``telegram_approval`` threshold. It
    must return the message-ID (string) so the audit can record it. When it
    is None (test mode), the manager's ``respond()`` is expected to be called
    out-of-band before ``timeout_sec`` elapses.

    ``discipline_warning_callable(run_id)`` plays the same role for the
    second Telegram round-trip when no external reference is available — the
    sentinel criterion_id is ``__discipline_warning__``.
    """
    prompt = _PREREG_PROMPT.format(framing=framing.framing_text)
    result = provider.run(
        prompt,
        cwd=str(run_dir.parent.parent if run_dir.parent.name == "scientific-investigation" else run_dir),
        timeout=timeout_sec,
        read_only=True,
    )
    if not getattr(result, "success", False):
        raise RuntimeError(
            f"prereg LLM call failed: {getattr(result, 'error', 'unknown')}"
        )
    yaml_text = _extract_yaml_block(result.output or "")
    parsed = _parse_yaml_minimal(yaml_text)
    if not isinstance(parsed, dict) or "thresholds" not in parsed:
        raise ValueError(f"prereg output missing 'thresholds': {yaml_text[:200]}")

    thresholds_raw = parsed.get("thresholds", []) or []
    if not isinstance(thresholds_raw, list) or not thresholds_raw:
        raise ValueError("prereg thresholds must be a non-empty list")
    if len(thresholds_raw) > 5:
        raise ValueError(f"prereg has {len(thresholds_raw)} thresholds — max 5 allowed")

    thresholds: list[Threshold] = []
    for idx, raw in enumerate(thresholds_raw, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"threshold #{idx} not a mapping: {raw!r}")
        criterion_id = str(raw.get("criterion_id") or f"F{idx}").strip()
        if criterion_id != f"F{idx}":
            raise ValueError(
                f"threshold #{idx} criterion_id {criterion_id!r} — must be F{idx}"
            )
        source = str(raw.get("source", "")).strip()
        if source not in ("norm_reference", "paper_reference", "telegram_approval"):
            raise ValueError(
                f"threshold {criterion_id} source {source!r} — must be one of "
                "norm_reference, paper_reference, telegram_approval"
            )
        reference = str(raw.get("reference", "")).strip()
        _validate_reference(criterion_id, source, reference)
        thresholds.append(Threshold(
            criterion_id=criterion_id,
            description=str(raw.get("description", "")).strip(),
            threshold_value=str(raw.get("threshold_value", "")).strip(),
            source=source,
            reference=reference,
        ))

    # Resolve telegram-approval thresholds and write audit entries.
    manager = get_manager()
    for t in thresholds:
        if t.source == "telegram_approval":
            msg_id = ""
            if notify_callable is not None:
                msg_id = str(notify_callable(run_id, t.criterion_id, t) or "")
            response, returned_msg_id, approver, _reason = manager.request_threshold_approval(
                run_id=run_id,
                criterion_id=t.criterion_id,
                timeout_sec=telegram_timeout_sec,
            )
            if response != "approved":
                raise RuntimeError(
                    f"threshold {t.criterion_id} telegram approval not granted: "
                    f"{response}"
                )
            t.telegram_msg_id = returned_msg_id or msg_id
            t.approver = approver

    # Disziplin-Warnung — when no threshold has an external (norm/paper) source.
    discipline_warning = not any(
        t.source in ("norm_reference", "paper_reference") for t in thresholds
    )
    discipline_warning_approved = False
    if discipline_warning:
        if discipline_warning_callable is not None:
            discipline_warning_callable(run_id)
        response, returned_msg_id, approver, _reason = manager.request_threshold_approval(
            run_id=run_id,
            criterion_id="__discipline_warning__",
            timeout_sec=telegram_timeout_sec,
        )
        discipline_warning_approved = response == "approved"
        try:
            audit_trail.append_audit_entry(run_dir, {
                "type": "preregistration_warning",
                "msg": (
                    "Disziplin-Warnung: Investigation ohne externe Normen — "
                    "methodological_rigor wird LOW."
                ),
                "user_response": response,
                "telegram_msg_id": returned_msg_id,
                "approver": approver,
            })
        except (OSError, ValueError) as exc:
            logger.warning("audit append failed for discipline-warning: %s", exc)
        if not discipline_warning_approved:
            raise RuntimeError(
                f"Disziplin-Warnung nicht approved (Response={response}). "
                "Investigation abgebrochen."
            )

    # Now write the threshold audit entries (only after disciplin-gate is cleared).
    for t in thresholds:
        entry: dict[str, Any] = {
            "type": "preregistration_threshold",
            "criterion_id": t.criterion_id,
            "source": t.source,
            "claim_id": run_id,
            "description": t.description,
            "threshold_value": t.threshold_value,
        }
        if t.source in ("norm_reference", "paper_reference"):
            entry["reference"] = t.reference
        else:
            entry["telegram_msg_id"] = t.telegram_msg_id
            entry["approver"] = t.approver
            entry["user_response"] = "approved"
        try:
            audit_trail.append_audit_entry(run_dir, entry)
        except (OSError, ValueError) as exc:
            logger.warning("audit append failed for threshold %s: %s", t.criterion_id, exc)

    prereg_hash = compute_prereg_hash(thresholds)
    return PreRegResult(
        thresholds=thresholds,
        discipline_warning=discipline_warning,
        discipline_warning_approved=discipline_warning_approved,
        prereg_hash=prereg_hash,
    )


def _validate_reference(criterion_id: str, source: str, reference: str) -> None:
    """Enforce the snippet-format requirements from the prompt."""
    if source == "telegram_approval":
        return
    if len(reference) < 20:
        raise ValueError(
            f"threshold {criterion_id} reference too short ({len(reference)} chars, min 20)"
        )
    if source == "paper_reference":
        # Look for a DOI substring; we don't require the whole reference to BE a DOI
        # (it usually has author/snippet text wrapped around it).
        if not re.search(r"10\.\d{4,9}/", reference):
            raise ValueError(
                f"threshold {criterion_id} paper_reference missing DOI (10.xxxx/...)"
            )
    if not re.search(r"\d", reference):
        raise ValueError(
            f"threshold {criterion_id} reference must contain at least one digit "
            "(threshold value or norm number)"
        )


def compute_prereg_hash(thresholds: list[Threshold]) -> str:
    """Canonical SHA256 over the threshold list. Same input → same hash.

    Used for the hash-lock written into plan.md frontmatter; later phases
    re-compute this and surface a warning if the lock and content drift.
    """
    canonical = json.dumps(
        [
            {
                "criterion_id": t.criterion_id,
                "description": t.description,
                "threshold_value": t.threshold_value,
                "source": t.source,
                "reference": t.reference,
            }
            for t in sorted(thresholds, key=lambda x: x.criterion_id)
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── plan.md writer ─────────────────────────────────────────────────────────


def write_plan_md(
    run_dir: Path,
    *,
    task: str,
    framing: FramingResult,
    prereg: PreRegResult,
) -> Path:
    """Render plan.md combining Phase 0 + Phase 0.5."""
    similarity_block = ""
    if framing.similarity_hits:
        lines = [
            "## Cross-Investigation-Similarity-Hinweise",
            "",
            "Die folgenden früheren Investigations haben ein ähnliches Framing "
            f"(Cosine ≥ {TOOL_SI_CHERRYPICKING_SIMILARITY_THRESHOLD}):",
            "",
        ]
        for hit in framing.similarity_hits:
            lines.append(
                f"- run_id `{hit['run_id']}` — score {hit['score']:.3f} "
                f"(indexiert {hit.get('ts_utc', '?')})"
            )
        lines.append("")
        lines.append(
            "**Hinweis (Sichtbarkeit, kein Hard-Block):** Wenn diese Investigation "
            "auf einem prior-Run aufbaut, entsprechend in der Decision-Log dokumentieren."
        )
        similarity_block = "\n".join(lines) + "\n\n"

    threshold_lines: list[str] = []
    for t in prereg.thresholds:
        threshold_lines.append(f"### {t.criterion_id}")
        threshold_lines.append(f"- **Beschreibung:** {t.description}")
        threshold_lines.append(f"- **Schwellwert:** {t.threshold_value}")
        threshold_lines.append(f"- **Quelle:** `{t.source}`")
        if t.source == "telegram_approval":
            threshold_lines.append(
                f"- **Telegram-Message-ID:** {t.telegram_msg_id or '(pending)'}"
            )
            threshold_lines.append(f"- **Approver:** {t.approver or '(pending)'}")
        else:
            threshold_lines.append(f"- **Referenz:** {t.reference}")
        threshold_lines.append("")

    discipline_block = ""
    if prereg.discipline_warning:
        discipline_block = (
            "> **Disziplin-Warnung:** Keine der Pre-Reg-Schwellen verweist auf "
            "eine externe Norm/Paper-Quelle. `methodological_rigor` ist auf "
            "**LOW** capped (User hat per Telegram fortgesetzt).\n\n"
        )

    plan_md = f"""---
run_id: {run_dir.name}
prereg_hash: {prereg.prereg_hash}
discipline: {framing.discipline}
discipline_warning: {str(prereg.discipline_warning).lower()}
---

# Investigation Plan

## Aufgabe (Originaltext)

{task}

## Phase 0: Framing

- **Frage:** {framing.question}
- **Hypothese:** {framing.hypothesis}
- **Bias-Statement:** {framing.bias_statement}
- **Disziplin:** `{framing.discipline}`

### Framing-Text (für Similarity-Index)

{framing.framing_text}

{similarity_block}## Phase 0.5: Pre-Registration

{discipline_block}{chr(10).join(threshold_lines)}
"""
    out = run_dir / "plan.md"
    out.write_text(plan_md, encoding="utf-8")
    return out


# ── Phase 1: Persona-Allocation ────────────────────────────────────────────


def phase_persona_allocation(
    primary_provider: BaseProvider,
    *,
    run_dir: Path,
    run_id: str,
    cross_provider_none: bool,
    provider_lookup=None,
) -> list[PersonaAllocation]:
    """Assign each persona to a concrete provider.

    Allocation rules (Plan §3, §2.1):
      * Author → primary provider always.
      * DA → cross-provider if available AND cross_provider_none is False.
        If cross_provider_none was used, falls back to primary with
        ``cross_provider_satisfied=False``.
      * Methodiker → any external provider that is also distinct from DA's
        choice; falls back to DA's provider, then to primary.

    Each allocation is recorded in the audit trail with type
    ``persona_allocation`` so later phases (status-tuple, decision-log) can
    verify cross-provider coverage deterministically.

    ``provider_lookup`` is an optional callable ``(name) -> BaseProvider | None``
    used in tests to avoid touching the real dispatcher singleton. Defaults
    to ``dispatcher.get_provider_by_name``.
    """
    if provider_lookup is None:
        from dispatcher import get_provider_by_name as _lookup
        provider_lookup = _lookup

    primary_name = primary_provider.name
    # Candidates we'd consider for "cross" — anything other than the primary.
    # We probe a small static list to avoid pulling in the full dispatcher
    # priority list (keeps this function testable).
    candidate_names = ("claude", "gemini", "codex", "openrouter")
    cross_candidates = [
        n for n in candidate_names
        if n != primary_name and provider_lookup(n) is not None
    ]

    allocations: list[PersonaAllocation] = []
    for persona in ALL_PHASE2_PERSONAS:
        if persona.provider_preference == "primary":
            allocations.append(PersonaAllocation(
                persona=persona,
                provider_name=primary_name,
                cross_provider_satisfied=False,
            ))
            continue

        # cross / any_external behave the same when cross_provider_none is
        # False: pick the first available cross-provider. Distinct from any
        # already-allocated cross persona.
        if cross_provider_none:
            allocations.append(PersonaAllocation(
                persona=persona,
                provider_name=primary_name,
                cross_provider_satisfied=False,
            ))
            continue

        already_used = {
            a.provider_name for a in allocations
            if a.cross_provider_satisfied
        }
        choice = next(
            (n for n in cross_candidates if n not in already_used),
            None,
        )
        if choice is None and persona.provider_preference == "any_external":
            # any_external falls back to primary — no cross-coverage possible
            allocations.append(PersonaAllocation(
                persona=persona,
                provider_name=primary_name,
                cross_provider_satisfied=False,
            ))
            continue
        if choice is None:
            # Strict "cross" requirement could not be satisfied — degrade to
            # primary but flag it. Status-tuple will downgrade the run later.
            allocations.append(PersonaAllocation(
                persona=persona,
                provider_name=primary_name,
                cross_provider_satisfied=False,
            ))
            continue
        allocations.append(PersonaAllocation(
            persona=persona,
            provider_name=choice,
            cross_provider_satisfied=True,
        ))

    # Audit trail entry per allocation.
    for alloc in allocations:
        try:
            audit_trail.append_audit_entry(run_dir, {
                "type": "persona_allocation",
                "run_id": run_id,
                "role": alloc.persona.role,
                "name": alloc.persona.name,
                "provider": alloc.provider_name,
                "primary_provider": primary_name,
                "cross_provider_satisfied": alloc.cross_provider_satisfied,
                "cross_provider_none_tag": cross_provider_none,
            })
        except (OSError, ValueError) as exc:
            logger.warning(
                "audit append failed for persona_allocation %s: %s",
                alloc.persona.role, exc,
            )
    return allocations
