# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

> **Detail-Docs**: Implementierungs-Details pro Modul → [`docs/architecture/components.md`](docs/architecture/components.md). Patterns/Invariants + Phase B Feature Flag → [`docs/architecture/patterns.md`](docs/architecture/patterns.md). Bei Code-Änderungen beide Files (diese Datei + zugehörige Detail-Datei) aktualisieren.

## Obsidian-Projektdoku

`<your-vault>/01_Tasks/01_Projekte/.../AI-System-Intelligence-Orchestrator.md`

## Project

Autonomous task orchestrator routing work across Claude Code, Gemini (HTTP API or legacy CLI), and Codex CLI, plus two opt-in providers that never enter the fallback chain: OpenRouter (HTTP) and Mistral Vibe (reviewer-only). Tasks come from an Obsidian vault Markdown queue (`99_System/AI/agent-queue.md`). Pure Python stdlib + pyyaml, Windows-first.

## Commands

```bash
# Run all tests (~1723 tests, ~90 s)
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
- **`dispatcher.py`** — Provider selection (Claude → Gemini → Codex fallback), cooldowns, model-alias routing; `_REVIEWER_ONLY` guard so an unregistered reviewer tag parks the task instead of degrading to an executor
- **`queue_manager.py`** — Obsidian MD queue parser, sidecar `.lock`, regex metadata, `#needs:`/`#id:` two-pass deps, subtask-aware mutations; P1 `#worktree`/`#keep-worktree` tags for parallel isolation. **Known gap:** `MODEL_TAG_RE` hard-codes 6 of the 20 model aliases — the other 14 route to the right provider but never force the model (details + fix vector in `components.md`)
- **`policy.py`** — `PolicyEngine` singleton, AUTO/APPROVE/DENY classification, Telegram-blocking; P3 `ToolContract` API (`get_tool_contract(name)`) + `tool_contracts:` yaml section (per-tool budget/stop_conditions/reporting_path, doctor schema-validates)
- **`profiles.py`** — `#agent:<name>` YAML profiles (provider order, allowed roots, skill allow/deny, timeout, policy overrides); vault first, repo-local second
- **`usage_suggester.py`** — `UsageSuggester` singleton, proactive task suggestions on low capacity
- **`usage_budget.py`** — Pace factor for rolling windows (7d) + CLI/Telegram formatting; consumed by heartbeat, orchestrator, usage_suggester
- **`memory.py`** — TF-IDF + temporal decay over past results, CWD-filtered lessons, daily log (80-char summaries)
- **`heartbeat.py`** — Scheduled health checks (14 handlers, mtime-reloaded), Phase-A CLI-probe + Phase-B LLM heuristic for model drift; P6 `status-recap` (daily 24h Telegram summary), P4 `check-ci-failures` (gh poll → dev-loop queue items)
- **`limits.py`** — `cclimits` wrapper, OAuth refresh, 3-tier 429 fallback, polling 10min idle / 5min active (idle matches cclimits `--cache-ttl=600`); Phase-0 calibration hook in `_bg_refresh_loop`
- **`quota_calibration.py`** — Phase-0 telemetry: pairs cclimits utilization-% with locally-aggregated JSONL token counts per Claude window (5h/7d), CSV schema v2, async single-worker write (never blocks refresh loop). Calibration result (2026-05-27): `io_only` model both windows, cache-tokens negligible to quota; 1h/5m tier-split refuted. One-time analysis: `quota_calibration_backfill.py`
- **`quota_state.py`** — Phase-1 SoTH: `_bg_refresh_loop` writes `logs/cc_quota_state.json` (atomic, per-window 5h/7d snapshot + embedded calibration constants) each poll; external read-only consumers (statusline, `--check-limits`) read via `read_quota_state` instead of re-polling cclimits. 429-fallback per-window split for Claude uses calibrated `ESTIMATE_TOKENS_PER_PCT_CLAUDE_WINDOWS` (5h≈5400/7d≈75000) via `limits._estimate_window_usage_calibrated` (scalar cancels); uncalibrated providers keep the reset-time heuristic. **Phase-2 (flag `ORCH_QUOTA_LIVE_ESTIMATE`, default OFF):** `get_limits()` decrements the cached snapshot by the live between-poll estimate (`_apply_live_estimate`, accumulated in `report_estimated_usage`, re-anchored each poll via `_reset_live_estimate`); optional daily auto-recalibration of the factors from the running CSV (`ORCH_QUOTA_AUTO_RECALIBRATE` → `quota_calibration.recalibrate_claude_factors` → `limits.set_calibrated_windows`, min-samples + clamp guarded)
- **`analytics.py`** — TaskRecord/LimitSnapshot/QueueEvent/ToolTraceEvent dataclasses, billing-cost units, cache-hit rate, tool-trace stats
- **`dashboard.py`** — Standalone HTTP server (port 8211, `DASHBOARD_PORT`-overridable, auto-fallback to a free port if taken/Windows-reserved), Chart.js dashboard, 60s refresh; Active-Runs-Panel (Live-View laufender Tool-Runs, 30s refresh über `/api/data?only=active_runs`)
- **`config.py`** — Centralized constants (~70+), `.env` loader, model aliases (Claude/Gemini/Codex/OpenRouter/Vibe — 20 total), safety patterns, timeouts
- **`logging_setup.py`** — Rotating file logger (5MB × 3) + console
- **`doctor.py`** — 18 setup validation checks, `--fix`/`--yes` auto-repair, concurrent alias probes (subscription CLIs only — pay-per-token providers are deliberately not probed)
- **`shutdown.py`** — Shutdown state machine + cancellation
- **`notifier.py`** — Telegram notifications, 3500-char truncation
- **`telegram_listener.py`** — Bot listener, `/chat` AI mode, slash tool-commands (`/review` `/security` `/audit` `/dev` `/critique` `/brainstorm`), `/approve` SI-Manager routing; P5 `/pr-fix <owner/repo#N>` + `/pr-ignore <owner/repo#N>` for PR-Babysitter report-only mode
- **`queue_linter.py`** — Offline validator (`--lint-queue`), exit codes 0/1/2; shares `extract_model_tag` with the queue parser, so it inherits the `MODEL_TAG_RE` gap and does not yet validate `#vibe*` tags
- **`idempotency.py`** — Duplicate-trigger dedup (JSONL store, sha256 keys, 30-day retention)
- **`session_registry.py`** — Append-only JSONL whitelist of orchestrator-created Claude session UUIDs
- **`replay.py`** — Machine-readable run summaries (`logs/runs.jsonl`), one record per task end (ok/retry/error/blocked), 30-day rotation → gzip archive
- **`taxonomy.py`** — Classifies replay records into 19 failure categories (rate_limit, timeout, auth_error, model_refusal, stdin_incomplete, etc.) for analytics + retry logic
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
- **`process_runner.py`** — Liveness-/Hang-Watchdog: `Popen` + 2 daemon-Reader-Threads (stdout/stderr gleichzeitig, sonst Pipe-Buffer-Deadlock), `idle_timeout` (Hang) + `hard_timeout` (Backstop), Tree-Kill (Windows `taskkill /F /T` doppelt, POSIX `killpg` SIGTERM→SIGKILL). Claude tool-aware (`liveness_lines=True`: laufender `tool_use` pausiert idle-Timer via `_Liveness`), Gemini/Codex byte-only. `run_with_watchdog` ersetzt `subprocess.run` in allen 3 Providern (DRY). Raise `TimeoutExpired` mit `timeout_kind` ("idle"|"hard"). **stdin-Zustellung wird verifiziert** (`_StdinDelivery`, fail-closed): gepufferter `TextIOWrapper` → ein fehlgeschlagener Tail-Flush schneidet das Prompt-ENDE ab, wo `_build_prompt` den Task-Text platziert → CLI antwortet auf den Kontext-Rest, exit 0, sah früher wie Erfolg aus (5× beobachtet, 3 Tage Daten-Lücke). Fehler → `WatchdogResult.stdin_error` → Provider liefern bare code `stdin_incomplete`, aber nur an zwei Stellen: **im Erfolgszweig** und **als letzter Fallback bei `rc == 0`**. Nie vorab — ein früh sterbendes Kind zerreißt die Pipe ebenfalls, ein Vorab-Check würde also `rate_limit`/`session_missing` maskieren; umgekehrt würde der Fallback bei `rc != 0` gewöhnliche CLI-Abstürze einsammeln. Bei `rc != 0` gewinnt der echte Fehler, die Diagnose wird an `output` angehängt. Im Orchestrator **bewusst nicht sonderbehandelt** (generischer Pfad = Cooldown + Rotation, bounded); nur in der `_run_with_retry`-Abbruchliste
- **`claude.py`** — `--output-format stream-json --verbose` (NDJSON-Liveness-Events), NDJSON-Parser `_extract_result_event` (letztes `type==result`), Token-Feld-Parity erhalten, rate_limit/session-Keyword-Detektion auf ROH-stdout, `--exclude-dynamic-system-prompt-sections` for cache stability, session-id/resume support, `supports_sessions=True`; idle-Kill → `error="hang"`, hard → `error="timeout"`
- **`gemini.py`** — **dual-mode** (consumer Gemini CLI shut down 2026-06-18). `GEMINI_API_KEY` set → HTTP REST API via `urllib` (`generateContent`, stdlib, like `openrouter.py`); real tokens from `usageMetadata` (thinking tokens folded into output), 429→`rate_limit`+cooldown, finishReason/promptFeedback → `model_refusal`. No key → legacy `gemini` CLI (`--yolo`/`--approval-mode default`/`--model`, byte-only Liveness) for Standard/Enterprise users. Stays in the fallback chain either way; HTTP mode availability is cooldown-driven (`dispatcher._limits_ok` + `limits` bypass cclimits when key set)
- **`codex.py`** — `codex exec` + `-c approval_policy=never` + `--sandbox read-only|workspace-write` (CLI ≥0.130; `--full-auto` deprecated) + `--skip-git-repo-check`, `--model <id>` forced; byte-only Liveness (`CLI_IDLE_TIMEOUT_NO_LIVENESS_SEC`)
- **`openrouter.py`** — HTTP/urllib (no `requests` dep), pay-per-token, NEVER in fallback chain, opt-in via `#openrouter`/`#or_*` tags, conditional registration on `OPENROUTER_API_KEY`
- **`vibe.py`** — Mistral Vibe CLI, zweite Nicht-Claude-Stimme neben Codex. Pay-per-token, NEVER in fallback chain, opt-in via `#vibe`/`#vibe_*` oder `#second_opinion:vibe`; Registrierung nur wenn das `vibe`-Binary auf dem PATH liegt. **Reviewer, kein Executor:** `read_only` → `--disabled-tools "*"`, sonst nur `read_file`+`grep` — nie `bash`/`edit`/`write_file`. `-p` **ohne Wert** = Prompt über stdin (als argv würde ein 100-KB-Prompt die Windows-Kommandozeile sprengen); `--trust` sonst Hänger am Trust-Prompt; **kein `--model`-Flag** — Modellwahl über `VIBE_ACTIVE_MODEL` (vibe-*Alias*, unbekannter Wert fällt still zurück); `PYTHONUTF8=1` im Child gegen den cp1252-`charmap`-Crash. Liefert **keine** Token-Zahlen → `RunResult` bleibt bei 0, die Schätzung im Orchestrator greift (wie Codex)

