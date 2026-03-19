"""
Review-Loop Tool: Iterative Review → Fix → Re-Review until clean.

Inspired by codex_p1_review_loop.py but works with any provider (Claude, Gemini, Codex).

Usage in queue:
    - [ ] Review und fixe Bugs in main.py #tool:review-loop cwd:/d/programmieren/projekt
    - [ ] Review uncommitted changes #tool:review-loop #codex cwd:/d/programmieren/projekt
"""

import re
import time

from config import (
    TOOL_MAX_ITERATIONS,
    TOOL_REVIEW_TIMEOUT_SEC,
    TOOL_FIX_TIMEOUT_SEC,
    TOOL_INTER_STEP_SLEEP_SEC,
    TOOL_VERIFICATION_TIMEOUT_SEC,
)
from limits import is_cached_provider_available
from notifier import notify_tool_progress, notify_tool_done
from providers.base import BaseProvider
from tools.base_tool import BaseTool, ToolResult, _build_system_prompt, _make_capacity_exhausted_result

# Matches priority findings like: - [P1] Some issue
FINDING_RE = re.compile(r"^\s*-\s+\[P[1-3]\]\s+.+", re.MULTILINE)
# Common fallback formats from providers (e.g. "1. `P2` Something...")
ALT_FINDING_RE = re.compile(
    r"^\s*(?:[-*]|\d+[.)])\s*(?:`|\*\*)?\[?(P[1-3])\]?(?:`|\*\*)?(?:\s*[:\-]\s*|\s+)(.+)$",
    re.IGNORECASE,
)
NO_FINDINGS_RE = re.compile(
    r"^\s*(?:`|\*\*)?\s*No\s+P1(?:\s*(?:/|,|and|or)\s*P2)(?:\s*(?:/|,|and|or)\s*P3)\s+findings(?:\s+found)?\.?\s*(?:`|\*\*)?\s*$",
    re.IGNORECASE,
)


def _parse_findings(text: str) -> list[str]:
    """Extract P1/P2/P3 findings from review output."""
    findings: list[str] = []
    for line in text.splitlines():
        if FINDING_RE.match(line):
            findings.append(line.strip())
            continue
        match = ALT_FINDING_RE.match(line)
        if match:
            severity, body = match.groups()
            findings.append(f"- [{severity.upper()}] {body.strip()}")
    return findings


def _is_no_findings_output(text: str) -> bool:
    """Accept the exact sentinel and trivial markdown-wrapped variants."""
    return any(NO_FINDINGS_RE.match(line.strip()) for line in text.splitlines())


_REVIEW_PROMPT_BODY = """
Perform a code review of UNCOMMITTED changes in the current git working tree.

Rules:
- Review only files affected by uncommitted changes (tracked and untracked).
- If needed, inspect changes via git commands (`git diff`, `git status --porcelain`, `git ls-files --others --exclude-standard`).
- Focus on correctness, bugs, security, and crashes.
- Include file paths and line numbers.
- Do NOT modify any files.

Output format (strict):
- One bullet per finding: `- [P1] ...`, `- [P2] ...`, `- [P3] ...`
- P1 = critical/crash, P2 = significant bug, P3 = minor issue
- If no findings (or no uncommitted changes): output exactly: `No P1/P2/P3 findings.`
"""

_FIX_PROMPT_BODY = """
You are fixing issues found by a code review (iteration {iteration}).

Task:
- Fix ALL P1, P2, and P3 issues listed below.
- Apply changes directly to the files.
- Run validation/tests if feasible.
- Summarize what was fixed.

Review findings:
{findings}
{lessons_hint}"""

_VERIFICATION_PROMPT_BODY = """
Final verification of the review/fix cycle.

Steps:
1. Run any available tests (pytest, npm test, etc.) to confirm nothing is broken.
2. Check that no new warnings or errors were introduced by the fixes.
3. Verify that the original task requirement is fully met.
4. Confirm the working tree is in a clean, committable state.

Output format (strict):
- If everything checks out: output exactly: `VERIFIED`
- If there are remaining concerns: list them as bullets, then output: `NOT VERIFIED: [brief reason]`
"""


