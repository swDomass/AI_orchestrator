"""
Critical Review Tool: 3-pass adversarial plan/architecture review.

3-pass workflow with optional cross-provider support:
  Pass 1 (Analysis):   Radical-honesty review of the plan/codebase
  Pass 2 (Adversarial): A different persona challenges Pass 1's findings
  Pass 3 (Synthesis):   Produces an improved version of the plan

If a plan file is referenced (wikilink or file path), Pass 3 writes {name}-v2.md.
If no plan file, Pass 3 is skipped (2-pass mode, review-only).

Cross-provider support enables real perspective diversity:
  - [ ] Prüfe docs/plan.md #tool:critical-review #pass1:claude #pass2:gemini cwd:/d/proj
  - [ ] Prüfe [[Plan]] #tool:critical-review #pass1:claude #pass2:claude cwd:/d/proj
  - [ ] Review auth #tool:critical-review cwd:/d/proj  (no plan file → 2-pass review-only)

Output:
  - Review report → {cwd}/docs/critical-review-YYYYMMDD-HHMMSS.md
  - Improved plan → {plan_dir}/{plan_name}-v2.md (only when plan file referenced)
"""

import re
from datetime import datetime
from pathlib import Path

from config import (
    TOOL_CR_MAX_PLAN_CHARS,
    TOOL_CR_PASS1_MAX_INJECT_CHARS,
    TOOL_CR_PASS1_TIMEOUT_SEC,
    TOOL_CR_PASS2_TIMEOUT_SEC,
    TOOL_CR_PASS3_TIMEOUT_SEC,
    MAX_CONTEXT_FILE_SIZE,
)
from limits import is_cached_provider_available
from notifier import notify_tool_done, notify_tool_progress
from providers.base import BaseProvider
from tools.base_tool import (
    BaseTool,
    SessionContext,
    ToolResult,
    ToolTracer,
    _build_system_prompt,
    _make_capacity_exhausted_result,
    _make_report_header,
    _write_tool_file,
)

# ── Prompt Templates ─────────────────────────────────────────────────

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

_REVIEW_PROMPT_TEMPLATE = """
{reviewer_persona}

---

## Task

Perform a radical-honesty review.
Focus area (from queue): {task}

{plan_section}

Start by thoroughly exploring the codebase:
- Read the README, CLAUDE.md / docs, architecture diagrams
- Examine source files, tests, config
- Check git log for recent churn and hotspots
- Look at CI/CD config, dependencies, error handling patterns

Then write the structured review following the format above.
Do NOT modify any files. This is a read-only analysis.
""".strip()

_ADVERSARIAL_PROMPT = """
## Role: Devil's Advocate — Adversarial Review

You are a different senior architect. You have been given a critical review written by
another reviewer. Your job is NOT to agree or add more of the same. Your job is:

1. **Challenge the reviewer's own assumptions** — what did they take for granted?
2. **Find what they missed** — which angles did they not consider?
3. **Question their recommendations** — are the proposed fixes actually correct?
4. **Stress-test the "What's Actually Good" section** — is the praise deserved?
5. **Identify contradictions** — where does the review contradict itself?
6. **Evaluate completeness** — which dimensions (security, ops, perf) got shallow treatment?

You are adversarial to the REVIEW, not to the codebase. The codebase may be
better than the reviewer claims. Or worse. Find out.

---

## Previous Review to Challenge

{pass1_output}

---

{plan_section}

## Output Format

### Meta-Review Verdict
2-3 sentences: Was this review thorough and accurate? Or did it miss the point?

### Missed Angles
Issues the previous review failed to identify. Be specific — name files, patterns,
consequences.

### Challenged Findings
For each major finding in the previous review: do you agree, disagree, or
find it incomplete? State why with evidence.

### Overclaims
Where did the previous review overstate problems or praise things that don't
deserve it?

### Underclaims
Where did the previous review understate real problems?

### Revised Risk Assessment
Your own prioritized list of the actual top risks, combining valid findings
from the previous review with your own additions.

### Synthesis: Actionable Recommendations
Concrete next steps that account for BOTH perspectives. Maximum 5 items,
prioritized.

---

## Behavioral Constraints
- You have access to the same codebase. Verify claims, don't just challenge them rhetorically.
- If the previous review got something right, say so briefly and move on.
- If you find the previous review was mostly correct, say so — but still identify
  at least 2-3 angles they missed or understated.
- Do NOT modify any files. This is a read-only analysis.

## Task context (from queue): {task}
""".strip()

