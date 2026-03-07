---
name: research-qa
description: Deep Research + Fragen-Katalog vor Implementierungsbeginn
version: "1.0"
requires:
  bins: []
  env: []
  os: []
  providers: []
tags: ["research", "questions", "planning", "analysis", "pre-implementation"]
config:
  timeout_minutes: 60
---
## System Prompt Addition

Perform a structured pre-implementation research workflow:

1. **Discovery**: Explore the codebase thoroughly — read docs, directory structure,
   relevant source files, tests, configs, git history. Understand the project's
   architecture, patterns, and conventions. Do NOT modify any files.

2. **Analysis**: Think deeply about implementation approaches (2-3 alternatives),
   required changes, data/API impact, security, performance, testing strategy,
   risks, and edge cases. Do NOT modify any files.

3. **Question Generation**: Produce a prioritized list of questions the developer
   must answer before starting. Mark critical blockers with [BLOCKING]. Reference
   concrete code where relevant. Suggest options where possible.

Output written to `.research-qa/`. This is a read-only tool — no code changes.
