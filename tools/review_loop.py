"""
Review-Loop Tool: Iterative Review → Fix → Re-Review until clean.

Inspired by codex_p1_review_loop.py but works with any provider (Claude, Gemini, Codex).

Usage in queue:
    - [ ] Review und fixe Bugs in main.py #tool:review-loop cwd:/d/programmieren/projekt
    - [ ] Review uncommitted changes #tool:review-loop #codex cwd:/d/programmieren/projekt
"""

import re
import subprocess
import time

from config import (
    CLAUDE_MODEL_ALIASES,
    CODEX_MODEL_ALIASES,
    GEMINI_MODEL_ALIASES,
    OPENROUTER_MODEL_ALIASES,
    TOOL_MAX_ITERATIONS,
    TOOL_REVIEW_TIMEOUT_SEC,
    TOOL_FIX_TIMEOUT_SEC,
    TOOL_INTER_STEP_SLEEP_SEC,
    TOOL_RL_SECOND_OPINION_MAX_DIFF_CHARS,
    TOOL_RL_SECOND_OPINION_TIMEOUT_SEC,
    TOOL_VERIFICATION_TIMEOUT_SEC,
)
from limits import is_cached_provider_available
from notifier import notify_tool_progress, notify_tool_done
from providers.base import BaseProvider
from tools.base_tool import BaseTool, SessionContext, TokenCounter, ToolResult, ToolTracer, _build_system_prompt, _make_capacity_exhausted_result

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

# Fix prompt is split into a STABLE prefix (cached across iterations) and a
# VOLATILE suffix (iteration-specific findings + lessons hint). Anthropic
# prompt cache matches the longest identical prefix → keeping the stable
# part in front maximises cross-iteration cache hits.
_FIX_PROMPT_STABLE = """
You are fixing issues found by a code review.

Task:
- Fix ALL P1, P2, and P3 issues listed below.
- Apply changes directly to the files.
- Run validation/tests if feasible.
- Summarize what was fixed.
"""

_FIX_PROMPT_VOLATILE = """
Iteration: {iteration}

Review findings (must all be addressed):
{findings}
{lessons_hint}"""

# Second-opinion prompt — sent to a non-agentic LLM (typically OpenRouter) that
# cannot navigate the codebase itself. The git diff is pre-loaded and injected.
# The model is told what the primary reviewer already found so it can focus on
# what was missed, not duplicate work.
_SECOND_OPINION_PROMPT = """\
You are providing a SECOND OPINION on a code review.

A primary reviewer has already reviewed the uncommitted changes below and
listed their findings. Your job: identify ADDITIONAL P1/P2/P3 issues the
primary reviewer MISSED. Do not restate findings they already found.

Focus on: correctness, security, crashes, edge cases, hidden coupling,
subtle bugs the primary reviewer might overlook.

== UNCOMMITTED DIFF ==
{diff}

== PRIMARY REVIEWER FINDINGS ==
{primary_findings}

== OUTPUT FORMAT (strict) ==
- One bullet per additional finding: `- [P1] ...`, `- [P2] ...`, `- [P3] ...`
- Include file paths and line numbers where possible.
- If the primary reviewer covered everything, output exactly: `No P1/P2/P3 findings.`
- Do NOT modify any files. Do NOT restate findings already listed above.
"""


# Maps alias → owning provider. Used to resolve #second_opinion:<alias> tags.
# Bare provider names (without underscore) map to themselves with no model
# override → default CLI model.
_SECOND_OPINION_BARE_PROVIDERS = {"openrouter", "claude", "gemini", "codex"}


