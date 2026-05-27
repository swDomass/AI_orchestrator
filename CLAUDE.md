# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

> **Detail-Docs**: Implementierungs-Details pro Modul → [`docs/architecture/components.md`](docs/architecture/components.md). Patterns/Invariants + Phase B Feature Flag → [`docs/architecture/patterns.md`](docs/architecture/patterns.md). Bei Code-Änderungen beide Files (diese Datei + zugehörige Detail-Datei) aktualisieren.

## Obsidian-Projektdoku

`<your-vault>/01_Tasks/01_Projekte/.../AI-System-Intelligence-Orchestrator.md`

## Project

Autonomous task orchestrator routing work across Claude Code, Gemini CLI, and Codex CLI. Tasks come from an Obsidian vault Markdown queue (`99_System/AI/agent-queue.md`). Pure Python stdlib + pyyaml, Windows-first.

## Commands

```bash
# Run all tests (~1463 tests, ~50 s)
python -m pytest tests/ -q

# Run a single test file / single test
python -m pytest tests/test_parallel_runner.py -v
python -m pytest tests/test_queue_manager_regressions.py::test_extract_cwd_supports_spaces -v

# Install dependencies
pip install -r requirements.txt

# Validate setup
python orchestrator.py --doctor
python orchestrator.py --doctor --fix --yes

# Modes
python orchestrator.py                # single-shot
python orchestrator.py --watch        # continuous + heartbeat
python orchestrator.py --dry-run      # parse queue, no execute
python orchestrator.py --check-limits # provider capacity
python orchestrator.py --list-tools   # available #tool: handlers
python orchestrator.py --dashboard    # analytics web dashboard
python orchestrator.py --lint-queue   # validate agent-queue.md
```

## Architecture

**Execution flow**: Queue read → provider selection (fallback chain) → profile loading → policy check → skill gating → memory context injection → prompt building → provider execution → result persistence → heartbeat.

**Module map** (details in [`docs/architecture/components.md`](docs/architecture/components.md)):

### Core orchestration
- **`orchestrator.py`** — Main loop (`run_once`/`run_watch`), prompt building, heartbeat thread, blocked-task handling
- **`dispatcher.py`** — Provider selection (Claude → Gemini → Codex fallback), cooldowns, model-alias routing
- **`queue_manager.py`** — Obsidian MD queue parser, sidecar `.lock`, regex metadata, `#needs:`/`#id:` two-pass deps, subtask-aware mutations; P1 `#worktree`/`#keep-worktree` tags for parallel isolation
- **`policy.py`** — `PolicyEngine` singleton, AUTO/APPROVE/DENY classification, Telegram-blocking; P3 `ToolContract` API (`get_tool_contract(name)`) + `tool_contracts:` yaml section (per-tool budget/stop_conditions/reporting_path, doctor schema-validates)
- **`usage_suggester.py`** — `UsageSuggester` singleton, proactive task suggestions on low capacity
- **`memory.py`** — TF-IDF + temporal decay over past results, CWD-filtered lessons, daily log (80-char summaries)
- **`heartbeat.py`** — Scheduled health checks (12 handlers, mtime-reloaded), Phase-A CLI-probe + Phase-B LLM heuristic for model drift; P6 `status-recap` (daily 24h Telegram summary), P4 `check-ci-failures` (gh poll → dev-loop queue items)
- **`limits.py`** — `cclimits` wrapper, OAuth refresh, 3-tier 429 fallback, polling 10min idle / 5min active (idle matches cclimits `--cache-ttl=600`); Phase-0 calibration hook in `_bg_refresh_loop`
- **`quota_calibration.py`** — Phase-0 telemetry: pairs cclimits utilization-% with locally-aggregated JSONL token counts per Claude window (5h/7d), CSV schema v2, async single-worker write (never blocks refresh loop). Calibration result (2026-05-27): `io_only` model both windows, cache-tokens negligible to quota; 1h/5m tier-split refuted. One-time analysis: `quota_calibration_backfill.py`
- **`analytics.py`** — TaskRecord/LimitSnapshot/QueueEvent/ToolTraceEvent dataclasses, billing-cost units, cache-hit rate, tool-trace stats
- **`dashboard.py`** — Standalone HTTP server (port 8411), Chart.js dashboard, 60s refresh; Active-Runs-Panel (Live-View laufender Tool-Runs, 30s refresh über `/api/data?only=active_runs`)
- **`config.py`** — Centralized constants (~70+), `.env` loader, model aliases (Claude/Gemini/Codex/OpenRouter), safety patterns, timeouts
- **`logging_setup.py`** — Rotating file logger (5MB × 3) + console
- **`doctor.py`** — 16+ setup validation checks, `--fix`/`--yes` auto-repair, concurrent alias probes
- **`shutdown.py`** — Shutdown state machine + cancellation
- **`notifier.py`** — Telegram notifications, 3500-char truncation
- **`telegram_listener.py`** — Bot listener, `/chat` AI mode, slash tool-commands (`/review` `/security` `/audit` `/dev` `/critique` `/brainstorm`), `/approve` SI-Manager routing; P5 `/pr-fix <owner/repo#N>` + `/pr-ignore <owner/repo#N>` for PR-Babysitter report-only mode
- **`queue_linter.py`** — Offline validator (`--lint-queue`), exit codes 0/1/2
- **`idempotency.py`** — Duplicate-trigger dedup (JSONL store, sha256 keys, 30-day retention)
- **`session_registry.py`** — Append-only JSONL whitelist of orchestrator-created Claude session UUIDs
- **`replay.py`** — Machine-readable run summaries (`logs/runs.jsonl`), one record per task end (ok/retry/error/blocked), 30-day rotation → gzip archive
- **`taxonomy.py`** — Classifies replay records into 16 failure categories (rate_limit, timeout, auth_error, model_refusal, etc.) for analytics + retry logic
- **`preflight.py`** — Per-tool deterministic context collectors (git status, manifests, file histograms), 5s timeout, day-cached at `{cwd}/.<tool>/preflight-{date}.md`
- **`queue_healing.py`** — Detects long-blocked tasks (>24h, failed deps, dead ends), proposes `/unblock`/`/drop`/`/retry` via Telegram, 24h notify cooldown
- **`skill_suggester.py`** — Pattern-gated draft generator: N>=3 occurrences of (tool, cwd, task-shape) in 30 days → SKILL.md draft to `Skills-Drafts/`, 90-day cooldown, never auto-activates
- **`skills/index.py`** — Progressive skill loading: always-present INDEX block + lazy section extraction by phase name
- **`gh_helpers.py`** — Thin subprocess wrapper around `gh` CLI (typed errors: `gh_not_found`/`gh_auth`/`gh_timeout`/`gh_not_found_repo`); shared by PR-Babysitter (P2/P5) and CI-Watcher (P4)
- **`ci_watcher.py`** — P4 CI-Failure-Watcher: `sweep_once()` lists failed GitHub-Action runs per repo, dedups by `(repo, headSha)`, queues `#tool:dev-loop` task per new failure, persistent state in `logs/ci-watcher-state.json`
- **`parallel_runner.py`** — P1 worktree-isolation helpers (`_is_clean_git_repo`, `_create_worktree`, `_remove_worktree`) — one worktree per CWD group, retained on failure, removed on success unless `#keep-worktree`
- **`run_orchestrator.ps1`** — Crash-resistant watchdog (PS 5.1+7+), exponential backoff, Telegram alerts

### Providers (`providers/`)
- **`base.py`** — `BaseProvider` ABC, per-provider `_lock`, `threading.local()` for forced model, `RunResult` with 4 token fields, `supports_sessions` capability flag
- **`claude.py`** — `--output-format json`, `--exclude-dynamic-system-prompt-sections` for cache stability, session-id/resume support, `supports_sessions=True`
- **`gemini.py`** — `--yolo` full tool access, `--approval-mode default` read-only, `--model <id>` forced
- **`codex.py`** — `codex exec --full-auto` or sandbox, `--model <id>` forced
- **`openrouter.py`** — HTTP/urllib (no `requests` dep), pay-per-token, NEVER in fallback chain, opt-in via `#openrouter`/`#or_*` tags, conditional registration on `OPENROUTER_API_KEY`

### Tools (`tools/`)
- **`base_tool.py`** — `BaseTool` ABC + 4-layer prompt assembly (stability-ordered for cache hits), `ToolResult`, `TokenCounter`, `SessionContext`, `ToolTracer`, `ActiveRunRegistry` (zentraler `logs/active_runs/<run_id>.json` Index für Dashboard-Live-View, atomic writes, stale >6h, cleanup >24h)
- **`research_qa.py`** — 3-phase read-only pre-implementation (Discovery → Analysis → Questions), `#tool:research-qa`
- **`review_loop.py`** — Iterative review fixing all P1/P2/P3 (max 20 iter), stable/volatile prompt split, optional `#second_opinion:<alias>`, Drift-Check (`auto`/`always`/`skip` via `policy.yaml` `tool_phases.review-loop.drift_check_mode`) injiziert Refocus-Warning in den Fix-Prompt bei Goal-Adherence-Verletzung, `#tool:review-loop`
- **`dev_loop.py`** — Research+Plan (merged call) → Execute → Quality+Resolution Review loop (max 20 iter), `#tool:dev-loop`
- **`critical_review.py`** — 3-pass adversarial (Pass 1 + Pass 2 + Pass 3 Synthesis), cross-provider via `#pass1:`/`#pass2:`, `{plan}-v2.md` output, `#tool:critical-review`
- **`security_audit.py`** — 2-phase audit + fix (Phase 1 read-only scan, Phase 2 write+pytest), `#tool:security-audit`
- **`scientific_investigation.py`** + phase modules (`scientific_investigation_phases.py`, `_phase2/4/5/7/8.py`, `_approvals.py`, `crosschecks/`, `personas/`) — Wissenschaftlicher Autopilot (Plan v5), Phase 0 → 0.5 → 1 → 2 → 3 → 4 → 5a/b → 7 → 8 → 6 pipeline, Status-Tuple max MEDIUM (HIGH structurally excluded), `#tool:scientific-investigation`
- **`brainstorm.py`** + `brainstorm_phases.py` — Multi-persona round-table (4-6 dynamic personas, Phase 0 → 0.5 → 1 → 2 → K-Konvergenz → 3 Synthese), opt-in `#cross-provider`, `#tool:brainstorm`
- **`deep_security_audit.py`** — Multi-agent (6 personas + CISO synthesis + optional fix), Phase C1 sub-agent vs. sequential, Phase 6.5 round-table opt-in via `#roundtable`, `#tool:deep-security-audit`
- **`pr_babysitter.py`** — P2/P5 PR-Babysitter: polls open PRs via `gh`, dedups by `(repo, comment_count, sha, ci_state)`, queues `#tool:dev-loop` (default) or sends Telegram summary with `/pr-fix`+`/pr-ignore` commands (`#pr-mode:report-only`). Tags: `#repos:owner/r1,owner/r2`, `#pr-labels:auto-fix`, `#pr-mode:queue|report-only`. State in `{cwd}/.pr-babysitter/state.json`, 1h cooldown. `#tool:pr-babysitter`

