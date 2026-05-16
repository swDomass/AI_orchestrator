# AI Orchestrator

Autonomous task executor for `claude`, `gemini`, and `codex` CLI tools — driven by a Markdown queue file, with multi-provider fallback, Telegram control, and a security/approval layer.

**Goal:** Run routine work (code, reviews, tests, docs, refactors) from a Markdown queue without managing API keys in your project. Uses existing CLI logins (OAuth/subscription).

## Features

- Multi-provider routing with fallback (`Claude → Gemini → Codex`)
- Capacity checking via `cclimits` (with local JSONL fallback on HTTP 429)
- Retry handling on rate limits / provider failures
- Obsidian-compatible queue with `cwd:`, `#tool:`, `#agent:`, `#parallel`, `#shutdown`, `#approve:*` tags
- Tool loops: `dev-loop`, `review-loop`, `test-loop`, `research-qa`, `security-audit`, `deep-security-audit`, `critical-review`, `knowledge-transfer`, `scientific-investigation`, `brainstorm`
- Skills / `SKILL.md` discovery with requirements gating
- Memory (TF-IDF + temporal decay) for recurring tasks
- Execution profiles (provider order, allowed skills, timeout, policy overrides)
- Execution policy (`AUTO` / `APPROVE` / `DENY`) with Telegram approval flows
- Telegram listener (queue control, status, plain-text AI chat)
- Heartbeat + Doctor (monitoring / onboarding checks)
- Analytics web dashboard (Chart.js, port 8411)
- `SOUL.md` as central prompt/personality configuration
- **Anthropic prompt-cache optimization**: static system-prompt (cwd/git-status moved to first user message via `--exclude-dynamic-system-prompt-sections`), stable prompt prefixes, billing analytics with cache-hit-rate
- **Optional Claude session reuse** (`CLAUDE_SESSION_ENABLED=true`): `dev-loop`, `review-loop`, same-provider `critical-review`, and `deep-security-audit` share Claude `--session-id`/`--resume` across phases for cross-call cache hits

## Requirements

- Python `3.10+`
- `cclimits` CLI (`npm install -g cclimits`)
- Provider CLIs in `PATH`: `claude`, `gemini`, `codex`
- Valid authentication in each CLI (OAuth / subscription login)

## Installation

```bash
git clone https://github.com/swDomass/AI_orchestrator.git
cd AI_orchestrator
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your vault path and optional Telegram credentials
```

`requirements.txt` includes:
- `pyyaml>=6.0`
- `claude-monitor>=3.0.0` *(optional — enables local JSONL fallback for Claude HTTP 429; requires `CLAUDE_PLAN` in `.env`)*

## Configuration

All configuration lives in `.env` (auto-loaded, no external dotenv library needed).

| Variable | Required | Default | Description |
|---|---|---|---|
| `ORCH_VAULT_PATH` | **Yes** | — | Path to your Obsidian vault (or any directory) |
| `ORCH_QUEUE_FILE` | No | `<vault>/99_System/AI/agent-queue.md` | Direct path to queue file |
| `ALLOWED_CWD_ROOTS` | No | *(allow all)* | Semicolon-separated list of root paths; `cwd:` tags are validated against these. Leave empty to allow all paths. |
| `TELEGRAM_BOT_TOKEN` | No | — | Telegram bot token (from @BotFather) |
| `TELEGRAM_CHAT_ID` | No | — | Your Telegram chat ID |
| `MIN_CAPACITY_PERCENT` | No | `10` | Global minimum remaining capacity (%) before a provider is skipped |
| `CLAUDE_FIVE_HOUR_MIN_CAPACITY_PCT` | No | `10` | Per-window override for Claude 5h window |
| `CLAUDE_SEVEN_DAY_MIN_CAPACITY_PCT` | No | `3` | Per-window override for Claude 7d window |
| `CODEX_PRIMARY_MIN_CAPACITY_PCT` | No | `10` | Per-window override for Codex primary |
| `CODEX_SECONDARY_MIN_CAPACITY_PCT` | No | `3` | Per-window override for Codex secondary |
| `CLAUDE_PLAN` | No | — | Claude subscription plan for local 429 fallback: `pro`, `max5`, `max20`, `custom` |
| `OPENROUTER_API_KEY` | No | — | OpenRouter API key. When set, enables `#openrouter`/`#or_*` tags as an opt-in pay-per-token provider for non-agentic tasks (heartbeat checks, summaries). Never enters the default fallback chain. |
| `OPENROUTER_BASE_URL` | No | `https://openrouter.ai/api/v1` | Override for testing or self-hosted proxy |
| `OPENROUTER_DEFAULT_MODEL` | No | `minimax/minimax-m2.5:free` | Model used by `#openrouter` without a specific `#or_*` alias |
| `DASHBOARD_PORT` | No | `8411` | Port for the analytics web dashboard |
| `TELEGRAM_MAX_TASK_LENGTH` | No | `500` | Max characters for `/task` command |
| `CLAUDE_SESSION_ENABLED` | No | `false` | Opt-in: Claude `--session-id`/`--resume` across tool phases for prompt-cache reuse. Off = today's stateless behaviour. |
| `ORCH_SESSION_RETENTION_DAYS` | No | `14` | Heartbeat session-cleanup retention for orchestrator-created session JSONL files in `~/.claude/projects/`. Whitelist via sidecar registry. |

