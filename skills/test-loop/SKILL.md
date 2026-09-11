---
name: test-loop
description: Run tests, fix failures, re-run until all tests pass
version: "1.0"
requires:
  bins: []
  env: []
  os: []
  providers: []
tags: ["testing", "quality"]
config:
  max_iterations: 10
  timeout_minutes: 40
---
## System Prompt Addition

Run the test suite, analyze any failures, fix the root causes, and re-run.
Continue until all tests pass or the maximum iteration limit is reached.
Report a summary of what was fixed and the final test status.

**Rundenreflexion (from iteration 2 on):** before fixing, the fixer writes a
`## Rundenreflexion` section asking whether it is over-building or chasing an edge case
beyond the task. A failing test judged to be such an edge case may be deferred instead of
fixed: `- [BEKANNTE GRENZE] <test id> — <reason>`. No P1/P2/P3 model exists here — a
deferral never turns the run green; it stays red and is listed as "Bekannte Grenzen" in
the final failure message.
