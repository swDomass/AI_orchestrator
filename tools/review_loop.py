"""
Review-Loop Tool: Iterative Review → Fix → Re-Review until clean.

Inspired by codex_p1_review_loop.py but works with any provider (Claude, Gemini, Codex).

Usage in queue:
    - [ ] Review und fixe Bugs in main.py #tool:review-loop cwd:/d/programmieren/projekt
    - [ ] Full code review #tool:review-loop #codex cwd:/d/programmieren/projekt
"""

import re
import time

from config import SAFETY_RULES, TOOL_MAX_ITERATIONS, TOOL_REVIEW_TIMEOUT_SEC, TOOL_FIX_TIMEOUT_SEC
from notifier import notify_tool_progress, notify_tool_done
from providers.base import BaseProvider
from tools.base_tool import BaseTool, ToolResult

# Matches priority findings like: - [P1] Some issue
FINDING_RE = re.compile(r"^\s*-\s+\[P[1-3]\]\s+.+", re.MULTILINE)
# Common fallback formats from providers (e.g. "1. `P2` Something...")
ALT_FINDING_RE = re.compile(
    r"^\s*(?:[-*]|\d+[.)])\s*(?:`|\*\*)?\[?(P[1-3])\]?(?:`|\*\*)?(?:\s*[:\-]\s*|\s+)(.+)$",
    re.IGNORECASE,
)
NO_FINDINGS_RE = re.compile(r"^\s*`?No P1/P2/P3 findings\.`?\s*$", re.IGNORECASE)


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


_REVIEW_PROMPT = f"""{SAFETY_RULES}

Perform a code review of the current working directory.

Rules:
- Review all code files, focus on correctness, bugs, security, crashes.
- Include file paths and line numbers.
- Do NOT modify any files.

Output format (strict):
- One bullet per finding: `- [P1] ...`, `- [P2] ...`, `- [P3] ...`
- P1 = critical/crash, P2 = significant bug, P3 = minor issue
- If no findings: output exactly: `No P1/P2/P3 findings.`
"""

_FIX_PROMPT = SAFETY_RULES + """

You are fixing issues found by a code review (iteration {iteration}).

Task:
- Fix ALL P1, P2, P3 issues listed below.
- Apply changes directly to the files.
- Run validation/tests if feasible.
- Summarize what was fixed.

Review findings:
{findings}
"""


class ReviewLoopTool(BaseTool):
    name = "review-loop"
    description = "Iterative Review > Fix > Re-Review bis keine P1/P2/P3 Findings mehr"

    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
    ) -> ToolResult:
        print(f"  [review-loop] Starte iterativen Review/Fix-Loop (max {TOOL_MAX_ITERATIONS}x)")

        # Combine user task with review prompt
        review_prompt = f"{task}\n\n{_REVIEW_PROMPT}"
        seen_signatures: set[tuple[str, ...]] = set()
        all_outputs: list[str] = []

        for iteration in range(1, TOOL_MAX_ITERATIONS + 1):
            print(f"\n  [review-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: REVIEW ===")

            # Step 1: Review
            review_result = provider.run(
                review_prompt,
                cwd=cwd,
                timeout=TOOL_REVIEW_TIMEOUT_SEC,
            )

            if not review_result.success:
                msg = f"Review fehlgeschlagen: {review_result.error}"
                print(f"  [review-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                )

            all_outputs.append(f"--- Review {iteration} ---\n{review_result.output}")
            review_output = review_result.output.strip()
            findings = _parse_findings(review_output)

            if not findings and not _is_no_findings_output(review_output):
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
                )

            print(f"  [review-loop] {len(findings)} Findings gefunden")
            for f in findings[:5]:
                print(f"    {f}")
            if len(findings) > 5:
                print(f"    ... und {len(findings) - 5} weitere")

            # No findings → success!
            if _is_no_findings_output(review_output):
                msg = f"Keine P1/P2/P3 Findings nach {iteration} Iteration(en)."
                print(f"  [review-loop] ✅ {msg}")
                notify_tool_done(self.name, iteration, True, msg)
                return ToolResult(
                    success=True,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
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
                )
            seen_signatures.add(signature)

            # Step 2: Fix
            print(f"  [review-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: FIX ===")
            notify_tool_progress(
                self.name, iteration, TOOL_MAX_ITERATIONS,
                f"{len(findings)} Findings werden gefixt..."
            )

            findings_text = "\n".join(findings)
            fix_prompt = _FIX_PROMPT.format(iteration=iteration, findings=findings_text)

            fix_result = provider.run(
                fix_prompt,
                cwd=cwd,
                timeout=TOOL_FIX_TIMEOUT_SEC,
            )

            if not fix_result.success:
                msg = f"Fix fehlgeschlagen in Iteration {iteration}: {fix_result.error}"
                print(f"  [review-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                )

            all_outputs.append(f"--- Fix {iteration} ---\n{fix_result.output}")
            print(f"  [review-loop] Fix durchgeführt. Starte Re-Review...")

            # Small pause between iterations
            time.sleep(2)

        # Max iterations reached
        msg = f"Max Iterationen ({TOOL_MAX_ITERATIONS}) erreicht. Noch Findings offen."
        print(f"  [review-loop] ⚠️ {msg}")
        notify_tool_done(self.name, TOOL_MAX_ITERATIONS, False, msg)
        return ToolResult(
            success=False,
            output="\n\n".join(all_outputs),
            iterations=TOOL_MAX_ITERATIONS,
            error=msg,
        )
