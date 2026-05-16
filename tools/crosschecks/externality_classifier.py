"""Deterministic T2/T3 classifier for crosscheck files.

Reads ``audit/approvals.jsonl`` (via ``audit_trail.load_audit_entries``) and
matches a crosscheck file against ``crosscheck_tier`` entries. T2 is granted
only when:

  1. An entry of type ``crosscheck_tier`` for this exact file_path exists.
  2. The entry's stored SHA256 matches the file's current SHA256.
  3. The entry's source is one of {norm_reference, paper_reference,
     telegram_approval}.

Otherwise the file is T3 (default).

Why this matters
----------------
The plan (§1.3) explicitly says: tier classification must NEVER be based on
LLM self-reporting. The LLM may write to approvals.jsonl, but tier-up only
takes effect when (a) a corresponding entry exists AND (b) the file content
has not drifted since the entry was written. SHA mismatch → T3 fallback.

This is the only deterministic safeguard in the audit pattern. It does not
prevent a malicious LLM from inventing entries, but it makes silent drift
("LLM marked T2, then changed the file") impossible.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from tools.crosschecks.audit_trail import compute_sha256, load_audit_entries

logger = logging.getLogger(__name__)

Tier = Literal["T2", "T3"]

# Sources that confer T2. Telegram-approved is included because the message-ID
# is a separate audit anchor (see audit_trail.record_telegram_message).
T2_ELIGIBLE_SOURCES = frozenset({
    "norm_reference",
    "paper_reference",
    "telegram_approval",
})


def classify_crosscheck_tier(file_path: Path, run_dir: Path) -> Tier:
    """Classify a crosscheck file as T2 or T3.

    Parameters
    ----------
    file_path
        Path to the crosscheck source file (e.g. ``tests/crosscheck_balance.py``).
        Should be relative to the project root or absolute — stored as-is in
        the audit entry, so the caller MUST pass it consistently both at
        write- and read-time.
    run_dir
        The run's ``docs/scientific-investigation-{ts}/`` directory.
    """
    if not file_path.exists():
        logger.debug("classify_crosscheck_tier: %s missing → T3", file_path)
        return "T3"

    current_sha = compute_sha256(file_path)
    if not current_sha:
        return "T3"

    file_path_str = str(file_path)
    entries = load_audit_entries(run_dir, entry_type="crosscheck_tier")

    for entry in entries:
        if entry.get("file_path") != file_path_str:
            continue
        if entry.get("file_sha256") != current_sha:
            # File changed after tier assignment → invalid.
            continue
        if entry.get("source") not in T2_ELIGIBLE_SOURCES:
            continue
        if entry.get("assigned_tier") == "T2":
            return "T2"

    return "T3"


def crosscheck_tiers_per_subtask(
    subtask_files: dict[str, list[Path]],
    run_dir: Path,
) -> dict[str, list[Tier]]:
    """Classify every crosscheck file for every sub-task.

    Used by the status-tuple computation in synthesis (Phase 4) — see
    ``compute_status_tuple`` in the tool implementation.
    """
    return {
        sub_id: [classify_crosscheck_tier(p, run_dir) for p in files]
        for sub_id, files in subtask_files.items()
    }
