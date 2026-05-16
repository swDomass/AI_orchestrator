"""Phase 8 — Final User-Approval Gate (Plan §2.8, I8).

Workflow:

  1. Build a concise Telegram summary: question, status-tuple, top-3
     limitations, draft-file path, run_id.
  2. Send via notifier (caller provides ``notify_callable`` so tests stay
     hermetic — production callers point it at ``notifier.send_message``).
  3. Block on the existing ``PreRegApprovalManager`` with the sentinel
     ``criterion_id="__investigation__"`` (already defined in I1). The
     telegram_listener routes ``/approve <run_id>`` (no criterion) to that
     same sentinel — wiring already in place from I1.
  4. On ``approved``: move ``draft/proof.md`` → ``proof.md`` atomically,
     append an ``investigation_approval`` audit entry with the
     ``telegram_msg_id``, return ``Phase8Result(state="approved")``.
  5. On ``rejected``: append the audit entry, mark the run INCONCLUSIVE,
     leave the draft in place — caller surfaces this in the decision-log.
  6. On ``timeout``: same as rejected but with ``state="timeout"`` so the
     caller can re-issue via ``#resume:<run_id>``.

The ``state.json`` is NOT touched here — Phase 8 is the very last gate,
and the caller persists the final tuple together with the engineering-
reviewer status.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from config import TOOL_SI_APPROVAL_TIMEOUT_HOURS
from tools.crosschecks import audit_trail
from tools.scientific_investigation_approvals import (
    INVESTIGATION_CRITERION,
    get_manager,
)

logger = logging.getLogger(__name__)

ApprovalState = Literal["approved", "rejected", "timeout", "skipped"]


# ── Data classes ─────────────────────────────────────────────────────────


@dataclass
class Phase8Summary:
    """Inputs to the Telegram approval message."""
    question: str
    methodological_rigor: str
    residual_risk: str
    evidence_basis: str
    criteria_test_status: str
    top_limitations: list[str]   # up to 3, each <= 100 chars
    draft_path: Path
    run_id: str

    def to_telegram_text(self) -> str:
        lim_lines = "\n".join(
            f"{i + 1}. {lim[:100]}" for i, lim in enumerate(self.top_limitations[:3])
        ) or "(keine extrahiert)"
        return (
            f"Investigation {self.run_id} fertig zur Approval\n\n"
            f"Frage: {self.question[:200]}\n\n"
            f"Status:\n"
            f"- methodological_rigor: {self.methodological_rigor}\n"
            f"- residual_risk: {self.residual_risk}\n"
            f"- evidence_basis: {self.evidence_basis}\n"
            f"- criteria_test_status: {self.criteria_test_status}\n\n"
            f"Top-3 Limitations:\n{lim_lines}\n\n"
            f"Draft: {self.draft_path}\n\n"
            f"Approve mit: /approve {self.run_id}\n"
            f"Reject mit: /reject {self.run_id} <Begründung>\n"
            f"Timeout: {TOOL_SI_APPROVAL_TIMEOUT_HOURS}h"
        )


@dataclass
class Phase8Result:
    state: ApprovalState
    telegram_msg_id: str
    approver: str
    reason: str
    final_proof_path: Path | None  # None when state != "approved"


# ── Limitations extraction (top-3 first sentences) ───────────────────────


def extract_top_limitations(proof_md: str, *, max_count: int = 3) -> list[str]:
    """Pull the first sentence of each Limitations subsection.

    Mirrors the parser used in Phase 4's validator but returns just enough
    text to fit Telegram. Falls back to empty list if no subsections are
    found.
    """
    from tools.scientific_investigation_phase4 import _parse_limitation_subsections
    sections = _parse_limitation_subsections(proof_md)
    out: list[str] = []
    for s in sections[:max_count]:
        body = s["body"].strip().replace("\n", " ")
        # First sentence — split on the first '. ' that follows letters.
        first_sentence = body.split(". ", 1)[0]
        if first_sentence and not first_sentence.endswith("."):
            first_sentence += "."
        out.append(first_sentence)
    return out


# ── Runner ────────────────────────────────────────────────────────────────


NotifyCallable = Callable[[str], str]
"""Send the Telegram message and return the Telegram message ID (string).
In tests this is a stub that returns a fixed ID; in production it points
at ``notifier.send_message_returning_id`` (or equivalent)."""


def phase_final_approval(
    *,
    summary: Phase8Summary,
    run_dir: Path,
    run_id: str,
    notify_callable: NotifyCallable,
    timeout_sec: float,
) -> Phase8Result:
    """Block on user approval. Returns ``Phase8Result``.

    On approved, atomically renames ``draft/proof.md`` to ``proof.md``
    (using ``Path.replace`` — POSIX/Windows safe). Audit entry is written
    BEFORE the move; if the move fails, the audit still records what the
    user decided. Re-running on the same run_id is safe (re-issuing the
    approval will overwrite the existing proof.md).
    """
    telegram_text = summary.to_telegram_text()
    telegram_msg_id = ""
    try:
        sent_id = notify_callable(telegram_text)
        if sent_id:
            telegram_msg_id = str(sent_id)
    except Exception as exc:  # pragma: no cover  — defensive
        logger.warning("Phase 8: notify_callable raised: %s", exc)

    # Record the notification → message-id mapping so audit readers can join.
    try:
        audit_trail.record_telegram_message(
            run_dir,
            telegram_msg_id=telegram_msg_id or "(send-failed)",
            purpose="phase8_approval_request",
            payload={"run_id": run_id, "preview": telegram_text[:300]},
        )
    except OSError as exc:
        logger.warning("Phase 8: telegram_messages.jsonl append failed: %s", exc)

    manager = get_manager()
    response, returned_msg_id, approver, reason = manager.request_threshold_approval(
        run_id=run_id,
        criterion_id=INVESTIGATION_CRITERION,
        timeout_sec=timeout_sec,
    )

    effective_msg_id = returned_msg_id or telegram_msg_id
    state: ApprovalState
    if response == "approved":
        state = "approved"
    elif response == "rejected":
        state = "rejected"
    elif response == "timeout":
        state = "timeout"
    else:
        state = "skipped"

    # Audit entry — always written, regardless of outcome.
    try:
        audit_trail.append_audit_entry(run_dir, {
            "type": "investigation_approval",
            "run_id": run_id,
            "source": "telegram_approval",
            "telegram_msg_id": effective_msg_id,
            "approver": approver,
            "user_response": response,
            "reason": reason,
            "status_tuple_at_approval": {
                "methodological_rigor": summary.methodological_rigor,
                "evidence_basis": summary.evidence_basis,
                "criteria_test_status": summary.criteria_test_status,
            },
        })
    except (OSError, ValueError) as exc:
        logger.warning("Phase 8: audit append failed: %s", exc)

    if state != "approved":
        return Phase8Result(
            state=state,
            telegram_msg_id=effective_msg_id,
            approver=approver,
            reason=reason,
            final_proof_path=None,
        )

    # Atomic move: draft/proof.md → run_dir/proof.md.
    draft = summary.draft_path
    final_path = run_dir / "proof.md"
    if not draft.exists():
        logger.warning(
            "Phase 8: approved but draft %s missing — final_proof_path stays None",
            draft,
        )
        return Phase8Result(
            state="approved",
            telegram_msg_id=effective_msg_id,
            approver=approver,
            reason=reason,
            final_proof_path=None,
        )
    try:
        # Path.replace is atomic on POSIX and atomic-replace on Windows.
        draft.replace(final_path)
    except OSError as exc:
        logger.warning("Phase 8: draft→final move failed: %s", exc)
        return Phase8Result(
            state="approved",
            telegram_msg_id=effective_msg_id,
            approver=approver,
            reason=reason,
            final_proof_path=None,
        )

    return Phase8Result(
        state="approved",
        telegram_msg_id=effective_msg_id,
        approver=approver,
        reason=reason,
        final_proof_path=final_path,
    )
