"""
Skill suggestion (#36) — draft-only, pattern-gated.

When the orchestrator sees the same workflow pattern N>=3 times within
30 days, it offers a candidate ``SKILL.md`` for the user to review.
**Activation is always manual** — drafts land in a separate directory.

Pattern definition
------------------

A "pattern" is the triple ``(tool, cwd, task_shape)`` where:
  * ``tool``      = ``record["tool"]`` (empty for single-shot tasks → skip)
  * ``cwd``       = ``record["cwd"]`` (empty → skip)
  * ``task_shape``= top-5 TF-IDF keywords of ``record["task_text"]``, sorted

The shape is *normalized* (lowercase, stopwords removed) so phrasing
variations like "fix bug X" vs. "fix bug Y" map to the same pattern.

Gate
----

* N>=3 records within ``PATTERN_WINDOW_DAYS`` (30 days)
* No active draft for the same pattern within ``PATTERN_COOLDOWN_DAYS`` (90 days)

Output
------

* Saves to ``<VAULT>/99_System/AI/Skills-Drafts/<slug>/SKILL.md``
* Sends one Telegram notification per draft
* Records the suggestion in ``logs/skill-suggestions.jsonl`` for the cooldown gate

Activation
----------

Manual: user moves the file from ``Skills-Drafts/`` to ``Skills/``. There is
no ``/activate-skill`` command — by design.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

DEFAULT_LEDGER = Path(__file__).parent / "logs" / "skill-suggestions.jsonl"
PATTERN_WINDOW_DAYS = 30
PATTERN_COOLDOWN_DAYS = 90
PATTERN_MIN_OCCURRENCES = 3
TASK_SHAPE_KEYWORDS = 5

_STOPWORDS_DE_EN = {
    "the", "and", "for", "with", "from", "that", "this", "into", "your", "have",
    "und", "die", "der", "das", "ein", "eine", "ist", "auf", "für", "mit",
    "von", "nicht", "auch", "wenn", "wird", "noch", "über", "zur", "zum",
    "task", "run", "do", "make", "add", "fix", "use", "set",
}


# ---------------------------------------------------------------------------
# Ledger (cooldown tracking)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_ledger_path: Path = DEFAULT_LEDGER


def set_ledger_path(path: Path) -> None:
    global _ledger_path
    with _lock:
        _ledger_path = Path(path)


def get_ledger_path() -> Path:
    return _ledger_path


def reset_for_tests() -> None:
    with _lock:
        try:
            _ledger_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _read_ledger() -> list[dict]:
    if not _ledger_path.exists():
        return []
    out: list[dict] = []
    try:
        with open(_ledger_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.debug("skill-suggester ledger read failed: %s", e)
    return out


def _append_ledger(entry: dict) -> None:
    _ledger_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(_ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("skill-suggester ledger write failed: %s", e)


def _pattern_recently_suggested(pattern_id: str) -> bool:
    cutoff = datetime.now() - timedelta(days=PATTERN_COOLDOWN_DAYS)
    for entry in _read_ledger():
        if entry.get("pattern_id") != pattern_id:
            continue
        try:
            ts = datetime.strptime(entry["ts"], "%Y-%m-%dT%H:%M:%S")
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            return True
    return False


# ---------------------------------------------------------------------------
# Pattern extraction
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-zA-ZäöüÄÖÜß0-9_-]{3,}")


def _normalize_task_shape(task_text: str, *, top_n: int = TASK_SHAPE_KEYWORDS) -> tuple[str, ...]:
    """Return the top-N keywords (lowercased, stopwords removed, sorted)."""
    tokens = [t.lower() for t in _WORD_RE.findall(task_text or "")]
    filtered = [t for t in tokens if t not in _STOPWORDS_DE_EN]
    if not filtered:
        return ()
    counts = Counter(filtered).most_common(top_n)
    return tuple(sorted(t for t, _ in counts))


def pattern_id(tool: str, cwd: str, task_shape: tuple[str, ...]) -> str:
    """Stable hash of the pattern triple."""
    payload = "\x00".join([tool or "", cwd or "", "|".join(task_shape)])
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass(frozen=True)
class CandidatePattern:
    pattern_id: str
    tool: str
    cwd: str
    task_shape: tuple[str, ...]
    occurrences: int
    sample_task_texts: tuple[str, ...]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_candidates(
    records: Iterable[dict],
    *,
    min_occurrences: int = PATTERN_MIN_OCCURRENCES,
    window_days: int = PATTERN_WINDOW_DAYS,
    now: datetime | None = None,
) -> list[CandidatePattern]:
    """Group replay records by (tool, cwd, task_shape) and return patterns
    that occur >= min_occurrences within the last window_days."""
    now = now or datetime.now()
    cutoff = now - timedelta(days=window_days)

    buckets: dict[str, list[dict]] = defaultdict(list)
    triples: dict[str, tuple[str, str, tuple[str, ...]]] = {}

    for rec in records:
        try:
            ts = datetime.strptime(rec.get("ts_start", ""), "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        # Successful tool runs only — failure patterns are taxonomy territory.
        if (rec.get("exit_status") or "").lower() != "ok":
            continue

        tool = (rec.get("tool") or "").strip()
        cwd = (rec.get("cwd") or "").strip()
        if not tool or not cwd:
            continue

        shape = _normalize_task_shape(rec.get("task_text") or "")
        if not shape:
            continue

        pid = pattern_id(tool, cwd, shape)
        buckets[pid].append(rec)
        triples[pid] = (tool, cwd, shape)

    out: list[CandidatePattern] = []
    for pid, recs in buckets.items():
        if len(recs) < min_occurrences:
            continue
        tool, cwd, shape = triples[pid]
        samples = tuple((r.get("task_text") or "")[:200] for r in recs[:3])
        out.append(CandidatePattern(
            pattern_id=pid,
            tool=tool,
            cwd=cwd,
            task_shape=shape,
            occurrences=len(recs),
            sample_task_texts=samples,
        ))
    return out


# ---------------------------------------------------------------------------
# Draft generation
# ---------------------------------------------------------------------------

def _slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (s[:max_len] or "skill").rstrip("-")


def _build_skill_md(candidate: CandidatePattern, *, summary: str = "") -> str:
    """Build the SKILL.md content for a candidate.

    ``summary`` is the optional LLM-generated description. When empty we fall
    back to a generic template — the user still has a useful starting point.
    """
    name_slug = f"{candidate.tool}-{_slug('-'.join(candidate.task_shape))}"
    tags = ", ".join(f'"{t}"' for t in candidate.task_shape[:5])
    description = summary.strip() or (
        f"Auto-generated draft based on {candidate.occurrences} successful "
        f"`{candidate.tool}` runs in `{candidate.cwd}`."
    )
    samples = "\n".join(f"- {s}" for s in candidate.sample_task_texts)
    return (
        "---\n"
        f"name: {name_slug}\n"
        f"description: {description}\n"
        "version: 0.1\n"
        f"tags: [{tags}]\n"
        "---\n\n"
        f"## Pattern (auto-detected)\n\n"
        f"- Tool: `{candidate.tool}`\n"
        f"- CWD: `{candidate.cwd}`\n"
        f"- Top-Keywords: {', '.join(f'`{t}`' for t in candidate.task_shape)}\n"
        f"- Observed: {candidate.occurrences} mal in den letzten 30 Tagen\n\n"
        "## Sample Tasks\n\n"
        f"{samples}\n\n"
        "## System Prompt Addition\n\n"
        "<!-- TODO: Beschreibe hier, wie das Tool für diesen Pattern agieren soll. -->\n"
        "Es scheint, dass diese Tasks denselben Workflow auslösen — überprüfe und ergänze die\n"
        "Schritte/Regeln, bevor du diesen Skill aktivierst.\n\n"
        "## Activation\n\n"
        f"1. Diese Datei nach `99_System/AI/Skills/{name_slug}/SKILL.md` verschieben.\n"
        "2. Felder `description` und `## System Prompt Addition` finalisieren.\n"
        f"3. Tasks mit `#tool:{name_slug}` taggen.\n"
    )


def write_draft(
    candidate: CandidatePattern,
    drafts_root: Path,
    *,
    summary: str = "",
) -> Path:
    """Persist the draft to ``<drafts_root>/<slug>/SKILL.md`` and return the path."""
    slug = f"{candidate.tool}-{_slug('-'.join(candidate.task_shape))}"
    target_dir = drafts_root / slug
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "SKILL.md"
    target_file.write_text(_build_skill_md(candidate, summary=summary), encoding="utf-8")
    return target_file


# ---------------------------------------------------------------------------
# Suggestion run
# ---------------------------------------------------------------------------

def suggest_once(
    *,
    drafts_root: Path,
    records: Iterable[dict] | None = None,
    notify_fn=None,
    summary_fn=None,
) -> list[tuple[CandidatePattern, Path]]:
    """Run a full suggestion pass.

    Args:
        drafts_root: ``<VAULT>/99_System/AI/Skills-Drafts`` (or a tmp_path in tests).
        records: replay records to inspect. If None, reads from ``replay.read_runs``.
        notify_fn: optional callable taking a Telegram-style message.
        summary_fn: optional ``(candidate) -> str`` that returns an LLM-written
            description. When None, the draft uses the generic template only.

    Returns: list of (candidate, draft_path) for newly written drafts.
    """
    if records is None:
        try:
            import replay
            records = replay.read_runs(
                since=datetime.now() - timedelta(days=PATTERN_WINDOW_DAYS),
                include_archive=False,
            )
        except Exception as exc:
            logger.debug("skill-suggester replay read failed: %s", exc)
            return []

    candidates = find_candidates(list(records))
    written: list[tuple[CandidatePattern, Path]] = []

    for cand in candidates:
        if _pattern_recently_suggested(cand.pattern_id):
            continue

        summary = ""
        if summary_fn is not None:
            try:
                summary = summary_fn(cand) or ""
            except Exception as exc:
                logger.debug("skill-suggester summary failed: %s", exc)
                summary = ""

        try:
            draft_path = write_draft(cand, drafts_root, summary=summary)
        except OSError as exc:
            logger.warning("skill-suggester write failed for %s: %s",
                           cand.pattern_id, exc)
            continue

        with _lock:
            _append_ledger({
                "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "pattern_id": cand.pattern_id,
                "tool": cand.tool,
                "cwd": cand.cwd,
                "task_shape": list(cand.task_shape),
                "occurrences": cand.occurrences,
                "draft_path": str(draft_path),
            })

        if notify_fn is not None:
            try:
                notify_fn(
                    f"💡 Skill-Vorschlag basierend auf {cand.occurrences} `{cand.tool}`-Runs "
                    f"in `{cand.cwd}`.\n\n"
                    f"Draft: `{draft_path}`\n\n"
                    f"Aktivieren: Datei nach `99_System/AI/Skills/` verschieben."
                )
            except Exception as exc:
                logger.warning("skill-suggester notify failed: %s", exc)

        written.append((cand, draft_path))

    return written