_SYNTHESIS_PROMPT = """
## Role: Plan Architect — Synthesis

You are a senior architect. You have the original plan AND two independent reviews:
- Review 1 (Analysis): found strengths and weaknesses
- Review 2 (Adversarial): challenged Review 1, found missed angles

Your job: produce an **improved version of the plan** that addresses the valid
findings from both reviews while keeping what was already good.

---

## Original Plan

{plan_content}

---

## Review 1: Analysis

{pass1_output}

---

## Review 2: Adversarial Challenge

{pass2_output}

---

## Instructions

1. Keep the original plan's structure and intent where it was correct.
2. Fix every issue that BOTH reviewers agreed on (conceded findings).
3. For disputed findings: use your judgment. If the adversarial reviewer
   had a stronger argument, adopt their position.
4. Add any missing sections or considerations that were identified.
5. Remove or simplify parts that were criticized as over-engineered.
6. Mark significant changes with `<!-- CHANGED: reason -->` comments
   so the author can see what was modified and why.

Output the complete improved plan as a standalone Markdown document.
Do NOT include the reviews or meta-commentary — just the improved plan.
Do NOT modify any files in the repository. Just output the plan text.
""".strip()

# ── File Reference Patterns ──────────────────────────────────────────

# Same patterns as queue_manager but we only need them for local extraction
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")
_FILEPATH_RE = re.compile(
    r"""(["'])((?:[A-Za-z]:)?[\w/\\ .-]+?\.md)\1"""
    r"""|(?:^|\s)((?:[A-Za-z]:)?[\w/\\.-]+\.md)"""
)


# ── Plan File Resolution ─────────────────────────────────────────────


def _resolve_plan_file(task: str, cwd_path: Path) -> tuple[Path | None, str]:
    """Find a plan file referenced in the task text.

    Searches in order:
      1. File paths relative to CWD (e.g. "docs/plan.md")
      2. Wikilinks resolved against vault

    Returns (resolved_path, original_ref) or (None, "") if no file found.
    """
    # Collect all file references
    refs: list[str] = []

    # File paths first (more specific)
    for m in _FILEPATH_RE.finditer(task):
        ref = (m.group(2) or m.group(3) or "").strip()
        if ref:
            refs.append(ref)

    # Then wikilinks
    for m in _WIKILINK_RE.finditer(task):
        refs.append(m.group(1).strip())

    for ref in refs:
        # Try CWD-relative first
        candidate = cwd_path / ref
        if candidate.is_file():
            return candidate, ref

        # Try with .md extension
        if not ref.endswith(".md"):
            candidate_md = cwd_path / (ref + ".md")
            if candidate_md.is_file():
                return candidate_md, ref

        # Try vault resolution
        try:
            from queue_manager import _resolve_note
            vault_path = _resolve_note(ref)
            if vault_path and vault_path.is_file():
                return vault_path, ref
        except (ImportError, OSError):
            pass

    return None, ""


def _read_plan_content(plan_path: Path) -> str | None:
    """Read plan file content with size and encoding safety."""
    try:
        size = plan_path.stat().st_size
        if size > MAX_CONTEXT_FILE_SIZE:
            print(f"  [critical-review] Plan zu groß ({size // 1024}KB), übersprungen")
            return None
        try:
            return plan_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return plan_path.read_text(encoding="cp1252")
    except (OSError, UnicodeDecodeError) as e:
        print(f"  [critical-review] Plan nicht lesbar: {e}")
        return None


def _make_plan_section(plan_content: str | None, plan_ref: str) -> str:
    """Build the plan injection block for prompts."""
    if not plan_content:
        return ""
    return (
        f"## Plan Under Review ({plan_ref})\n\n"
        f"```markdown\n{plan_content}\n```"
    )


