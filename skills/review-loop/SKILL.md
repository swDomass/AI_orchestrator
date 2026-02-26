---
name: review-loop
description: Iterative code review with P1/P2/P3 findings until clean
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

After each round, fix all P1 and P2 findings, then re-review.
Continue until no P1 or P2 findings remain. Report final P3 findings as summary.