def _resolve_second_opinion(alias: str | None) -> tuple[BaseProvider, str | None] | None:
    """Resolve a #second_opinion:<alias> value to (provider, model_id | None).

    Returns None when:
      - alias is falsy
      - alias is unknown (not a model alias and not a bare provider name)
      - the resolved provider is not registered (e.g. OpenRouter without API key)

    The caller logs a warning and skips the second-opinion phase on None.
    """
    if not alias:
        return None

    if alias in OPENROUTER_MODEL_ALIASES:
        provider_name, model_id = "openrouter", alias
    elif alias in CLAUDE_MODEL_ALIASES:
        provider_name, model_id = "claude", alias
    elif alias in GEMINI_MODEL_ALIASES:
        provider_name, model_id = "gemini", alias
    elif alias in CODEX_MODEL_ALIASES:
        provider_name, model_id = "codex", alias
    elif alias in _SECOND_OPINION_BARE_PROVIDERS:
        provider_name, model_id = alias, None
    else:
        return None

    from dispatcher import get_provider_by_name
    provider = get_provider_by_name(provider_name)
    if provider is None:
        return None
    return provider, model_id


def _load_git_diff(cwd: str | None, max_chars: int) -> str | None:
    """Capture uncommitted diff + status for second-opinion injection.

    Returns None when:
      - cwd is None
      - git is not available / cwd is not a repo
      - the combined output exceeds max_chars (too large to inject safely)
    """
    if not cwd:
        return None
    try:
        diff = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=30, check=False,
            encoding="utf-8", errors="replace",
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, timeout=10, check=False,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if diff.returncode != 0 and status.returncode != 0:
        return None

    parts: list[str] = []
    if status.stdout.strip():
        parts.append("=== git status --porcelain ===\n" + status.stdout.strip())
    if diff.stdout.strip():
        parts.append("=== git diff HEAD ===\n" + diff.stdout)
    if not parts:
        return None

    combined = "\n\n".join(parts)
    if len(combined) > max_chars:
        return None
    return combined


