---
name: dev-loop
description: Research → Execute → Dual-Review Loop (Code Quality + Issue Resolution) bis beide Reviews bestanden
version: "1.0"
requires:
  bins: ["git"]
  env: []
  os: []
  providers: []
tags: ["dev", "review", "quality", "issue", "feature", "coding"]
config:
  # Descriptive only — no production code reads skill.config. The value that actually
  # applies is config.TOOL_MAX_ITERATIONS (tools/dev_loop.py). Kept in sync so this file
  # does not state a different number than the loop enforces.
  max_iterations: 20
  timeout_minutes: 150
---
## System Prompt Addition

Implement tasks in a structured 3-phase loop:

1. **Research+Plan** (merged into ONE subprocess call): Analyze the codebase, read
   relevant files, AND produce a concrete implementation plan in the same response.
   Search the web only when local code and docs are insufficient. Output: `.dev-loop/research-and-plan.md`.

2. **Execute**: Implement the solution based on the research+plan output.
   On subsequent iterations, also fix all **P1 and P2** findings from previous reviews.
   **No P3 is ever requested here.** The tool passes only blocking findings into the next
   execution prompt, so there is no "take a P3 along while the file is open" decision to
   make. Every P3 is collected across all iterations and appended once to the final output
   as an offer; the user decides. Caveat: with `CLAUDE_SESSION_ENABLED=true` all phases
   share one conversation, so a P3 from an earlier review is still in history and the
   executor *can* see it — the guarantee covers the prompt, not the whole context. What
   holds in both modes is that no P3 gates progress.
   Why: touching working code for cosmetics widens the diff without functional gain, and
   since each review re-reads the diff fresh, every P3 fix can surface new P3 — the loop
   would feed itself and burn iterations on style.

3. **Dual-Review** (both must pass before finishing, both read-only):
   - **Quality Review** (P1/P2/P3): correctness, security, performance, maintainability,
     testability, robustness, documentation, compliance.
     P1 = critical/crash/security | P2 = significant | P3 = minor (non-blocking)
   - **Resolution Review** (RESOLVED/PARTIAL/UNRESOLVED): does the code solve the
     original task 100%? Quality is irrelevant here — only task completion matters.
   Both review phases re-read `git diff` fresh; they do not trust pinned context.

Loop repeats until both reviews pass. No auto-commit or push. Output written to `.dev-loop/`.

When `CLAUDE_SESSION_ENABLED=true` (opt-in), all phases share a Claude `--session-id`/`--resume`
session for prompt-cache reuse, with iteration-cap=5 rollover.
