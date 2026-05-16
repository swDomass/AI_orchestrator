"""Scientific-Investigation Tool — internes Engineering-Notebook-Evidence-Building
mit dokumentiertem Audit-Trail (Plan v5).

Plan: ``docs/plans/scientific-investigation-tool-v5.md``.

Status: **Increment I0 (Engineering-Layer)** is implemented in this file:
    * Tag-Parser for ``#prior:``, ``#cross-provider:none``, ``#discipline:no-norms``,
      ``#resume:``, ``#engineering_reviewer:``.
    * Run isolation: ``{cwd}/docs/scientific-investigation-{ts}/`` plus the
      sub-task state directory under ``{cwd}/.scientific-investigation/{run_id}/``.
    * Atomic state-write helper.
    * Initial manifest.json with code-commit-SHA, embedding-model info,
      and the resolved tag-set.
    * Audit-trail initialization (creates approvals.jsonl skeleton +
      cross-provider-bypass entry when ``#cross-provider:none`` was used and
      the rate-limit allowed it; PolicyEngine route is a TODO until I2).

Phases 0–9 (Framing, Pre-Reg, Persona-Allocation, Investigation-Plan,
Execution-Loop, Synthesis, Falsification-Check, Decision-Log, Engineering-
Reviewer, Final-Approval-Gate, Audit-Pack) land in increments I1–I9. The
``run()`` method documents the I0 boundary clearly so an early invocation
returns a structured "scaffold only" result instead of failing silently.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import (
    TOOL_SI_BYPASS_LIMIT_PER_30_DAYS,
    TOOL_SI_EMBEDDING_MODEL,
    TOOL_SI_PHASE0_TIMEOUT_SEC,
    TOOL_SI_PHASE0_5_TIMEOUT_SEC,
    TOOL_SI_TELEGRAM_APPROVAL_TIMEOUT_SEC,
)
from notifier import notify_tool_done
from providers.base import BaseProvider
from tools.base_tool import (
    BaseTool,
    ToolResult,
    ToolTracer,
)
from tools.crosschecks import audit_trail, bypass_counter
from tools.crosschecks.cherrypicking_detector import (
    build_cherrypicking_block,
    build_persona_allocation_block,
    write_decision_log,
)
from tools.scientific_investigation_phase2 import (
    phase_investigation_plan_review,
    write_investigation_plan_md,
    write_review_findings_md,
)
from tools.scientific_investigation_phases import (
    phase_framing,
    phase_persona_allocation,
    phase_prereg,
    write_plan_md,
)

logger = logging.getLogger(__name__)

# Top-level run directory under the project CWD.
RUN_DIR_PREFIX = "docs/scientific-investigation-"
# Sub-task state directory under the project CWD.
STATE_DIR_NAME = ".scientific-investigation"

# Tag patterns. Kept here (not in queue_manager) because they are tool-local.
# Note: ``\b`` between ``e`` (word char) and ``-`` (non-word char) DOES match,
# so for tags whose value is a fixed word followed by a possible hyphen-suffix
# we must use a negative lookahead ``(?![-\w])`` instead.
_TAG_PRIOR_RE = re.compile(r"#prior:([\w-]+)")
_TAG_CROSS_PROVIDER_NONE_RE = re.compile(r"#cross-provider:none(?![-\w])")
_TAG_DISCIPLINE_NO_NORMS_RE = re.compile(r"#discipline:no-norms(?![-\w])")
_TAG_RESUME_RE = re.compile(r"#resume:([\w-]+)")
_TAG_ENG_REVIEWER_RE = re.compile(r"#engineering_reviewer:([\w-]+)")


@dataclass
class _Tags:
    """Parsed tool-specific tags from the queue task text."""
    prior_run_id: str | None = None
    cross_provider_none: bool = False
    discipline_no_norms: bool = False
    resume_run_id: str | None = None
    engineering_reviewer: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_audit_dict(self) -> dict[str, Any]:
        """Serializable subset for the manifest."""
        return {
            "prior_run_id": self.prior_run_id,
            "cross_provider_none": self.cross_provider_none,
            "discipline_no_norms": self.discipline_no_norms,
            "resume_run_id": self.resume_run_id,
            "engineering_reviewer": self.engineering_reviewer,
        }


def parse_tags(task: str) -> _Tags:
    """Extract tool-specific tags from the task text.

    Tags are independent of the queue-level tags (#tool:, #claude_*, cwd:, …)
    that ``queue_manager`` already parses — those reach the tool via separate
    fields. This function only reads tags that are tool-internal.
    """
    tags = _Tags()
    m = _TAG_PRIOR_RE.search(task)
    if m:
        tags.prior_run_id = m.group(1)
    if _TAG_CROSS_PROVIDER_NONE_RE.search(task):
        tags.cross_provider_none = True
    if _TAG_DISCIPLINE_NO_NORMS_RE.search(task):
        tags.discipline_no_norms = True
    m = _TAG_RESUME_RE.search(task)
    if m:
        tags.resume_run_id = m.group(1)
    m = _TAG_ENG_REVIEWER_RE.search(task)
    if m:
        tags.engineering_reviewer = m.group(1)
    return tags


def _ts_slug(now: datetime | None = None) -> str:
    """Filesystem-safe timestamp slug (UTC, second resolution)."""
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")


def build_run_dir(root_cwd: Path, ts_slug: str) -> Path:
    """Return the run's top-level docs directory, creating layout subdirs."""
    run_dir = root_cwd / f"{RUN_DIR_PREFIX}{ts_slug}"
    (run_dir / "draft").mkdir(parents=True, exist_ok=True)
    (run_dir / "traces").mkdir(parents=True, exist_ok=True)
    (run_dir / "audit").mkdir(parents=True, exist_ok=True)
    return run_dir


def build_state_dir(root_cwd: Path, run_id: str) -> Path:
    """Return the run's sub-task state directory under .scientific-investigation/."""
    state_dir = root_cwd / STATE_DIR_NAME / run_id
    (state_dir / "sub-tasks").mkdir(parents=True, exist_ok=True)
    return state_dir


def atomic_write_state(path: Path, data: dict[str, Any]) -> None:
    """Write JSON state atomically (write to .tmp, then rename).

    Used for state.json files where a concurrent reader could see a half-
    written file otherwise. NOT for append-only audit logs — those use
    open(mode='a') and write one line at a time.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _git_commit_sha(cwd: Path) -> str:
    """Return current git HEAD SHA, or empty string if not a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("git rev-parse failed in %s: %s", cwd, exc)
    return ""


def write_manifest(
    run_dir: Path,
    *,
    run_id: str,
    task: str,
    provider_name: str,
    root_cwd: Path,
    tags: _Tags,
) -> Path:
    """Write the run's audit/manifest.json with provenance metadata."""
    manifest = {
        "run_id": run_id,
        "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "task": task,
        "provider": provider_name,
        "root_cwd": str(root_cwd),
        "git_commit_sha": _git_commit_sha(root_cwd),
        "embedding_model": TOOL_SI_EMBEDDING_MODEL,
        "tags": tags.as_audit_dict(),
        "tool_version": "scientific-investigation/v5/I3",
    }
    out = run_dir / "audit" / "manifest.json"
    atomic_write_state(out, manifest)
    return out


def _resolve_root_cwd(cwd: str | None) -> Path:
    """Return an absolute Path for the project root CWD."""
    return Path(cwd or ".").resolve()


class ScientificInvestigationTool(BaseTool):
    name = "scientific-investigation"
    description = (
        "Wissenschaftlicher Autopilot mit Audit-Trail: Pre-Registration, "
        "Multi-Persona-Review, Crosschecks, Engineering-Reviewer + Telegram-"
        "Approval-Gate (Plan v5)."
    )
    # NOT read_only — this tool writes investigation files, audit-trail entries,
    # and (in later increments) crosscheck code.
    read_only = False

    def _handle_cross_provider_bypass(
        self,
        bypass_requested: bool,
        *,
        run_dir: Path,
        root_cwd: Path,
        run_id: str,
    ) -> tuple[str, str] | None:
        """Process a ``#cross-provider:none`` tag.

        Returns ``None`` on success (run may proceed) or
        ``(error_message, error_code)`` to abort the run. PolicyEngine
        routing fires only when the rolling 30-day counter is exceeded —
        normal-cap bypasses are recorded silently.
        """
        if not bypass_requested:
            return None

        if not bypass_counter.is_bypass_over_limit(root_cwd):
            count = bypass_counter.record_bypass(root_cwd, run_id=run_id)
            try:
                audit_trail.append_audit_entry(run_dir, {
                    "type": "cross_provider_bypass",
                    "run_id": run_id,
                    "bypass_count_in_window": count,
                    "limit": TOOL_SI_BYPASS_LIMIT_PER_30_DAYS,
                    "policy_routed": False,
                })
            except (OSError, ValueError) as exc:
                logger.warning(
                    "scientific-investigation: audit append failed for "
                    "cross_provider_bypass: %s", exc,
                )
            return None

        # Over the cap → ask PolicyEngine for explicit user approval.
        try:
            from policy import get_engine
            engine = get_engine()
        except ImportError as exc:
            return (
                f"PolicyEngine-Modul nicht verfügbar — Bypass über Limit "
                f"({TOOL_SI_BYPASS_LIMIT_PER_30_DAYS}/30d) nicht entscheidbar: {exc}",
                "policy_unavailable",
            )

        reasons = [
            f"Cross-provider bypass over rolling limit "
            f"({TOOL_SI_BYPASS_LIMIT_PER_30_DAYS}/30d).",
            f"Run-ID: {run_id}",
        ]
        try:
            response = engine.request_approval(
                f"scientific-investigation #cross-provider:none for run {run_id}",
                reasons,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("policy.request_approval failed: %s", exc)
            response = "denied"

        # Always record the routing event in the audit trail, regardless of
        # outcome — the audit needs to see WHY a bypass was granted/denied.
        try:
            audit_trail.append_audit_entry(run_dir, {
                "type": "cross_provider_bypass",
                "run_id": run_id,
                "bypass_count_in_window": bypass_counter.recent_bypass_count(root_cwd),
                "limit": TOOL_SI_BYPASS_LIMIT_PER_30_DAYS,
                "policy_routed": True,
                "policy_response": response,
            })
        except (OSError, ValueError) as exc:
            logger.warning("audit append failed for policy-routed bypass: %s", exc)

        if response != "approved":
            return (
                f"#cross-provider:none Bypass von PolicyEngine nicht "
                f"freigegeben (Antwort={response}). Run {run_id} abgebrochen.",
                "policy_denied",
            )

        # Approved by PolicyEngine — record the bypass usage so it counts
        # against the next window.
        bypass_counter.record_bypass(root_cwd, run_id=run_id)
        return None

    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
        *,
        notify_threshold_callable=None,
        notify_discipline_warning_callable=None,
        provider_lookup=None,
        **kwargs,
    ) -> ToolResult:
        root_cwd = _resolve_root_cwd(cwd)
        tags = parse_tags(task)
        ts_slug_value = _ts_slug()
        run_id = tags.resume_run_id or str(uuid.uuid4())

        run_dir = build_run_dir(root_cwd, ts_slug_value)
        state_dir = build_state_dir(root_cwd, run_id)
        manifest_path = write_manifest(
            run_dir,
            run_id=run_id,
            task=task,
            provider_name=provider.name,
            root_cwd=root_cwd,
            tags=tags,
        )

        tracer = ToolTracer.create(self.name, str(run_dir))
        tracer.emit(
            "run_start",
            run_id=run_id,
            task=task[:200],
            provider=provider.name,
            tags=tags.as_audit_dict(),
            ts_slug=ts_slug_value,
            resume=bool(tags.resume_run_id),
        )

        # Cross-provider bypass: rate-limited at the Engineering-Layer level
        # (counter), then routed through the existing PolicyEngine when over
        # the rolling 30-day cap.
        bypass_outcome = self._handle_cross_provider_bypass(
            tags.cross_provider_none,
            run_dir=run_dir,
            root_cwd=root_cwd,
            run_id=run_id,
        )
        if bypass_outcome is not None:
            tracer.emit("run_end", success=False, reason=bypass_outcome[1])
            notify_tool_done(self.name, 0, False, bypass_outcome[0])
            return ToolResult(
                success=False, output="", iterations=0,
                error=bypass_outcome[0],
                error_code=bypass_outcome[1],
                retryable=False,
            )

        # ── Phase 0: Framing + Bias-Statement (+ Similarity) ────────────────
        tracer.emit("phase_start", phase="framing")
        try:
            framing = phase_framing(
                task,
                provider,
                run_dir=run_dir,
                root_cwd=root_cwd,
                run_id=run_id,
                timeout_sec=TOOL_SI_PHASE0_TIMEOUT_SEC,
            )
        except (RuntimeError, ValueError) as exc:
            msg = f"Phase 0 (Framing) fehlgeschlagen: {exc}"
            tracer.emit("run_end", success=False, reason="phase0_failed", error=str(exc))
            notify_tool_done(self.name, 0, False, msg)
            return ToolResult(
                success=False, output="", iterations=0, error=msg,
                error_code="phase0_failed", retryable=False,
            )
        tracer.emit(
            "phase_end", phase="framing",
            similarity_hits=len(framing.similarity_hits),
            discipline=framing.discipline,
        )

        # ── Phase 0.5: Pre-Registration ────────────────────────────────────
        tracer.emit("phase_start", phase="prereg")
        try:
            prereg = phase_prereg(
                framing,
                provider,
                run_dir=run_dir,
                run_id=run_id,
                timeout_sec=TOOL_SI_PHASE0_5_TIMEOUT_SEC,
                telegram_timeout_sec=TOOL_SI_TELEGRAM_APPROVAL_TIMEOUT_SEC,
                notify_callable=notify_threshold_callable,
                discipline_warning_callable=notify_discipline_warning_callable,
            )
        except (RuntimeError, ValueError) as exc:
            msg = f"Phase 0.5 (Pre-Registration) fehlgeschlagen: {exc}"
            tracer.emit("run_end", success=False, reason="phase05_failed", error=str(exc))
            notify_tool_done(self.name, 1, False, msg)
            return ToolResult(
                success=False, output="", iterations=1, error=msg,
                error_code="phase05_failed", retryable=False,
            )
        tracer.emit(
            "phase_end", phase="prereg",
            thresholds=len(prereg.thresholds),
            discipline_warning=prereg.discipline_warning,
            prereg_hash=prereg.prereg_hash,
        )

        # Persist plan.md (combines Phase 0 + Phase 0.5).
        plan_path = write_plan_md(
            run_dir, task=task, framing=framing, prereg=prereg,
        )

        # ── Phase 1: Persona-Allocation ────────────────────────────────────
        tracer.emit("phase_start", phase="persona_allocation")
        try:
            allocations = phase_persona_allocation(
                provider,
                run_dir=run_dir,
                run_id=run_id,
                cross_provider_none=tags.cross_provider_none,
                provider_lookup=provider_lookup,
            )
        except (RuntimeError, ValueError) as exc:
            msg = f"Phase 1 (Persona-Allocation) fehlgeschlagen: {exc}"
            tracer.emit("run_end", success=False, reason="phase1_failed", error=str(exc))
            notify_tool_done(self.name, 2, False, msg)
            return ToolResult(
                success=False, output="", iterations=2, error=msg,
                error_code="phase1_failed", retryable=False,
            )
        tracer.emit(
            "phase_end", phase="persona_allocation",
            personas=len(allocations),
            cross_provider_satisfied=sum(
                1 for a in allocations if a.cross_provider_satisfied
            ),
        )

        # ── Phase 2: Investigation-Plan + Multi-Persona-Review ─────────────
        tracer.emit("phase_start", phase="investigation_plan_review")
        try:
            phase2 = phase_investigation_plan_review(
                framing,
                prereg,
                allocations,
                provider,
                run_dir=run_dir,
                run_id=run_id,
                provider_lookup=provider_lookup,
            )
        except (RuntimeError, ValueError) as exc:
            msg = f"Phase 2 (Investigation-Plan-Review) fehlgeschlagen: {exc}"
            tracer.emit("run_end", success=False, reason="phase2_failed", error=str(exc))
            notify_tool_done(self.name, 3, False, msg)
            return ToolResult(
                success=False, output="", iterations=3, error=msg,
                error_code="phase2_failed", retryable=False,
            )
        tracer.emit(
            "phase_end", phase="investigation_plan_review",
            sub_tasks=len(phase2.plan.sub_tasks),
            iterations_used=phase2.iterations_used,
            converged=phase2.converged,
            open_findings=len(phase2.latest_findings()),
        )
        plan_md_path = write_investigation_plan_md(
            run_dir,
            plan=phase2.plan,
            converged=phase2.converged,
            iterations=phase2.iterations_used,
        )
        findings_md_path = write_review_findings_md(
            run_dir,
            findings_by_iteration=phase2.findings_by_iteration,
        )

        # ── Phase 6 (initial decision-log) ─────────────────────────────────
        # Phase 6 is normally run AFTER synthesis (Plan §1.1 ordering), but
        # the persona-allocation + cherry-picking + Phase-2-summary blocks
        # are stable from this point on, so we emit an early decision-log
        # skeleton that later phases append/overwrite.
        cherry_block = build_cherrypicking_block(
            root_cwd, framing_text=framing.framing_text, run_id=run_id,
        )
        persona_block = build_persona_allocation_block(allocations)
        phase2_summary = (
            f"- Iterationen: **{phase2.iterations_used}**\n"
            f"- Konvergiert: **{'ja' if phase2.converged else 'nein (cap erreicht, offene P1)'}**\n"
            f"- Sub-Tasks: **{len(phase2.plan.sub_tasks)}**\n"
            f"- Findings letzte Iteration: **{len(phase2.latest_findings())}**\n"
        )
        decision_log_path = write_decision_log(
            run_dir,
            run_id=run_id,
            sections=[
                ("Phase 1: Persona-Allocation", persona_block),
                ("Phase 2: Multi-Persona-Review Summary", phase2_summary),
                ("Phase 6: Cherry-Picking-Detection", cherry_block),
                ("Phase 7: Engineering-Reviewer-Findings",
                 "*(Phase 7 — wird in I7 ausgefüllt)*"),
                ("Phase 8: Investigation-Approval",
                 "*(Phase 8 — wird in I8 ausgefüllt)*"),
            ],
        )

        # Persist run state — used by I4+ phases on resume.
        state_file = state_dir / "state.json"
        atomic_write_state(state_file, {
            "run_id": run_id,
            "ts_slug": ts_slug_value,
            "phase": "phase2_review_done",
            "prereg_hash": prereg.prereg_hash,
            "discipline_warning": prereg.discipline_warning,
            "discipline_warning_approved": prereg.discipline_warning_approved,
            "rigor_cap": "LOW" if prereg.discipline_warning else None,
            "framing": framing.as_yaml_dict(),
            "thresholds": [
                {
                    "criterion_id": t.criterion_id,
                    "source": t.source,
                    "reference": t.reference,
                    "telegram_msg_id": t.telegram_msg_id,
                }
                for t in prereg.thresholds
            ],
            "personas": [a.as_audit_dict() for a in allocations],
            "cross_provider_satisfied_count": sum(
                1 for a in allocations if a.cross_provider_satisfied
            ),
            "phase2": {
                "iterations_used": phase2.iterations_used,
                "converged": phase2.converged,
                "sub_task_count": len(phase2.plan.sub_tasks),
                "open_p1_count": sum(
                    1 for f in phase2.latest_findings() if f.severity == "P1"
                ),
            },
        })

        # I3 boundary: Phases 3–5, 7, 8 land in I4–I8.
        scaffold_msg = (
            f"Scientific-Investigation I3 complete (Phases 0, 0.5, 1, 2, 6-init).\n"
            f"  run_id:        {run_id}\n"
            f"  plan.md:       {plan_path}\n"
            f"  invest_plan:   {plan_md_path}\n"
            f"  findings:      {findings_md_path}\n"
            f"  decision_log:  {decision_log_path}\n"
            f"  thresholds:    {len(prereg.thresholds)}\n"
            f"  prereg_hash:   {prereg.prereg_hash}\n"
            f"  personas:      {len(allocations)} "
            f"(cross-provider: "
            f"{sum(1 for a in allocations if a.cross_provider_satisfied)})\n"
            f"  sub_tasks:     {len(phase2.plan.sub_tasks)}\n"
            f"  phase2:        {phase2.iterations_used} iters, "
            f"converged={phase2.converged}\n"
            f"  rigor_cap:     {'LOW' if prereg.discipline_warning else '(none)'}\n"
            f"  similarity:    {len(framing.similarity_hits)} prior hits ≥ threshold\n"
            f"NOTE: Phases 3–5, 7, 8 land in increments I4–I8 (see plan v5)."
        )
        logger.info(scaffold_msg)
        tracer.emit("run_end", success=True, reason="i3_phase2_done")
        notify_tool_done(self.name, 3, True, "Phase 0, 0.5, 1, 2, 6-init abgeschlossen")
        return ToolResult(
            success=True,
            output=scaffold_msg,
            iterations=3,
            error="",
            error_code="i3_phase2_done",
            retryable=False,
        )
