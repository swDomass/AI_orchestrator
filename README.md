# AI Orchestrator

Autonomous task executor for the `claude` and `codex` CLI tools — driven by a Markdown queue file, with multi-provider fallback, Telegram control, and a security/approval layer.

> **Provider status (2026-08-15).** The active fallback chain is **Claude → Codex**. Gemini is still implemented (`providers/gemini.py`, HTTP API + legacy CLI) and every `#gemini*` tag still resolves, but it left `dispatcher._PRIORITY` and the shipped `tool_providers:` policy — nothing routes there by default. OpenRouter and Mistral Vibe remain opt-in and never enter the chain. See [Provider status in detail](#provider-status-in-detail).

**Goal:** Run routine work (code, reviews, tests, docs, refactors) from a Markdown queue without managing API keys in your project. Uses existing CLI logins (OAuth/subscription).

## Why this exists

Routine engineering work — code reviews, refactors, test loops, documentation sync, security audits — is the ideal candidate for autonomous execution by an LLM CLI. But the existing tools fall apart at the operational layer:

- **No orchestration across providers.** The vendor CLIs are each excellent in isolation, but none of them routes a task to another vendor with capacity-aware fallback. When Claude hits a 5 h block, the workflow shouldn't stop — it should silently fail over.
- **Long-running autonomous loops need an audit trail.** Every dev-loop iteration, every security finding, every retry decision should be inspectable after the fact, not lost to stdout.
- **State should live in plain Markdown**, not in a database. A human edits the queue with a text editor, version-controls it in Git, and reads every result alongside the task that produced it.
- **Subscription-driven CLIs (OAuth, no API keys) are the cheapest way to run an autonomous agent** for a single user — provided you have a layer on top that respects quota windows, retries 429s, and knows when *not* to run.

This is the orchestrator built around that reality.

## Engineering highlights

The codebase prioritises auditability, safety, and operational fitness over feature breadth. If you're evaluating the architecture rather than the feature list:

- **~2100 tests / ~100 s** — full pytest suite covers queue parsing, dispatcher fallback, policy classification, provider mocks, stdin delivery verification, post-task verify checks, parallel execution, idempotency, quota calibration + SoTH state + live estimation, and per-tool phase logic. Tests are synchronous (no asyncio), pure stdlib + pytest fixtures, no live network calls. Run them with `-p no:randomly`: `tests/test_telegram_listener.py` is order-dependent, so a random seed can turn the suite red without a code change (known, unfixed — see [Known Limitations](#known-limitations)).
- **Defence in depth.** `scripts/safety_hook.py` is a Claude Code `PreToolUse` hook that hard-denies destructive commands (`rm -rf`, force-push, `DROP TABLE`, raw disk writes, `git push`, …) even under `--dangerously-skip-permissions`. A second layer (`SAFETY_RULES`) is injected into the non-Claude provider prompts, which have no hook system. CWD validation against `ALLOWED_CWD_ROOTS` blocks writes outside whitelisted roots.
- **Three-tier approval policy.** `policy.py` classifies every task as `AUTO`, `APPROVE`, or `DENY`. `APPROVE` tasks block until a Telegram `/approve` arrives; `DENY` never runs. Per-tool budgets and stop conditions (`max_iterations`, `max_runtime_sec`, `max_files_touched`, `reporting_path`) are declared in a YAML `tool_contracts:` section with schema validation at startup — one auditable place for every guard rail.
- **Operational resilience.** Three-tier HTTP 429 fallback (cclimits → local JSONL → optimistic), provider cooldowns with model-alias routing, OAuth-aware capacity polling (5 min active / 10 min idle, matching `cclimits --cache-ttl`), and a crash-resistant PowerShell watchdog with exponential backoff and Telegram alerts on every restart.
- **Auditability built in.** Every task end emits a structured `logs/runs.jsonl` record (19-category failure taxonomy: `rate_limit`, `timeout`, `auth_error`, `model_refusal`, …), per-tool action traces in `{cwd}/.<tool>/traces/*.jsonl`, an offline queue linter (`--lint-queue`, exit codes 0/1/2 for CI gating), and an 18-check `--doctor` with `--fix --yes` auto-repair.
- **Prompt-cache aware.** Stable prompt prefixes, `--exclude-dynamic-system-prompt-sections` to freeze the system prompt for cache hits, opt-in Claude session reuse (`--session-id` / `--resume`) for ~30–50 % token savings across multi-phase tools, and billing analytics with weighted cost (`input × 1.0 + cache_creation × 1.25 + cache_read × 0.1 + output × 5.0`) and per-task cache-hit rate.
- **Observability.** Standalone HTTP analytics dashboard (port 8211, Chart.js, 60 s refresh) backed by `logs/runs.jsonl`. Live "Active Runs" panel (30 s refresh) shows currently-running tool iterations, phase, tokens (in / out / cache_read) and elapsed time via the central `ActiveRunRegistry` in `logs/active_runs/`. Daily Telegram status recap (07:00) summarises the previous 24 h — tasks done/failed, provider breakdown, pending approvals, blocked tasks.
- **Calibrated quota model.** Phase-0 telemetry (`logs/quota-calibration.csv`) paired every `cclimits` poll with locally-aggregated JSONL token counts per Anthropic 5 h / 7 d window; ~6 days of data selected the `io_only` model (input + output — cache tokens are negligible to the rate limit). Phase 1 writes a single-source-of-truth `logs/cc_quota_state.json` each poll (consumed by the Claude Code statusline and `--check-limits`) and feeds the calibrated per-window factors into the 429-fallback estimator, reducing reliance on the undocumented, rate-limited `cclimits` endpoint.

Architecture details → [`docs/architecture/components.md`](docs/architecture/components.md) (per-module specs), [`docs/architecture/patterns.md`](docs/architecture/patterns.md) (patterns and invariants).

## Features

- Multi-provider routing with fallback (`Claude → Codex`; Gemini retired from the chain 2026-08-15, code retained)
- **Two opt-in providers outside the fallback chain**: OpenRouter (pay-per-token, `#openrouter`/`#or_*`) for cheap single-call work, and Mistral Vibe (`#vibe`, `#second_opinion:vibe`) as a read-only second non-Claude reviewer. Both are registered only when their prerequisite exists (API key / binary on `PATH`); a `#vibe` tag without the CLI **parks** the task rather than handing a review job to a file-writing executor. Under the shipped policy both are barred for tool tasks — see [Provider tags are subject to `tool_providers`](#supported-tags).
- Capacity checking via `cclimits` (with local JSONL fallback on HTTP 429)
- Retry handling on rate limits / provider failures
- Obsidian-compatible queue with `cwd:`, `#tool:`, `#agent:`, `#parallel`, `#worktree`, `#keep-worktree`, `#shutdown`, `#verify:`, `#approve:*` tags
- **`#verify:<script>` post-task outcome check**: a provider run can exit 0 with a well-formed result event and still have achieved nothing. The named script runs after success and inspects the *result* (did the file actually get written?); a non-zero exit raises a Telegram alarm and annotates the stored task result. Fail-closed — a missing or unlaunchable script counts as failed. The task itself is still finalized: failing it would need its own bounded retry counter, or a broken check script would requeue a working task forever.
- **`#worktree` parallel isolation (P1)**: each CWD group of a `#parallel` parent runs in its own `git worktree` under `.worktrees/parallel-<hash>`. Failed groups retain the worktree for inspection. `#keep-worktree` suppresses cleanup on success.
- Tool loops: `dev-loop`, `review-loop`, `test-loop`, `research-qa`, `security-audit`, `deep-security-audit`, `critical-review`, `knowledge-transfer`, `scientific-investigation`, `brainstorm`, `pr-babysitter`
- **`#tool:pr-babysitter` (P2/P5)**: polls open PRs via `gh`, queues `#tool:dev-loop` fix-tasks on new comments / CI failures. Two modes via `#pr-mode:queue|report-only` — report-only sends a Telegram summary with `/pr-fix <repo>#N` and `/pr-ignore <repo>#N` slash commands instead of writing the queue directly.
- **CI-Watcher heartbeat (P4)**: `check-ci-failures` polls `gh run list --status=failure` per `CI_WATCHER_REPOS` whitelist and queues one fix-task per new failing commit (dedup by `(repo, headSha)`, 2h cooldown). Optional repo→local-path mapping via `CI_WATCHER_REPO_PATHS`.
- **Status-Recap heartbeat (P6)**: daily 07:00 Telegram summary — tasks done/failed, provider breakdown, pending approvals, blocked tasks, top successes/failures of the last 24h. Idle day → one-liner.
- **Tool Contracts (P3)**: `tool_contracts:` section in `policy.yaml` declares per-tool budgets (`max_iterations`, `max_runtime_sec`, `max_files_touched`), stop conditions, and reporting paths in one auditable place. `PolicyEngine.get_tool_contract(name)` returns a `ToolContract` dataclass; tools may fall back to `config.TOOL_*` constants for fields the contract omits (staged migration).
- Skills / `SKILL.md` discovery with requirements gating
- Memory (TF-IDF + temporal decay) for recurring tasks
- Execution profiles (provider order, allowed skills, timeout, policy overrides)
- Execution policy (`AUTO` / `APPROVE` / `DENY`) with Telegram approval flows
- Telegram listener (queue control, status, plain-text AI chat)
- Heartbeat + Doctor (monitoring / onboarding checks)
- **Reliability layer (Tier 5)**: queue linter (`--lint-queue`), idempotency keys for external triggers, slash-commands (`/review`, `/dev`, `/security`, `/audit`, `/critique`, `/brainstorm`), schedule tags (`#at:`, `#every:`), machine-readable run summaries (`logs/runs.jsonl`), 19-category failure taxonomy, per-tool preflight hooks (deterministic context collection), queue-healing with `/unblock`/`/drop`/`/retry`, draft-only skill suggester, progressive skill loading
- Analytics web dashboard (Chart.js, port 8211)
- **Calibrated quota model (Phase 0 + 1)**: Phase-0 telemetry (`logs/quota-calibration.csv`) selected the `io_only` `tokens_per_pct` model (input + output). Phase 1 persists a SoTH `logs/cc_quota_state.json` per poll (read by the statusline + `--check-limits`) and uses calibrated per-window factors in the 429-fallback estimator. The undocumented `cclimits` endpoint stays the calibration anchor (polled every 5–10 min), not the per-reading source. Details → [`docs/architecture/components.md`](docs/architecture/components.md#quota_calibrationpy--quota_statepy).
- `SOUL.md` as central prompt/personality configuration
- **Anthropic prompt-cache optimization**: static system-prompt (cwd/git-status moved to first user message via `--exclude-dynamic-system-prompt-sections`), stable prompt prefixes, billing analytics with cache-hit-rate
- **Optional Claude session reuse** (`CLAUDE_SESSION_ENABLED=true`): `dev-loop`, `review-loop`, same-provider `critical-review`, and `deep-security-audit` share Claude `--session-id`/`--resume` across phases for cross-call cache hits

## Requirements

- Python `3.10+` — ⚠️ **this floor is currently unverified**: `import limits`
  fails with a `NameError` on 3.10–3.13 (only 3.14 works). See
  [Linting & Typing](#linting--typing) before relying on it.
- `cclimits` CLI (`npm install -g cclimits`)
- Provider CLIs in `PATH`: `claude`, `codex`
- Valid authentication in each CLI (OAuth / subscription login)
- Optional, retired by default: `gemini`. The provider code is still shipped, but it is out of the fallback chain and out of the default policy since 2026-08-15 — re-enabling it means putting `gemini` back into `dispatcher._PRIORITY` **and** into the relevant `tool_providers:` entry, plus either the `gemini` CLI (Standard/Enterprise only — the consumer CLI was retired 2026-06-18) or a `GEMINI_API_KEY`.
- Optional, opt-in only: `vibe` (Mistral Vibe CLI) as a second non-Claude reviewer, and an `OPENROUTER_API_KEY` for pay-per-token single-call tasks. Neither is required, and neither ever enters the default fallback chain.

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

## Linting & Typing

Ruff and mypy are configured in `pyproject.toml` (tooling sections only — the
repo is a flat script collection, not an installable package). Neither is
required to run the orchestrator and neither is wired into a CI gate yet.

```bash
pip install ruff mypy types-PyYAML
ruff check .
python -m mypy .
```

The first full measurement — 1351 ruff findings, 131 mypy errors, which of them
are real defects, and a proposed work order — is recorded in
[`docs/lint-baseline-2026-09-02.md`](docs/lint-baseline-2026-09-02.md). Note
that it documents an **import failure on Python 3.10–3.13** that contradicts the
`3.10+` claim under [Requirements](#requirements); read it before relying on
that version floor.

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
| `GEMINI_API_KEY` | No | *(commented out since 2026-08-15)* | Google AI Studio key. When set, the Gemini provider uses the HTTP REST API instead of the CLI (the consumer Gemini CLI was shut down 2026-06-18); without a key it falls back to the legacy `gemini` CLI (Standard/Enterprise only). **Setting the key no longer puts Gemini back in the chain** — that needs `dispatcher._PRIORITY` and `tool_providers:` as well. What the key *does* control is the capacity gate: with it set, `dispatcher._limits_ok("gemini", …)` short-circuits to `True` (the REST API exposes no pollable quota), so Gemini would be reported unconditionally available. Leaving it unset is what disarms that. |
| `GEMINI_BASE_URL` | No | `https://generativelanguage.googleapis.com/v1beta` | Override for testing or a proxy |
| `GEMINI_DEFAULT_MODEL` | No | `gemini-3.5-flash` | Model for HTTP mode without a `#gemini_*` tag (free-tier-safe GA Flash) |
| `GEMINI_MAX_OUTPUT_TOKENS` | No | `16384` | Output cap for HTTP mode; generous because gemini-3.x thinking tokens count toward it |
| `OPENROUTER_API_KEY` | No | — | OpenRouter API key. When set, enables `#openrouter`/`#or_*` tags as an opt-in pay-per-token provider for non-agentic tasks (heartbeat checks, summaries). Never enters the default fallback chain. |
| `OPENROUTER_BASE_URL` | No | `https://openrouter.ai/api/v1` | Override for testing or self-hosted proxy |
| `OPENROUTER_DEFAULT_MODEL` | No | `minimax/minimax-m2.5:free` | Model used by `#openrouter` without a specific `#or_*` alias |
| `VIBE_MAX_PRICE_USD` | No | `0.50` | Per-run cost ceiling for the Mistral Vibe provider (vibe interrupts itself above it) |
| `VIBE_MAX_TURNS` | No | `12` | Assistant-turn budget for tool-enabled Vibe runs (`read_file`/`grep` only) |
| `VIBE_READONLY_MAX_TURNS` | No | `1` | Turn budget when all tools are disabled — a single turn is all that can happen |
| `ORCH_QUOTA_LIVE_ESTIMATE` | No | `false` | Phase 2: decrement the cached quota snapshot by a live per-task estimate between cclimits polls |
| `ORCH_QUOTA_AUTO_RECALIBRATE` | No | `false` | Requires the flag above: re-derive the per-window `tokens_per_pct` factors daily from `logs/quota-calibration.csv` (min-samples + clamp guarded) |
| `DASHBOARD_PORT` | No | `8211` | Port for the analytics web dashboard (auto-falls back to a free port if taken/Windows-reserved) |
| `TELEGRAM_MAX_TASK_LENGTH` | No | `500` | Max characters for `/task` command |
| `CLAUDE_SESSION_ENABLED` | No | `false` | Opt-in: Claude `--session-id`/`--resume` across tool phases for prompt-cache reuse. Off = today's stateless behaviour. |
| `ORCH_SESSION_RETENTION_DAYS` | No | `14` | Heartbeat session-cleanup retention for orchestrator-created session JSONL files in `~/.claude/projects/`. Whitelist via sidecar registry. |
| `PR_BABYSITTER_REPOS` | No | *(empty)* | Semicolon-separated `owner/name` list for `#tool:pr-babysitter` without an explicit `#repos:` tag. Empty = tool requires the tag. |
| `PR_BABYSITTER_QUEUE_COOLDOWN_HOURS` | No | `1` | Suppresses duplicate `dev-loop` queue items for the same PR within the window. |
| `CI_WATCHER_REPOS` | No | *(empty)* | Semicolon-separated `owner/name` list for the `check-ci-failures` heartbeat. Empty list disables the handler. |
| `CI_WATCHER_REPO_PATHS` | No | *(empty)* | Optional `owner/name=local/path[;...]` mapping so the auto-generated queue item carries a usable `cwd:` tag. |
| `CI_WATCHER_QUEUE_COOLDOWN_HOURS` | No | `2` | Cooldown per `(repo, headSha)` before re-queueing a CI failure. |

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
- Unknown model alias (`#claude_*`, `#gemini_*`, `#codex_*`, `#vibe_*`, `#or_*`)
- Unknown `#effort:` level (`unknown_effort`) — the tag regex is deliberately loose so a typo is *reported* here instead of being indistinguishable from no tag at all; at runtime the task falls back to the session default
- Cross-provider model leakage (`#claude_opus` + explicit `#gemini` = error)
- Duplicate `#id:` values in the open queue
- `#needs:` referencing IDs that will never resolve (warning)
- `#openrouter` / `#or_*` without `OPENROUTER_API_KEY` configured (warning — task falls back to default chain)
- `#vibe` / `#vibe_*` without the `vibe` CLI on PATH (warning, `vibe_missing_cli`) — worded differently from the OpenRouter case: a missing Vibe binary does not fall back to the default chain, it **parks** the task (`dispatcher._REVIEWER_ONLY`)
- `#parallel` with 0-1 subtasks (warning) or shared CWD (info)
- HTML comment inside the task body (`html_comment_in_body`) — truncates the task and deletes the trailing tags on rewrite, see [Retry Markers](#retry-markers)
- HTML comment at the line end that is not a valid `retry`/`hang` marker (`html_comment_trailing`) — silently dropped on rewrite; a near-miss marker means the schedule never applies

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
- [ ] Prüfe docs/plan.md #tool:critical-review #pass1:claude #pass2:codex cwd:D:\projects\app
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
| Force provider | `#claude`, `#codex`, `#vibe`, `#openrouter` (`#gemini` still parses but is barred by the shipped policy) | `- [ ] Task #codex` |
| Claude model | `#claude_haiku`, `#claude_sonnet`, `#claude_opus` | `- [ ] Task #claude_haiku` |
| Reasoning effort (**Claude only**) | `#effort:low\|medium\|high\|xhigh\|max` → `claude --effort` | `- [ ] Classify inbox #claude_opus #effort:low` |
| Gemini model *(retired — parses, but barred by the shipped policy)* | `#gemini_pro`, `#gemini_flash`, `#gemini_flash_lite` | `- [ ] Iterate #gemini_flash` |
| Codex model | `#codex_5` (gpt-5.6-sol), `#codex_5_4` (gpt-5.6-terra), `#codex_mini` (gpt-5.6-luna) | `- [ ] Run #codex_mini` |
| OpenRouter model (opt-in, requires `OPENROUTER_API_KEY`) | Free: `#or_minimax_free`, `#or_deepseek_free`, `#or_qwen_free`, `#or_nemotron_free`. Paid flagships: `#or_glm`, `#or_kimi`, `#or_qwen`, `#or_deepseek`, `#or_minimax`. Generic: `#openrouter` (default model). | `- [ ] Daily summary #or_minimax_free` |
| Vibe / Mistral (opt-in, requires the `vibe` CLI) | `#vibe` — routes to Vibe using its configured model. Reviewer only: never writes files, never enters the fallback chain, and if the CLI is missing the task is **parked rather than handed to an executor**. | `- [ ] Second opinion #vibe` |
| Vibe model choice | `#second_opinion:vibe_medium` (mistral-medium-3.5) / `#second_opinion:vibe_small` (devstral-small). Since 2026-08-15 the bare task tags `#vibe_medium`/`#vibe_small` force the model too — `MODEL_TAG_RE` is generated from `dispatcher._TAG_MAP` and covers all 20 model aliases. | `- [ ] Review #tool:review-loop #second_opinion:vibe_small` |
| Run tool | `#tool:<name>` | `- [ ] Review #tool:review-loop` |
| Restrict providers (task-level) | `#tool_providers:<p1,p2>` | `#tool_providers:claude,codex` |
| Working directory | `cwd:<path>` | `cwd:D:\projects\repo` |
| Working directory with spaces | `cwd:"<path>"` | `cwd:"D:\My Projects\App"` |
| Timeout (hard backstop, not aggressive deadline; for tools an upper cap) | `#timeout:<n>[s\|m\|h]` | `#timeout:30s`, `#timeout:15m`, `#timeout:1h` |
| Execution profile | `#agent:<name>` | `#agent:work` |
| Parallel task | `#parallel` | Parent task with indented subtasks |
| Task ID | `#id:<name>` | `- [ ] Build backend #id:build` |
| Task dependency | `#needs:<id1,id2>` | `- [ ] Test #needs:build` |
| One-time schedule | `#at:<timestamp>` | `- [ ] Review #at:2026-05-17T22:00` |
| Recurring schedule | `#every:<duration>` | `- [ ] Daily review #every:24h` |
| Time-of-day anchor | `#at:<HH:MM>` + `#every:<Nd>` | `- [ ] Daily brief #at:08:00 #every:24h` |
| Skip stale slot | `#freshonly` | `- [ ] Daily brief #at:08:00 #every:24h #freshonly` |
| Stale grace window | `#grace:<duration>` | `- [ ] Recap #at:19:00 #every:24h #freshonly #grace:4h` |
| Shutdown after task | `#shutdown` | `- [ ] Backup #shutdown` |
| Post-task outcome check | `#verify:<script>` | `- [ ] Daily brief #verify:scripts\check_brief.ps1` |
| Cross-provider pass | `#pass1:<provider>`, `#pass2:<provider>` — provider is `claude`, `gemini`, `codex`, `vibe` or `openrouter` | `#pass1:claude #pass2:codex` |
| Preapproval | `#approve:<category,...>` | `#approve:push,publish` |
| Second opinion (review-loop) | `#second_opinion:<alias\|provider>` | `#second_opinion:codex`, `#second_opinion:vibe_small` |

**Provider tags are subject to `tool_providers`.** Every routing tag above is filtered through the `tool_providers:` section of `policy.yaml` (task-level `#tool_providers:` → profile → global). A tag naming a barred provider does **not** quietly fall back to another one: the task fails with a logged `provider_not_allowed`, because in an unattended run a silent swap hides which model actually did the work. The same filter applies to **every** provider lookup **inside** tools (second opinion, `#pass2:`, cross-provider persona allocation *and* the step that resolves an allocation into a provider instance), all of which previously bypassed it entirely — all seven raw call sites are converted. The two resolution sites (`tools/brainstorm.py`, `tools/scientific_investigation_phase2.py`) are a second gate, not a duplicate one: they resolve from an allocation list that a persisted state file or the degraded fallback path can outlive, so a name that was legal when written is re-checked against today's policy before a prompt goes out over it. The one deliberately raw call left is `tools/critical_review.py`, which asks `policy_allows_provider()` first so that "policy said no" and "unknown provider" produce different log lines. Under the shipped policy (`[claude, codex]`, `review-loop`/`test-loop`: `[claude]`) this makes `#gemini*`, `#openrouter`/`#or_*` and `#vibe*` inert — including `#second_opinion:vibe_small` and `#second_opinion:codex` for `review-loop`. Widen the relevant `tool_providers:` entry to use them.

**If `policy.yaml` is missing or unreadable, the pay-per-token providers stay barred.** The file cannot be assumed present — it lives in a synced folder — and an absent one is indistinguishable from an empty one. So the fallback is asymmetric: the capped providers (`claude`, `codex`, `gemini`) stay allowed, because a lost config file must not take the orchestrator offline, while `openrouter` and `vibe` stay blocked, because they are billed per token and have no cost ceiling to fall back on. Only an explicit allow-list re-enables them. A task whose policy allows nothing routable is finalized with `no_provider_allowed` rather than parked — no quota reset can lift a policy restriction, so parking it would mean waiting forever.

**`#effort:` details.** As a heuristic (not benchmarked in this repo), try lowering the effort level before dropping a model tier. Without the tag the CLI keeps its own session default; the orchestrator never injects a default of its own. `--effort` exists **only** on the Claude CLI: Codex, Gemini, Vibe and OpenRouter ignore the tag by construction (only `providers/claude.py` reads it), and `--lint-queue` warns (`effort_non_claude`) when a task carrying the tag is explicitly routed elsewhere. An unknown or malformed level is a lint **error**; at runtime it degrades to the session default rather than failing a scheduled run. On a **parent** task the orchestrator also logs a warning naming the offending tag, so a typo leaves a trace in a scheduled run. Two cases stay lint-only: a tag on a **subtask** (the subtask parser warns about nothing) and a bare `#effort` with no value (warned about nowhere, and deliberately not stripped so the word survives in prose). Subtasks honour the tag too and inherit the parent's level when they carry none.

**`#verify:` details.** Relative paths resolve against `cwd:` (without a `cwd:` tag, against the orchestrator process's working directory — prefer absolute paths there). Quote paths containing spaces or `#`. Supported types: `.ps1` (via `pwsh -NoProfile -File`), `.py` (via the running interpreter with `-X utf8`), `.cmd`/`.bat`/`.exe` and extensionless executables; anything else is rejected with a clear message. Exit 0 = passed, non-zero = alarm, and the script's stdout (or stderr when stdout is empty) becomes the alarm text — a PowerShell check should set `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8` so umlauts survive the redirect. Runs in all three success paths (single-shot, `#tool:`, `#parallel`), always after the queue line is finalized; for `#parallel` only when every subtask succeeded. A failing check raises the alarm but does not requeue the task — see `--lint-queue` for the `verify_without_path` rule that catches a `#verify:` with no script.

### Known Limitations

**`select_provider()` is still fail-open for a bare `#vibe`/`#openrouter` tag when the policy is missing.** The fail-closed rule described above lives in `policy_allows_provider()` / `dispatcher._allows()`. The *forced-provider* path in `select_provider()` was not converted in the same pass, so a task carrying a bare `#vibe` or `#openrouter` tag can still reach that provider if `policy.yaml` is absent or unreadable. Deliberately left open — closing it means reworking roughly ten routing tests — but it is the one hole left in the pay-per-token ceiling, and it matters exactly in the scenario the ceiling was built for (a synced `policy.yaml` that failed to arrive before an unattended run).

**An unregistered value in `#pass1:`/`#pass2:` is dropped without a word.** Since 2026-09-02 the regex accepts `vibe` and `openrouter` alongside `claude`/`gemini`/`codex`, but anything else — a typo, a provider that was never registered — fails twice over: `extract_pass_providers()` drops the pass silently, and `strip_metadata_tags()` does not recognise the tag either, so it survives into the prompt as literal text. Measured: `#pass1:claude #pass2:mistral` yields `{1: 'claude'}` and a prompt still ending in `#pass2:mistral`. The queue linter has no check for it.

**The safety hook cannot see a git write handed to a non-shell wrapper.** `find . -exec git push …`, `docker exec c git push …` and `xargs git push` are accepted residuals: `_CMD_START` treats `-` as a non-boundary character, and recognising these would need real argv parsing rather than a regex. Likewise, a **heredoc body line** that begins with a git write command matches, because `\n` is a genuine command separator and the hook cannot tell body text from a command — a deliberate false-positive trade in the safe direction. The hook also only covers Claude; the Codex path is fenced by its own sandbox, which depends on `experimental_windows_sandbox = true` in `~/.codex/config.toml`.

**`stdin_incomplete` can requeue without bound.** `<!-- hang: N -->` is the queue's only persistent per-task counter, and only `hang` and `format_error` *increment* it. Every other `error_code` — `stdin_incomplete` included — requeues without raising it, so a task failing that way indefinitely is retried indefinitely across polls. The retry *count* is unbounded; the *rate* is still throttled by the 5-minute provider cooldown and the park cycle, so this burns polls rather than spinning. Pre-existing, not introduced by the 2026-08-15 work. (Since 2026-08-15 those parks at least no longer *reset* the counter — see the note under `MAX_HANG_RETRIES` — so a task that alternates between real failures and parks does reach the cap.)

**`tests/test_telegram_listener.py` is order-dependent** and `tests/test_usage_suggester.py` depends on environment/live state absent in a fresh worktree. Run the suite with `-p no:randomly`; a red run in a fresh worktree is more likely these two than a real regression.

**`#freshonly` recovery is limited to a daily cadence.** A `#every:Nd` task (N > 1) that missed its slot is never recovered onto the current day, because a multi-day cadence is measured from `now` rather than from a fixed calendar phase — there is no well-defined "slot for today" to recover. It moves to the next occurrence instead. Likewise, a `#grace:` wider than half the interval does not extend the recovery window: the late run would otherwise land closer to the next slot than to its own. Neither is flagged by `--lint-queue`.

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

- A task with `#needs:` stays **blocked** until all named IDs appear as `[x] … ✅ …` (done) or `[-]` (cancelled).
- Blocked tasks are **not removed** from the queue — they stay open and are re-checked each cycle.
- The queue header shows `(N runnable, M blocked)` when blocked tasks are present.
- `[-]` (cancelled) tasks **do** unblock dependents. That is a human decision — either you ticked it off in Obsidian, or you sent `/drop` — and the downstream task decides how to handle it.
- A task the orchestrator **gave up on** does not. It is written as `- [x] <task> ❌ <ts> (<provider>)`: checked off so it is never picked up again, stamped ❌ so it satisfies nothing. See [Terminal failures](#terminal-failures--x--) below.

### Terminal failures (`- [x] … ❌ …`)

When a task ends without doing what it was asked — retry cap for hang/format breaks
reached, `tool_providers` policy denial, invalid `cwd:`, unknown tool, tool runtime
budget exhausted, a `#parallel` run with a failed subtask — the queue line is stamped:

```md
- [x] Fix the version floor #id:nightfloor ❌ 2026-09-04 01:21 (claude+dev-loop)
```

- **Out of the queue.** `[x]` means the parser never picks it up again, so a task at
  its retry cap cannot burn a full runtime every following night.
- **Satisfies nothing.** `#needs:` is only released by ✅ or by `[-]`.
- **Obsidian-safe.** The Tasks plugin knows four status symbols in this vault
  (`x`, `-`, ` `, `/`) and treats an unregistered one as *TODO* — a fifth symbol
  would make failed tasks reappear as open in every task query. The mark carries
  the verdict instead of the checkbox.
- **Visible.** ❌ against ✅ is the one thing you see when skimming the file.
- Archived to `agent-queue-erledigt.md` on the same 48 h clock as a success, and
  recorded in `logs/runs.jsonl` with its `error_code`.
- Recurring (`#every:`) tasks are the exception: they are rescheduled rather than
  stamped, exactly as before — a failure today does not cancel tomorrow's slot.
- `/unblock <id>` (Telegram) promotes such a line to ✅, `/retry <id>` reopens it as
  `- [ ]`.

### Schedules (`#at:` / `#every:`)

```md
- [ ] One-time review tonight #at:2026-05-17T22:00 #tool:review-loop cwd:D:\proj
- [ ] Daily review #every:24h #tool:review-loop cwd:D:\proj
- [ ] Daily brief, anchored at 08:00 #at:08:00 #every:24h cwd:D:\proj
- [ ] Daily brief, skip if missed #at:08:00 #every:24h #freshonly #grace:4h cwd:D:\proj
```

Both schedule tags reuse the existing retry primitive — no separate scheduler.

- **`#at:<timestamp>`** delays the task until the given time. Formats: `YYYY-MM-DDTHH:MM`, `YYYY-MM-DD HH:MM`, or `HH:MM` (closest-day interpretation). For a **one-time** task (no `#every:`), it is marked `[x]` after the first fire.
- **`#every:<duration>`** turns the task into a recurring schedule. Units: `s|m|h|d`. On successful completion, the line is rewritten as open with a new `<!-- retry: ... -->` annotation instead of `[x]` — it fires again on schedule.
- **Anchored recurrence (no drift)**: combine `#at:<HH:MM>` with a day-multiple `#every:` (e.g. `#every:24h`, `#every:7d`). The next run is computed as the **next occurrence of the anchor time-of-day** (not `now + duration`), so a late boot never shifts the daily slot. The `#at:` anchor is **preserved** (normalized to bare `HH:MM`). Without an `#at:` anchor (or with a sub-day interval), the legacy `now + duration` behavior applies and a stale one-time `#at:` is stripped.
- **`#freshonly`** marks a recurring task whose run is only meaningful close to its slot (daily briefs). If the orchestrator was off and the slot is more than the grace window in the past, the task is **realigned without running** — no late, stale fire. It is realigned to the **most recent anchor slot that is still inside its own grace window**, so a task whose current slot remains valid runs late today rather than being pushed a full day; only once that window has closed too does it move to the next occurrence. Tasks **without** `#freshonly` keep catch-up semantics (a missed weekly maintenance run still executes on next start).
- **`#grace:<duration>`** sets how late after the anchor a `#freshonly` task may still run before it counts as stale (default `2h`). Example: `#grace:4h` lets a 19:00 recap still fire on a 22:00 boot.
- **Pause / remove** a recurring schedule = edit the queue file (delete the line, comment it out, or mark `[x]` manually).
- **Missed runs**: without `#freshonly`, the retry time is in the past on next start → the task runs immediately (catch-up). With `#freshonly`, a stale slot is skipped and realigned — onto the current slot if that one is still within grace (late run today), otherwise onto the next occurrence (no run).
- **Validated by `--lint-queue`**: malformed `#at:`, `#every:` and `#grace:` values are flagged as errors; `#freshonly` without `#every:` and `#grace:` without `#freshonly` are warnings.

### Retry Markers

```md
- [ ] Task <!-- retry: 2026-02-26 23:10 -->
```

**Never put an HTML comment inside the task body.** `OPEN_TASK_RE` tolerates comments only at the very end of the line (`retry`/`hang` markers). As soon as the line *ends* in a comment, one sitting earlier in the text makes the parser end the task at the first `<!--` and swallow everything up to the last `-->` on the line — so the provider receives a prompt cut off mid-sentence, and rewriting the completed line deletes the swallowed range (including the `#every:`/`#at:`/model tags) from the file for good. The recurring task then silently stops firing. A body comment on a line that does not yet end in a comment still parses in full — but on an `#every:` task the first completion appends a retry marker and arms it. `--lint-queue` flags this as error `html_comment_in_body`, and a line-end comment the parser does not read as a marker as `html_comment_trailing` (dropped on the next rewrite — and if it was meant as a marker, note that `RETRY_TAG_RE` requires the spaces after `<!--`, after `retry:` and before `-->`; extra spaces are fine, missing ones are not). Put marker syntax and other comment-shaped instructions in a skill file the task points to.

## Built-in Tools

| Tool | Description |
|---|---|
| `dev-loop` | Research → Execute → Dual-Review loop (Code Quality + Issue Resolution). Both reviews must pass. Same **P1 + P2** semantics as `review-loop`: only blocking findings reach the executor, P3 is collected across all iterations and appended once as a closing offer. Output in `{cwd}/.dev-loop/`. |
| `review-loop` | Iterative Review → Fix → Re-Review loop. Fixes all **P1 + P2**; **P3 is non-blocking** and is reported once at the end as an offer instead of being fixed (cosmetics on working code widen the diff, and since each round re-reads the fresh diff, a P3 fix can surface new P3). A reviewer output that lists findings *and* the "no findings" sentinel counts as having findings — the sentinel alone used to pass the success gate with an unfixed blocker. Max 20 iterations with infinite-loop detection. Optional drift-check (`policy.yaml` `tool_phases.review-loop.drift_check_mode`, default `auto`) injiziert eine Refocus-Warning in den nächsten Fix-Prompt, wenn der Reviewer in unrelated Refactoring abgedriftet ist. |
| `test-loop` | Iterative test / fix loop until tests pass or max iterations. |
| `research-qa` | Read-only pre-implementation research: Discovery → Analysis → Question catalogue. Output in `{cwd}/.research-qa/`. No code changes. |
| `knowledge-transfer` | Cross-domain knowledge transfer: Vault expertise → industry applications (via web search) → Obsidian idea note. |
| `critical-review` | 3-pass adversarial review: analysis → challenge → synthesis. Reference a plan file to get `{name}-v2.md`. Cross-provider via `#pass1:claude #pass2:codex`. Output in `{cwd}/docs/critical-review-*.md`. |
| `security-audit` | Two-phase workflow: Audit (read-only) → Fix + pytest. Scans for hardcoded secrets, command injection, path traversal, unsafe deserialization, SSRF, and more. Output in `{cwd}/docs/security-audit-*.md`. |
| `deep-security-audit` | Multi-agent deep audit: 6 expert personas (pentester, architect, SAST, supply chain, data privacy, forensics) + CISO synthesis + optional fix. `#no-fix` skips fix phase. `#roundtable` inserts a Phase 6.5 dialogue where each persona reviews the others' findings (~6 extra subprocess calls, more robust CISO synthesis on conflicting findings). Output in `{cwd}/docs/deep-security-audit-*.md`. Structured action trace at `{cwd}/.deep-security-audit/traces/<run_id>.jsonl`. |
| `scientific-investigation` | Wissenschaftlicher Autopilot mit Audit-Trail. Pipeline (Plan v5, I0–I9): Framing + Pre-Registration → Multi-Persona Review (Author + Devils-Advocate + Methodiker) → Sub-Task-Execution-Loop → Synthesis mit Falsifikations-Tabelle → Mechanical & heuristic check → Engineering-Reviewer Rework → Final Telegram-Approval. Status-Tuple `methodological_rigor=MEDIUM\|LOW` (HIGH strukturell ausgeschlossen). Output in `{cwd}/docs/scientific-investigation-{ts}/` + audit-pack via `scripts/build_audit_pack.py`. |
| `pr-babysitter` | Polls open GitHub PRs via `gh` and reacts to new review comments / CI failures. `#pr-mode:queue` (default) appends a `#tool:dev-loop` fix task, `#pr-mode:report-only` sends a Telegram summary with `/pr-fix` + `/pr-ignore`. Tool itself is read-only. |
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

## Timeout / Liveness-Watchdog

CLI provider calls run through a liveness/hang watchdog (`providers/process_runner.py`) instead of a raw wall-clock deadline. A run that keeps making progress runs to completion; only a truly frozen process is killed — with a real process-tree kill (Windows `taskkill /F /T`, POSIX `killpg`), unlike `subprocess.run` which orphaned the real grandchild and then blocked on it.

**Semantics change:** `#timeout:` / profile `timeout_minutes` now set the HARD backstop (absolute upper bound for a progressing run), not an aggressive deadline. For iterative tools the value is an upper cap only and never raises per-phase caps above the `TOOL_*_TIMEOUT_SEC` constants; total tool runtime is bounded by `ToolContract.max_runtime_sec`.

| Constant | Default | Meaning |
|---|---|---|
| `TASK_TIMEOUT_SEC` | 5400 (90 min) | Hard backstop per CLI call (env-overridable) |
| `TASK_IDLE_TIMEOUT_SEC` | 300 (5 min) | Idle/hang detector for Claude (tool-aware: a running `tool_use` pauses the timer) |
| `CLI_IDLE_TIMEOUT_NO_LIVENESS_SEC` | 1200 (20 min) | Idle detector for Gemini/Codex (byte-only, conservative — covers a long single tool phase) |
| `MAX_HANG_RETRIES` | 2 | Idle-kills (`error="hang"`) are requeued with a short backoff up to this many times, then the task is BLOCKED (not quota-reset-retried forever) |
| `HANG_RETRY_BACKOFF_SEC` | 300 (5 min) | Backoff before requeueing a hung task |
| `TOOL_DEFAULT_MAX_RUNTIME_SEC` | 3600 (60 min) | Fallback total-runtime deadline for an iterative tool when its ToolContract omits `max_runtime_sec` |

**The `<!-- hang: N -->` counter survives parks, and only real failures raise it.** `mark_retry()` is the single writer of that marker and it rebuilds the whole queue line, so "the caller passed no count" has to mean *keep what is there* — until 2026-08-15 it meant *erase it*, and every capacity, timeout, strict-mode or approval park silently reset the count to 0. A task alternating between format errors and capacity parks therefore never reached `MAX_HANG_RETRIES` and requeued forever, unseen. The rule now: `hang` and `format_error` pass `previous + 1` because they are unsuccessful attempts **at the task**; every other park passes nothing and the counter is carried forward unchanged, because capacity or a quota reset says nothing about the task. A successful run clears it (the line is rewritten by `finalize_task_with_result()`).

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

# Cross-provider (Claude analyzes, Codex challenges)
- [ ] Prüfe docs/plan.md #tool:critical-review #pass1:claude #pass2:codex cwd:D:\projects\app

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
  Mit #cross-provider: Round-Robin über die Kandidaten-Tupel
  (claude, gemini, codex, openrouter) — jeder Kandidat läuft aber
  durch den tool_providers-Filter, ein gesperrter fällt raus.
  Unter der ausgelieferten Policy bleiben claude + codex übrig.
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

A battle-tested 8-step queue pattern for implementing a plan end-to-end with cost-optimized model tiering. Strong models (Opus) handle value creation and final validation; cheaper tiers (`codex_mini`, `codex`) do the iterative cleanup; Codex runs strictly read-only as a second opinion.

**Recommendation:** keep plans small (one feature / one phase per plan file) and apply this flow per plan. For multi-phase changes, split the plan file into several smaller ones — one commit per plan is cleaner than one commit for many phases.

```markdown
- [ ] Implement docs\plan-XXX.md. dont commit the changes! #id:ID1 #tool:dev-loop #claude_opus cwd:<repo>

- [ ] security-audit of the uncommitted changes. dont commit the changes! #id:ID2 #need:ID1 #tool:security-audit #claude_opus cwd:<repo>

- [ ] use your simplify skill for the uncommitted changes. dont commit the changes! #id:ID3 #need:ID2 #claude_sonnet cwd:<repo>

- [ ] Review-fix loop for the uncommitted changes. dont commit the changes! #tool:review-loop #id:ID4 #need:ID3 #codex_mini cwd:<repo>

- [ ] Review-fix loop for the uncommitted changes. dont commit the changes! #tool:review-loop #id:ID5 #need:ID4 #codex cwd:<repo>

- [ ] Critical review (read-only) of the uncommitted changes against docs\plan-XXX.md #tool:critical-review #pass1:claude #pass2:codex #id:ID6 #need:ID5 cwd:<repo>

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
| 5. review-loop (pass B) | `#codex` (CLI default) | Mid-tier — structural issues, missing coverage |
| 6. critical-review | `#pass1:claude` + `#pass2:codex` | Independent second opinion, strictly read-only — zero risk of broken code |
| 7. review-loop (final) | `#claude_opus` | Final validator; integrates critical-review findings. If Opus finds nothing here, the code is genuinely clean |
| 8. commit | `#claude_haiku` | Trivial — diff + doc sync + single commit. Escalate to `#claude_sonnet` if the plan spans multiple commits |

### Variants

- **Minimal** (trivial changes): dev-loop → review-loop `#codex_mini` → review-loop `#claude_opus` → commit `#claude_haiku`
- **Security-critical**: swap step 2 for `#tool:deep-security-audit` (6-agent deep scan)
- **Multi-commit plans**: raise step 8 to `#claude_sonnet` and instruct it to split via `git add -p`

### External second opinion: Codex first, Mistral Vibe for high-risk work

Step 6 (`#tool:critical-review #pass2:codex`) runs Codex as an independent, read-only second opinion — a non-Claude voice catches assumptions the Claude reviewers share. Codex is the default external reviewer.

For high-risk changes a **second** non-Claude voice is available: `#second_opinion:vibe` (or `:vibe_medium` / `:vibe_small`) on a `#tool:review-loop` task routes to the Mistral Vibe CLI, which runs strictly read-only — all tools disabled, or `read_file`+`grep` at most. It is opt-in, pay-per-token (`VIBE_MAX_PRICE_USD` caps a run), and never joins the fallback chain. Reserve it for cases where one external opinion is not enough; two paid reviewers on trivial diffs is waste.

Gemini is **not used at all any more** (2026-08-15). It was dropped as a reviewer first — consumer CLI retired 2026-06-18, and data-training/privacy concerns rule out the HTTP mode for review content — and then, after an `IneligibleTierError` left the remaining path unusable, out of execution too. It is out of `dispatcher._PRIORITY`, out of the shipped `tool_providers:` policy, and `GEMINI_API_KEY` is commented out in `.env`. The provider code is retained but no active path reaches it.

### Provider status in detail

| Provider | In fallback chain | Reachable by tag | Notes |
|---|---|---|---|
| `claude` | yes (first) | `#claude`, `#claude_*` | Only provider that honours `#effort:` |
| `codex` | yes (second) | `#codex`, `#codex_*` | Default external second opinion |
| `gemini` | **no** (left `_PRIORITY` 2026-08-15) | tag parses, then barred by the shipped `tool_providers:` | Code retained; re-enabling needs `_PRIORITY` **and** policy **and** a key/CLI |
| `openrouter` | never (by design) | `#openrouter`, `#or_*` | Pay-per-token; barred by the shipped policy, and fail-**closed** when `policy.yaml` is missing |
| `vibe` | never (by design) | `#vibe`, `#vibe_*`, `#second_opinion:vibe*` | Reviewer-only; same fail-closed treatment as OpenRouter |

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
| `/reject <run_id> [criterion] [reason]` | Reject a scientific-investigation pre-registration / final gate |
| `/unblock <id>`, `/drop <id>`, `/retry <dep-id>` | Queue-healing responses for long-blocked tasks |
| `/pr-fix <owner/repo#N>`, `/pr-ignore <owner/repo#N>` | PR-Babysitter report-only mode: queue a fix task or silence the PR |
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
  - 14 built-in handlers: `queue-idle`, `queue-healing`, `git-status`, `disk-space`, `check-limits`, `log-capacity`, `summarize`, `stale-branch`, `usage-suggest`, `session-cleanup`, `model-check`, `skill-suggest`, `status-recap`, `check-ci-failures`
  - Sections support `## Every N minutes/hours/days` and `## Daily (after HH:MM)` — so a monthly check is just `## Every 30 days`.
  - `model-check` (recommended monthly): CLI-probes every entry in `CLAUDE_MODEL_ALIASES`/`GEMINI_MODEL_ALIASES`/`CODEX_MODEL_ALIASES` to detect dead IDs (skips providers currently in cooldown; pay-per-token aliases are not probed — a scheduled ping would be a recurring charge), then asks the best available LLM with today's date as anchor whether newer IDs are known. That second phase lists **all five** alias dicts including `or_*` and `vibe_*`, since one call costs the same regardless of list length. Telegram notification only fires on findings; LLM-call failures surface as `⚠️ LLM-Check failed: …` instead of being silently swallowed.
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

Default port: `8211` (configurable via `DASHBOARD_PORT`). If the port is already in use or reserved by Windows (Hyper-V/WSL dynamic ranges → `WinError 10013`), the server automatically falls back to a free port and logs the actual URL.

## Quota Calibration & State (cclimits anchor → local estimation)

`cclimits` reads Claude/Codex/Gemini quota from the undocumented, aggressively rate-limited `api.anthropic.com/api/oauth/usage` endpoint ([anthropics/claude-code#31637](https://github.com/anthropics/claude-code/issues/31637)). To depend on it less, the orchestrator calibrates a local `tokens_per_pct` model against real cclimits readings and persists a single-source-of-truth state file:

- **Phase 0 — telemetry** (`quota_calibration.py`): each successful `cclimits` poll appends a CSV row per Claude window (5h / 7d) to `logs/quota-calibration.csv`, pairing the real utilization-% with locally-aggregated JSONL token counts. Calibration selected the `io_only` model (input + output; cache tokens are negligible to the rate limit).
- **Phase 1 — SoTH + estimation** (`quota_state.py`): `_bg_refresh_loop` writes `logs/cc_quota_state.json` atomically each poll (per-window `remaining_pct` / reset times + embedded calibration constants). The Claude Code statusline and `--check-limits` read it instead of re-polling cclimits; the 429-fallback estimator uses the calibrated per-window factors.
- **Phase 2 — live rebalancing** (opt-in, `ORCH_QUOTA_LIVE_ESTIMATE`, default off): between polls, `get_limits()` decrements the cached snapshot by the calibrated per-task estimate and re-anchors on each fresh cclimits poll; optional daily auto-recalibration of the factors from the running CSV (`ORCH_QUOTA_AUTO_RECALIBRATE`, min-samples + clamp guarded). Default off keeps cclimits the per-poll source of truth.

The calibrated factors are plan- and workload-specific (env-overridable, not universal constants). Full CSV schema, calibration result, limitations, and the refuted 1h/5m tier-split experiment → [`docs/architecture/components.md`](docs/architecture/components.md#quota_calibrationpy--quota_statepy).

## Security / Guardrails

- Hard bans on destructive commands (`rm -rf`, `git reset --hard`, force-push, `DROP TABLE`, etc.)
- File deletion limits
- Protection against changes outside `cwd` (unless explicitly requested)
- `cwd:` validation against `ALLOWED_CWD_ROOTS` (when set)
- File-change snapshot + change summary after each task

## Prompt Budget (Token Allocation)

| Component | Budget | Source |
|---|---|---|
| Core (task + safety) | uncapped | `SOUL.md`, falling back to `SYSTEM_PROMPTS` (`config.py:325`) — `get_system_prompt` (`config.py:906`), called without truncation in `orchestrator.py:371` |
| Curated Memory (L1) | ~500 tokens | `MEMORY.md` |
| Daily Log (L2) | ~500 tokens | `daily/` |
| TF-IDF Memory (L3) | ~2000 tokens | `memory.py` |
| Lessons (L4) | ~2000 **chars** | `memory.get_lessons_context` (`memory.py:873`); `#tool:` path only (`tools/base_tool.py:542`), not `_build_prompt` |
| Wikilink context | ~1500 tokens | `queue_manager.py` |
| Skill prompt | ~2000 tokens | `SKILL.md` body (only with `#tool:`) |
| **Sum of the caps above** | **6 500 tokens** | the five `PROMPT_*_TOKENS` that have a caller. This is an addition, **not** an enforced ceiling: Core and the task text are uncapped, and the five do not all apply on every path (Skill only with `#tool:`). Nothing checks the total. |

## Doctor (`--doctor`)

`python orchestrator.py --doctor` runs 18 checks:

- Provider CLIs (`claude`, `gemini`, `codex`; `vibe` reported as WARN when absent — it is optional)
- Worktrees (orphaned `.worktrees/parallel-*` dirs, `--fix` prunes them)
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
- **Model IDs** — CLI-pings every entry in `CLAUDE/GEMINI/CODEX_MODEL_ALIASES` concurrently (~5–10 s). FAIL on rejected IDs (unknown/deprecated), WARN on transient probe errors, PASS when every alias responds live. The pay-per-token aliases (`or_*`, `vibe_*`) are deliberately **not** probed — a probe would cost money on every `--doctor` run. The deeper LLM-based "are there newer IDs?" heuristic runs only in the monthly heartbeat `model-check`, not here.

With `--fix` (optionally `--yes`) simple problems are auto-created/repaired.

## Architecture

```text
orchestrator.py
  → dispatcher.py          (provider selection + fallback)
      → providers/         claude, codex                        ← fallback chain
                           gemini (HTTP or CLI)                 ← retired 2026-08-15, code retained
                           openrouter, vibe                     ← opt-in only, tag-gated
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
  → quota_calibration.py   (Phase-0 telemetry: cclimits ↔ JSONL token pairs)
  → quota_state.py         (Phase-1 SoTH: logs/cc_quota_state.json for statusline)
  → logging_setup.py       (rotating file logger)
  → doctor.py              (setup validation / --doctor)
  → queue_linter.py        (offline queue validation / --lint-queue)
  → idempotency.py         (duplicate-trigger dedup for external sources)
  → replay.py              (logs/runs.jsonl run records)
  → taxonomy.py            (19-category failure classification over runs.jsonl)
  → preflight.py           (deterministic per-tool context collection)
  → queue_healing.py       (long-blocked task detection + /unblock /drop /retry)
  → skill_suggester.py     (draft-only SKILL.md proposals, pattern-gated)
  → session_registry.py    (whitelist of orchestrator-created Claude sessions)
  → usage_budget.py        (pace analysis over rolling windows)
  → ci_watcher.py          (CI-failure sweep) → gh_helpers.py (gh CLI wrapper)
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
# Run all tests (~2100 tests, ~100 s) — fixed order, see below
python -m pytest tests/ -q -p no:randomly

# Run a single test file
python -m pytest tests/test_parallel_runner.py -v
```

`-p no:randomly` is not cosmetic: `tests/test_telegram_listener.py` leaks state under some orderings, so `pytest-randomly` can colour the suite red without a code change. Last measured green run: **2098 passed in 99 s** (2026-08-15).

## Contributing

PRs welcome. Run `python -m pytest tests/ -q` before submitting. All tests must pass.

## License

MIT — see [LICENSE](LICENSE).