def _merge_findings(primary: list[str], extra: list[str]) -> list[str]:
    """Union with exact-string dedup, preserving order: primary first, then new."""
    seen = set(primary)
    merged = list(primary)
    for item in extra:
        if item not in seen:
            merged.append(item)
            seen.add(item)
    return merged


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
        **kwargs,
    ) -> ToolResult:
        print(f"  [review-loop] Starte iterativen Review/Fix-Loop (max {TOOL_MAX_ITERATIONS}x)")

        # Second-opinion: opt-in via #second_opinion:<alias> tag in the queue.
        # Resolved by the orchestrator and passed as kwarg; we re-resolve here as
        # fallback so that direct unit tests can pass the alias instead.
        second_opinion_alias: str | None = kwargs.get("second_opinion_alias")
        second_opinion: tuple[BaseProvider, str | None] | None = kwargs.get("second_opinion")
        if second_opinion is None and second_opinion_alias:
            second_opinion = _resolve_second_opinion(second_opinion_alias)
            if second_opinion is None:
                print(
                    f"  [review-loop] ⚠️ Second-Opinion alias '{second_opinion_alias}' "
                    f"unbekannt oder Provider nicht konfiguriert — wird übersprungen"
                )

        tracer = ToolTracer.create(self.name, cwd)
        tracer.emit("run_start", task=task[:200], provider=provider.name,
                    max_iterations=TOOL_MAX_ITERATIONS,
                    second_opinion=second_opinion[0].name if second_opinion else None)

        system_prompt = _build_system_prompt(provider.name, memory_context, tool_name=self.name, cwd=cwd)
        review_prompt = f"{system_prompt}\n\n{task}\n\n{_REVIEW_PROMPT_BODY}"
        seen_signatures: set[tuple[str, ...]] = set()
        last_findings_tuple: tuple[str, ...] = ()
        all_outputs: list[str] = []

        review_timeout = timeout or TOOL_REVIEW_TIMEOUT_SEC
        fix_timeout = timeout or TOOL_FIX_TIMEOUT_SEC
        verification_timeout = timeout or TOOL_VERIFICATION_TIMEOUT_SEC

        tokens = TokenCounter()

        # Phase B: optional shared session across review→fix iterations.
        # cap=5 triggers a rollover; the next review prompt is independent
        # (always reads `git diff` fresh), so a fresh session continues cleanly.
        sess = SessionContext.create(provider, tool_name=self.name, cwd=cwd, cap=5)
        first_call = True

        def _session_kwargs() -> dict:
            nonlocal first_call
            if not sess.enabled:
                return {}
            kw = sess.first_call_kwargs() if first_call else sess.resume_kwargs()
            first_call = False
            return kw

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
                return _make_capacity_exhausted_result(
                    msg, "\n\n".join(all_outputs), iteration - 1, **tokens.as_kwargs(),
                )

            # Step 1: Review
            review_result = provider.run(
                review_prompt,
                cwd=cwd,
                timeout=review_timeout,
                read_only=True,  # safe-by-CLI: review must not edit files
                **_session_kwargs(),
            )
            # Fallback: session lost between calls (e.g. cleanup race) — reset.
            if not review_result.success and review_result.error == "session_missing":
                print("  [review-loop] ⚠️ Session missing — Fallback auf fresh session")
                sess.rollover(self.name, cwd)
                first_call = True
                review_result = provider.run(
                    review_prompt, cwd=cwd, timeout=review_timeout,
                    read_only=True,
                    **_session_kwargs(),
                )
            tokens.add(review_result)

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
                    **tokens.as_kwargs(),
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
                    **tokens.as_kwargs(),
                )

            print(f"  [review-loop] {len(findings)} Findings gefunden")
            for f in findings[:5]:
                print(f"    {f}")
            if len(findings) > 5:
                print(f"    ... und {len(findings) - 5} weitere")

            # Second-opinion phase (iteration 1 only, opt-in).
            # Runs after the primary review to catch missed P1/P2/P3 issues.
            # Failures (cooldown, diff fetch, parse) are non-fatal — we log and
            # continue with the primary findings only.
            if iteration == 1 and second_opinion is not None:
                so_provider, so_model_id = second_opinion
                if not is_cached_provider_available(so_provider.name):
                    print(
                        f"  [review-loop] Second-Opinion ({so_provider.name}) "
                        f"nicht verfügbar — übersprungen"
                    )
                else:
                    diff_text = _load_git_diff(
                        cwd, TOOL_RL_SECOND_OPINION_MAX_DIFF_CHARS
                    )
                    if diff_text is None:
                        print(
                            f"  [review-loop] Second-Opinion: git-Diff nicht "
                            f"verfügbar oder zu groß (> "
                            f"{TOOL_RL_SECOND_OPINION_MAX_DIFF_CHARS} chars) — "
                            f"übersprungen"
                        )
                    else:
                        print(
                            f"  [review-loop] === SECOND OPINION "
                            f"({so_provider.name}"
                            f"{':' + so_model_id if so_model_id else ''}) ==="
                        )
                        so_prompt = _SECOND_OPINION_PROMPT.format(
                            diff=diff_text,
                            primary_findings=(
                                "\n".join(findings) if findings else "(none)"
                            ),
                        )
                        prev_model = getattr(so_provider, "_forced_model", None)
                        if so_model_id is not None:
                            so_provider._forced_model = so_model_id
                        try:
                            so_result = so_provider.run(
                                so_prompt,
                                cwd=cwd,
                                timeout=TOOL_RL_SECOND_OPINION_TIMEOUT_SEC,
                                read_only=True,
                            )
                        finally:
                            so_provider._forced_model = prev_model
                        tokens.add(so_result)

                        if not so_result.success:
                            print(
                                f"  [review-loop] Second-Opinion fehlgeschlagen: "
                                f"{so_result.error} — wird ignoriert"
                            )
                        else:
                            extra = _parse_findings(so_result.output.strip())
                            so_no_findings = _is_no_findings_output(
                                so_result.output.strip()
                            )
                            all_outputs.append(
                                f"--- Second Opinion ({so_provider.name}) ---\n"
                                f"{so_result.output}"
                            )
                            if extra:
                                merged = _merge_findings(findings, extra)
                                new_count = len(merged) - len(findings)
                                findings = merged
                                # Any new finding invalidates the no_findings shortcut.
                                if new_count > 0:
                                    no_findings = False
                                print(
                                    f"  [review-loop] Second-Opinion: "
                                    f"{new_count} zusätzliche Findings"
                                )
                            elif so_no_findings:
                                print(
                                    f"  [review-loop] Second-Opinion bestätigt: "
                                    f"keine zusätzlichen Findings"
                                )
                            else:
                                print(
                                    f"  [review-loop] Second-Opinion-Output "
                                    f"ohne parsbare Findings — ignoriert"
                                )

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
                        read_only=True,  # verification must not edit files
                        **_session_kwargs(),
                    )
                    if not verify_result.success and verify_result.error == "session_missing":
                        print("  [review-loop] ⚠️ Session missing in Verification — Fallback")
                        sess.rollover(self.name, cwd)
                        first_call = True
                        verify_result = provider.run(
                            verify_prompt, cwd=cwd, timeout=verification_timeout,
                            read_only=True,
                            **_session_kwargs(),
                        )
                    tokens.add(verify_result)

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

                # Auto-lesson: generate LLM summary if it took more than 1 iteration
                if iteration > 1 and memory_module is not None:
                    print(f"  [review-loop] Generiere Lesson Learned...")
                    memory_module.create_lesson_from_loop(
                        self.name, task, all_outputs, provider, cwd=cwd
                    )

                tracer.emit("run_end", success=True, iterations=iteration)
                notify_tool_done(self.name, iteration, True, msg)
                return ToolResult(
                    success=True,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    **tokens.as_kwargs(),
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
                    **tokens.as_kwargs(),
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

            # Stable prefix (cached) + volatile suffix (iteration-specific).
            fix_prompt = (
                f"{system_prompt}\n\n"
                + _FIX_PROMPT_STABLE
                + _FIX_PROMPT_VOLATILE.format(
                    iteration=iteration,
                    findings=findings_text,
                    lessons_hint=lessons_hint,
                )
            )

            fix_result = provider.run(
                fix_prompt,
                cwd=cwd,
                timeout=fix_timeout,
                **_session_kwargs(),
            )
            if not fix_result.success and fix_result.error == "session_missing":
                print("  [review-loop] ⚠️ Session missing in Fix — Fallback")
                sess.rollover(self.name, cwd)
                first_call = True
                fix_result = provider.run(
                    fix_prompt, cwd=cwd, timeout=fix_timeout,
                    **_session_kwargs(),
                )
            tokens.add(fix_result)

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
                    **tokens.as_kwargs(),
                )

            all_outputs.append(f"--- Fix {iteration} ---\n{fix_result.output}")
            print(f"  [review-loop] Fix durchgeführt. Starte Re-Review...")

            # Phase B: rollover session every cap iterations to bound conversation
            # length. Review prompt always reads `git diff` fresh, so a fresh
            # session continues without loss.
            sess.bump()
            if sess.needs_rollover():
                print(
                    f"  [review-loop] Session-Rollover nach {sess.iteration_count} "
                    f"Iterationen (cap={sess.cap})"
                )
                sess.rollover(self.name, cwd)
                first_call = True

            # Small pause between iterations
            time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

        # Max iterations reached
        msg = f"Max Iterationen ({TOOL_MAX_ITERATIONS}) erreicht. Noch Findings offen."
        print(f"  [review-loop] ⚠️ {msg}")
        tracer.emit("run_end", success=False, reason="max_iterations", iterations=TOOL_MAX_ITERATIONS)
        notify_tool_done(self.name, TOOL_MAX_ITERATIONS, False, msg)
        return ToolResult(
            success=False,
            output="\n\n".join(all_outputs),
            iterations=TOOL_MAX_ITERATIONS,
            error=msg,
            **tokens.as_kwargs(),
        )
