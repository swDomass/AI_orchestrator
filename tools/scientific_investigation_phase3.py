"""Phase 3 — Execution-Loop (Plan §1.1, §2.3, I4).

For each Sub-Task in the InvestigationPlan:

  1. Build the sub-state-CWD (``{root_cwd}/.scientific-investigation/{run_id}/sub-tasks/{sub_id}/``).
  2. Build env with ``PYTHONPATH=root_cwd`` so the sub-tool's CLI subprocess
     can import the project's source modules even though its CWD is the
     state directory.
  3. Run the Sub-Task via an injected executor (default: DevLoopTool).
  4. Scan the project's ``tests/`` directory for new crosscheck files
     (``crosscheck_*.py``) and classify each as T2 or T3 via the
     externality classifier.
  5. For ``literature_search`` Sub-Tasks: also kick off the adversarial-
     citation-search with diversity + tool-call audit.

Returns a ``Phase3Result`` summarising per-Sub-Task outcomes, the list of
crosscheck files with their tiers, and the adversarial-search reports.
The caller writes the corresponding markdown reports (
``write_execution_report_md``) and updates the audit + state files.

Why injectable executors
-------------------------
DevLoopTool is the real production sub-task runner, but tests want
hermetic behaviour and exact control over what the sub-task produces. The
``sub_task_executor`` callable lets tests script outputs (success/fail,
crosscheck file contents) without touching the real dev-loop subprocess
chain. Same pattern as the persona/provider lookup in Phase 2.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from config import TOOL_SI_SUBTASK_TIMEOUT_SEC
from providers.base import BaseProvider
from tools.crosschecks import adversarial_search, audit_trail
from tools.crosschecks.adversarial_search import (
    AdversarialSearchReport,
    SearchExecutor,
    SearchQuery,
)
from tools.crosschecks.externality_classifier import (
    Tier,
    classify_crosscheck_tier,
)
from tools.scientific_investigation_phase2 import InvestigationPlan, SubTask
from tools.sub_tool_context import build_sub_env, ensure_sub_state_dir

logger = logging.getLogger(__name__)

# Default glob for files the classifier considers crosschecks.
CROSSCHECK_GLOB = "tests/crosscheck_*.py"


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass
class CrosscheckFile:
    """A crosscheck source file produced (or already present) for a Sub-Task."""
    path: Path  # absolute path
    sub_id: str
    tier: Tier  # T2 or T3 (classifier output)


@dataclass
class SubTaskResult:
    sub_task: SubTask
    success: bool
    output: str
    error: str = ""
    error_code: str = ""
    duration_sec: float = 0.0
    crosscheck_files: list[CrosscheckFile] = field(default_factory=list)
    adversarial: AdversarialSearchReport | None = None

    def as_audit_dict(self) -> dict:
        return {
            "sub_id": self.sub_task.sub_id,
            "title": self.sub_task.title,
            "type": self.sub_task.type,
            "success": self.success,
            "error_code": self.error_code,
            "duration_sec": round(self.duration_sec, 2),
            "crosscheck_count": len(self.crosscheck_files),
            "crosscheck_tiers": [c.tier for c in self.crosscheck_files],
            "adversarial_pass": (
                self.adversarial.overall_pass() if self.adversarial else None
            ),
        }


@dataclass
class Phase3Result:
    sub_task_results: list[SubTaskResult]
    total_duration_sec: float
    crosscheck_tiers_per_subtask: dict[str, list[Tier]]

    def all_successful(self) -> bool:
        return all(r.success for r in self.sub_task_results)

    def at_least_one_t2_per_subtask(self) -> bool:
        """K5 condition: every sub-task needs at least one T2 crosscheck."""
        if not self.sub_task_results:
            return False
        return all(
            "T2" in tiers
            for tiers in self.crosscheck_tiers_per_subtask.values()
        )


# ── Sub-Task Executor Protocol ────────────────────────────────────────────


SubTaskExecutor = Callable[..., SubTaskResult]
"""Signature: (sub_task, sub_state_cwd, env, provider, timeout) -> SubTaskResult.

Defaults to ``default_devloop_executor`` which wraps DevLoopTool. Tests
can pass any callable that returns a SubTaskResult.
"""


def default_devloop_executor(
    sub_task: SubTask,
    *,
    sub_state_cwd: Path,
    env: dict[str, str],
    provider: BaseProvider,
    timeout: int,
) -> SubTaskResult:
    """Real executor — invokes DevLoopTool with the Sub-CWD + PYTHONPATH.

    Imported lazily so test runs that mock-inject their own executor never
    pull DevLoopTool's import side-effects.
    """
    from tools.dev_loop import DevLoopTool
    started = time.time()
    # DevLoopTool's run() does not accept an env override (today), so we
    # set the env vars on the current process for the duration of the call.
    # This is acceptable because Phase 3 runs sub-tasks sequentially and
    # the env additions are idempotent (PYTHONPATH is rebuilt every iter).
    import os as _os
    prior_env = {k: _os.environ.get(k) for k in env if k not in _os.environ}
    try:
        for key, value in env.items():
            _os.environ[key] = value
        result = DevLoopTool().run(
            sub_task.description,
            provider,
            cwd=str(sub_state_cwd),
            timeout=timeout,
        )
    finally:
        for key, value in prior_env.items():
            if value is None:
                _os.environ.pop(key, None)
            else:
                _os.environ[key] = value
    return SubTaskResult(
        sub_task=sub_task,
        success=getattr(result, "success", False),
        output=getattr(result, "output", "") or "",
        error=getattr(result, "error", ""),
        error_code=getattr(result, "error_code", ""),
        duration_sec=time.time() - started,
    )


# ── Crosscheck discovery ───────────────────────────────────────────────────


def discover_crosscheck_files(
    root_cwd: Path,
    sub_id: str,
    run_dir: Path,
    *,
    glob_pattern: str = CROSSCHECK_GLOB,
) -> list[CrosscheckFile]:
    """Discover crosscheck source files in the project tree and classify each."""
    out: list[CrosscheckFile] = []
    for path in sorted(root_cwd.glob(glob_pattern)):
        if not path.is_file():
            continue
        tier = classify_crosscheck_tier(path, run_dir)
        out.append(CrosscheckFile(path=path, sub_id=sub_id, tier=tier))
    return out


# ── Phase 3 main loop ─────────────────────────────────────────────────────


def phase_execution_loop(
    investigation_plan: InvestigationPlan,
    primary_provider: BaseProvider,
    *,
    run_dir: Path,
    root_cwd: Path,
    run_id: str,
    sub_task_executor: SubTaskExecutor | None = None,
    adversarial_query_generator: Optional[Callable[[SubTask], list[SearchQuery]]] = None,
    adversarial_search_executor: Optional[SearchExecutor] = None,
    timeout_per_sub_task: int | None = None,
) -> Phase3Result:
    """Run all Sub-Tasks. Each Sub-Task is independent — we DON'T abort the
    loop on the first failure; instead the per-task ``SubTaskResult`` carries
    the error and the caller (Phase 4 synthesis) decides what to do.

    Adversarial-citation-search is invoked only for ``type == 'literature_search'``
    Sub-Tasks AND only when both a query generator and a search executor
    are provided. Today (I4) both have to come from the caller — there is
    no default LLM-driven generator yet; that's left for a follow-up that
    can budget tokens accordingly.
    """
    if sub_task_executor is None:
        sub_task_executor = default_devloop_executor
    if timeout_per_sub_task is None:
        timeout_per_sub_task = TOOL_SI_SUBTASK_TIMEOUT_SEC

    started = time.time()
    results: list[SubTaskResult] = []
    crosscheck_tiers: dict[str, list[Tier]] = {}

    for sub_task in investigation_plan.sub_tasks:
        sub_state_cwd = ensure_sub_state_dir(root_cwd, run_id, sub_task.sub_id)
        env = build_sub_env(root_cwd)
        try:
            sub_result = sub_task_executor(
                sub_task,
                sub_state_cwd=sub_state_cwd,
                env=env,
                provider=primary_provider,
                timeout=timeout_per_sub_task,
            )
        except Exception as exc:  # pragma: no cover  — defensive
            logger.exception(
                "phase_execution_loop: executor raised for %s", sub_task.sub_id,
            )
            sub_result = SubTaskResult(
                sub_task=sub_task,
                success=False,
                output="",
                error=f"executor exception: {exc}",
                error_code="executor_exception",
            )

        # Discover + classify crosscheck files (always — regardless of
        # success, because partial executions may still have written code).
        sub_result.crosscheck_files = discover_crosscheck_files(
            root_cwd, sub_task.sub_id, run_dir,
        )
        crosscheck_tiers[sub_task.sub_id] = [
            c.tier for c in sub_result.crosscheck_files
        ]

        # Adversarial search (literature_search sub-tasks only, if injected).
        if (
            sub_task.type == "literature_search"
            and adversarial_query_generator is not None
            and adversarial_search_executor is not None
        ):
            queries = adversarial_query_generator(sub_task)
            sub_result.adversarial = adversarial_search.run_adversarial_search(
                queries,
                claim_id=f"{run_id}::{sub_task.sub_id}",
                run_dir=run_dir,
                search_executor=adversarial_search_executor,
            )

        # Audit entry per sub-task (success/failure + tier summary).
        try:
            audit_trail.append_audit_entry(run_dir, {
                "type": "execution_sub_task",
                "run_id": run_id,
                "provider": primary_provider.name,
                "summary": sub_result.as_audit_dict(),
            })
        except (OSError, ValueError) as exc:
            logger.warning(
                "Phase 3: audit append failed for sub_task %s: %s",
                sub_task.sub_id, exc,
            )

        results.append(sub_result)

    return Phase3Result(
        sub_task_results=results,
        total_duration_sec=time.time() - started,
        crosscheck_tiers_per_subtask=crosscheck_tiers,
    )


# ── Output writer ──────────────────────────────────────────────────────────


def write_execution_report_md(
    run_dir: Path,
    *,
    phase3: Phase3Result,
) -> Path:
    """Render execution_report.md summarising each Sub-Task's outcome."""
    rows: list[str] = ["# Phase 3 — Execution Report\n"]
    rows.append(
        f"**Sub-Tasks gesamt:** {len(phase3.sub_task_results)}  \n"
        f"**Erfolgreich:** "
        f"{sum(1 for r in phase3.sub_task_results if r.success)}  \n"
        f"**Gesamtdauer:** {phase3.total_duration_sec:.1f}s  \n"
        f"**Mindestens 1×T2 pro Sub-Task:** "
        f"{'✅' if phase3.at_least_one_t2_per_subtask() else '❌'}\n"
    )
    rows.append("")
    rows.append("## Per Sub-Task")
    rows.append("")
    for r in phase3.sub_task_results:
        mark = "✅" if r.success else "❌"
        rows.append(f"### {mark} {r.sub_task.sub_id}: {r.sub_task.title}")
        rows.append(f"- **Typ:** `{r.sub_task.type}`")
        rows.append(f"- **Dauer:** {r.duration_sec:.1f}s")
        rows.append(
            f"- **Crosschecks:** {len(r.crosscheck_files)} "
            f"(Tiers: {','.join(c.tier for c in r.crosscheck_files) or '—'})"
        )
        if r.adversarial is not None:
            rows.append(
                f"- **Adversarial-Search:** {len(r.adversarial.queries)} queries, "
                f"diversity_pass={r.adversarial.diversity.pass_}, "
                f"tool_call_pass={r.adversarial.tool_calls.pass_}"
            )
        if not r.success:
            rows.append(f"- **Error:** `{r.error_code}` — {r.error[:200]}")
        rows.append("")
    out = run_dir / "execution_report.md"
    out.write_text("\n".join(rows), encoding="utf-8")
    return out