### Scripts
- **`scripts/safety_hook.py`** — Claude Code `PreToolUse` hook, hard-deny via `SAFETY_DENY_PATTERNS`
- **`scripts/build_audit_pack.py`** — Scientific-investigation audit pack builder (zip + meta JSON)

## Phase B Feature Flag — `CLAUDE_SESSION_ENABLED`

Default **OFF**. Opt-in via `.env`. When enabled, Claude tools share CLI session UUIDs across phases (`--session-id`/`--resume`) for ~30-50 % token savings. Rollback by setting `CLAUDE_SESSION_ENABLED=false` and restart. Details + retention policy → [`docs/architecture/patterns.md#phase-b-feature-flag--claude_session_enabled`](docs/architecture/patterns.md#phase-b-feature-flag--claude_session_enabled).

## Key Patterns

Stichworte — Long-form in [`docs/architecture/patterns.md`](docs/architecture/patterns.md):

- **Singletons with threading** — `PolicyEngine`, `UsageSuggester`, providers; own `_lock` + `threading.Event`
- **Provider-bound model tags** — `config.model_id_for_provider(tag, provider)` returns `None` on mismatch; `_forced_model` via `threading.local()`
- **OpenRouter never in fallback chain** — `dispatcher._PRIORITY` omits openrouter; `.get(name)` not `[name]` for silent fallback
- **Mtime-cached config** — policy, profiles, SOUL.md, heartbeat use `(mtime, content)` tuples
- **Token-budget injection** — `_build_prompt()` truncates to `PROMPT_*_TOKENS`
- **Sidecar file locking** — `queue_manager.py` `.lock` file (msvcrt/fcntl)
- **Subtask-aware queue mutations** — `mark_done/mark_retry/finalize` accept `subtasks` kwarg
- **Task dependencies** — `#id:`/`#needs:`, two-pass resolution, blocked-task header
- **Schedule tags** — `#at:`/`#every:` reuse retry primitive; queue file is single source of truth
- **Worktree isolation (P1)** — `#worktree` on parent → one `git worktree add --detach` per CWD group; subtask cwd rewritten; failed groups retain the worktree (path appended to error)
- **Tool Contracts (P3)** — `tool_contracts:` section in `policy.yaml`; `PolicyEngine.get_tool_contract(name)` returns dataclass with `max_iterations`/`max_runtime_sec`/`stop_conditions`/`reporting_path`; tools may fall back to `config.TOOL_*_*` constants for fields the contract omits (staged migration)
- **`.env` comment stripping** — whitespace-before-`#` rule protects URLs/paths
- **HTTP 429 resilience** — 3-tier fallback (claude-monitor JSONL → snapshot cache → optimistic), 5-min polling backoff
- **3-tier token estimation** — actual JSON → text-char estimate → duration heuristic

## Testing Conventions

- `unittest.mock.patch` + `pytest` fixtures (`tmp_path`, `monkeypatch`)
- Mock `config._load_dotenv` when importing modules with config side-effects
- All tests synchronous (no async)
- Test files mirror source: `tests/test_<module>.py`

## Safety Rules (enforced in code)

- **Hard deny**: `scripts/safety_hook.py` (Claude Code `PreToolUse` hook, works even with `--dangerously-skip-permissions`)
- **Soft deny**: `SAFETY_RULES` injected into Gemini/Codex prompts (no hook system)
- **Blocked categories**: `rm -rf`, `git push --force`, `git reset --hard`, `git clean -f`, `DROP/TRUNCATE TABLE`, `DELETE FROM` without WHERE, `format`/`mkfs`/`diskpart`, fork bombs, raw disk writes, credential exfiltration, Windows `del /s`/`rd /s /q`/`Remove-Item -Recurse -Force`
- CWD validation against `ALLOWED_CWD_ROOTS`
- Skill gating (bins, env vars, OS, provider) before execution
- Policy layer can block tasks pending Telegram approval

Full pattern catalog → [`docs/architecture/patterns.md#safety-rules-enforced-in-code--details`](docs/architecture/patterns.md#safety-rules-enforced-in-code--details).
