# AI Orchestrator

[![CI](https://github.com/swDomass/AI_orchestrator/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/swDomass/AI_orchestrator/actions/workflows/ci.yml)

Autonomous task executor for the `claude` and `codex` CLI tools — driven by a Markdown queue file, with multi-provider fallback, Telegram control, and a security/approval layer.

> **Provider status (2026-09-04).** The active fallback chain is **Claude → Codex**. Gemini is still implemented (`providers/gemini.py`, HTTP API + legacy CLI) and every `#gemini*` tag still resolves, but it left `dispatcher._PRIORITY` and the shipped `tool_providers:` policy — nothing routes there by default. OpenRouter, Mistral Vibe and opencode CLI remain opt-in and never enter the chain. See [Provider status in detail](#provider-status-in-detail).

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

- **2844 tests / ~90–180 s** — full pytest suite covers queue parsing, dispatcher fallback, policy classification, provider mocks, stdin delivery verification, post-task verify checks, parallel execution, idempotency, quota calibration + SoTH state + live estimation, and per-tool phase logic. Tests are synchronous (no asyncio), pure stdlib + pytest fixtures, no live network calls. The documented command pins the order with `-p no:randomly` for reproducibility; since 2026-09-25 the suite is also green in random order (eight `pytest-randomly` seeds, Linux, Cloud — see [Known Limitations](#known-limitations)).
- **Defence in depth.** `scripts/safety_hook.py` is a Claude Code `PreToolUse` hook that hard-denies destructive commands (`rm -rf`, force-push, `DROP TABLE`, raw disk writes, `git push`, …) even under `--dangerously-skip-permissions`. A second, softer layer (`SAFETY_RULES`) rides in the system prompt — but know its exact reach before relying on it, because it is narrower *and* wider than "the non-Claude providers", and a `SOUL.md` switches it off entirely. `config.SYSTEM_PROMPTS` has entries for **claude, codex, gemini and opencode** — so Claude gets the rules as well, while **`vibe` and `openrouter` get an empty string** (`get_system_prompt()` does a `.get(name, "")`). And the moment a `SOUL.md` exists, `get_system_prompt()` returns that file's `base` section plus an optional per-provider override and never consults `SYSTEM_PROMPTS` at all — so the `SAFETY_RULES` constant then reaches **no** provider, and whatever safety text ships is whatever your `SOUL.md` happens to carry. If you use a `SOUL.md`, repeat the rules in its `base` section, and re-check that copy whenever `SAFETY_RULES` changes: nothing keeps the two in sync, and the hook (`SAFETY_DENY_PATTERNS`) is unaffected either way. CWD validation against `ALLOWED_CWD_ROOTS` blocks writes outside whitelisted roots.
- **Three-tier approval policy.** `policy.py` classifies every task as `AUTO`, `APPROVE`, or `DENY`. `APPROVE` tasks block until a Telegram `/approve` arrives; `DENY` never runs. Per-tool budgets and stop conditions (`max_iterations`, `max_runtime_sec`, `max_files_touched`, `reporting_path`) are declared in a YAML `tool_contracts:` section with schema validation at startup — one auditable place for every guard rail.
- **Operational resilience.** Three-tier HTTP 429 fallback (cclimits → local JSONL → optimistic), provider cooldowns with model-alias routing, OAuth-aware capacity polling (5 min active / 10 min idle, matching `cclimits --cache-ttl`), and a crash-resistant PowerShell watchdog with exponential backoff and Telegram alerts on every restart.
- **Auditability built in.** Every task end emits a structured `logs/runs.jsonl` record (22-category failure taxonomy: `rate_limit`, `timeout`, `auth_error`, `model_refusal`, `verify_failed`, `worktree_dirty`, …), per-tool action traces in `{cwd}/.<tool>/traces/*.jsonl`, an offline queue linter (`--lint-queue`, exit codes 0/1/2 for CI gating), and a 19-check `--doctor` with `--fix --yes` auto-repair.
- **Prompt-cache aware.** Stable prompt prefixes, `--exclude-dynamic-system-prompt-sections` to freeze the system prompt for cache hits, opt-in Claude session reuse (`--session-id` / `--resume`) for ~30–50 % token savings across multi-phase tools, and billing analytics with weighted cost (`input × 1.0 + cache_creation × 1.25 + cache_read × 0.1 + output × 5.0`) and per-task cache-hit rate.
- **Observability.** Standalone HTTP analytics dashboard (port 8211, Chart.js, 60 s refresh) backed by `logs/runs.jsonl`. Live "Active Runs" panel (30 s refresh) shows currently-running tool iterations, phase, tokens (in / out / cache_read) and elapsed time via the central `ActiveRunRegistry` in `logs/active_runs/`. Daily Telegram status recap (07:00) summarises the previous 24 h — tasks done/failed, provider breakdown, pending approvals, blocked tasks.
- **Calibrated quota model.** Phase-0 telemetry (`logs/quota-calibration.csv`) paired every `cclimits` poll with locally-aggregated JSONL token counts per Anthropic 5 h / 7 d window; ~6 days of data selected the `io_only` model (input + output — cache tokens are negligible to the rate limit). Phase 1 writes a single-source-of-truth `logs/cc_quota_state.json` each poll (consumed by the Claude Code statusline and `--check-limits`) and feeds the calibrated per-window factors into the 429-fallback estimator, reducing reliance on the undocumented, rate-limited `cclimits` endpoint.

Architecture details → [`docs/architecture/components.md`](docs/architecture/components.md) (per-module specs), [`docs/architecture/patterns.md`](docs/architecture/patterns.md) (patterns and invariants).

## Features

- Multi-provider routing with fallback (`Claude → Codex`; Gemini retired from the chain 2026-08-15, code retained)
- **Three opt-in providers outside the fallback chain**: OpenRouter (pay-per-token, `#openrouter`/`#or_*`) for cheap single-call work, Mistral Vibe (`#vibe`, `#second_opinion:vibe`) as a read-only second non-Claude reviewer, and opencode CLI (`#opencode`, `#opencode_*`) as a fourth training lineage and, via handpicked `openrouter/zdr-review*` aliases, the only ZDR-guaranteed review path for customer code. All three are registered only when their prerequisite exists (API key / binary on `PATH` / both required agents configured in `opencode.json`); a `#vibe` or `#opencode` tag without the provider registered **parks** the task rather than falling back to another provider (for `#vibe` because that would hand a review job to a file-writing executor; for `#opencode` because the tag itself is the ask — no Claude quota spent, or the only ZDR path — and a silent fallback would defeat it). opencode is also the only one of the three that is *capped*: its own OpenRouter key carries a real $/day limit, polled live, so it does not need the fail-closed policy treatment the other two get. Under the shipped policy all three are barred for tool tasks — see [Provider tags are subject to `tool_providers`](#supported-tags).
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
- **Reliability layer (Tier 5)**: queue linter (`--lint-queue`), idempotency keys for external triggers, slash-commands (`/review`, `/dev`, `/security`, `/audit`, `/critique`, `/brainstorm`), schedule tags (`#at:`, `#every:`), machine-readable run summaries (`logs/runs.jsonl`), 22-category failure taxonomy, per-tool preflight hooks (deterministic context collection), queue-healing with `/unblock`/`/drop`/`/retry`, draft-only skill suggester, progressive skill loading
- Analytics web dashboard (Chart.js, port 8211)
- **Calibrated quota model (Phase 0 + 1)**: Phase-0 telemetry (`logs/quota-calibration.csv`) selected the `io_only` `tokens_per_pct` model (input + output). Phase 1 persists a SoTH `logs/cc_quota_state.json` per poll (read by the statusline + `--check-limits`) and uses calibrated per-window factors in the 429-fallback estimator. The undocumented `cclimits` endpoint stays the calibration anchor (polled every 5–10 min), not the per-reading source. Details → [`docs/architecture/components.md`](docs/architecture/components.md#quota_calibrationpy--quota_statepy).
- `SOUL.md` as central prompt/personality configuration
- **Anthropic prompt-cache optimization**: static system-prompt (cwd/git-status moved to first user message via `--exclude-dynamic-system-prompt-sections`), stable prompt prefixes, billing analytics with cache-hit-rate
- **Optional Claude session reuse** (`CLAUDE_SESSION_ENABLED=true`): `dev-loop`, `review-loop`, same-provider `critical-review`, and `deep-security-audit` share Claude `--session-id`/`--resume` across phases for cross-call cache hits

## Requirements

- Python `3.12+` — matches `ruff`/`mypy` `target-version`/`python_version` in
  `pyproject.toml` and is now a real machine-readable key there
  (`requires-python = ">=3.12"`, 2026-09-04, was a comment before). The floor is
  empirically verified: `import limits` succeeds on 3.12, 3.13, and 3.14 (fixed
  2026-09-04, was a `NameError` on 3.10–3.13 before a forward-reference
  annotation got quoted). 3.10/3.11 are not available on the machine that
  verified this and are therefore not claimed as supported.
- `cclimits` CLI (`npm install -g cclimits`)
- Provider CLIs in `PATH`: `claude`, `codex`
- Valid authentication in each CLI (OAuth / subscription login)
- Optional, retired by default: `gemini`. The provider code is still shipped, but it is out of the fallback chain and out of the default policy since 2026-08-15 — re-enabling it means putting `gemini` back into `dispatcher._PRIORITY` **and** into the relevant `tool_providers:` entry, plus either the `gemini` CLI (Standard/Enterprise only — the consumer CLI was retired 2026-06-18) or a `GEMINI_API_KEY`.
- Optional, opt-in only: `vibe` (Mistral Vibe CLI) as a second non-Claude reviewer, `opencode` CLI (Stufe 2, tag-activated third external voice — needs `extern-review`/`extern-dev` configured in `opencode.json` plus a capped OpenRouter key), and an `OPENROUTER_API_KEY` for pay-per-token single-call tasks. None of the three is required, and none ever enters the default fallback chain.

## Installation

```bash
git clone https://github.com/swDomass/AI_orchestrator.git
cd AI_orchestrator
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your vault path and optional Telegram credentials
```

`requirements.txt` holds the only hard dependency, `pyyaml>=6.0`.
`requirements-optional.txt` adds `claude-monitor>=3.0.0` *(enables local JSONL fallback for Claude HTTP 429; requires `CLAUDE_PLAN` in `.env`)* — install it separately with `pip install -r requirements-optional.txt`. CI does not install it, which is why one test in `tests/test_limits.py` skips there.

## Linting & Typing

Ruff and mypy are configured in `pyproject.toml` (tooling sections only — the
repo is a flat script collection, not an installable package). Neither is
required to run the orchestrator. CI runs both on every push, advisory only —
the tests are the sole gate (`.github/workflows/ci.yml`).

```bash
pip install ruff mypy types-PyYAML
ruff check .
python -m mypy .
```

The first full measurement — 1351 ruff findings, 131 mypy errors, which of them
are real defects, and a proposed work order — is recorded in
[`docs/lint-baseline-2026-09-02.md`](docs/lint-baseline-2026-09-02.md). Its
headline defect, an import failure on Python 3.10–3.13, was fixed 2026-09-04
(see the doc's status note); the `3.12+` floor under
[Requirements](#requirements) reflects that fix.

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
| `OPENCODE_DEFAULT_MODEL` | No | `openrouter/zdr-review` | Model for `#opencode` without an `#opencode_*` alias. Must be resolvable by opencode: an unknown identifier produces **no error but a stall in initialisation** (measured), which `--doctor` now warns about in advance. |
| `OPENCODE_IDLE_TIMEOUT_SEC` | No | `300` | Watchdog idle timeout for opencode runs — the net under the stall above (idle kill → `hang` → requeue). |
| `OPENCODE_MAX_PROMPT_BYTES` | No | `50000` | Hard cap on the `-f` prompt file, in **both** modes. Exceeding it fails loudly with `prompt_too_large` rather than truncating, because a file outside `--dir` can never be re-read by the agent. |
| `OPENCODE_MIN_REMAINING_USD` | No | `0.25` | Strict `>` threshold on the OpenRouter key's `limit_remaining` below which opencode reports no capacity. |
| `OPENCODE_PICKER_PROFILE` | No | `zdr` | Profile handed to the optional external model picker. |
| `OPENCODE_MODEL_PICKER` | No | *(empty)* | Optional external picker script (90 s timeout). Absence is the normal case — resolution then falls to `OPENCODE_DEFAULT_MODEL`. |
| `OPENCODE_CONFIG_PATH` | No | *(empty)* | Override for the `opencode.json` location that `--doctor` validates. The permission blocks in that file (`bash: deny`, `external_directory: deny`) are the **only** fence around the writing `extern-dev` agent — see Security. |
| `ORCH_QUOTA_LIVE_ESTIMATE` | No | `false` | Phase 2: decrement the cached quota snapshot by a live per-task estimate between cclimits polls |
| `ORCH_QUOTA_AUTO_RECALIBRATE` | No | `false` | Requires the flag above: re-derive the per-window `tokens_per_pct` factors daily from `logs/quota-calibration.csv` (min-samples + clamp guarded) |
| `DASHBOARD_PORT` | No | `8211` | Port for the analytics web dashboard (auto-falls back to a free port if taken/Windows-reserved) |
| ~~`TELEGRAM_MAX_TASK_LENGTH`~~ | — | `500` | Max characters for `/task`. **Not readable from `.env`** — `config.py:519` assigns it literally, with no `os.getenv()`. Listed here only because it was documented as configurable until 2026-09-05; change the constant, or wire it through `_parse_int_env()`. |
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
- Unknown model alias (`#claude_*`, `#gemini_*`, `#codex_*`, `#vibe_*`, `#or_*`, `#opencode_*`) — the prefixes are derived from `queue_manager._MODEL_ALIAS_PREFIXES`, so the linter cannot fall behind `dispatcher._TAG_MAP`
- Unknown `#effort:` level (`unknown_effort`) — the tag regex is deliberately loose so a typo is *reported* here instead of being indistinguishable from no tag at all; at runtime the task falls back to the session default
- Cross-provider model leakage (`#claude_opus` + explicit `#gemini` = error)
- Duplicate `#id:` values in the open queue
- `#needs:` referencing IDs that will never resolve (warning)
- `#openrouter` / `#or_*` without `OPENROUTER_API_KEY` configured (warning — task falls back to default chain)
- `#vibe` / `#vibe_*` without the `vibe` CLI on PATH (warning, `vibe_missing_cli`) — worded differently from the OpenRouter case: a missing Vibe binary does not fall back to the default chain, it **parks** the task (`dispatcher._NO_FALLBACK_PROVIDERS`)
- `#parallel` with 0-1 subtasks (warning) or shared CWD (info)
- HTML comment inside the task body (`html_comment_in_body`) — truncates the task and deletes the trailing tags on rewrite, see [Retry Markers](#retry-markers)
- HTML comment at the line end that is not a valid `retry`/`hang` marker (`html_comment_trailing`) — silently dropped on rewrite; a near-miss marker means the schedule never applies
- A provider tag the `tool_providers:` policy **bars** (`provider_not_allowed`, error) — the runtime verdict comes from `dispatcher.forced_provider_policy_violation()` itself, so linter and orchestrator cannot disagree, and the task's `#agent:` profile and `#tool_providers:` tag are resolved first (both legitimately widen the allow-list). This is a *different question* from the registration checks above: `#vibe`/`#opencode` can be installed and still barred
- The two silently-degrading variants, both warnings: `#pass2:` on a `#tool:critical-review` task naming a barred provider (`pass_provider_not_allowed` — the pass falls back to the primary) and `#second_opinion:` on a `#tool:review-loop` task resolving to one (`second_opinion_not_allowed` — the phase is skipped without a word). `#pass1:` is deliberately **not** reported: nothing in the repo reads `pass_providers[1]`, so pass 1 runs on the primary provider under every policy — a policy warning there would blame the policy for a non-effect and suggest that widening `tool_providers` would change something. Same reason for `#pass2:` on any other tool: `orchestrator.py` hands the tag to every tool, only `critical-review` reads it — and, for the same reason, for `#second_opinion:` on any tool other than `review-loop`, and for an alias `review-loop` does not resolve at all. That last one is not hypothetical: `#second_opinion:opencode_glm` is a valid opencode alias, but the second-opinion phase only consults the OpenRouter/Claude/Codex/Vibe alias maps, so it is skipped under *every* policy. The linter therefore asks `review_loop.second_opinion_target()` — the tool's own mapping — instead of the repo-wide alias table
- `policy.yaml` missing (`policy_missing`, warning) or present-but-unusable (`policy_unreadable`, error: parse failure, non-mapping root, non-mapping `tool_providers:`); an empty file is `policy_empty` (warning). `PolicyEngine` reports all of these as "no restriction configured", so the linter reads the file itself — and reads the *running engine's* file (`PolicyEngine.config_path`), never `config.VAULT_PATH` separately. The `policy_missing` text says what the runtime does since 2026-09-17: claude/codex/opencode keep running, while a bare `#vibe`/`#openrouter` tag without explicit authorisation ends terminally with `provider_not_allowed` — and the per-task check reports exactly that line as an ERROR. (Until then the forced-tag branch ignored the fail-closed rule and the warning had to say such a tag would run, because a report claiming a pay-per-token provider is blocked while it is about to start is worse than no report)

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
| Force provider | `#claude`, `#codex`, `#vibe`, `#openrouter`, `#opencode` (`#gemini` still parses but is barred by the shipped policy) | `- [ ] Task #codex` |
| Claude model | `#claude_haiku`, `#claude_sonnet`, `#claude_opus` | `- [ ] Task #claude_haiku` |
| Reasoning effort (**Claude only**) | `#effort:low\|medium\|high\|xhigh\|max` → `claude --effort` | `- [ ] Classify inbox #claude_opus #effort:low` |
| Gemini model *(retired — parses, but barred by the shipped policy)* | `#gemini_pro`, `#gemini_flash`, `#gemini_flash_lite` | `- [ ] Iterate #gemini_flash` |
| Codex model | `#codex_5` (gpt-5.6-sol), `#codex_5_4` (gpt-5.6-terra), `#codex_mini` (gpt-5.6-luna) | `- [ ] Run #codex_mini` |
| OpenRouter model (opt-in, requires `OPENROUTER_API_KEY`) | Free: `#or_minimax_free`, `#or_deepseek_free`, `#or_qwen_free`, `#or_nemotron_free`. Paid flagships: `#or_glm`, `#or_kimi`, `#or_qwen`, `#or_deepseek`, `#or_minimax`. Generic: `#openrouter` (default model). | `- [ ] Daily summary #or_minimax_free` |
| Vibe / Mistral (opt-in, requires the `vibe` CLI) | `#vibe` — routes to Vibe using its configured model. Reviewer only: never writes files, never enters the fallback chain, and if the CLI is missing the task is **parked rather than handed to an executor**. | `- [ ] Second opinion #vibe` |
| Vibe model choice | `#second_opinion:vibe_medium` (mistral-medium-3.5) / `#second_opinion:vibe_small` (devstral-small). Since 2026-08-15 the bare task tags `#vibe_medium`/`#vibe_small` force the model too — `MODEL_TAG_RE` is generated from `dispatcher._TAG_MAP` and covers all 23 model aliases (plus 6 bare provider-selection tags via the separate `PROVIDER_TAG_RE`, 29 routing tags total). | `- [ ] Review #tool:review-loop #second_opinion:vibe_small` |
| opencode (opt-in, Stufe 2 — requires `opencode.exe` resolvable + `extern-review`/`extern-dev` in `opencode.json`) | `#opencode` routes to the default resolution order (forced alias > optional external picker > `OPENCODE_DEFAULT_MODEL`). Handpicked ZDR aliases only: `#opencode_deepseek` (deepseek-v4-pro), `#opencode_deepseek_long` (deepseek-v4-flash, 1M ctx), `#opencode_glm` (glm-5.2). Capped by its own OpenRouter key ($/day, polled live) rather than uncapped like Vibe/OpenRouter; if unregistered the task is **parked**, same non-fallback treatment as `#vibe` but for a different reason (the tag itself asks to avoid Claude quota / to guarantee ZDR). | `- [ ] Second opinion #opencode_deepseek` |
| Run tool | `#tool:<name>` | `- [ ] Review #tool:review-loop` |
| Restrict providers (task-level) | `#tool_providers:<p1,p2>` | `#tool_providers:claude,codex` |
| Working directory | `cwd:<path>` | `cwd:D:\projects\repo` |
| Working directory with spaces | `cwd:"<path>"` | `cwd:"D:\My Projects\App"` |
| Timeout (hard backstop, not aggressive deadline; for tools an upper cap) | `#timeout:<n>[s\|m\|h]` | `#timeout:30s`, `#timeout:15m`, `#timeout:1h` |
| Execution profile | `#agent:<name>` | `#agent:work` |
| Parallel task | `#parallel` | Parent task with indented subtasks |
| Waive the clean-worktree gate | `#allow-dirty` | `- [ ] Fix it #tool:dev-loop #allow-dirty cwd:D:\repo` |
| Task ID | `#id:<name>` | `- [ ] Build backend #id:build` |
| Task dependency | `#needs:<id1,id2>` | `- [ ] Test #needs:build` |
| One-time schedule | `#at:<timestamp>` | `- [ ] Review #at:2026-05-17T22:00` |
| Recurring schedule | `#every:<duration>` | `- [ ] Daily review #every:24h` |
| Time-of-day anchor | `#at:<HH:MM>` + `#every:<Nd>` | `- [ ] Daily brief #at:08:00 #every:24h` |
| Skip stale slot | `#freshonly` | `- [ ] Daily brief #at:08:00 #every:24h #freshonly` |
| Stale grace window | `#grace:<duration>` | `- [ ] Recap #at:19:00 #every:24h #freshonly #grace:4h` |
| Shutdown after task | `#shutdown` | `- [ ] Backup #shutdown` |
| Skip the per-task auto-commit | `#no-commit` | `- [ ] Try something #no-commit cwd:D:\repo` |
| Post-task outcome check | `#verify:<script>` | `- [ ] Daily brief #verify:scripts\check_brief.ps1` |
| Cross-provider pass | `#pass1:<provider>`, `#pass2:<provider>` — provider is `claude`, `gemini`, `codex`, `vibe`, `openrouter` or `opencode` | `#pass1:claude #pass2:codex` |
| Preapproval | `#approve:<category,...>` | `#approve:push,publish` |
| Second opinion (review-loop) | `#second_opinion:<alias\|provider>` | `#second_opinion:codex`, `#second_opinion:vibe_small` |

**Provider tags are subject to `tool_providers`.** Every routing tag above is filtered through the `tool_providers:` section of `policy.yaml` (task-level `#tool_providers:` → profile → global). A tag naming a barred provider does **not** quietly fall back to another one: the task fails with a logged `provider_not_allowed`, because in an unattended run a silent swap hides which model actually did the work. The same filter applies to **every** provider lookup **inside** tools (second opinion, `#pass2:`, cross-provider persona allocation *and* the step that resolves an allocation into a provider instance), all of which previously bypassed it entirely — all seven raw call sites are converted. The two resolution sites (`tools/brainstorm.py`, `tools/scientific_investigation_phase2.py`) are a second gate, not a duplicate one: they resolve from an allocation list that a persisted state file or the degraded fallback path can outlive, so a name that was legal when written is re-checked against today's policy before a prompt goes out over it. The one deliberately raw call left is `tools/critical_review.py`, which asks `policy_allows_provider()` first so that "policy said no" and "unknown provider" produce different log lines. Under the shipped policy (`[claude, codex]`, `review-loop`/`test-loop`: `[claude]`) this makes `#gemini*`, `#openrouter`/`#or_*` and `#vibe*` inert — including `#second_opinion:vibe_small` and `#second_opinion:codex` for `review-loop`. `#opencode`/`#opencode_*` is inert for `#tool:` tasks too, but by omission rather than by bar: no `tool_providers:` entry names `opencode`, which is the intended state for Stufe 2. **Plain** tasks are a different matter and were broken until 2026-09-04: `default:` is what a queue line without any `#tool:` gets, and while it read `[claude, codex]` a bare `#opencode` task ended terminal ❌ with `provider_not_allowed` — the opposite of tag activation, and invisible to `--lint-queue`, which validates CLI registration rather than policy. `default:` now reads `[claude, codex, opencode]`; that adds no fallback route (opencode is still absent from `dispatcher._PRIORITY`) and leaves the eleven tool entries untouched. Widen the relevant `tool_providers:` entry to use any of them.

**If `policy.yaml` is missing or unreadable, the pay-per-token providers stay barred — except opencode.** The file cannot be assumed present — it lives in a synced folder — and an absent one is indistinguishable from an empty one. So the fallback is asymmetric: the capped providers (`claude`, `codex`, `gemini`) stay allowed, because a lost config file must not take the orchestrator offline, while `openrouter` and `vibe` stay blocked, because they are billed per token and have no cost ceiling to fall back on. `opencode` is billed per token too but IS capped (its own OpenRouter key carries a real, live-polled $/day limit), so it gets the same fail-**open** treatment as claude/codex/gemini rather than the fail-closed one — a lost policy file bars an already-capped provider from nothing extra. Only an explicit allow-list re-enables `openrouter`/`vibe`. A task whose policy allows nothing routable is finalized with `no_provider_allowed` rather than parked — no quota reset can lift a policy restriction, so parking it would mean waiting forever.

**`#effort:` details.** As a heuristic (not benchmarked in this repo), try lowering the effort level before dropping a model tier. Without the tag the CLI keeps its own session default; the orchestrator never injects a default of its own. `--effort` exists **only** on the Claude CLI: Codex, Gemini, Vibe and OpenRouter ignore the tag by construction (they read `_forced_effort` nowhere). **opencode is the exception since 2026-09-04**: `providers/opencode.py:370` reads the same property and passes the value through to its own `--variant` flag, raw and unmapped, so the tag is not a no-op there the way it is for the others, and `--lint-queue` warns (`effort_non_claude`) when a task carrying the tag is explicitly routed elsewhere. An unknown or malformed level is a lint **error**; at runtime it degrades to the session default rather than failing a scheduled run. On a **parent** task the orchestrator also logs a warning naming the offending tag, so a typo leaves a trace in a scheduled run. Two cases stay lint-only: a tag on a **subtask** (the subtask parser warns about nothing) and a bare `#effort` with no value (warned about nowhere, and deliberately not stripped so the word survives in prose). Subtasks honour the tag too and inherit the parent's level when they carry none.

**`#verify:` details.** Relative paths resolve against `cwd:` (without a `cwd:` tag, against the orchestrator process's working directory — prefer absolute paths there). Quote paths containing spaces or `#`. Supported types: `.ps1` (via `pwsh -NoProfile -File`), `.py` (via the running interpreter with `-X utf8`), `.cmd`/`.bat`/`.exe` and extensionless executables; anything else is rejected with a clear message. Exit 0 = passed, non-zero = alarm, and the script's stdout (or stderr when stdout is empty) becomes the alarm text — a PowerShell check should set `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8` so umlauts survive the redirect. Runs in all three success paths (single-shot, `#tool:`, `#parallel`), always after the queue line is finalized; for `#parallel` only when every subtask succeeded. A failing check raises the alarm but does not requeue the task — see `--lint-queue` for the `verify_without_path` rule that catches a `#verify:` with no script. **A script that resolves to a path which does not exist is its own case, not a plain failure** (fixed 2026-09-16, measured incident: `cwd:` pointed at the haus-repo while the `#verify:` script lived in the vault, "Skript nicht gefunden" 8× since 2026-09-05 read indistinguishably from "the task ran and did not deliver its result"). Runtime now reports `error_code="verify_missing"` (own taxonomy category, distinct from `verify_failed`) with an alarm text starting "Konfigurationsfehler: Prüfskript nicht gefunden: …" — the fail-closed handling (❌ restamp, no `#needs:` release) is unchanged, only the diagnosis differs. `--lint-queue` catches the same defect offline as `verify_script_missing`, resolving the path exactly like the runtime (reuses `orchestrator._resolve_verify_path`) so linter and runtime cannot disagree **given the same process cwd and no `cwd:` tag on the line** — without a `cwd:` tag both resolve against their own process cwd, so a `--lint-queue` invocation from a different directory than the later orchestrator run can produce a finding the runtime does not confirm (the function itself documents this: it can only ever PROVE a problem, not rule one out); skipped when `cwd:` itself is invalid (already reported as `invalid_cwd`). Neither this check nor `verify_without_path` scans `#parallel` **subtask** lines — a `#verify:` there is silently dead markup, both offline and at runtime (`_execute_tool_task(skip_queue=True)` only ever reads the PARENT line's verify tag) — a pre-existing gap this pair inherited rather than introduced.

### Known Limitations

**A git snapshot restores tracked files only, and old ones are pruned.** Before every
non-read-only task in a git repo the orchestrator writes a rollback point to
`refs/orchestrator-backup/<timestamp>` (never to `refs/stash` — that list stays
yours). Restore with `git stash apply refs/orchestrator-backup/<timestamp>`; the ref
name is printed and logged on creation, because `git stash list` does not show it.
Two limits: `git stash create` does **not** capture untracked files, so a snapshot
rolls back modifications to tracked files only; and each new snapshot prunes the
namespace by `age >= 14 d AND (age > 30 d OR outside the newest 50)`. The 14-day
window is a veto over both caps — a successful run now commits its own paths to an
`orch/*` branch, but the snapshot is the only copy of the state *before* the run and
the only copy at all for paths the commit deliberately left behind or for runs that
failed — which means the count cap
can be starved in a high-churn repo. Nothing outside `refs/orchestrator-backup/` is
ever touched, so moving a ref elsewhere keeps it forever. Age is the commit date, not
the date the ref appeared: a ref moved into the namespace by hand keeps the *old*
commit's age and can therefore be pruned on the very first run. Every deletion is
logged with ref name and sha (recoverable with `git stash apply <sha>` until `git gc`);
deleting more than one ref in a single pass is logged at `WARNING`, because routine
ageing retires at most one at a time.

**`select_provider()` is fail-closed for a bare `#vibe`/`#openrouter` tag when the policy is missing (closed 2026-09-17).** The fail-closed rule lives in `dispatcher._allows()` / `policy_allows_provider()`. The **profile branch** of `_selection_order()` has consulted it per candidate since 2026-09-04; the **tag/forced branch** has done the same since 2026-09-17 — both `_selection_order()` and `forced_provider_policy_violation()` now judge the forced provider with `_allows()` instead of testing `allowed` for truthiness. With `policy.yaml` absent, unreadable, or present without a `tool_providers:` section (`PolicyEngine` cannot tell these apart), a bare `#vibe`/`#vibe_*`/`#openrouter`/`#or_*` tag ends terminally as `- [x] … ❌ …` with `provider_not_allowed` — same message and code as an explicit policy ban. `claude`/`codex`/`gemini` and **opencode** (capped via its own OpenRouter key budget) stay fail-open. Authorise an uncapped provider deliberately with a `tool_providers:` entry, a profile, or `#tool_providers:vibe,claude` on the queue line. Named limit: a `#parallel` **subtask** tagged `#vibe` under a missing policy now simply fails (no dedicated `provider_not_allowed` message on that path).

**An unregistered value in `#pass1:`/`#pass2:` is dropped without a word.** Since 2026-09-02 the regex accepts `vibe` and `openrouter` alongside `claude`/`gemini`/`codex`, but anything else — a typo, a provider that was never registered — fails twice over: `extract_pass_providers()` drops the pass silently, and `strip_metadata_tags()` does not recognise the tag either, so it survives into the prompt as literal text. Measured: `#pass1:claude #pass2:mistral` yields `{1: 'claude'}` and a prompt still ending in `#pass2:mistral`. The queue linter has no check for it.

**The safety hook cannot see a git write handed to a non-shell wrapper.** `find . -exec git push …`, `docker exec c git push …` and `xargs git push` are accepted residuals: `_CMD_START` treats `-` as a non-boundary character, and recognising these would need real argv parsing rather than a regex. Likewise, a **heredoc body line** that begins with a git write command matches, because `\n` is a genuine command separator and the hook cannot tell body text from a command — a deliberate false-positive trade in the safe direction. The hook also only covers Claude; the Codex path is fenced by its own sandbox, which depends on `experimental_windows_sandbox = true` in `~/.codex/config.toml`.

**`stdin_incomplete` can requeue without bound.** `<!-- hang: N -->` is the queue's only persistent per-task counter, and only `hang` and `format_error` *increment* it. Every other `error_code` — `stdin_incomplete` included — requeues without raising it, so a task failing that way indefinitely is retried indefinitely across polls. The retry *count* is unbounded; the *rate* is still throttled by the 5-minute provider cooldown and the park cycle, so this burns polls rather than spinning. Pre-existing, not introduced by the 2026-08-15 work. (Since 2026-08-15 those parks at least no longer *reset* the counter — see the note under `MAX_HANG_RETRIES` — so a task that alternates between real failures and parks does reach the cap.)

**Random test order is verified by sampling, not by construction (order leak closed 2026-09-25).** `tests/test_telegram_listener.py` used to go red under some `pytest-randomly` orderings. The state leaked in from `tests/test_shutdown.py`: `test_shutdown_state_management` left the process-wide `shutdown.shutdown_pending` Event set, and `TelegramListener._handle_message` treats a pending shutdown as "cancel it" — an extra reply, and plain text is swallowed. An autouse fixture in `tests/test_shutdown.py` now clears both shutdown Events around each test. Measured on Linux, Cloud (CPython 3.12.3, pytest-randomly 4.1.0): seeds 22222, 1, 42, 1234, 31415, 99999, 114 and 120 all 2837 passed / 7 skipped; without the fix, 22222, 114 and 120 were red. What remains is the limit of that method: eight seeds do not prove every order, and CI runs only the fixed order. `-p no:randomly` stays in the documented command and in CI as the reproducible order, but is no longer load-bearing. (`tests/test_usage_suggester.py` used to be red in any fresh worktree too — 14 cases depended on a `.env` with Telegram credentials; they patch `TELEGRAM_ENABLED` themselves since 2026-09-24.)

**`--lint-queue`'s policy check predicts the runtime.** The linter reads `policy.yaml` since 2026-09-09 (before that it only asked whether a `#vibe`/`#opencode` CLI was *registered*, which hid a total outage: with `tool_providers.default: [claude, codex]` **every** `#opencode` line ended terminally as `- [x] … ❌ …` with `provider_not_allowed` while the linter reported "no problems found", measured 2026-09-04). It calls the runtime's own `forced_provider_policy_violation()` rather than reimplementing the layering, so linter and orchestrator cannot disagree — which is also why closing the forced-branch hole (above, 2026-09-17) needed no linter code of its own: a bare `#vibe`/`#openrouter` tag under a missing policy is now reported as `provider_not_allowed` (ERROR), because that is what will actually happen. Two things it still does not see: `policy_dead_end()` (a task with **no** provider tag that the policy leaves unroutable — a separate check, not built) and a provider tag on a `#parallel` **subtask**, which `parallel_runner` does route on its own (`select_provider(force_name=subtask.provider_forced, …)`). The subtask blind spot is the pre-existing one that model tags already have; widening it is a change of its own.

**The status display shows what the policy allows, which is not the same as what exists.** Since 2026-09-09 the heartbeat health-check summary, `/status` and `/limits` derive their provider list from `limits.display_provider_names()` — `dataclasses.fields(AllLimits)` filtered through `dispatcher.policy_allows_provider(name, None)`. (They used to hand-enumerate `("claude", "gemini", "codex")`, wrong in both directions: opencode became an `AllLimits` field on 2026-09-04 and was never shown, while gemini left every active path on 2026-08-15 and was shown on every poll. Gemini now disappears because its *retirement* says so — it left policy.yaml's `tool_providers` — not because a second list repeats the decision.)

**The capacity log is deliberately not filtered.** `heartbeat._append_capacity_log()` uses the unfiltered `limits.all_provider_names()` instead, because `logs/capacity-log.md` is not a message but the input of `analytics._parse_capacity_log()`, which feeds the dashboard's current-limits panel and its historical series. Filtering a recorder does not tidy anything — it ends a provider's history, including for a provider that is still running: with `default: [claude]` and `dev-loop: [claude, codex]`, Codex keeps executing dev-loop tasks while the `default:` lookup drops it, so no Codex row is ever appended again and the dashboard reports no Codex capacity at all.

Three consequences worth knowing. The display filter asks the **`default:` entry**, so a provider allowed only for one specific tool and absent from `default:` is hidden from the *messages* even though tasks route to it (no such provider exists today; the log still records it). The filter is **fail-open and never empty**: a lost or unreadable policy.yaml re-adds gemini to every status message, and a policy barring everything still prints all four — noise in the safe direction, chosen over blanking the status display at 03:00. And the list is *not* universal: `limits.py`'s own cclimits enumerations (in `_providers_with_429()` and `_apply_429_fallback()` — named rather than line-numbered, because the two pointers written here on 2026-09-09 were already off by six lines when they were committed) and `quota_state.py:51` stay hand-written on purpose (opencode has no cclimits quota to probe), while `orchestrator.py:3379` (`--check-limits`) still hand-counts all four — complete today, and left alone deliberately; see ROADMAP.

**`.dev-loop/` grows one subdirectory per distinct task text, and nothing prunes it.** Output is keyed by task since 2026-09-09 — `{cwd}/.dev-loop/<task-hash>/`, so two dev-loop tasks pointed at the same repo no longer overwrite each other's `research-and-plan.md`, `round-00N.md`, `summary.md` or `state.json` (they did until then: the path used the cwd alone and `_task_hash()` only ever reached the *inside* of `state.json`). The parent `.dev-loop/` deliberately keeps its name, because `ToolTracer` derives `{cwd}/.dev-loop/traces/` from the *tool name* and `analytics` globs `**/.*/traces/*.jsonl` — a renamed parent would move every trace file and change tool attribution. Two consequences: **pre-2026-09-09 artefacts are not migrated, moved or deleted** (they stay directly under `.dev-loop/`, and a run interrupted by the upgrade still resumes — `_load_state()` falls back to the shared `.dev-loop/state.json` after validating its task hash), and **neither location is ever cleaned up**. Editing a task's text creates a new subdirectory rather than continuing the old one. Deleting run artefacts automatically was rejected as destructive housekeeping; do it by hand.

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

### Clean-worktree gate (`#tool:dev-loop`)

A `#tool:dev-loop` task does not start unless its `cwd:` is a git repository with a
clean working tree. On a violation the task is **not started at all** and is
finalized as a terminal failure with `error_code="worktree_dirty"`.

Why dev-loop specifically: it *produces* the diff that its own Quality and
Resolution reviewers then judge. Uncommitted changes from an earlier task are not
noise there, they corrupt the object under review. `#tool:review-loop` is
deliberately **not** gated — it *consumes* an existing diff, so requiring a clean
tree would be the opposite of what it is for.

The scope is a property of the tool class (`BaseTool.requires_clean_worktree`), not
of the queue line, so a hand-written task is right by default with no extra tag to
remember. Plain tasks (no `#tool:`) in permanently dirty repos are unaffected.

- **`#allow-dirty`** waives the check for a repo that legitimately always carries
  uncommitted work.
- A task **without `cwd:` is refused**, not skipped: `cwd=None` is passed straight
  to `Popen`, which inherits the orchestrator's own working directory, so the task
  would run against an unspecified repo. `#allow-dirty` waives this too.
- `#parallel` **subtasks are gated individually** (`parallel_runner`), since they
  are what actually runs; the parent itself is exempt.
- There is **no** automatic branch switch or reset: a reset can destroy work from
  another session, and refusing to start makes the same mistake just as visible.
- Terminal rather than parked: nothing cleans the tree on its own, so a park would
  re-check the same dirty tree every poll, forever and unattended.
- **Not covered:** a *clean* tree left on a foreign branch. The orchestrator cannot
  know which branch a task expects — that lives only in the prompt text.

### Per-task auto-commit (`orch/<id>-<date>`)

A successful run commits **its own** changes onto a branch of its own and leaves the
working tree clean, so the next `#tool:dev-loop` in the same repo does not die on
`worktree_dirty` and the morning review gets one branch per task to read, merge or
throw away. On by default; `GIT_AUTO_COMMIT=false` in `.env` turns it off globally,
`#no-commit` for a single queue line.

**HEAD is never moved.** The commit is built through a temporary index
(`GIT_INDEX_FILE`) plus `commit-tree` and `update-ref`; only afterwards are the
committed paths restored in the working tree. A `git checkout -b` / `git checkout
<orig>` pair would be shorter but leaves HEAD on a foreign branch if the process
dies between the two commands — the measured `nightstash` failure this exists to fix.

Which paths: exactly the ones the before/after `_snapshot_dir` comparison reports,
intersected with what git actually sees, **and only where the index agrees with
HEAD**. A path someone else has staged (`MM`, `AM`, a merge conflict's `UU`, a
rename) is skipped entirely and reported — touching it would discard that staged
content. Pathspecs are passed as `:(literal)` so a filename containing glob
metacharacters cannot reach a neighbouring file.

Silent no-op when: no git repo, no diff, unborn HEAD, `#no-commit`, a read-only
tool, or a **red `#verify:`** — a run that reported success without producing the
result must not be committed. A commit that fails does not make the task red; it
rides along in the Telegram report as a warning.

Two rules decide which paths may be touched, and both must hold: the **index must
agree with HEAD** (so nothing someone staged is ever discarded) and the path must have
been **clean when the run started** (so a file a human edits during a `--watch` run is
never mistaken for the run's own work — measured: without it, an unstaged live edit ends
up in the branch and back at its HEAD content on disk). If the pre-run state cannot be
determined, nothing is committed at all rather than guessed.

> **Upgrading an existing install:** this is ON by default and it *writes git state* —
> after a successful task it commits the run's paths and restores them in the working
> tree. If your workflow is "read the diff in the tree in the morning", that diff now
> lives on an `orch/*` branch instead. Set `GIT_AUTO_COMMIT=false` to keep the old
> behaviour. Note that `#allow-dirty` (which waives the clean-worktree gate for a repo
> that permanently carries uncommitted work) does **not** imply `#no-commit` — the
> clean-at-start rule protects that work, but combining the two tags is the explicit way
> to keep the orchestrator out of git entirely for such a repo.

Not covered (deliberate): `#parallel`/`#worktree`, push/PR/merge, model-written
commit messages, and retention for old `orch/*` branches — a recurring `#every:` task
therefore accrues one branch per day.


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
- A failed **`#verify:` outcome check** flips an already-written ✅ to ❌. The three
  success paths finalize before they verify (so a re-run cannot alarm twice), and
  `#verify:` is the one signal for "the run was clean and the work did not happen"
  — without the flip, that line would still release its `#needs:` dependents.
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
| `dev-loop` | Research → Execute → Dual-Review loop (Code Quality + Issue Resolution). Both reviews must pass. Same **P1 + P2** semantics as `review-loop`: only blocking findings reach the executor, P3 is collected across all iterations and appended once as a closing offer. From iteration 2 on, the executor writes a `## Rundenreflexion` section (over-building? chasing an edge case?) and may defer any P2 finding as `- [BEKANNTE GRENZE] <finding> — <reason>` instead of fixing it; a P1 can never be deferred this way. Deferred findings are listed as "Bekannte Grenzen" in the final output, separate from the P3 offer. Output in `{cwd}/.dev-loop/<task-hash>/`. |
| `review-loop` | Iterative Review → Fix → Re-Review loop. Fixes all **P1 + P2**; **P3 is non-blocking** and is reported once at the end as an offer instead of being fixed (cosmetics on working code widen the diff, and since each round re-reads the fresh diff, a P3 fix can surface new P3). A reviewer output that lists findings *and* the "no findings" sentinel counts as having findings — the sentinel alone used to pass the success gate with an unfixed blocker. Max 20 iterations with infinite-loop detection. Optional drift-check (`policy.yaml` `tool_phases.review-loop.drift_check_mode`, default `auto`) injiziert eine Refocus-Warning in den nächsten Fix-Prompt, wenn der Reviewer in unrelated Refactoring abgedriftet ist. Same Rundenreflexion/BEKANNTE GRENZE mechanism as `dev-loop`, from iteration 2 of the fix prompt on. |
| `test-loop` | Iterative test / fix loop until tests pass or max iterations. From iteration 2 on, the fixer may defer a test it judges an edge case beyond the task as `- [BEKANNTE GRENZE] <test id> — <reason>`; no P1/P2/P3 model here, and a deferral never turns the run green — it is listed as "Bekannte Grenzen" in the failure message. |
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
  → Saved to {cwd}/.dev-loop/<task-hash>/research-and-plan.md
  → State persisted under phase=research_and_plan_done for capacity-resume.

Phase 2 — Execution
  Implements the solution based on the merged research+plan output.
  On iteration > 1: includes findings from both prior reviews, plus a
  Rundenreflexion prompt (over-building? chasing an edge case?) that lets
  the executor defer any P2 finding as a BEKANNTE GRENZE instead
  of fixing it.

Phase 3a — Code Quality Review  (P1/P2/P3, read-only)
  Checks: Correctness, Clean, Secure, Performant, Maintainable,
          Testable, Robust, Documented, Compliant.
  P1/P2 = blocking | P3 = non-blocking
  Re-reads `git diff` fresh — does NOT trust pinned context.

Phase 3b — Issue Resolution Review  (RESOLVED/PARTIAL/UNRESOLVED, read-only)
  Checks only: Does the code solve the original task 100%?
  Ignores code quality entirely. Re-reads `git diff` fresh.

→ Both reviews must pass → loop ends, no auto-push.
→ Per-iteration output in {cwd}/.dev-loop/<task-hash>/round-NNN.md
→ Final summary: {cwd}/.dev-loop/<task-hash>/summary.md
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
| `TOOL_LANDING_RESERVE_SEC` | 2400 (40 min) | Wall-clock held back from the budget so a run can LAND instead of being cut off. Crossing `deadline - this` marks the current iteration as the last, tells the executor to stabilise, and still runs the reviews. Derived from the longest complete dev-loop iteration ever traced (1829 s), plus 31 % headroom |
| `TOOL_LANDING_MIN_PHASE_SEC` | 60 | Floor for a phase timeout once clamped to the remaining wall-clock. Below `3 ×` this, the landing round is not started at all |

**A tool that runs out of budget lands; it is not cut off.** Until 2026-09-10 the
total-runtime deadline was checked between iterations and returned
`tool_runtime_exceeded` on the spot, which `orchestrator.py` finalises terminally —
so a run one review away from done was stamped failed. Now the last
`TOOL_LANDING_RESERVE_SEC` of the budget are a landing round: no new full iteration
is started, the execution prompt is told to stabilise rather than begin anything,
and the reviews still run. **A landing round whose reviews pass is a success** and
gets the normal ✅; only an unresolved one keeps the old terminal outcome. The
reserve is enforced rather than merely scheduled — **every** phase of **every** round is
clamped to the wall-clock left at the moment that phase starts, because a single
execution phase may otherwise ask for `TOOL_DEV_EXEC_TIMEOUT_SEC` (7200 s) and overrun
the whole budget. `TOOL_LANDING_MIN_PHASE_SEC` is the one deliberate way past the
deadline (handing a provider a zero timeout spends a full prompt on a call that cannot
finish), and the overrun is bounded by the number of calls that hit that floor:
`2 ×` it in the usual case, at most `6 ×` if every phase also needs a session-missing
retry — 120 s to 360 s against a 3 h budget.

**A quota exhaustion mid-run parks the task and the next run continues it.**
`providers/claude.py` now reads `session limit` as a rate limit (the CLI says
"You've hit your session limit · resets 1:30am"; the four older keywords missed it,
so the raw prose escaped classification and a finished two-hour dev-loop was
finalised as failed on 2026-09-09). Inside `dev-loop` a capacity error in any phase
is turned into `capacity_exhausted`, which parks the task until the quota reset
instead of rotating it to the next provider — a rotation would restart the loop at
iteration 1 with a fresh deadline and no review context. Before parking, the run
writes an iteration checkpoint into its existing `state.json` (`state_version: 2`;
version-1 files stay readable as research caches), and the resumed run picks up at
that iteration with the previous review findings, the deferred P3 list, the loop
detectors, the token counts — and with the consumed wall-clock subtracted, so the
budget bounds the **task**, not one process. The worktree gate lets such a
continuation start in a dirty repo via `tool.resume_permits_dirty()`, which proves
ownership by comparing today's dirty path set against the one recorded at park time;
any path that was clean then restores the normal refusal.

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

> **Why every writing step carries `#no-commit`.** This whole chain is built on one
> thing: each step inspects the *uncommitted* changes the previous step left in the
> working tree. Since the per-task auto-commit landed (2026-09-11) a successful run
> commits its own paths and restores them — so without the tag, step 1 would leave an
> empty tree and step 2 would audit nothing. The prose "dont commit the changes!" that
> used to stand here is an instruction to the *provider*; the orchestrator's own commit
> does not read prose, only the tag. Step 6 (`critical-review`) needs no tag because it
> is `read_only`, and step 8 is the one that is *supposed* to commit.
>
> This is the general shape, not a quirk of this template: **any `#needs:` chain whose
> later steps consume the earlier steps' uncommitted diff needs `#no-commit` on every
> writing step but the last.**

```markdown
- [ ] Implement docs\plan-XXX.md. #no-commit #id:ID1 #tool:dev-loop #claude_opus cwd:<repo>

- [ ] security-audit of the uncommitted changes. #no-commit #id:ID2 #need:ID1 #tool:security-audit #claude_opus cwd:<repo>

- [ ] use your simplify skill for the uncommitted changes. #no-commit #id:ID3 #need:ID2 #claude_sonnet cwd:<repo>

- [ ] Review-fix loop for the uncommitted changes. #no-commit #tool:review-loop #id:ID4 #need:ID3 #codex_mini cwd:<repo>

- [ ] Review-fix loop for the uncommitted changes. #no-commit #tool:review-loop #id:ID5 #need:ID4 #codex cwd:<repo>

- [ ] Critical review (read-only) of the uncommitted changes against docs\plan-XXX.md #tool:critical-review #pass1:claude #pass2:codex #id:ID6 #need:ID5 cwd:<repo>

- [ ] Review-fix loop for the uncommitted changes. Also incorporate findings from the most recent critical-review report in docs/. #no-commit #tool:review-loop #id:ID7 #need:ID6 #claude_opus cwd:<repo>

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

Gemini is **not used at all any more** (2026-08-15). It was dropped as a reviewer first — consumer CLI retired 2026-06-18, and data-training/privacy concerns rule out the HTTP mode for review content — and then, after an `IneligibleTierError` left the remaining path unusable, out of execution too. It is out of `dispatcher._PRIORITY`, out of the shipped `tool_providers:` policy, and `GEMINI_API_KEY` is commented out in `.env`. The provider code is retained.

**Be precise about how it is retired, because "no active path reaches it" is too strong** (corrected 2026-09-05). Gemini is still *named* in the code: the cross-provider allocators carry it in their candidate tuples (`tools/brainstorm_phases.py:362`, `tools/scientific_investigation_phases.py:640`, plus the two ordered tuples in `scientific_investigation.py` / `_phase7.py`), and `--check-limits` iterates it. What stops it is the **policy filter** — those names resolve through `policy_provider_lookup()`, a barred provider resolves to `None` and drops out, so diversity degrades instead of breaking. Remove Gemini from `tool_providers:` and it comes back. The same tuples do **not** contain `opencode`, so a `#cross-provider` brainstorm cannot reach opencode no matter what the policy says.

### Provider status in detail

| Provider | In fallback chain | Reachable by tag | Notes |
|---|---|---|---|
| `claude` | yes (first) | `#claude`, `#claude_*` | Only provider that honours `#effort:` via the shared `_forced_effort` mapping table (opencode below reads the same property, but as a raw, unmapped pass-through) |
| `codex` | yes (second) | `#codex`, `#codex_*` | Default external second opinion |
| `gemini` | **no** (left `_PRIORITY` 2026-08-15) | tag parses, then barred by the shipped `tool_providers:` | Code retained; re-enabling needs `_PRIORITY` **and** policy **and** a key/CLI |
| `openrouter` | never (by design) | `#openrouter`, `#or_*` | Pay-per-token; barred by the shipped policy, and fail-**closed** when `policy.yaml` is missing |
| `vibe` | never (by design) | `#vibe`, `#vibe_*`, `#second_opinion:vibe*` | Reviewer-only; same fail-closed treatment as OpenRouter |
| `opencode` | never (by design; Stufe 3 `_PRIORITY` membership is explicitly not built) | `#opencode`, `#opencode_*` | Pay-per-token but **capped** (own OpenRouter key, live-polled $/day limit) — fail-**open** on a missing `policy.yaml`, unlike OpenRouter/Vibe. Unregistered tag parks the task (`_NO_FALLBACK_PROVIDERS`), same as Vibe but for tag-intent, not blast-radius, reasons. No `tool_providers:` entry names it, so it stays unreachable for `#tool:` tasks; reachable for plain tagged tasks only because `default:` lists it (added 2026-09-04 — without that entry the tag was a terminal `provider_not_allowed`) |

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
- Non-destructive git rollback point before each task, written to
  `refs/orchestrator-backup/<timestamp>` (not to your `git stash` list) and capped
  by age/count. Restore: `git stash apply refs/orchestrator-backup/<timestamp>`
- **The orchestrator writes git state itself**, on by default: after a successful task
  it commits that run's paths to `orch/<id>-<date>` and restores them in the working
  tree. HEAD is never moved, nothing is ever pushed, and a path is only touched when
  the index agrees with HEAD *and* the path was clean when the run started. Switch it
  off with `GIT_AUTO_COMMIT=false`, or per queue line with `#no-commit`. See
  [Per-task auto-commit](#per-task-auto-commit-orchid-date)

## Prompt Budget (Token Allocation)

| Component | Budget | Source |
|---|---|---|
| Core (task + safety) | uncapped | `SOUL.md`, falling back to `SYSTEM_PROMPTS` (`config.py:402`) — `get_system_prompt` (`config.py:1091`), called without truncation in `orchestrator.py:1089` |
| Curated Memory (L1) | ~500 tokens | `MEMORY.md` |
| Daily Log (L2) | ~500 tokens | `daily/` |
| TF-IDF Memory (L3) | ~2000 tokens | `memory.py` |
| Lessons (L4) | ~2000 **chars** | `memory.get_lessons_context` (`memory.py:873`); `#tool:` path only (`tools/base_tool.py:542`), not `_build_prompt` |
| Wikilink context | ~1500 tokens | `queue_manager.py` |
| Skill prompt | ~2000 tokens | `SKILL.md` body (only with `#tool:`) |
| **Sum of the caps above** | **6 500 tokens** | the five `PROMPT_*_TOKENS` that have a caller. This is an addition, **not** an enforced ceiling: Core and the task text are uncapped, and the five do not all apply on every path (Skill only with `#tool:`). Nothing checks the total. |

## Doctor (`--doctor`)

`python orchestrator.py --doctor` runs 19 checks:

- Provider CLIs (`claude`, `gemini`, `codex`; `vibe` reported as WARN when absent — it is optional)
- **opencode CLI** — WARN-only, mirrors `check_vibe_cli()`'s optional posture but checks six things at once: `opencode.exe` resolvable past the npm shim, both `extern-review`/`extern-dev` agents configured, every `OPENCODE_MODEL_ALIASES` entry present in `opencode.json` with `data_collection:"deny"`+`zdr:true`, top-level `small_model` pointing at an `openrouter/` alias (unset lets the small-model path bypass both the $ cap and ZDR — measured), `OPENCODE_DEFAULT_MODEL` present in `opencode.json` (an identifier opencode cannot resolve produces no error but a **stall in initialisation** — measured twice at 120 s and 150 s with no output and no exit, against 8 s for a valid alias), and the OpenRouter key behind opencode carrying a spending cap at all (`AllLimits.opencode` is permanently `available=False` without one). Reads `opencode.json`, never writes it.
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
- Node.js runtime (`check_node()` — `cclimits` and the Claude CLI are npm-installed, so a missing Node is the root cause behind several other checks failing at once)
- **Model IDs** — CLI-pings every entry in `CLAUDE/GEMINI/CODEX_MODEL_ALIASES` concurrently (~5–10 s). FAIL on rejected IDs (unknown/deprecated), WARN on transient probe errors, PASS when every alias responds live. The pay-per-token aliases (`or_*`, `vibe_*`, `opencode_*`) are deliberately **not** probed — a probe would cost money on every `--doctor` run. The deeper LLM-based "are there newer IDs?" heuristic runs only in the monthly heartbeat `model-check`, not here.

With `--fix` (optionally `--yes`) simple problems are auto-created/repaired.

## Architecture

```text
orchestrator.py
  → dispatcher.py          (provider selection + fallback)
      → providers/         claude, codex                        ← fallback chain
                           gemini (HTTP or CLI)                 ← retired 2026-08-15, code retained
                           openrouter, vibe, opencode           ← opt-in only, tag-gated
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
  → taxonomy.py            (22-category failure classification over runs.jsonl)
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
# Run all tests (2844 tests, ~90-180 s) — fixed order, see below
python -m pytest tests/ -q -p no:randomly

# Run a single test file
python -m pytest tests/test_parallel_runner.py -v
```

`-p no:randomly` keeps the order reproducible; it is no longer load-bearing. Until 2026-09-25 `tests/test_telegram_listener.py` went red under some `pytest-randomly` orderings because `tests/test_shutdown.py` leaked a set `shutdown_pending` Event (fixed there, details under [Known Limitations](#known-limitations)). Linux, Cloud, 2026-09-25: **2837 passed / 7 skipped in 66–68 s** under eight random seeds and in the fixed order (CPython 3.12.3). Last measured green run on the development machine: **2811 passed / 0 failed in 120 s** (2026-09-16, +10 aus dem externen Review-Nachzug — P2-1/P2-2/P3-2/P3-5 fixes plus their proof tests, oc r1 — on top of that day's 2801 from both Betriebsprüfung branches merged; 2764 on the verify/test-isolation branch alone the same day, 2711 in 148 s on 2026-09-11, 2533 in 93 s on 2026-09-09, 123 s at 2447 tests on 2026-09-05, 99 s at 2098 on 2026-08-15 — so treat 90-155 s as the band; the spread is machine load, plus the ~35 tests that drive real git subprocesses in `tmp_path` since the per-task auto-commit landed, which are seconds rather than milliseconds by design).

**The suite must never write into the real vault or this repo's real `docs/`.** Three layers of isolation in `tests/conftest.py`: (1) `ORCH_VAULT_PATH` redirects `config.VAULT_PATH` at import time to a scratch directory, before any derived constant is computed; `_find_real_vault_path()` separately preserves the real vault path in `_ORCH_TEST_REAL_VAULT_PATH` for the guard. (2) `_guard_real_vault_and_docs_untouched` (session-scoped) verifies file counts and content hashes of `lessons.md`/`MEMORY.md` at start and end. (3) `memory._refuse_real_vault_writes_under_pytest()` is called at every write/move/delete operation (9 locations) and refuses any target path inside the real vault while `PYTEST_CURRENT_TEST` is set — a distributed check independent of any single fixture, closing the class of bug (missing writer) rather than patching instances. Before the fix (measured 2026-09-16): 63 stray entries in vault daily logs and 4491 stray files in `docs/` from two tests running `orchestrator.run_once()` and two tools defaulting `cwd=None` to `Path(".")`. **The guard stays hard but has a rare, known false-positive source (P2-2, oc r1):** if a live `run_orchestrator.ps1 --watch` process is running against the same real vault while the suite executes, a task it completes inside the ~2-minute session window writes/moves a real file the same way a leaking test would, and the guard fails the run — the failure message lists the concretely new/missing/hash-changed paths with mtime and names both candidate causes; re-run the suite with the live orchestrator idle to tell them apart.

## Contributing

PRs welcome. Run `python -m pytest tests/ -q` before submitting. All tests must pass.

## License

MIT — see [LICENSE](LICENSE).
