"""Append-only JSONL audit trail for scientific-investigation runs.

The audit file lives at ``{run_dir}/audit/approvals.jsonl`` where ``run_dir``
is ``{cwd}/docs/scientific-investigation-{ts}/``. Every entry documents *what
happened* — pre-registration thresholds with their sources, crosscheck-tier
assignments with file SHA256, and the final investigation-level approval.

This is an audit pattern, not a security mechanism (see plan §1.3, §10):

  * The LLM is allowed to write here — it MUST, because user approvals get
    recorded as new entries.
  * Append-only is enforced by file-open mode (``"a"``), not by NTFS perms.
  * Trust shifts to: user discipline + Telegram-Approval gate (Phase 8) +
    deterministic SHA-lookup at classification time.

Source values
-------------
  * ``norm_reference``      — DIN/EN/ISO/IEEE citation with snippet.
  * ``paper_reference``     — DOI + page/snippet from a published paper.
  * ``telegram_approval``   — user approved via Telegram (with message-ID).
  * ``user_chat_signoff``   — explicit user OK in current chat (audit demands
                              a second signal).

Entry types
-----------
  * ``preregistration_threshold`` — Pre-Reg threshold + source.
  * ``crosscheck_tier``           — Crosscheck file + tier + source.
  * ``investigation_approval``    — Final user approval for the investigation.
  * ``preregistration_warning``   — Disziplin-Warnung was emitted.
  * ``cross_provider_bypass``     — #cross-provider:none was used.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# JSONL filenames inside the run's audit/ directory.
APPROVALS_FILE = "approvals.jsonl"
TELEGRAM_MAPPING_FILE = "telegram_messages.jsonl"

# Allowed source values (frozen — extending requires explicit code change).
ALLOWED_SOURCES = frozenset({
    "norm_reference",
    "paper_reference",
    "telegram_approval",
    "user_chat_signoff",
})

# Allowed entry types (frozen).
ALLOWED_ENTRY_TYPES = frozenset({
    "preregistration_threshold",
    "preregistration_warning",
    "crosscheck_tier",
    "investigation_approval",
    "cross_provider_bypass",
    "persona_allocation",
})


def audit_dir(run_dir: Path) -> Path:
    """Return the audit/ subdirectory for a run, creating it if needed."""
    out = run_dir / "audit"
    out.mkdir(parents=True, exist_ok=True)
    return out


def approvals_path(run_dir: Path) -> Path:
    """Return the path to approvals.jsonl for a run."""
    return audit_dir(run_dir) / APPROVALS_FILE


def telegram_mapping_path(run_dir: Path) -> Path:
    """Return the path to telegram_messages.jsonl for a run."""
    return audit_dir(run_dir) / TELEGRAM_MAPPING_FILE


def compute_sha256(file_path: Path) -> str:
    """Stream-hash a file's bytes. Returns hex digest. Empty string on error."""
    h = hashlib.sha256()
    try:
        with file_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        logger.warning("compute_sha256 failed for %s: %s", file_path, exc)
        return ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_audit_entry(run_dir: Path, entry: dict[str, Any]) -> None:
    """Append a JSON object as a single line to approvals.jsonl.

    Validates the entry shape before writing — rejects unknown ``type`` /
    ``source`` values so a typo cannot silently produce a tier-bypass.
    Always sets ``ts`` if missing.

    Raises ``ValueError`` on validation failure. Filesystem errors are logged
    and re-raised so callers can decide whether the run continues.
    """
    if not isinstance(entry, dict):
        raise ValueError("audit entry must be a dict")
    entry_type = entry.get("type")
    if entry_type not in ALLOWED_ENTRY_TYPES:
        raise ValueError(f"unknown audit entry type: {entry_type!r}")
    source = entry.get("source")
    if source is not None and source not in ALLOWED_SOURCES:
        raise ValueError(f"unknown audit source: {source!r}")
    if entry_type == "crosscheck_tier":
        if not entry.get("file_path") or not entry.get("file_sha256"):
            raise ValueError(
                "crosscheck_tier entry requires file_path + file_sha256"
            )
        if entry.get("assigned_tier") not in ("T2", "T3"):
            raise ValueError("crosscheck_tier requires assigned_tier in {T2,T3}")
    entry.setdefault("ts", _utc_now_iso())

    target = approvals_path(run_dir)
    # Append-only via file-open mode. Each entry is one line; no in-place edit.
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_audit_entries(
    run_dir: Path,
    *,
    entry_type: str | None = None,
) -> list[dict[str, Any]]:
    """Read all audit entries for a run.

    Tolerates malformed JSON lines (logs + skips). Returns chronological order
    (file order = append order). When ``entry_type`` is given, only matching
    entries are returned.
    """
    path = approvals_path(run_dir)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "audit_trail: skipping malformed line %d in %s: %s",
                    line_no, path, exc,
                )
                continue
            if entry_type is not None and entry.get("type") != entry_type:
                continue
            out.append(entry)
    return out


def record_telegram_message(
    run_dir: Path,
    *,
    telegram_msg_id: str,
    purpose: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append a Telegram-message ↔ approval mapping entry.

    Used so an audit reader can later jump from an approvals.jsonl entry with
    ``source=telegram_approval`` and ``telegram_msg_id=12345`` to the original
    notification payload.
    """
    target = telegram_mapping_path(run_dir)
    entry = {
        "ts": _utc_now_iso(),
        "telegram_msg_id": str(telegram_msg_id),
        "purpose": purpose,
        "payload": payload or {},
    }
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
