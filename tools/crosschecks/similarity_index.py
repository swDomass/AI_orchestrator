"""Cross-investigation similarity index for cherry-picking detection.

The plan (§1.2) places the index at
``{root_cwd}/.scientific-investigation/investigation-similarity-index.json``.
It is append-only by semantics — entries may be added but never changed or
removed. This lets Phase 0 (framing) flag investigations that look
suspiciously similar to a prior run, and Phase 6 (decision-log) document
the relationship.

Algorithm
---------
TF-IDF-like Jaccard cosine over tokenized text — same approach used in
``memory.py`` (no external deps, fits the stdlib-only constraint of the
project). Plan §6.3 documents ``all-MiniLM-L6-v2`` as the *planned* upgrade
once the threshold is empirically calibrated (DEFERRED until 20+
investigations, see §8.2). The embedding model used at run time is recorded
in each run's manifest.json so a future re-vectorization can trace what
algorithm produced the original score.

The threshold is intentionally NOT a hard block — Phase 0 only writes a
visibility note into the pre-registration; the user decides what to do with
duplicates.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

INDEX_FILE = ".scientific-investigation/investigation-similarity-index.json"

# Same regex constants as memory.py — kept local to avoid import-cycle risks.
_RE_CAMEL_SPLIT = re.compile(r"([a-z])([A-Z])")
_RE_DELIMITERS = re.compile(r"[_\-./\\:;,!?\"'()\[\]{}]+")
_RE_WORDS = re.compile(r"[a-zA-ZäöüÄÖÜß0-9]{3,}")
_STOPWORDS = frozenset({
    # English
    "the", "and", "for", "with", "from", "this", "that", "into", "onto",
    "are", "was", "were", "have", "has", "had", "but", "not", "all", "any",
    "can", "will", "would", "could", "should", "may", "might", "must",
    # German
    "und", "die", "der", "das", "den", "dem", "des", "ein", "eine", "einen",
    "mit", "von", "auf", "ist", "sind", "war", "waren", "wird", "werde",
    "werden", "für", "vom", "zum", "zur", "nicht", "auch", "aber", "doch",
})


def index_path(root_cwd: Path) -> Path:
    """Path to the similarity index JSON file. Creates parent dir."""
    out = root_cwd / INDEX_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def _tokenize(text: str) -> set[str]:
    """Lowercase, split on delimiters and camelCase, keep words ≥3 chars."""
    text = _RE_CAMEL_SPLIT.sub(r"\1 \2", text)
    text = _RE_DELIMITERS.sub(" ", text)
    words = _RE_WORDS.findall(text.lower())
    return {w for w in words if w not in _STOPWORDS}


def cosine_similarity(text_a: str, text_b: str) -> float:
    """Jaccard-cosine approximation: |intersection| / sqrt(|a| * |b|).

    Returns 0.0 for any empty input. Range [0.0, 1.0].
    """
    a = _tokenize(text_a)
    b = _tokenize(text_b)
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    return intersection / math.sqrt(len(a) * len(b))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(root_cwd: Path) -> list[dict[str, Any]]:
    path = index_path(root_cwd)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("similarity_index: %s unreadable, treating as empty: %s", path, exc)
        return []
    if not isinstance(data, list):
        logger.warning("similarity_index: %s has wrong shape (expected list), resetting", path)
        return []
    return data


def _save(root_cwd: Path, entries: list[dict[str, Any]]) -> None:
    path = index_path(root_cwd)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def append_investigation(
    root_cwd: Path,
    *,
    run_id: str,
    framing_text: str,
    embedding_model: str,
) -> None:
    """Append one investigation to the index. Append-only — never overwrites
    existing entries. Idempotent on ``run_id`` (a second call with the same
    run_id is silently dropped to keep the dataset clean across resumes).
    """
    entries = _load(root_cwd)
    if any(e.get("run_id") == run_id for e in entries):
        logger.debug("similarity_index: run_id=%s already indexed, skipping", run_id)
        return
    entries.append({
        "run_id": run_id,
        "ts_utc": _utc_now_iso(),
        "embedding_model": embedding_model,
        "framing_text": framing_text,
    })
    _save(root_cwd, entries)


def find_similar_investigations(
    root_cwd: Path,
    *,
    framing_text: str,
    threshold: float,
    exclude_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return prior investigations whose framing similarity ≥ threshold.

    Each returned entry has the original index fields plus ``score``.
    Sorted by score descending. ``exclude_run_id`` lets the caller drop the
    current run (so re-running an existing investigation does not match
    itself).
    """
    hits: list[dict[str, Any]] = []
    for entry in _load(root_cwd):
        if exclude_run_id and entry.get("run_id") == exclude_run_id:
            continue
        prior_text = entry.get("framing_text", "")
        score = cosine_similarity(framing_text, prior_text)
        if score >= threshold:
            hit = dict(entry)
            hit["score"] = round(score, 4)
            hits.append(hit)
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits
