"""
Dev-Loop Tool: Research → Execute → Dual-Review (Quality + Resolution) → Iterate.

Three-phase workflow:
  1. Research Agent:   Analyzes codebase, creates implementation plan, web-searches only if needed.
  2. Execution Agent:  Implements the solution (re-runs with review context on each iteration).
  3a. Quality Review:  Checks correctness, security, performance, maintainability, etc. (P1/P2/P3).
  3b. Resolution Review: Checks only "does the code solve the original task 100%?" (RESOLVED/PARTIAL/UNRESOLVED).

Loop continues until BOTH reviews pass. No auto-push.
Output is written to {cwd}/.dev-loop/ for traceability.

Usage in queue:
    - [ ] Fix login bug in auth.py #tool:dev-loop cwd:/d/programmieren/projekt
    - [ ] Add CSV export to dashboard #tool:dev-loop cwd:/d/programmieren/projekt
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

from config import (
    TOOL_DEV_EXEC_TIMEOUT_SEC,
    TOOL_DEV_PLAN_TIMEOUT_SEC,
    TOOL_DEV_QUALITY_REVIEW_TIMEOUT_SEC,
    TOOL_DEV_RESEARCH_TIMEOUT_SEC,
    TOOL_DEV_RESOLUTION_REVIEW_TIMEOUT_SEC,
    TOOL_INTER_STEP_SLEEP_SEC,
    TOOL_MAX_ITERATIONS,
)
from limits import is_cached_provider_available
from notifier import notify_tool_done, notify_tool_progress
from providers.base import BaseProvider
from tools.base_tool import BaseTool, ToolResult, _build_system_prompt, _make_capacity_exhausted_result, _write_tool_file
from tools.review_loop import _is_no_findings_output, _parse_findings

DEV_LOOP_DIR = ".dev-loop"
_STATE_FILE = "state.json"


def _task_hash(task: str) -> str:
    return hashlib.sha256(task.encode()).hexdigest()[:8]


def _load_state(cwd: str, task_hash: str) -> "dict | None":
    """Load persisted research state if it matches the current task."""
    path = os.path.join(cwd, DEV_LOOP_DIR, _STATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("task_hash") == task_hash and state.get("tool") == "dev-loop":
            return state
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _save_state(cwd: str, state: dict) -> None:
    """Persist state to .dev-loop/state.json (creates dir if needed)."""
    dir_path = os.path.join(cwd, DEV_LOOP_DIR)
    os.makedirs(dir_path, exist_ok=True)
    path = os.path.join(dir_path, _STATE_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _clear_state(cwd: str) -> None:
    """Remove persisted state after successful completion."""
    path = os.path.join(cwd, DEV_LOOP_DIR, _STATE_FILE)
    try:
        os.remove(path)
    except OSError:
        pass


# Resolution review output patterns
_RESOLVED_RE = re.compile(r"^\s*RESOLVED\s*:", re.IGNORECASE | re.MULTILINE)
_PARTIAL_RE = re.compile(r"^\s*PARTIAL\s*:", re.IGNORECASE | re.MULTILINE)
_UNRESOLVED_RE = re.compile(r"^\s*UNRESOLVED\s*:", re.IGNORECASE | re.MULTILINE)


def _parse_resolution(text: str) -> str:
    """Return 'RESOLVED', 'PARTIAL', 'UNRESOLVED', or 'UNKNOWN'.

    Uses earliest-match logic so that e.g. a 'PARTIAL:' on line 1
    wins over a 'RESOLVED:' mentioned on a later line.
    """
    best_label, best_pos = "UNKNOWN", len(text) + 1
    for label, regex in [
        ("RESOLVED", _RESOLVED_RE),
        ("PARTIAL", _PARTIAL_RE),
        ("UNRESOLVED", _UNRESOLVED_RE),
    ]:
        m = regex.search(text)
        if m and m.start() < best_pos:
            best_label, best_pos = label, m.start()
    return best_label



# ── Prompts ──────────────────────────────────────────────────────────────────

_RESEARCH_PROMPT = """\
You are a Research Agent. Analyze the codebase to understand what needs to be \
done for the following task.

TASK: {task}

Steps:
1. Explore relevant files (git status, directory listing, read key files).
2. Understand the current code structure and the root cause of the issue or \
the requirements for the new feature.
3. Search the web ONLY if you cannot determine required library APIs, \
error meanings, or documentation from the local codebase alone.
4. Identify all files that need to be changed or created.

Output format (required sections):

## Problem Analysis
[Clear description of the issue / feature to build]

## Relevant Files
[Files that need to be changed or created, with brief reason for each]

## Implementation Plan
[Concrete step-by-step plan for the Execution Agent]

## Dependencies & Edge Cases
[External libs, API changes, error handling concerns, edge cases]
"""

_PLAN_PROMPT = """\
You are a Planning Agent. Based on the research findings, create a concrete \
implementation plan.

ORIGINAL TASK: {task}

RESEARCH FINDINGS:
{research}

Create a plan with these sections:

## Implementation Plan
1. Files to create or modify (exact paths)
2. Changes per file (brief description of what to add/modify/remove)
3. Order of operations (which changes first)

## Test Strategy
- How to verify the changes work
- Which tests to run or create

## Risks & Edge Cases
- What could go wrong
- Edge cases to handle
"""

_EXECUTION_PROMPT = """\
You are an Execution Agent. Implement the following task based on the research \
findings and implementation plan below.

ORIGINAL TASK: {task}

RESEARCH FINDINGS:
{research}

IMPLEMENTATION PLAN:
{plan}
{review_context}
Instructions:
- Implement the solution exactly as laid out in the implementation plan.
- Fix ALL issues raised in previous reviews (listed above, if any).
- Apply changes directly to the files.
- Run existing tests if feasible.
- Do NOT commit, push, or deploy.
- Summarize what you changed at the end.
"""

_QUALITY_REVIEW_PROMPT = """\
You are a Code Quality Review Agent. Review ONLY the uncommitted changes in \
the current git working tree.

ORIGINAL TASK: {task}

Review these aspects:
- Correctness: Does the code do what it's supposed to?
- Clean: Readable, well-structured, no dead code or commented-out blocks?
- Secure: No injection vulnerabilities, hardcoded secrets, unsafe operations?
- Performant: No obvious bottlenecks or unnecessary operations?
- Maintainable: Good abstractions, clear naming, no magic values?
- Testable: Tests included or updated where appropriate?
- Robust: Handles edge cases, errors, and unexpected inputs?
- Documented: Public APIs have docstrings where appropriate?
- Compliant: Follows existing project conventions and style?

Inspect changes via: git diff, git status --porcelain, \
git ls-files --others --exclude-standard.
Do NOT modify any files.
Ignore the `.dev-loop/` directory — it contains tool metadata.

Output format (strict):
- One bullet per finding: `- [P1] ...`, `- [P2] ...`, `- [P3] ...`
- P1 = critical / crash / security, P2 = significant issue, P3 = minor / style
- If no findings at all: output exactly: `No P1/P2/P3 findings.`
"""

_RESOLUTION_REVIEW_PROMPT = """\
You are an Issue Resolution Review Agent.
Your ONLY job: determine whether the uncommitted code changes fully solve the \
original task.

ORIGINAL TASK: {task}

Instructions:
- Use git diff / git status to inspect what was actually changed.
- Focus ONLY on whether the task requirements are fully met.
- Do NOT evaluate code quality — that is handled by a separate agent.
- Do NOT modify any files.
- Ignore the `.dev-loop/` directory — it contains tool metadata.

Output format (strict — begin your response with exactly one of these):

RESOLVED: [brief explanation of how the task is fully solved]
PARTIAL: [what is done and what is still missing]
UNRESOLVED: [what was attempted and why it does not solve the task]
"""


class DevLoopTool(BaseTool):
    name = "dev-loop"
    description = (
        "Research → Execute → Dual-Review Loop "
        "(Code Quality + Issue Resolution) bis beide Reviews grünes Licht geben"
    )

    def _get_plan_approval_mode(self) -> str:
        """Check policy.yaml for plan approval mode: auto | approve | skip."""
        try:
            from policy import load_policy
            policy = load_policy()
            phases = policy.get("tool_phases", {}).get("dev-loop", {})
            return phases.get("plan_approval", "auto")
        except (ImportError, OSError, ValueError):
            return "auto"

    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
        **kwargs,
    ) -> ToolResult:
        print(f"  [dev-loop] Starte Dev-Loop (max {TOOL_MAX_ITERATIONS} Iterationen)")

        dev_loop_dir = Path(cwd or ".") / DEV_LOOP_DIR
        system_prompt = _build_system_prompt(provider.name, memory_context, tool_name=self.name)
        all_outputs: list[str] = []
        seen_quality_signatures: set[tuple[str, ...]] = set()
        last_quality_tuple: tuple[str, ...] = ()
        seen_review_signatures: set[tuple[tuple[str, ...], str, str]] = set()

        research_timeout = timeout or TOOL_DEV_RESEARCH_TIMEOUT_SEC
        plan_timeout = timeout or TOOL_DEV_PLAN_TIMEOUT_SEC
        exec_timeout = timeout or TOOL_DEV_EXEC_TIMEOUT_SEC
        quality_timeout = timeout or TOOL_DEV_QUALITY_REVIEW_TIMEOUT_SEC
        resolution_timeout = timeout or TOOL_DEV_RESOLUTION_REVIEW_TIMEOUT_SEC

        total_input_tokens = 0
        total_output_tokens = 0

        # Load memory module for lessons
        try:
            import memory as memory_module
        except (ImportError, OSError):
            memory_module = None

        # ── Phase 1: Research ─────────────────────────────────────────────────
        t_hash = _task_hash(task)
        cached_state = _load_state(cwd, t_hash) if cwd else None

        if cached_state and cached_state.get("phase") == "research_done":
            print(f"  [dev-loop] Research aus Cache geladen (task_hash={t_hash})")
            research_output = cached_state["research_output"]
            all_outputs.append(f"--- Research (cached) ---\n{research_output}")
        else:
            # Capacity guard before expensive research phase
            if not is_cached_provider_available(provider.name):
                msg = "Provider nicht verfügbar — Suspend vor Research-Phase"
                print(f"  [dev-loop] ⏸ {msg}")
                return _make_capacity_exhausted_result(msg, "", 0, total_input_tokens, total_output_tokens)

            print("  [dev-loop] === Phase 1: RESEARCH ===")
            research_prompt = system_prompt + "\n\n" + _RESEARCH_PROMPT.format(task=task)
            research_result = provider.run(research_prompt, cwd=cwd, timeout=research_timeout)
            total_input_tokens += research_result.input_tokens
            total_output_tokens += research_result.output_tokens

            if not research_result.success:
                msg = f"Research fehlgeschlagen: {research_result.error}"
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, 0, False, msg)
                return ToolResult(
                    success=False,
                    output="",
                    iterations=0,
                    error=msg,
                    error_code=research_result.error,
                    retryable=True,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

            research_output = research_result.output.strip()
            all_outputs.append(f"--- Research ---\n{research_output}")
            _write_tool_file(
                dev_loop_dir,
                "research.md",
                f"# Dev-Loop Research\n\nTask: {task}\n\n{research_output}\n",
            )
            print(f"  [dev-loop] Research abgeschlossen → {dev_loop_dir / 'research.md'}")

            # Persist research so a capacity-exhausted retry can skip this phase
            if cwd:
                _save_state(cwd, {
                    "tool": "dev-loop",
                    "task_hash": t_hash,
                    "phase": "research_done",
                    "research_output": research_output,
                })

        # ── Phase 1.5: Plan ───────────────────────────────────────────────────
        plan_approval_mode = self._get_plan_approval_mode()
        plan_output = ""

        if plan_approval_mode != "skip":
            print("  [dev-loop] === Phase 1.5: PLAN ===")
            plan_prompt = system_prompt + "\n\n" + _PLAN_PROMPT.format(
                task=task,
                research=research_output,
            )
            plan_result = provider.run(plan_prompt, cwd=cwd, timeout=plan_timeout)
            total_input_tokens += plan_result.input_tokens
            total_output_tokens += plan_result.output_tokens

            if not plan_result.success:
                msg = f"Plan-Phase fehlgeschlagen: {plan_result.error}"
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, 0, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=0,
                    error=msg,
                    error_code=plan_result.error,
                    retryable=True,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

            plan_output = plan_result.output.strip()
            all_outputs.append(f"--- Plan ---\n{plan_output}")
            _write_tool_file(
                dev_loop_dir,
                "plan.md",
                f"# Dev-Loop Plan\n\nTask: {task}\n\n{plan_output}\n",
            )
            print(f"  [dev-loop] Plan erstellt → {dev_loop_dir / 'plan.md'}")

            # Telegram approval if configured
            if plan_approval_mode == "approve":
                try:
                    from notifier import request_approval
                    approved = request_approval(
                        f"Dev-Loop Plan fuer: {task[:100]}\n\n{plan_output[:500]}",
                        timeout=600,
                    )
                    if not approved:
                        msg = "Plan wurde via Telegram abgelehnt."
                        print(f"  [dev-loop] {msg}")
                        notify_tool_done(self.name, 0, False, msg)
                        return ToolResult(
                            success=False,
                            output="\n\n".join(all_outputs),
                            iterations=0,
                            error=msg,
                            input_tokens=total_input_tokens,
                            output_tokens=total_output_tokens,
                        )
                except (ImportError, OSError, ValueError) as exc:
                    print(f"  [dev-loop] Plan-Approval uebersprungen (Fehler: {exc})")

            time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

        # ── Phase 2+3: Execute → Dual-Review → Iterate ───────────────────────
        previous_quality_findings: list[str] = []
        previous_resolution_output: str = ""

        for iteration in range(1, TOOL_MAX_ITERATIONS + 1):
            print(f"\n  [dev-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: EXECUTION ===")

            # Capacity guard: abort loop if provider is below threshold (RAM-cache, no API call)
            if not is_cached_provider_available(provider.name):
                msg = f"Provider nicht verfügbar — Suspend nach Iteration {iteration - 1}"
                print(f"  [dev-loop] ⏸ {msg}")
                return _make_capacity_exhausted_result(msg, "\n\n".join(all_outputs), iteration - 1, total_input_tokens, total_output_tokens)

            # Build review context for execution prompt (empty on first iteration)
            review_context = ""
            if previous_quality_findings or previous_resolution_output:
                parts: list[str] = []
                if previous_quality_findings:
                    parts.append(
                        f"QUALITY REVIEW (Iteration {iteration - 1}):\n"
                        + "\n".join(previous_quality_findings)
                    )
                if previous_resolution_output:
                    parts.append(
                        f"RESOLUTION REVIEW (Iteration {iteration - 1}):\n"
                        + previous_resolution_output
                    )
                review_context = (
                    "\nPREVIOUS REVIEWS — fix all issues listed here:\n\n"
                    + "\n\n".join(parts)
                    + "\n"
                )

            # Search lessons for hints related to current review findings
            if previous_quality_findings and memory_module is not None:
                try:
                    findings_text = "\n".join(previous_quality_findings)
                    hint = memory_module.search_lessons(findings_text)
                    if hint:
                        review_context += (
                            f"\nLESSONS FROM PREVIOUS SIMILAR ISSUES:\n{hint}\n"
                        )
                except (ImportError, OSError, ValueError):
                    pass

            exec_prompt = system_prompt + "\n\n" + _EXECUTION_PROMPT.format(
                task=task,
                research=research_output,
                plan=plan_output or "(no plan generated)",
                review_context=review_context,
            )
            notify_tool_progress(
                self.name, iteration, TOOL_MAX_ITERATIONS, "Implementierung läuft..."
            )
            exec_result = provider.run(exec_prompt, cwd=cwd, timeout=exec_timeout)
            total_input_tokens += exec_result.input_tokens
            total_output_tokens += exec_result.output_tokens

            if not exec_result.success:
                msg = f"Execution fehlgeschlagen in Iteration {iteration}: {exec_result.error}"
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    error_code=exec_result.error,
                    retryable=True,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

            exec_output = exec_result.output.strip()
            all_outputs.append(f"--- Execution {iteration} ---\n{exec_output}")
            time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

            # ── Phase 3a: Code Quality Review ────────────────────────────────
            print(f"  [dev-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: QUALITY REVIEW ===")
            quality_prompt = system_prompt + "\n\n" + _QUALITY_REVIEW_PROMPT.format(task=task)
            quality_result = provider.run(quality_prompt, cwd=cwd, timeout=quality_timeout)
            total_input_tokens += quality_result.input_tokens
            total_output_tokens += quality_result.output_tokens

            if not quality_result.success:
                msg = f"Quality-Review fehlgeschlagen in Iteration {iteration}: {quality_result.error}"
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    error_code=quality_result.error,
                    retryable=True,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

            quality_output = quality_result.output.strip()
            all_outputs.append(f"--- Quality Review {iteration} ---\n{quality_output}")
            quality_findings = _parse_findings(quality_output)
            no_quality_findings = _is_no_findings_output(quality_output)
            if not quality_findings and not no_quality_findings:
                msg = (
                    "Quality-Review-Output entspricht nicht dem erwarteten Format "
                    "(keine P1/P2/P3 Findings und kein 'No P1/P2/P3 findings.')."
                )
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )
            # P3-only findings are non-blocking; only P1/P2 block progress
            blocking_findings = [f for f in quality_findings if not f.startswith("- [P3]")]
            quality_ok = no_quality_findings or not blocking_findings
            time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

            # ── Phase 3b: Resolution Review ───────────────────────────────────
            print(f"  [dev-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: RESOLUTION REVIEW ===")
            resolution_prompt = system_prompt + "\n\n" + _RESOLUTION_REVIEW_PROMPT.format(task=task)
            resolution_result = provider.run(resolution_prompt, cwd=cwd, timeout=resolution_timeout)
            total_input_tokens += resolution_result.input_tokens
            total_output_tokens += resolution_result.output_tokens

            if not resolution_result.success:
                msg = f"Resolution-Review fehlgeschlagen in Iteration {iteration}: {resolution_result.error}"
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    error_code=resolution_result.error,
                    retryable=True,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

            resolution_output = resolution_result.output.strip()
            all_outputs.append(f"--- Resolution Review {iteration} ---\n{resolution_output}")
            resolution_status = _parse_resolution(resolution_output)
            if resolution_status == "UNKNOWN":
                msg = (
                    "Resolution-Review-Output entspricht nicht dem erwarteten Format "
                    "(erwartet: RESOLVED/PARTIAL/UNRESOLVED)."
                )
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )
            resolution_ok = resolution_status == "RESOLVED"

            # ── Write round file ──────────────────────────────────────────────
            _write_tool_file(
                dev_loop_dir,
                f"round-{iteration:03d}.md",
                (
                    f"# Dev-Loop Round {iteration}\n\n"
                    f"## Task\n{task}\n\n"
                    f"## Execution Summary\n{exec_output}\n\n"
                    f"## Quality Review\n{quality_output}\n\n"
                    f"## Resolution Review\n{resolution_output}\n"
                ),
            )

            quality_label = "OK" if quality_ok else f"{len(blocking_findings)} blocking findings"
            print(
                f"  [dev-loop] Quality: {quality_label} | Resolution: {resolution_status}"
            )
            for f in blocking_findings[:3]:
                print(f"    {f}")

            # ── Both pass → done ──────────────────────────────────────────────
            if quality_ok and resolution_ok:
                msg = (
                    f"Beide Reviews bestanden nach {iteration} Iteration(en). "
                    "Bereit fuer deinen Review + Push."
                )
                print(f"  [dev-loop] {msg}")
                _write_tool_file(
                    dev_loop_dir,
                    "summary.md",
                    (
                        f"# Dev-Loop Abgeschlossen\n\n"
                        f"Task: {task}\n\n"
                        f"Iterationen: {iteration}\n\n"
                        f"Status: DONE — bereit fuer Review + Git Push\n"
                    ),
                )

                # Auto-lesson — DEAKTIVIERT
                # Speichern von "letzten Findings" ist nicht sinnvoll: die Einträge sind
                # projektspezifisch und nach Code-Änderungen veraltet. Eine echte Lesson
                # bräuchte eine LLM-Summary über alle Iterationen (Muster, Root Cause).
                # TODO: Neu implementieren mit LLM-generierter Summary aus all_outputs.

                notify_tool_done(self.name, iteration, True, msg)
                if cwd:
                    _clear_state(cwd)
                return ToolResult(
                    success=True,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

            # ── Infinite loop detection (same quality findings twice) ──────────
            if blocking_findings:
                sig = tuple(sorted(blocking_findings))
                if sig in seen_quality_signatures:
                    msg = (
                        f"Quality-Findings wiederholen sich nach {iteration} Iterationen. "
                        "Loop abgebrochen."
                    )
                    print(f"  [dev-loop] {msg}")
                    notify_tool_done(self.name, iteration, False, msg)
                    return ToolResult(
                        success=False,
                        output="\n\n".join(all_outputs),
                        iterations=iteration,
                        error=msg,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                    )
                seen_quality_signatures.add(sig)
                last_quality_tuple = sig

            review_sig = (tuple(sorted(blocking_findings)), resolution_status, resolution_output)
            if review_sig in seen_review_signatures:
                msg = (
                    f"Review-Ergebnis wiederholt sich nach {iteration} Iterationen. "
                    "Loop abgebrochen."
                )
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )
            seen_review_signatures.add(review_sig)

            # Store context for next execution
            previous_quality_findings = blocking_findings
            previous_resolution_output = resolution_output if not resolution_ok else ""
            time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

        # Max iterations reached
        msg = f"Max Iterationen ({TOOL_MAX_ITERATIONS}) erreicht. Reviews noch nicht vollstaendig bestanden."
        print(f"  [dev-loop] {msg}")
        notify_tool_done(self.name, TOOL_MAX_ITERATIONS, False, msg)
        return ToolResult(
            success=False,
            output="\n\n".join(all_outputs),
            iterations=TOOL_MAX_ITERATIONS,
            error=msg,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
        )
