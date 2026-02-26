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