See `.env.example` for a complete annotated template.

## Quick Start

```bash
# Validate setup (CLIs, vault, queue, Telegram, skills, ...)
python orchestrator.py --doctor

# Process queue once
python orchestrator.py

# Watch mode (heartbeat + Telegram listener + auto-retry)
python orchestrator.py --watch
```

### Crash-Resistant Watchdog (Windows)

For long-running unattended operation, use the PowerShell watchdog wrapper. It restarts the orchestrator on crashes with exponential backoff and sends a Telegram alert on every restart. Works on both Windows PowerShell 5.1 and PowerShell 7+:

```powershell
# Foreground (visible console)
pwsh -File run_orchestrator.ps1
# Or, if you only have Windows PowerShell:
powershell.exe -File run_orchestrator.ps1

# Background — survives terminal close
Start-Process pwsh -ArgumentList "-File run_orchestrator.ps1" -WindowStyle Hidden
```

Behaviour:
- Exit 0 (Ctrl+C, `#shutdown`) ends the loop cleanly
- Non-zero exit → restart with backoff (10 s → 5 min cap)
- ≥5 crashes / 10 min → 30 min cooldown + Telegram alert
- All restarts logged to `logs/watchdog.log` (rotated at 10 MB → `watchdog.log.1`)
- Telegram credentials read directly from `.env` (no Python helper); URL-encodes the token, strips UTF-8 BOM, honors `# inline comments` per Python's dotenv rules

## CLI Commands

```bash
python orchestrator.py                  # Single queue pass
python orchestrator.py --watch          # Continuous mode
python orchestrator.py --dry-run        # Parse queue without executing
python orchestrator.py --check-limits   # Show provider capacity
python orchestrator.py --list-tools     # Show available #tool: handlers
python orchestrator.py --dashboard      # Launch analytics dashboard
python orchestrator.py --doctor         # Validate setup
python orchestrator.py --doctor --fix   # Auto-fix issues
python orchestrator.py --doctor --fix --yes
python orchestrator.py --lint-queue     # Validate agent-queue.md (no execution)
```

### `--lint-queue` (offline queue validation)

Runs a pure-validation pass over `agent-queue.md`. No LLM calls. Catches:

- Invalid / missing `cwd:` (path doesn't exist or outside `ALLOWED_CWD_ROOTS`)
- Unknown `#tool:<name>`
- Unknown model alias (`#claude_*`, `#gemini_*`, `#codex_*`, `#or_*`)
- Cross-provider model leakage (`#claude_opus` + explicit `#gemini` = error)
- Duplicate `#id:` values in the open queue
- `#needs:` referencing IDs that will never resolve (warning)
- `#openrouter` / `#or_*` without `OPENROUTER_API_KEY` configured (warning — task falls back to default chain)
- `#parallel` with 0-1 subtasks (warning) or shared CWD (info)

Exit codes: **0** = clean, **1** = warnings only, **2** = errors. Wire into CI / pre-commit if you have a shared queue file.

## Queue File Syntax

The queue is read from Markdown. Open tasks are standard checkbox lines:

```md
- [ ] Fix bug in parser cwd:D:\projects\app #codex #timeout:10m
- [ ] Review + fix repo #tool:review-loop cwd:"D:\projects\my repo" #agent:work
- [ ] Fix login bug #tool:dev-loop cwd:D:\projects\app
- [ ] Add CSV export #tool:dev-loop cwd:D:\projects\app #agent:work
- [ ] Add OAuth2 flow #tool:research-qa cwd:D:\projects\app
- [ ] Architecture audit #tool:critical-review cwd:D:\projects\app
- [ ] Prüfe docs/plan.md #tool:critical-review #pass1:claude #pass2:gemini cwd:D:\projects\app
- [ ] Security audit #tool:security-audit cwd:D:\projects\app
- [ ] Deep security audit #tool:deep-security-audit cwd:D:\projects\app
- [ ] Deep audit (no fix) #tool:deep-security-audit #no-fix cwd:D:\projects\app
- [ ] Deep audit with cross-expert dialog #tool:deep-security-audit #roundtable cwd:D:\projects\app
- [ ] Brainstorm pricing strategy #tool:brainstorm cwd:D:\projects\app
- [ ] Brainstorm with cross-provider personas #tool:brainstorm #cross-provider #top_n:7 cwd:D:\projects\app
```

The orchestrator automatically appends `## Results` and `## Log` sections to each task.

### Supported Tags

| Feature | Syntax | Example |
|---|---|---|
| Force provider | `#claude`, `#gemini`, `#codex` | `- [ ] Task #codex` |
| Claude model | `#claude_haiku`, `#claude_sonnet`, `#claude_opus` | `- [ ] Task #claude_haiku` |
| Gemini model | `#gemini_pro`, `#gemini_flash`, `#gemini_flash_lite` | `- [ ] Iterate #gemini_flash` |
| Codex model | `#codex_5` (gpt-5.5), `#codex_5_4` (gpt-5.4), `#codex_mini` (gpt-5.4-mini) | `- [ ] Run #codex_mini` |
| OpenRouter model (opt-in, requires `OPENROUTER_API_KEY`) | Free: `#or_minimax_free`, `#or_deepseek_free`, `#or_qwen_free`, `#or_nemotron_free`. Paid flagships: `#or_glm`, `#or_kimi`, `#or_qwen`, `#or_deepseek`, `#or_minimax`. Generic: `#openrouter` (default model). | `- [ ] Daily summary #or_minimax_free` |
| Run tool | `#tool:<name>` | `- [ ] Review #tool:review-loop` |
| Restrict providers (task-level) | `#tool_providers:<p1,p2>` | `#tool_providers:claude,gemini` |
| Working directory | `cwd:<path>` | `cwd:D:\projects\repo` |
| Working directory with spaces | `cwd:"<path>"` | `cwd:"D:\My Projects\App"` |
| Timeout | `#timeout:<n>[s\|m\|h]` | `#timeout:30s`, `#timeout:15m`, `#timeout:1h` |
| Execution profile | `#agent:<name>` | `#agent:work` |
| Parallel task | `#parallel` | Parent task with indented subtasks |
| Task ID | `#id:<name>` | `- [ ] Build backend #id:build` |
| Task dependency | `#needs:<id1,id2>` | `- [ ] Test #needs:build` |
| Shutdown after task | `#shutdown` | `- [ ] Backup #shutdown` |
| Cross-provider pass | `#pass1:<provider>`, `#pass2:<provider>` | `#pass1:claude #pass2:gemini` |
| Preapproval | `#approve:<category,...>` | `#approve:push,publish` |

### Parallel Tasks (`#parallel`)

```md
- [ ] Release prep #parallel #agent:work
  - run tests #tool:test-loop cwd:D:\proj
  - review code #tool:review-loop cwd:D:\proj
  - update changelog cwd:D:\proj #codex
```

- Subtasks with the **same `cwd`** run sequentially within a group.
- Subtasks with **different `cwd`s** run in parallel threads.
- One subtask failing does not stop the others.

### Task Dependencies (`#id:` / `#needs:`)

```md
- [ ] Build backend #id:build cwd:D:\projects\app
- [ ] Run integration tests #id:tests #needs:build cwd:D:\projects\app
- [ ] Deploy to staging #needs:build,tests cwd:D:\projects\app
```

- A task with `#needs:` stays **blocked** until all named IDs appear as `[x]` (done) or `[-]` (failed).
- Blocked tasks are **not removed** from the queue — they stay open and are re-checked each cycle.
- The queue header shows `(N runnable, M blocked)` when blocked tasks are present.
- `[-]` (failed) tasks also unblock dependents — the downstream task decides how to handle failure.

### Retry Markers

```md
- [ ] Task <!-- retry: 2026-02-26 23:10 -->
```

## Built-in Tools

| Tool | Description |
|---|---|
| `dev-loop` | Research → Execute → Dual-Review loop (Code Quality + Issue Resolution). Both reviews must pass. Output in `{cwd}/.dev-loop/`. |
| `review-loop` | Iterative Review → Fix → Re-Review loop. Fixes ALL P1/P2/P3 findings. Max 20 iterations with infinite-loop detection. |
| `test-loop` | Iterative test / fix loop until tests pass or max iterations. |
| `research-qa` | Read-only pre-implementation research: Discovery → Analysis → Question catalogue. Output in `{cwd}/.research-qa/`. No code changes. |
| `knowledge-transfer` | Cross-domain knowledge transfer: Vault expertise → industry applications (via web search) → Obsidian idea note. |
| `critical-review` | 3-pass adversarial review: analysis → challenge → synthesis. Reference a plan file to get `{name}-v2.md`. Cross-provider via `#pass1:claude #pass2:gemini`. Output in `{cwd}/docs/critical-review-*.md`. |
| `security-audit` | Two-phase workflow: Audit (read-only) → Fix + pytest. Scans for hardcoded secrets, command injection, path traversal, unsafe deserialization, SSRF, and more. Output in `{cwd}/docs/security-audit-*.md`. |
| `deep-security-audit` | Multi-agent deep audit: 6 expert personas (pentester, architect, SAST, supply chain, data privacy, forensics) + CISO synthesis + optional fix. `#no-fix` skips fix phase. `#roundtable` inserts a Phase 6.5 dialogue where each persona reviews the others' findings (~6 extra subprocess calls, more robust CISO synthesis on conflicting findings). Output in `{cwd}/docs/deep-security-audit-*.md`. Structured action trace at `{cwd}/.deep-security-audit/traces/<run_id>.jsonl`. |
| `scientific-investigation` | Wissenschaftlicher Autopilot mit Audit-Trail. Pipeline (Plan v5, I0–I9): Framing + Pre-Registration → Multi-Persona Review (Author + Devils-Advocate + Methodiker) → Sub-Task-Execution-Loop → Synthesis mit Falsifikations-Tabelle → Mechanical & heuristic check → Engineering-Reviewer Rework → Final Telegram-Approval. Status-Tuple `methodological_rigor=MEDIUM\|LOW` (HIGH strukturell ausgeschlossen). Output in `{cwd}/docs/scientific-investigation-{ts}/` + audit-pack via `scripts/build_audit_pack.py`. |
| `brainstorm` | Multi-persona Round-Table mit **domain-aware Personas**: LLM analysiert das Thema und wählt 4–6 themenspezifische Personas, die in Cross-Pollination-Runden Ideen produzieren bis Konvergenz (TF-IDF Cluster-Wachstum < 20 %) oder Hard-Cap (5 Iterationen). `#cross-provider` verteilt Personas Round-Robin über alle verfügbaren Provider. Synthesizer ranked Top-N (default 5) mit Pro/Contra/Nächster-Schritt. Output in `{cwd}/docs/brainstorm-*.md`, State + Per-Iteration-Files in `{cwd}/.brainstorm/{ts}/`. |

```bash
python orchestrator.py --list-tools
```

## Dev-Loop (`#tool:dev-loop`)

```
Phase 1 — Research + Plan  (merged into ONE subprocess call)
  Reads relevant code, understands the problem/feature,
  AND produces the implementation plan in the same response.
  Web search only if local sources are insufficient.
  → Saved to {cwd}/.dev-loop/research-and-plan.md
  → State persisted under phase=research_and_plan_done for capacity-resume.

Phase 2 — Execution
  Implements the solution based on the merged research+plan output.
  On iteration > 1: includes findings from both prior reviews.

Phase 3a — Code Quality Review  (P1/P2/P3, read-only)
  Checks: Correctness, Clean, Secure, Performant, Maintainable,
          Testable, Robust, Documented, Compliant.
  P1/P2 = blocking | P3 = non-blocking
  Re-reads `git diff` fresh — does NOT trust pinned context.

Phase 3b — Issue Resolution Review  (RESOLVED/PARTIAL/UNRESOLVED, read-only)
  Checks only: Does the code solve the original task 100%?
  Ignores code quality entirely. Re-reads `git diff` fresh.

→ Both reviews must pass → loop ends, no auto-push.
→ Per-iteration output in {cwd}/.dev-loop/round-NNN.md
→ Final summary: {cwd}/.dev-loop/summary.md
```

**Phase B opt-in**: When `CLAUDE_SESSION_ENABLED=true`, all phases share a Claude session (`--session-id` / `--resume`) for cross-call prompt-cache hits. Iteration cap of 5 per session triggers a rollover to a fresh UUID; explicit findings re-injection in the exec prompt makes the rollover seamless.

**Timeout configuration (`config.py`):**

| Constant | Default | Phase |
|---|---|---|
| `TOOL_DEV_RESEARCH_TIMEOUT_SEC` | 3600 (60 min) | Research portion of merged Phase 1 |
| `TOOL_DEV_PLAN_TIMEOUT_SEC` | 1800 (30 min) | Plan portion of merged Phase 1 (added to research timeout) |
| `TOOL_DEV_EXEC_TIMEOUT_SEC` | 7200 (2 h) | Execution |
| `TOOL_DEV_QUALITY_REVIEW_TIMEOUT_SEC` | 3600 (60 min) | Quality Review |
| `TOOL_DEV_RESOLUTION_REVIEW_TIMEOUT_SEC` | 1800 (30 min) | Resolution Review |

## Research-QA (`#tool:research-qa`)

```
Phase 1 — Discovery
  Explores codebase: docs, directory structure, relevant source files,
  tests, configs, git history. No code is changed.
  → Saved to {cwd}/.research-qa/01-discovery.md

Phase 2 — Analysis
  Deep analysis: 2–3 implementation approaches (pros/cons/effort/risk),
  security, performance, testing strategy, risks, edge cases.
  → Saved to {cwd}/.research-qa/02-analysis.md

Phase 3 — Questions
  Prioritised question catalogue (8–20 questions) with:
  - [BLOCKING] markers for critical blockers
  - Concrete code references
  - Suggested options (Option A / Option B)
  Categories: Requirements, Architecture, Scope, Technical Unknowns,
  Risk & Rollback, Testing & Validation.
  → Saved to {cwd}/.research-qa/03-questions.md

→ Combined document: {cwd}/.research-qa/research-qa-complete.md
→ No code changes — pure analysis and questions.
```

## Critical Review (`#tool:critical-review`)

3-pass adversarial review with optional cross-provider support:

```
Pass 1 — Analysis
  Radical-honesty review: concept, architecture, code quality,
  operational reality, methodology, blind spots.
  → Saved to {cwd}/docs/critical-review-*-pass1.md

Pass 2 — Adversarial Challenge
  A different persona challenges Pass 1's findings: missed angles,
  overclaims, underclaims, contradictions.
  Can use a different provider for real perspective diversity.

Pass 3 — Synthesis (only when plan file referenced)
  Produces an improved version of the plan based on both reviews.
  → Saved to {plan_dir}/{name}-v2.md

→ Combined report: {cwd}/docs/critical-review-YYYYMMDD-HHMMSS.md
```

**Usage examples:**

```md
# Review-only (no plan file → 2 passes)
- [ ] Review auth module #tool:critical-review cwd:D:\projects\app

# Plan review with improved output (3 passes)
- [ ] Prüfe docs/plan.md #tool:critical-review cwd:D:\projects\app

# Cross-provider (Claude analyzes, Gemini challenges)
- [ ] Prüfe docs/plan.md #tool:critical-review #pass1:claude #pass2:gemini cwd:D:\projects\app

# Same provider for both passes
- [ ] Prüfe [[MyPlan]] #tool:critical-review #pass1:claude #pass2:claude cwd:D:\projects\app
```

Plan files can be referenced as file paths (`docs/plan.md`) or wikilinks (`[[MyPlan]]`).

## Brainstorm (`#tool:brainstorm`)

Multi-persona round-table with **domain-aware personas** — the LLM picks 4–6 themenspezifische Rollen (z. B. für Pricing: Daten-Analyst, Boutique-Verkäuferin, Mitbewerber, Braut-Kundin), die in Cross-Pollination-Runden Ideen produzieren und gegenseitig challengen.

```
Phase 0 — Topic-Analyse + Persona-Generierung
  LLM analysiert das Thema und schlägt 4–6 unique Personas vor
  (kebab-case keys, je system_prompt ≥ 100 chars, paarweise distinct).
  → Sequentielle Validierung in parse_personas (Count, Keys, Prompt-Länge).

Phase 0.5 — Provider-Allocation
  Default: alle Personas auf Primary-Provider.
  Mit #cross-provider: Round-Robin über (claude, gemini, codex, openrouter).
  Degradiert sauber auf primary-only wenn keine Cross-Provider verfügbar.

Phase 1 — Initial Idea Generation
  Jede Persona unabhängig: bis zu 10 Ideen aus ihrer spezifischen Perspektive.
  Output pro Persona in {cwd}/.brainstorm/{ts}/iteration-1-{key}.md.

Phase 2 — Cross-Pollination (iterativ)
  Jede Persona sieht die Ideen der anderen + ihre eigenen,
  contributes Aufbau-/Synthese-/Challenge-/Gap-Ideen.

K — Konvergenz-Check (deterministisch, kein LLM-Call)
  TF-IDF Jaccard-Cosine Clustering (Threshold 0.40).
  Stop wenn neue Cluster < 20 % vom Total. Hard-Cap 5 Iterationen.

Phase 3 — Synthese + Ranking
  Synthesizer (Primary-Provider) wählt Top-N (default 5)
  mit Pro/Contra/Nächster-Schritt aus allen Clustern.

→ Final-Report: {cwd}/docs/brainstorm-YYYYMMDD-HHMMSS.md
→ State + Iterations: {cwd}/.brainstorm/{ts}/
→ Trace: {cwd}/.brainstorm/traces/<run_id>.jsonl
```

**Usage examples:**

```md
# Default: alle Personas auf Primary, 5 Iterationen max
- [ ] Brainstorm Pricing-Strategie WhiteLady #tool:brainstorm cwd:D:\projects\whitelady

# Cross-provider Diversität: jeder Persona ein anderes LLM
- [ ] Marketing-Ideen #tool:brainstorm #cross-provider #top_n:7 cwd:D:\projects\whitelady

# Persona-Count und Iter-Cap überschreiben
- [ ] Feature-Priorisierung #tool:brainstorm #min_personas:5 #max_personas:5 #max_iterations:3 cwd:D:\projects\app
```

**Tags:**

| Tag | Default | Range | Wirkung |
|---|---|---|---|
| `#cross-provider` | off | – | Round-Robin über alle verfügbaren Provider statt primary-only |
| `#max_iterations:N` | 5 | 1–10 | Hard-Cap auf die Konvergenz-Schleife |
| `#top_n:N` | 5 | 1–20 | Anzahl Top-Ideen im finalen Ranking |
| `#min_personas:N` | 4 | 2–10 | Untergrenze Persona-Count (LLM muss ≥ N liefern) |
| `#max_personas:N` | 6 | 2–10 | Obergrenze Persona-Count (LLM darf ≤ N liefern) |

**Tradeoffs:**

- `#cross-provider` skaliert die Kosten linear mit Persona-Count und Iterationen — Faustregel: 5 Personas × 3 Iter × 2 Phasen = ~30 LLM-Calls quer über alle Provider. Default-off ist bewusst.
- Konvergenz greift schneller bei thematisch klaren Topics; bei sehr breiten Fragestellungen erreicht der Hard-Cap das Ende des Loops.
- Empty-Topic (alle Tags entfernt → leerer String) wird vor dem ersten LLM-Call abgefangen (`error_code="empty_topic"`).

## Best Practice: Full Dev-Loop Workflow

A battle-tested 8-step queue pattern for implementing a plan end-to-end with cost-optimized model tiering. Strong models (Opus) handle value creation and final validation; cheaper tiers (`codex_mini`, `codex`) do the iterative cleanup; Gemini runs strictly read-only as a second opinion.

**Recommendation:** keep plans small (one feature / one phase per plan file) and apply this flow per plan. For multi-phase changes, split the plan file into several smaller ones — one commit per plan is cleaner than one commit for many phases.

```markdown
- [ ] Implement docs\plan-XXX.md. dont commit the changes! #id:ID1 #tool:dev-loop #claude_opus cwd:<repo>

- [ ] security-audit of the uncommitted changes. dont commit the changes! #id:ID2 #need:ID1 #tool:security-audit #claude_opus cwd:<repo>

- [ ] use your simplify skill for the uncommitted changes. dont commit the changes! #id:ID3 #need:ID2 #claude_sonnet cwd:<repo>

- [ ] Review-fix loop for the uncommitted changes. dont commit the changes! #tool:review-loop #id:ID4 #need:ID3 #codex_mini cwd:<repo>

- [ ] Review-fix loop for the uncommitted changes. dont commit the changes! #tool:review-loop #id:ID5 #need:ID4 #codex cwd:<repo>

- [ ] Critical review (read-only) of the uncommitted changes against docs\plan-XXX.md #tool:critical-review #pass1:gemini #pass2:claude #gemini_flash #id:ID6 #need:ID5 cwd:<repo>

- [ ] Review-fix loop for the uncommitted changes. Also incorporate findings from the most recent critical-review report in docs/. dont commit the changes! #tool:review-loop #id:ID7 #need:ID6 #claude_opus cwd:<repo>

- [ ] 1. check the uncommitted changes. 2. update all docs in the repo and the Obsidian Project. 3. commit it. #need:ID7 #claude_haiku cwd:<repo>
```

### Why this tiering

| Step | Model | Rationale |
|---|---|---|
| 1. dev-loop | `#claude_opus` | Core value creation; bad code here inflates every downstream step |
| 2. security-audit | `#claude_opus` | Finds subtle exploit chains; cheaper tiers miss logic flaws |
| 3. simplify | `#claude_sonnet` | Refactoring is a bounded task |
| 4. review-loop (pass A) | `#codex_mini` | Cheap first pass — obvious bugs, unused imports, trivial wins |
| 5. review-loop (pass B) | `#codex` (gpt-5.4) | Mid-tier — structural issues, missing coverage |
| 6. critical-review | `#gemini_flash` + `#pass2:claude` | Independent second opinion, strictly read-only — zero risk of broken code |
| 7. review-loop (final) | `#claude_opus` | Final validator; integrates critical-review findings. If Opus finds nothing here, the code is genuinely clean |
| 8. commit | `#claude_haiku` | Trivial — diff + doc sync + single commit. Escalate to `#claude_sonnet` if the plan spans multiple commits |

### Variants

- **Minimal** (trivial changes): dev-loop → review-loop `#codex_mini` → review-loop `#claude_opus` → commit `#claude_haiku`
- **Security-critical**: swap step 2 for `#tool:deep-security-audit` (6-agent deep scan)
- **Multi-commit plans**: raise step 8 to `#claude_sonnet` and instruct it to split via `git add -p`

### Gemini caveat

Gemini is included **only** in step 6 as `#tool:critical-review`, which is read-only and produces a report file. Do not use Gemini in `dev-loop` or `review-loop` — in write mode it has shown unreliable adherence to task specs.

## Skills (`SKILL.md`)

In addition to built-in tools, skills can be discovered from `SKILL.md` files.

Search order (higher priority overrides lower):
1. `<cwd>/.orchestrator/skills/<name>/SKILL.md`
2. `./skills/<name>/SKILL.md`
3. `<vault>/99_System/AI/Skills/<name>/SKILL.md`
4. `./tools/<name>/SKILL.md`

Skills can define requirements (binaries, env vars, OS, provider). Skills whose requirements are not met are gated rather than silently skipped.

## Execution Profiles (`#agent:<name>`)

Profiles are YAML files that bundle execution rules per task type.

Typical contents:
- Provider order
- Allowed / denied skills
- Timeout override
- Safety / sandbox level
- Profile-specific policy rules (`auto/approve/deny`)

Search locations:
- `<vault>/99_System/AI/profiles/<name>.yaml`
- `./profiles/<name>.yaml`

## Execution Policy & Approvals

The policy classifies tasks as:
- `AUTO` → runs without confirmation
- `APPROVE` → requires Telegram approval
- `DENY` → task is blocked

Policy file: `<vault>/99_System/AI/policy.yaml`

Telegram approval commands: `/approve`, `/approve-all <category>`, `/deny`, `/skip`

Tasks can also carry preapprovals: `#approve:push,publish`

## Telegram Control

In `--watch` mode a Telegram long-poll listener starts (when `TELEGRAM_*` env vars are set).

| Command | Description |
|---|---|
| `/task <text>` | Add free-form task to queue |
| `/review [cwd]` | Queue `#tool:review-loop` task for `cwd` (or last-cwd) |
| `/security [cwd]` | Queue `#tool:security-audit` task |
| `/audit [cwd]` | Queue `#tool:deep-security-audit` task |
| `/dev <desc> cwd:<path>` | Queue `#tool:dev-loop` task |
| `/critique <plan.md>` | Queue `#tool:critical-review` task (cwd = parent dir of plan) |
| `/brainstorm <topic>` | Queue `#tool:brainstorm` task (uses last-cwd) |
| `/status` | Queue size + provider status |
| `/limits` | Detailed limits with per-window breakdown |
| `/pause` / `/resume` | Pause / resume processing |
| `/approve`, `/approve-all <cat>`, `/deny`, `/skip` | Approval flow |
| `/pick N` | Accept usage suggestion (1–3) |
| `/decline` | Decline suggestions |
| `/cancel-shutdown` | Cancel pending shutdown |
| `/help` | Show available commands |

Plain text → AI chat (answered by best available provider).
`#shutdown` as standalone tag → schedule shutdown.

**Slash tool-commands** (`/review`, `/security`, `/audit`, `/dev`, `/critique`, `/brainstorm`) expand to a queue line with the right `#tool:` tag and run through the full pipeline (provider routing, policy, memory, approvals). Each chat has a RAM-only last-cwd memory: after one explicit `/review D:\foo`, subsequent commands without `cwd:` reuse that path. CWDs are validated against `ALLOWED_CWD_ROOTS` before any task is queued, and duplicate deliveries of the same Telegram message are deduplicated via `idempotency.py`.

Rate limits (anti-spam):

| Category | Limit |
|---|---|
| Commands | 20/min |
| AI chats | 5/min |
| Task adds | 10/min |

## Memory, Heartbeat, SOUL.md

- **Memory (`memory.py`)** — Four-layer architecture, ordered for max prompt-cache reuse:
  1. **Curated (`MEMORY.md`)**: Long-term patterns, conventions, decisions. Always in prompt. (Most static.)
  2. **Lessons Learned (`lessons.md`)**: LLM-summarized patterns from multi-iteration tool loops. CWD-filtered injection (universal `*` entries always, project-specific only when CWD matches). Semantic dedup via TF-IDF similarity at write time. (Stable per tool+cwd.)
  3. **Daily Logs (`daily/`)**: Append-only log for today + yesterday (temporal locality). (Grows during the day — placed AFTER lessons so daily growth doesn't break the cache prefix for tool reruns.)
  4. **TF-IDF Deep Search (`task_results/`)**: Keyword matching + temporal decay over all past tasks. (Most volatile — task-specific.)
  - Top-K relevant memories are intelligently injected into the prompt.
  - Auto-archival after 180 days.

- **Heartbeat (`heartbeat.py`)** — Proactive checks in `--watch` mode, configured via `<vault>/99_System/AI/HEARTBEAT.md`.
  - 10 built-in handlers: `queue-idle`, `git-status`, `disk-space`, `check-limits`, `log-capacity`, `summarize`, `stale-branch`, `usage-suggest`, `session-cleanup`, `model-check`
  - Sections support `## Every N minutes/hours/days` and `## Daily (after HH:MM)` — so a monthly check is just `## Every 30 days`.
  - `model-check` (recommended monthly): CLI-probes every entry in `CLAUDE_MODEL_ALIASES`/`GEMINI_MODEL_ALIASES`/`CODEX_MODEL_ALIASES` to detect dead IDs (skips providers currently in cooldown), then asks the best available LLM with today's date as anchor whether newer IDs are known. Telegram notification only fires on findings; LLM-call failures surface as `⚠️ LLM-Check failed: …` instead of being silently swallowed.
  - **Persistent state**: items with interval ≥ 1 day record their last run in `logs/heartbeat-state.json`, so a `## Every 30 days` check does NOT fire on every `--watch` restart.
  - `session-cleanup` deletes orchestrator-created Claude session JSONL files in `~/.claude/projects/**` older than `ORCH_SESSION_RETENTION_DAYS` — uses sidecar whitelist (`logs/orchestrator-sessions.jsonl`) to NEVER touch interactive Claude Code sessions.
  - Mtime-cached config — changes to `HEARTBEAT.md` take effect immediately (no restart).
  - Runs in a **daemon thread** (60s poll) so scheduled checks fire on time even during long-running tasks.

- **Usage Suggester (`usage_suggester.py`)** — Detects when Claude limits are about to reset with capacity still available. Proactively suggests 2–3 tasks via Telegram (skills, git changes, failed retries, vault tasks). Answer with `/pick N` or `/decline`.

- **SOUL.md** — Central prompt/personality definition at `<vault>/99_System/AI/SOUL.md`. Supports provider-specific sections (`### claude`, `### gemini`, `### codex`). Mtime-cached — changes take effect on the next task.

## Analytics Dashboard

```bash
# Start dashboard (opens browser automatically)
python orchestrator.py --dashboard

# Standalone with options
python dashboard.py
python dashboard.py --port 9000
python dashboard.py --no-open
```

Dashboard sections:
- **Summary cards**: total tasks, success rate, avg duration, active providers
- **Tasks/day** (30 days): bar chart of daily throughput
- **Provider distribution**: donut chart of usage per provider
- **Provider capacity** (48h / 7d / 30d): three timeline charts
- **Recent events**: error lines from logs + queue events
- **Session stats**: live data for the current `--watch` session
- **Billing analytics**: weighted token cost (`input × 1.0 + cache_creation × 1.25 + cache_read × 0.1 + output × 5.0`) and cache hit rate from Claude prompt cache. Quota gating uses ONLY `input + output` — cache fields are billing-only.

Default port: `8411` (configurable via `DASHBOARD_PORT`).

## Security / Guardrails

- Hard bans on destructive commands (`rm -rf`, `git reset --hard`, force-push, `DROP TABLE`, etc.)
- File deletion limits
- Protection against changes outside `cwd` (unless explicitly requested)
- `cwd:` validation against `ALLOWED_CWD_ROOTS` (when set)
- File-change snapshot + change summary after each task

## Prompt Budget (Token Allocation)

| Component | Budget | Source |
|---|---|---|
| Core (task + safety) | ~200 tokens | `config.py` / `SOUL.md` |
| Curated Memory (L1) | ~500 tokens | `MEMORY.md` |
| Lessons (L2) | ~1000 tokens | `lessons.md` (cwd-filtered) |
| Daily Log (L3) | ~500 tokens | `daily/` |
| TF-IDF Memory (L4) | ~2000 tokens | `memory.py` |
| Wikilink context | ~1500 tokens | `queue_manager.py` |
| Skill prompt | ~2000 tokens | `SKILL.md` body (only with `#tool:`) |
| **Total** | **~7 500 tokens** | (under `PROMPT_BUDGET_TOKENS=10000`) |

## Doctor (`--doctor`)

`python orchestrator.py --doctor` runs 16+ checks:

- Provider CLIs (`claude`, `gemini`, `codex`)
- `git`, `cclimits`
- Vault path + queue file
- Telegram bot configuration (`getMe` API call)
- `.env` (present + required keys)
- Skills discovery + requirements gating
- Memory directory
- Heartbeat file
- Profiles directory + validation
- Policy file
- Provider limits (via `cclimits`)
- **Model IDs** — CLI-pings every entry in `CLAUDE/GEMINI/CODEX_MODEL_ALIASES` concurrently (~5–10 s). FAIL on rejected IDs (unknown/deprecated), WARN on transient probe errors, PASS when every alias responds live. The deeper LLM-based "are there newer IDs?" heuristic runs only in the monthly heartbeat `model-check`, not here.

With `--fix` (optionally `--yes`) simple problems are auto-created/repaired.

## Architecture

```text
orchestrator.py
  → dispatcher.py          (provider selection + fallback)
  → queue_manager.py       (queue read/write, tags, atomic updates)
  → parallel_runner.py     (#parallel subtasks)
  → tools/registry.py      (#tool handlers)
  → skills/*               (SKILL.md discovery / gating / loader)
  → policy.py              (AUTO/APPROVE/DENY + Telegram approval)
  → profiles.py            (#agent profiles)
  → memory.py              (context store)
  → heartbeat.py           (watch-mode checks)
  → usage_suggester.py     (proactive suggestions on free capacity)
  → analytics.py           (data parsing + aggregation for dashboard)
  → dashboard.py           (HTTP server + Chart.js dashboard)
  → telegram_listener.py   (Telegram commands + chat)
  → notifier.py            (Telegram notifications)
  → shutdown.py            (shutdown countdown / cancel)
  → limits.py              (cclimits wrapper, disk cache, 429 resilience)
  → logging_setup.py       (rotating file logger)
  → doctor.py              (setup validation / --doctor)
  → queue_linter.py        (offline queue validation / --lint-queue)
  → idempotency.py         (duplicate-trigger dedup for external sources)
  → config.py              (constants, .env loader, SOUL.md loader)
```

## Troubleshooting

- Run `--doctor` first
- Run `--check-limits` if no providers are being used
- For `cwd:` errors: verify the path and set `ALLOWED_CWD_ROOTS` in `.env` if needed
- For Telegram issues: check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
- Logs: `logs/orchestrator.log`

## Testing

```bash
# Run all tests (~1200 tests, ~35 s)
python -m pytest tests/ -q

# Run a single test file
python -m pytest tests/test_parallel_runner.py -v
```

## Contributing

PRs welcome. Run `python -m pytest tests/ -q` before submitting. All tests must pass.

## License

MIT — see [LICENSE](LICENSE).
