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
  max_iterations: 10
  timeout_minutes: 150
---
## System Prompt Addition

Implement tasks in a structured 3-phase loop:

1. **Research**: Analyze the codebase, read relevant files, and create a concrete implementation plan.
   Search the web only when local code and docs are insufficient.

2. **Execute**: Implement the solution based on research findings.
   On subsequent iterations, also fix all issues found in previous reviews.

3. **Dual-Review** (both must pass before finishing):
   - **Quality Review** (P1/P2/P3): correctness, security, performance, maintainability,
     testability, robustness, documentation, compliance.
     P1 = critical/crash/security | P2 = significant | P3 = minor (non-blocking)
   - **Resolution Review** (RESOLVED/PARTIAL/UNRESOLVED): does the code solve the
     original task 100%? Quality is irrelevant here — only task completion matters.

Loop repeats until both reviews pass. No auto-commit or push. Output written to `.dev-loop/`.