def _plan_v2_path(plan_path: Path) -> Path:
    """Compute the -v2 output path for an improved plan."""
    stem = plan_path.stem
    suffix = plan_path.suffix or ".md"
    return plan_path.parent / f"{stem}-v2{suffix}"


# ── Provider Resolution ──────────────────────────────────────────────


def _resolve_pass2_provider(
    pass_providers: dict[int, str],
    default_provider: BaseProvider,
) -> BaseProvider:
    """Resolve the Pass 2 provider from task tags, falling back to the primary provider."""
    pass2_name = pass_providers.get(2)
    if not pass2_name:
        return default_provider

    from dispatcher import get_provider_by_name
    resolved = get_provider_by_name(pass2_name)
    if resolved is None:
        print(
            f"  [critical-review] Warnung: Pass 2 Provider '{pass2_name}' "
            f"unbekannt — verwende {default_provider.name}"
        )
        return default_provider
    return resolved


# ── Tool ─────────────────────────────────────────────────────────────


class CriticalReviewTool(BaseTool):
    name = "critical-review"
    description = (
        "Adversarial 3-pass review — analysis + challenge + synthesis. "
        "Reference a plan file to get {name}-v2.md output. "
        "Cross-provider via #pass1:#pass2: tags. "
        "Output → docs/critical-review-*.md + {plan}-v2.md"
    )
    read_only = True

    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
        **kwargs,
    ) -> ToolResult:
        pass_providers: dict[int, str] = kwargs.get("pass_providers", {})
        cwd_path = Path(cwd) if cwd else Path(".")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        docs_dir = cwd_path / "docs"

        tracer = ToolTracer.create(self.name, cwd)
        tracer.emit(
            "run_start",
            task=task[:200],
            provider=provider.name,
            pass2_provider=pass_providers.get(2) or provider.name,
        )

        pass1_timeout = timeout or TOOL_CR_PASS1_TIMEOUT_SEC
        pass2_timeout = timeout or TOOL_CR_PASS2_TIMEOUT_SEC
        pass3_timeout = timeout or TOOL_CR_PASS3_TIMEOUT_SEC

        total_input_tokens = 0
        total_output_tokens = 0

        # Phase C2: capability switch — sessions only when ALL passes use the
        # same provider (cross-provider mode keeps the explicit injection path
        # which is the actual mechanism, see CLAUDE.md "critical-review keeps
        # cross-provider"). For same-provider runs, the 3 passes share a
        # session → cache hits across the full chain. cap=20 means no rollover
        # within a typical 3-pass run.
        from config import CLAUDE_SESSION_ENABLED
        pass2_name = pass_providers.get(2) or provider.name
        same_provider_chain = (
            pass2_name == provider.name
            and getattr(provider, "supports_sessions", False)
            and CLAUDE_SESSION_ENABLED
        )
        sess = (
            SessionContext.create(provider, tool_name=self.name, cwd=cwd, cap=20)
            if same_provider_chain
            else None
        )
        cr_first_call = True

        def _cr_session_kwargs() -> dict:
            nonlocal cr_first_call
            if sess is None or not sess.enabled:
                return {}
            kw = sess.first_call_kwargs() if cr_first_call else sess.resume_kwargs()
            cr_first_call = False
            return kw

        # ── Resolve plan file ────────────────────────────────────────

        plan_path, plan_ref = _resolve_plan_file(task, cwd_path)
        plan_content: str | None = None
        if plan_path:
            plan_content = _read_plan_content(plan_path)
            if plan_content:
                if len(plan_content) > TOOL_CR_MAX_PLAN_CHARS:
                    plan_content = (
                        plan_content[:TOOL_CR_MAX_PLAN_CHARS]
                        + "\n\n...[Plan truncated]"
                    )
                print(f"  [critical-review] Plan geladen: {plan_path.name} ({len(plan_content)} Zeichen)")
            else:
                plan_path = None  # reset if unreadable

        has_plan = plan_content is not None
        total_passes = 3 if has_plan else 2
        plan_section = _make_plan_section(plan_content, plan_ref)

        # ── Pass 1: Analysis ─────────────────────────────────────────

        if not is_cached_provider_available(provider.name):
            msg = "Provider nicht verfügbar — Critical Review abgebrochen"
            print(f"  [critical-review] ⏸ {msg}")
            return _make_capacity_exhausted_result(msg, "", 0, 0, 0)

        notify_tool_progress(self.name, 1, total_passes, "Pass 1: Analyse...")
        print(f"  [critical-review] Pass 1 — Analyse ({cwd_path}) ...")

        system_prompt = _build_system_prompt(
            provider.name,
            memory_context=memory_context,
            tool_name=self.name,
            cwd=cwd,
        )

        review_prompt = (
            system_prompt + "\n\n"
            + _REVIEW_PROMPT_TEMPLATE
                .replace("{reviewer_persona}", _REVIEWER_PERSONA)
                .replace("{task}", task)
                .replace("{plan_section}", plan_section)
        )

        result1 = provider.run(
            review_prompt,
            cwd=str(cwd_path),
            timeout=pass1_timeout,
            read_only=True,
            **_cr_session_kwargs(),
        )
        if not result1.success and result1.error == "session_missing":
            print("  [critical-review] ⚠️ Session missing in Pass 1 — Fallback")
            if sess is not None:
                sess.rollover(self.name, cwd)
            cr_first_call = True
            result1 = provider.run(
                review_prompt, cwd=str(cwd_path), timeout=pass1_timeout,
                read_only=True, **_cr_session_kwargs(),
            )

        total_input_tokens += result1.input_tokens
        total_output_tokens += result1.output_tokens

        if result1.error:
            print(f"  [critical-review] ✗ Pass 1 Fehler: {result1.error}")
            retryable = result1.error in ("rate_limit", "unreachable", "timeout")
            return ToolResult(
                success=False,
                output=result1.output,
                iterations=1,
                error=result1.error,
                error_code=result1.error,
                retryable=retryable,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            )

        # Save Pass 1 standalone (safety net)
        pass1_filename = f"critical-review-{timestamp}-pass1.md"
        header1 = _make_report_header(
            "Critical Review — Pass 1 (Analysis)", timestamp, task,
            provider.name, cwd_path,
        )
        _write_tool_file(docs_dir, pass1_filename, header1 + result1.output)
        print(f"  [critical-review] ✓ Pass 1 gespeichert: {docs_dir / pass1_filename}")

        # ── Pass 2: Adversarial Challenge ────────────────────────────

        pass2_provider = _resolve_pass2_provider(pass_providers, provider)

        if not is_cached_provider_available(pass2_provider.name):
            msg = (
                f"Pass 2 Provider ({pass2_provider.name}) nicht verfügbar — "
                f"Pass 1 gespeichert als {pass1_filename}"
            )
            print(f"  [critical-review] ⏸ {msg}")
            return _make_capacity_exhausted_result(
                msg, result1.output, 1, total_input_tokens, total_output_tokens,
            )

        notify_tool_progress(self.name, 2, total_passes, f"Pass 2: Adversarial ({pass2_provider.name})...")
        print(f"  [critical-review] Pass 2 — Adversarial via {pass2_provider.name} ...")

        system_prompt2 = _build_system_prompt(
            pass2_provider.name,
            memory_context=memory_context,
            tool_name=self.name,
            cwd=cwd,
        )

        # In session mode (same-provider chain), Pass 1's full output already
        # lives in the conversation history — re-injecting it as text would
        # make Pass 2 see Pass 1 twice and weaken the adversarial challenge.
        # In stateless mode (cross-provider), the inject IS the only signal.
        same_pass2_session = (
            pass2_provider.name == provider.name
            and sess is not None
            and sess.enabled
        )
        if same_pass2_session:
            pass1_inject_block = (
                "_(Pass 1's full output is in your conversation history above. "
                "Read it there — do not expect a re-injection.)_"
            )
        else:
            pass1_for_injection = result1.output
            if len(pass1_for_injection) > TOOL_CR_PASS1_MAX_INJECT_CHARS:
                pass1_for_injection = (
                    pass1_for_injection[:TOOL_CR_PASS1_MAX_INJECT_CHARS]
                    + "\n\n...[Pass 1 output truncated]"
                )
            pass1_inject_block = pass1_for_injection

        adversarial_prompt = (
            system_prompt2
            + "\n\n"
            + _ADVERSARIAL_PROMPT
                .replace("{pass1_output}", pass1_inject_block)
                .replace("{task}", task)
                .replace("{plan_section}", plan_section)
        )

        # Apply model pin for pass2 provider if task has a matching model tag.
        # (Primary provider already has _forced_model set by orchestrator; pass2
        # may be a different provider and needs its own resolution.)
        from queue_manager import extract_model_tag
        from config import model_id_for_provider as _model_id_for_provider
        pass2_model_id = _model_id_for_provider(
            extract_model_tag(task), pass2_provider.name
        )
        pass2_prev_model = getattr(pass2_provider, "_forced_model", None)
        setattr(pass2_provider, "_forced_model", pass2_model_id)
        try:
            # Sessions only apply when pass2 is the same provider instance
            # (cross-provider runs use the explicit-injection mechanism).
            same_pass2 = pass2_provider.name == provider.name
            pass2_kw = _cr_session_kwargs() if same_pass2 else {}
            result2 = pass2_provider.run(
                adversarial_prompt,
                cwd=str(cwd_path),
                timeout=pass2_timeout,
                read_only=True,
                **pass2_kw,
            )
            # Session-missing fallback only meaningful in same-provider mode.
            if (
                same_pass2
                and not result2.success
                and result2.error == "session_missing"
            ):
                print("  [critical-review] ⚠️ Session missing in Pass 2 — Fallback")
                if sess is not None:
                    sess.rollover(self.name, cwd)
                cr_first_call = True
                result2 = pass2_provider.run(
                    adversarial_prompt, cwd=str(cwd_path), timeout=pass2_timeout,
                    read_only=True, **_cr_session_kwargs(),
                )
        finally:
            setattr(pass2_provider, "_forced_model", pass2_prev_model)

        total_input_tokens += result2.input_tokens
        total_output_tokens += result2.output_tokens

        if result2.error:
            print(f"  [critical-review] ⚠ Pass 2 Fehler: {result2.error}")
            print(f"  [critical-review] Pass 1 gespeichert: {docs_dir / pass1_filename}")
            retryable2 = result2.error in ("rate_limit", "unreachable", "timeout")
            return ToolResult(
                success=False,
                output=(
                    f"Pass 1 gespeichert: docs/{pass1_filename}\n\n"
                    f"Pass 2 Fehler: {result2.error}"
                ),
                iterations=1,
                error=result2.error,
                error_code=result2.error,
                retryable=retryable2,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            )

        # ── Pass 3: Synthesis (only when plan file present) ──────────

        v2_path: Path | None = None

        if has_plan and plan_path is not None:
            notify_tool_progress(self.name, 3, 3, "Pass 3: Plan-Synthese...")
            print(f"  [critical-review] Pass 3 — Synthese → {plan_path.stem}-v2.md ...")

            # Use Pass 1 provider for synthesis (has seen the codebase)
            if not is_cached_provider_available(provider.name):
                msg = (
                    f"Provider ({provider.name}) nicht verfügbar für Synthese — "
                    f"Review gespeichert, aber kein -v2 Plan erzeugt"
                )
                print(f"  [critical-review] ⏸ {msg}")
                # Continue without Pass 3 — still save the review report
            else:
                system_prompt3 = _build_system_prompt(
                    provider.name,
                    memory_context=memory_context,
                    tool_name=self.name,
                    cwd=cwd,
                )

                # In session mode, Pass 1 + Pass 2 are already in the
                # conversation history → skip the explicit inject (avoids
                # the same double-context bug as Pass 2).
                same_synth_session = (
                    sess is not None and sess.enabled and same_pass2
                )
                if same_synth_session:
                    p1_for_synth = (
                        "_(Pass 1's output is in your conversation history above.)_"
                    )
                    p2_for_synth = (
                        "_(Pass 2's adversarial review is in your conversation history above.)_"
                    )
                else:
                    p1_for_synth = result1.output
                    if len(p1_for_synth) > TOOL_CR_PASS1_MAX_INJECT_CHARS:
                        p1_for_synth = p1_for_synth[:TOOL_CR_PASS1_MAX_INJECT_CHARS] + "\n...[truncated]"
                    p2_for_synth = result2.output
                    if len(p2_for_synth) > TOOL_CR_PASS1_MAX_INJECT_CHARS:
                        p2_for_synth = p2_for_synth[:TOOL_CR_PASS1_MAX_INJECT_CHARS] + "\n...[truncated]"

                synthesis_prompt = (
                    system_prompt3
                    + "\n\n"
                    + _SYNTHESIS_PROMPT
                        .replace("{plan_content}", plan_content or "")
                        .replace("{pass1_output}", p1_for_synth)
                        .replace("{pass2_output}", p2_for_synth)
                )

                result3 = provider.run(
                    synthesis_prompt,
                    cwd=str(cwd_path),
                    timeout=pass3_timeout,
                    read_only=True,
                    **_cr_session_kwargs(),
                )
                if not result3.success and result3.error == "session_missing":
                    print("  [critical-review] ⚠️ Session missing in Pass 3 — Fallback")
                    if sess is not None:
                        sess.rollover(self.name, cwd)
                    cr_first_call = True
                    result3 = provider.run(
                        synthesis_prompt, cwd=str(cwd_path), timeout=pass3_timeout,
                        read_only=True, **_cr_session_kwargs(),
                    )

                total_input_tokens += result3.input_tokens
                total_output_tokens += result3.output_tokens

                if result3.error:
                    print(f"  [critical-review] ⚠ Pass 3 Fehler: {result3.error} — Review trotzdem gespeichert")
                else:
                    v2_path = _plan_v2_path(plan_path)
                    v2_path.parent.mkdir(parents=True, exist_ok=True)
                    v2_path.write_text(result3.output, encoding="utf-8")
                    print(f"  [critical-review] ✓ Verbesserter Plan: {v2_path}")

        # ── Combined Review Report ───────────────────────────────────

        combined_filename = f"critical-review-{timestamp}.md"
        provider_label = (
            f"{provider.name} / {pass2_provider.name}"
            if pass2_provider.name != provider.name
            else provider.name
        )
        combined_header = _make_report_header(
            "Critical Review (Adversarial)", timestamp, task,
            provider_label, cwd_path,
        )

        metadata_lines = [
            f"- Pass 1 (Analysis): {provider.name}",
            f"- Pass 2 (Adversarial): {pass2_provider.name}",
        ]
        if has_plan:
            metadata_lines.append(f"- Pass 3 (Synthesis): {provider.name}")
            metadata_lines.append(f"- Plan: {plan_ref}")
            if v2_path:
                metadata_lines.append(f"- Output: {v2_path}")
        metadata_lines.append(f"- Input tokens: {total_input_tokens}")
        metadata_lines.append(f"- Output tokens: {total_output_tokens}")

        metadata = "\n\n---\n\n## Review-Metadaten\n" + "\n".join(metadata_lines) + "\n"

        combined_content = (
            combined_header
            + "# Part 1: Analysis\n\n" + result1.output
            + "\n\n---\n\n"
            + "# Part 2: Adversarial Challenge\n\n" + result2.output
            + metadata
        )

        _write_tool_file(docs_dir, combined_filename, combined_content)

        print(f"  [critical-review] ✓ Review gespeichert: {docs_dir / combined_filename}")

        iterations = 3 if v2_path else 2
        output_parts = [f"Review gespeichert: docs/{combined_filename}"]
        if v2_path:
            output_parts.append(f"Verbesserter Plan: {v2_path}")

        tracer.emit("run_end", success=True, iterations=iterations)
        notify_tool_done(
            self.name, iterations, True,
            " | ".join(output_parts),
        )

        return ToolResult(
            success=True,
            output="\n".join(output_parts) + "\n\n" + combined_content,
            iterations=iterations,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
        )
