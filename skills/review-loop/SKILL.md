---
name: review-loop
description: Iterative code review — fix P1+P2 until clean, report P3 as an offer
version: "1.0"
requires:
  bins: []
  env: []
  os: []
  providers: []
tags: ["review", "quality", "code"]
config:
  # Descriptive only — no production code reads skill.config. The value that actually
  # applies is config.TOOL_MAX_ITERATIONS (tools/review_loop.py). Kept in sync so this
  # file does not state a different number than the loop enforces.
  max_iterations: 20
  timeout_minutes: 20
---
## System Prompt Addition

Perform an iterative code review. Classify findings as:
- P1 (blocker): bugs, security issues, data loss risks — blocking
- P2 (important): performance problems, maintainability issues — blocking
- P3 (minor): style, naming, minor improvements — NOT blocking

After each round, fix all P1 and P2 findings, then re-review. The loop ends when no
P1 or P2 remains — not when the finding list is empty. Max 20 iterations
(`config.TOOL_MAX_ITERATIONS`).

**P3 is not fixed here.** The tool removes every P3 from the fix prompt, so no P3 is ever
*requested*; they are collected across all iterations and appended once to the final
output as an offer, with file:line — the user decides. Do not ask for P3 fixes in this loop.

Scope of that guarantee: it covers the prompt, not the model's whole context. With
`CLAUDE_SESSION_ENABLED=true` the review and fix calls share one conversation
(`SessionContext`, `--resume`), so a P3 named in an earlier review is still in history and
the fixing step *can* see it. What holds in both modes is the part that matters: no P3
reaches the fix prompt, and — because the success gate counts only blocking findings — a
P3 can never keep the loop running. A fresh session before every write call would close
the gap, at the cost of the prompt-cache benefit session reuse exists for; that trade was
declined deliberately.

Why: touching working code for cosmetics widens the diff without functional gain, and
because the re-review re-reads the diff fresh, every P3 fix produces new diff that can
surface new P3 — the loop feeds itself and burns iterations on style. Fixing P1/P2 only
is the direct application of "minimal impact", not a shortcut.