class ReviewLoopTool(BaseTool):
    name = "review-loop"

    @property
    def description(self) -> str:
        return f"Review/Fix-Loop auf uncommitted Changes (max {TOOL_MAX_ITERATIONS}, P1-P3 alle fixen)"

    def _should_verify(self) -> bool:
        """Check policy.yaml whether verification phase is enabled."""
        try:
            from policy import load_policy
            policy = load_policy()
            phases = policy.get("tool_phases", {}).get("review-loop", {})
            return phases.get("verification", "auto") != "skip"
        except (ImportError, OSError, ValueError):
            return True  # default: verify

    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
    ) -> ToolResult:
        print(f"  [review-loop] Starte iterativen Review/Fix-Loop (max {TOOL_MAX_ITERATIONS}x)")

        system_prompt = _build_system_prompt(provider.name, memory_context, tool_name=self.name)
        review_prompt = f"{system_prompt}\n\n{task}\n\n{_REVIEW_PROMPT_BODY}"
        seen_signatures: set[tuple[str, ...]] = set()
        last_findings_tuple: tuple[str, ...] = ()
        all_outputs: list[str] = []

        review_timeout = timeout or TOOL_REVIEW_TIMEOUT_SEC
        fix_timeout = timeout or TOOL_FIX_TIMEOUT_SEC
        verification_timeout = timeout or TOOL_VERIFICATION_TIMEOUT_SEC

        total_input_tokens = 0
        total_output_tokens = 0

        # Load lessons for fix-prompt injection
        try:
            import memory as memory_module
        except (ImportError, OSError):
            memory_module = None

        for iteration in range(1, TOOL_MAX_ITERATIONS + 1):
            print(f"\n  [review-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: REVIEW ===")

            # Capacity guard: abort loop if provider is below threshold (RAM-cache, no API call)
            if not is_cached_provider_available(provider.name):
                msg = f"Provider nicht verfügbar — Suspend nach Iteration {iteration - 1}"
                print(f"  [review-loop] ⏸ {msg}")
                return _make_capacity_exhausted_result(msg, "\n\n".join(all_outputs), iteration - 1, total_input_tokens, total_output_tokens)

            # Step 1: Review
            review_result = provider.run(
                review_prompt,
                cwd=cwd,
                timeout=review_timeout,
            )
            total_input_tokens += review_result.input_tokens
            total_output_tokens += review_result.output_tokens

            if not review_result.success:
                msg = f"Review fehlgeschlagen: {review_result.error}"
                print(f"  [review-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    error_code=review_result.error,
                    retryable=True,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

            all_outputs.append(f"--- Review {iteration} ---\n{review_result.output}")
            review_output = review_result.output.strip()
            findings = _parse_findings(review_output)
            no_findings = _is_no_findings_output(review_output)

            if not findings and not no_findings:
                msg = (
                    "Review-Output entspricht nicht dem erwarteten Format "
                    "(keine P1/P2/P3 Findings und kein 'No P1/P2/P3 findings.')."
                )
                print(f"  [review-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

            print(f"  [review-loop] {len(findings)} Findings gefunden")
            for f in findings[:5]:
                print(f"    {f}")
            if len(findings) > 5:
                print(f"    ... und {len(findings) - 5} weitere")

            # No findings → run verification phase, then success
            if no_findings:
                # Verification phase (configurable via policy.yaml)
                if self._should_verify():
                    print(f"  [review-loop] === VERIFICATION PHASE ===")
                    verify_prompt = (
                        f"{system_prompt}\n\n{task}\n\n{_VERIFICATION_PROMPT_BODY}"
                    )
                    verify_result = provider.run(
                        verify_prompt,
                        cwd=cwd,
                        timeout=verification_timeout,
                    )
                    total_input_tokens += verify_result.input_tokens
                    total_output_tokens += verify_result.output_tokens

                    if verify_result.success:
                        all_outputs.append(
                            f"--- Verification ---\n{verify_result.output}"
                        )
                        verified = "VERIFIED" in verify_result.output.upper().replace("NOT VERIFIED", "")
                        if not verified:
                            print(f"  [review-loop] Verification nicht bestanden, Concerns gefunden.")
                            # Not a hard failure — log but still succeed
                            # (concerns are informational, findings were already clean)

                msg = f"Keine P1/P2/P3 Findings nach {iteration} Iteration(en)."
                print(f"  [review-loop] ✅ {msg}")

                # Auto-lesson — DEAKTIVIERT
                # Speichern von "letzten Findings" ist nicht sinnvoll: die Einträge sind
                # projektspezifisch und nach Code-Änderungen veraltet. Eine echte Lesson
                # bräuchte eine LLM-Summary über alle Iterationen (Muster, Root Cause).
                # TODO: Neu implementieren mit LLM-generierter Summary aus all_outputs.

                notify_tool_done(self.name, iteration, True, msg)
                return ToolResult(
                    success=True,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

            # Check for repeated findings (infinite loop detection)
            signature = tuple(sorted(findings))
            if signature in seen_signatures:
                msg = f"Findings wiederholen sich nach {iteration} Iterationen. Loop beendet."
                print(f"  [review-loop] ⚠️ {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )
            seen_signatures.add(signature)
            last_findings_tuple = signature

            # Step 2: Fix — with lessons injection
            print(f"  [review-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: FIX ===")
            notify_tool_progress(
                self.name, iteration, TOOL_MAX_ITERATIONS,
                f"{len(findings)} Findings werden gefixt..."
            )

            findings_text = "\n".join(findings)

            # Search lessons for hints related to current findings
            lessons_hint = ""
            if memory_module is not None:
                try:
                    hint = memory_module.search_lessons(findings_text)
                    if hint:
                        lessons_hint = (
                            f"\nPrevious lessons for similar issues:\n{hint}\n"
                        )
                except (ImportError, OSError, ValueError):
                    pass

            fix_prompt = (
                f"{system_prompt}\n\n"
                + _FIX_PROMPT_BODY.format(
                    iteration=iteration,
                    findings=findings_text,
                    lessons_hint=lessons_hint,
                )
            )

            fix_result = provider.run(
                fix_prompt,
                cwd=cwd,
                timeout=fix_timeout,
            )
            total_input_tokens += fix_result.input_tokens
            total_output_tokens += fix_result.output_tokens

            if not fix_result.success:
                msg = f"Fix fehlgeschlagen in Iteration {iteration}: {fix_result.error}"
                print(f"  [review-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    error_code=fix_result.error,
                    retryable=True,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

            all_outputs.append(f"--- Fix {iteration} ---\n{fix_result.output}")
            print(f"  [review-loop] Fix durchgeführt. Starte Re-Review...")

            # Small pause between iterations
            time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

        # Max iterations reached
        msg = f"Max Iterationen ({TOOL_MAX_ITERATIONS}) erreicht. Noch Findings offen."
        print(f"  [review-loop] ⚠️ {msg}")
        notify_tool_done(self.name, TOOL_MAX_ITERATIONS, False, msg)
        return ToolResult(
            success=False,
            output="\n\n".join(all_outputs),
            iterations=TOOL_MAX_ITERATIONS,
            error=msg,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
        )