### Tools (`tools/`)
- **`base_tool.py`** — `BaseTool` ABC + 4-layer prompt assembly (stability-ordered for cache hits), `ToolResult`, `TokenCounter`, `SessionContext`, `ToolTracer`, `ActiveRunRegistry` (zentraler `logs/active_runs/<run_id>.json` Index für Dashboard-Live-View, atomic writes, stale >6h, cleanup >24h)
- **`registry.py`** — `#tool:<name>` → handler table (`--list-tools`)
- **`research_qa.py`** — 3-phase read-only pre-implementation (Discovery → Analysis → Questions), `#tool:research-qa`
- **`test_loop.py`** — Run tests → fix failures → re-run until green, `#tool:test-loop`
- **`knowledge_transfer.py`** — Vault expertise → cross-domain applications (web search) → Obsidian idea note, `#tool:knowledge-transfer`
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
- **OpenRouter/Vibe never in fallback chain** — `dispatcher._PRIORITY` omits beide; `.get(name)` not `[name]` for silent fallback. Registrierung ist bedingt (API-Key bzw. Binary auf dem PATH)
- **Reviewer-only degradiert nicht zum Executor** — `dispatcher._REVIEWER_ONLY`: ein `#vibe`-Tag ohne registriertes Vibe liefert **keinen** Provider (Task wird geparkt), statt still auf Claude/Gemini/Codex durchzufallen. Bei OpenRouter ist derselbe Fallback harmlos (Executor → Executor), hier wäre er eine Ausweitung des Blast Radius: erbeten war eine nicht-schreibende Zweitmeinung
- **Child-Env statt `os.environ`** — `run_with_watchdog(..., env=…)` bekommt eine fertig gemergte Kopie (Popen ersetzt, merged nicht). Provider sind geteilte Singletons in Parallel-Threads: `os.environ` mutieren würde das Modell eines Runs in einen anderen lecken
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
- **Liveness-Watchdog statt Wall-Clock-Deadline** (`providers/process_runner.py`) — Popen + 2 Reader-Threads + Tree-Kill; idle_timeout (Hang) vs hard_timeout (Backstop). Claude tool-aware (laufender `tool_use` pausiert idle-Timer wegen stiller Tool-Phasen), Gemini/Codex byte-only mit konservativem idle. `#timeout:`/`timeout_minutes` = hard-Backstop + oberer Tool-Cap (nicht mehr aggressive Deadline). idle-Kill → `error="hang"` → Requeue mit Backoff bis `MAX_HANG_RETRIES`, dann Block (kein Quota-Reset-Retry)
- **Tool-Gesamt-Deadline** — `ToolContract.max_runtime_sec` (Fallback `TOOL_DEFAULT_MAX_RUNTIME_SEC`) deckelt die SUMME aller Phasen/Iterationen (`BaseTool._runtime_deadline`); ein hochgesetzter Task-`timeout` hebt die Per-Phase-Caps NICHT über die `TOOL_*_TIMEOUT_SEC`-Konstanten (`BaseTool._phase_cap`)

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
