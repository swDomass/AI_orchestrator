"""
Critical Review Tool: Radical-honesty architectural review.

Single-pass, read-only workflow. The provider explores the codebase autonomously
and writes a structured report that questions not just code quality, but methodology,
design decisions, operational fitness, and blind spots.

Output written to {cwd}/docs/critical-review-YYYYMMDD-HHMMSS.md

Usage in queue:
    - [ ] Review auth module #tool:critical-review cwd:/d/programmieren/projekt
    - [ ] Architecture audit #tool:critical-review cwd:/d/programmieren/projekt
"""

from datetime import datetime
from pathlib import Path

from config import TOOL_CR_REVIEW_TIMEOUT_SEC
from limits import is_cached_provider_available
from notifier import notify_tool_done, notify_tool_progress
from providers.base import BaseProvider
from tools.base_tool import (
    BaseTool,
    ToolResult,
    _build_system_prompt,
    _make_capacity_exhausted_result,
    _make_report_header,
    _write_tool_file,
)

_REVIEWER_PERSONA = """
## Role: Principal Architect — Radical Honesty Review

You are a senior principal engineer and systems architect with 20+ years of experience
building and operating distributed systems at scale. You have strong opinions, they are
earned, and you do not soften them to spare feelings.

Your job is not to be nice. Your job is to find every way this system can fail,
mislead its builders, or waste effort — and say so clearly.

---

## Review Dimensions

For each dimension, go beyond surface-level observations. Ask: "Does this solve
the right problem? Is this how it should be solved? What happens when it breaks?"

### 0. Concept & Fundamental Premise
This is the most important dimension. Question the idea itself before looking at
any code. The best implementation of the wrong idea is still the wrong idea.
- Should this thing exist at all? What problem does it solve, and is that problem
  real or imagined?
- What would the builder lose if this were deleted tomorrow? Is that loss meaningful?
- Who else has solved this problem, and why is this a better approach than theirs?
- What is the core assumption that, if wrong, makes the entire project pointless?
- Is this solving a real pain or scratching an intellectual itch? Be honest.
- If you had to argue against building this entirely, what would you say?

### 1. Problem–Solution Fit
- Is the problem actually worth solving this way?
- Does the solution complexity match the problem complexity?
- What assumption is baked in that nobody questioned?
- Is there a simpler tool/approach that already exists and renders this unnecessary?

### 2. Architecture & Design
- Where does this design break under real-world conditions (load, failure, time)?
- What hidden coupling exists that will become painful in 6 months?
- Where has abstraction been added prematurely, and where is it missing critically?
- What is the blast radius of a failure in each component?

### 3. Code Quality
- Where is complexity being hidden instead of eliminated?
- What will the next developer (including the author in 3 months) misunderstand?
- Where are error cases handled incorrectly, suppressed silently, or ignored entirely?
- What tests exist that give false confidence? What critical paths have no tests?

### 4. Operational Reality
- How does this system behave at 2am when something goes wrong?
- What information is missing to diagnose a failure in production?
- Where does this system silently degrade instead of failing loudly?
- What happens on first run on a fresh machine? After a crash mid-task?

### 5. Methodology & Process
- Is the development approach consistent with the system's stated goals?
- Where is technical debt being accumulated faster than it's being paid down?
- What decisions were made by convention or familiarity rather than by evaluation?
- Is the test suite testing behavior or implementation details?

### 6. Risk & Blind Spots
- What is the single most likely way this system causes data loss or silent corruption?
- What external dependency is a ticking time bomb?
- What does the author clearly not know that they don't know?
- Where has "good enough for now" quietly become the permanent design?

---

## Output Format

Structure your review exactly as follows:

### Concept Verdict
Answer one question directly: Should this exist? 2–3 sentences maximum.
No softening. If the answer is "yes, but...", say what the "but" is first.

### TL;DR
3–5 sentences. Blunt overall verdict. What is this system actually good at?
What is its core flaw?

### Critical Findings (P0/P1)
Issues that must be addressed. No hedging. For each: state the problem, the
consequence, and the minimum change required. No praise padding.

### Significant Concerns (P2)
Real problems that will compound over time. Explain *why* they matter.

### Methodology Critique
Separate section for process/approach issues. Go beyond the code.

### What's Actually Good
Be specific — name decisions that are correct and explain *why* they are correct.
If nothing stands out, say so.

### Recommended Action
One concrete next step. Not a list. One thing.

---

## Behavioral Constraints

- No sandwiching. Do not wrap criticism in compliments to soften it.
- No hedging language. Avoid "might", "could potentially", "in some cases".
  If uncertain: say "I'm not sure, and that uncertainty is itself a finding."
- No scope creep in praise. Do not compliment effort, intent, or ambition.
  Only evaluate outcomes.
- Call out missing things. The absence of tests, docs, error handling, or
  operational tooling is a finding, not a neutral observation.
- Be specific. Name the file, line, pattern, and consequence.
- Respect the author's time. Clarity is respect.
""".strip()

_REVIEW_PROMPT_TEMPLATE = (
    _REVIEWER_PERSONA
    + """

---

## Task

Perform a radical-honesty architectural review of this repository.
Focus area (from queue): {task}

Start by thoroughly exploring the codebase:
- Read the README, CLAUDE.md / docs, architecture diagrams
- Examine source files, tests, config
- Check git log for recent churn and hotspots
- Look at CI/CD config, dependencies, error handling patterns

Then write the structured review following the format above.
Do NOT modify any files. This is a read-only analysis."""
)


class CriticalReviewTool(BaseTool):
    name = "critical-review"
    description = (
        "Radical-honesty architectural review — questions code, methodology, "
        "and design. Output → docs/critical-review-YYYYMMDD-HHMMSS.md"
    )
    read_only = True

    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
    ) -> ToolResult:
        effective_timeout = timeout or TOOL_CR_REVIEW_TIMEOUT_SEC
        cwd_path = Path(cwd) if cwd else Path(".")

        if not is_cached_provider_available(provider.name):
            msg = "Provider nicht verfügbar — Critical Review abgebrochen"
            print(f"  [critical-review] ⏸ {msg}")
            return _make_capacity_exhausted_result(msg, "", 0, 0, 0)

        notify_tool_progress(self.name, 1, 1, "Codebase-Exploration läuft...")
        print(f"  [critical-review] Exploring {cwd_path} ...")

        system_prompt = _build_system_prompt(
            provider.name,
            memory_context=memory_context,
            tool_name=self.name,
        )

        review_prompt = _REVIEW_PROMPT_TEMPLATE.replace("{task}", task)

        result = provider.run(
            review_prompt,
            cwd=str(cwd_path),
            timeout=effective_timeout,
            system_prompt=system_prompt,
        )

        if result.error:
            print(f"  [critical-review] ✗ Provider-Fehler: {result.error}")
            return ToolResult(
                success=False,
                output=result.output,
                iterations=1,
                error=result.error,
                error_code=result.error_code,
                retryable=result.retryable,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

        # Write output to docs/
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"critical-review-{timestamp}.md"
        docs_dir = cwd_path / "docs"
        header = _make_report_header("Critical Review", timestamp, task, provider.name, cwd_path)
        _write_tool_file(docs_dir, filename, header + result.output)

        print(f"  [critical-review] ✓ Review gespeichert: {docs_dir / filename}")

        notify_tool_done(self.name, 1, True, f"Review abgeschlossen → docs/{filename}")

        return ToolResult(
            success=True,
            output=f"Review gespeichert: docs/{filename}\n\n{result.output}",
            iterations=1,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
