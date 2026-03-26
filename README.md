# AI Orchestrator

Autonomous task executor for `claude`, `gemini`, and `codex` CLI tools — driven by a Markdown queue file, with multi-provider fallback, Telegram control, and a security/approval layer.

**Goal:** Run routine work (code, reviews, tests, docs, refactors) from a Markdown queue without managing API keys in your project. Uses existing CLI logins (OAuth/subscription).

## Features

- Multi-provider routing with fallback (`Claude → Gemini → Codex`)
- Capacity checking via `cclimits` (with local JSONL fallback on HTTP 429)
- Retry handling on rate limits / provider failures
- Obsidian-compatible queue with `cwd:`, `#tool:`, `#agent:`, `#parallel`, `#shutdown`, `#approve:*` tags
- Tool loops: `dev-loop`, `review-loop`, `test-loop`, `research-qa`, `security-audit`, `critical-review`, `knowledge-transfer`
- Skills / `SKILL.md` discovery with requirements gating
- Memory (TF-IDF + temporal decay) for recurring tasks
- Execution profiles (provider order, allowed skills, timeout, policy overrides)
- Execution policy (`AUTO` / `APPROVE` / `DENY`) with Telegram approval flows
- Telegram listener (queue control, status, plain-text AI chat)
- Heartbeat + Doctor (monitoring / onboarding checks)
- Analytics web dashboard (Chart.js, port 8411)
- `SOUL.md` as central prompt/personality configuration

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
| `DASHBOARD_PORT` | No | `8411` | Port for the analytics web dashboard |
| `TELEGRAM_MAX_TASK_LENGTH` | No | `500` | Max characters for `/task` command |

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
```

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
```

The orchestrator automatically appends `## Results` and `## Log` sections to each task.

### Supported Tags

| Feature | Syntax | Example |
|---|---|---|
| Force provider | `#claude`, `#gemini`, `#codex` | `- [ ] Task #codex` |
| Claude model | `#claude_haiku`, `#claude_sonnet`, `#claude_opus` | `- [ ] Task #claude_haiku` |
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

```bash
python orchestrator.py --list-tools
```

## Dev-Loop (`#tool:dev-loop`)

```
Phase 1 — Research
  Reads relevant code, understands the problem/feature,
  creates a concrete implementation plan.
  Web search only if local sources are insufficient.
  → Saved to {cwd}/.dev-loop/research.md

Phase 2 — Execution
  Implements the solution based on the research plan.
  On iteration > 1: includes findings from both prior reviews.

Phase 3a — Code Quality Review  (P1/P2/P3)
  Checks: Correctness, Clean, Secure, Performant, Maintainable,
          Testable, Robust, Documented, Compliant.
  P1/P2 = blocking | P3 = non-blocking

Phase 3b — Issue Resolution Review  (RESOLVED/PARTIAL/UNRESOLVED)
  Checks only: Does the code solve the original task 100%?
  Ignores code quality entirely.

→ Both reviews must pass → loop ends, no auto-push.
→ Per-iteration output in {cwd}/.dev-loop/round-NNN.md
→ Final summary: {cwd}/.dev-loop/summary.md
```

**Timeout configuration (`config.py`):**

| Constant | Default | Phase |
|---|---|---|
| `TOOL_DEV_RESEARCH_TIMEOUT_SEC` | 3600 (60 min) | Research |
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
| `/task <text>` | Add task to queue |
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

Rate limits (anti-spam):

| Category | Limit |
|---|---|
| Commands | 20/min |
| AI chats | 5/min |
| Task adds | 10/min |

## Memory, Heartbeat, SOUL.md

- **Memory (`memory.py`)** — Four-layer architecture:
  1. **Curated (`MEMORY.md`)**: Long-term patterns, conventions, decisions. Always in prompt.
  2. **Daily Logs (`daily/`)**: Append-only log for today + yesterday (temporal locality).
  3. **TF-IDF Deep Search (`task_results/`)**: Keyword matching + temporal decay over all past tasks.
  4. **Lessons Learned (`lessons.md`)**: LLM-summarized patterns from multi-iteration tool loops. CWD-filtered injection (universal `*` entries always, project-specific only when CWD matches). Semantic dedup via TF-IDF similarity at write time.
  - Top-K relevant memories are intelligently injected into the prompt.
  - Auto-archival after 180 days.

- **Heartbeat (`heartbeat.py`)** — Proactive checks in `--watch` mode, configured via `<vault>/99_System/AI/HEARTBEAT.md`.
  - 7 built-in handlers: `queue-idle`, `git-status`, `disk-space`, `check-limits`, `summarize`, `stale-branch`, `usage-suggest`
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
| Daily Log (L2) | ~500 tokens | `daily/` |
| TF-IDF Memory (L3) | ~2000 tokens | `memory.py` |
| Wikilink context | ~3000 tokens | `queue_manager.py` |
| Skill prompt | ~2000 tokens | `SKILL.md` body (only with `#tool:`) |
| **Total** | **~10 000 tokens** | |

## Doctor (`--doctor`)

`python orchestrator.py --doctor` runs 15+ checks:

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
# Run all tests (~674 tests, ~6s)
python -m pytest tests/ -q

# Run a single test file
python -m pytest tests/test_parallel_runner.py -v
```

## Contributing

PRs welcome. Run `python -m pytest tests/ -q` before submitting. All tests must pass.

## License

MIT — see [LICENSE](LICENSE).
