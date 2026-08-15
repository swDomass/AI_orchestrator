---
name: review-loop
description: Iterative code review — fix all P1/P2/P3 findings until clean
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
- P1 (blocker): bugs, security issues, data loss risks
- P2 (important): performance problems, maintainability issues
- P3 (minor): style, naming, minor improvements

After each round, fix ALL findings (P1, P2, and P3), then re-review.
Continue until no findings remain. Max 20 iterations.
