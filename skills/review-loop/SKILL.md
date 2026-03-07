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
  max_iterations: 10
  timeout_minutes: 20
---
## System Prompt Addition

Perform an iterative code review. Classify findings as:
- P1 (blocker): bugs, security issues, data loss risks
- P2 (important): performance problems, maintainability issues
- P3 (minor): style, naming, minor improvements

After each round, fix ALL findings (P1, P2, and P3), then re-review.
Continue until no findings remain. Max 20 iterations.
