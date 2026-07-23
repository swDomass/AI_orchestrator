# Architecture — Patterns & Invariants

Cross-cutting patterns and feature flags. Linked from [CLAUDE.md](../../CLAUDE.md). Update both when behaviour changes.

---

## Key Patterns

### Singletons with threading

`PolicyEngine`, `UsageSuggester`, providers — each has own `_lock`, `threading.Event` for blocking operations, no global mutex.

### Provider-bound model tags

`#claude_opus`, `#gemini_flash`, `#codex_mini`, `#or_minimax_free`, `#or_glm`, `#vibe_small` etc. are resolved via `config.model_id_for_provider(tag, provider_name)`, which returns `None` if the tag doesn't belong to the target provider.

**Caveat:** the *provider* half of this works for all 20 aliases, the *model* half only for the 6 that `queue_manager.MODEL_TAG_RE` hard-codes — see the known gap under `queue_manager.py` in [components.md](components.md#queue_managerpy). `#second_opinion:<alias>` is unaffected (own regex, own resolution). This prevents a Claude model ID from leaking to Gemini during provider fallback. The `_forced_model` attribute lives on `BaseProvider` (via `threading.local()`), applied to any provider. Orchestrator and `parallel_runner` set it per-task without a claude-only gate. Cross-provider tools (e.g. `critical_review.py` pass2) re-apply the tag against the pass2 provider separately — see `CriticalReviewTool.run()` around `pass2_provider.run(...)`.

### OpenRouter and Vibe never in fallback chain

`dispatcher._PRIORITY` deliberately omits `openrouter` and `vibe` so untagged tasks never accidentally route to a pay-per-token provider. Both are registered conditionally — OpenRouter on `OPENROUTER_API_KEY`, Vibe on the `vibe` binary being on `PATH`. Tag resolution uses `_providers.get(name)` (not `[name]`) so a tag for an unregistered provider silently falls back to claude/gemini/codex instead of raising KeyError.

### Reviewer-only providers must not degrade into executors

`dispatcher._REVIEWER_ONLY = {"vibe"}` carves the one exception out of that silent fall-through. A `#vibe` tag with no Vibe installed yields **no provider at all** (`select_provider() → None`, task parked) rather than the next provider in the chain.

The asymmetry is the point: for an unregistered `#or_*` tag the fall-through is harmless — an executor is replaced by an executor, and the task still gets done. Answering "give me a second opinion without touching the files" with a file-writing executor is not the same task; it widens the blast radius past what was asked for. Parking is recoverable (install the CLI, or retag), a stray write is not. The guard runs only when no provider was explicitly forced, so `force_name` / an explicit `#claude` tag still wins.

### Child env replaces, it does not merge

`run_with_watchdog(..., env=…)` hands the dict straight to `Popen`, whose `env` **replaces** the child environment. Callers therefore pass a fully merged copy of `os.environ` plus their own keys (`providers/vibe.py` → `VIBE_ACTIVE_MODEL`, `PYTHONUTF8`), never a delta.

Building that copy per call rather than mutating `os.environ` is load-bearing, not stylistic: providers are shared singletons and `parallel_runner` drives them from several threads at once, so an in-place mutation would leak one run's forced model into a concurrent run. Same reasoning as `_forced_model` living in `threading.local()`.

### Mtime-cached config

Policy, profiles, SOUL.md, heartbeat all use `(mtime, content)` tuple caching for hot-reload in `--watch` mode.

### Token-budget injection

`_build_prompt()` truncates skill/memory/wikilink context to `PROMPT_*_TOKENS` constants before assembly.

### Sidecar file locking

`queue_manager.py` uses `.lock` file with platform-specific locking for multi-process safety.

### Subtask-aware queue mutations

`mark_done/mark_retry/finalize_task_with_result` accept `subtasks` kwarg; `_replace_open_task_line` uses it to disambiguate duplicate task texts in parallel queues. Fallback re-scan is O(N) — skips subtask block scan for non-matching task lines.

### `task_subtasks` in run_once

Extracted via `getattr(queue_task, "subtasks", None)` at loop start for test-mock compatibility (some tests use bare `SimpleNamespace`).

### Task dependencies (`#id:`/`#needs:`)

`#id:name` tags a task with a unique ID. `#needs:name1,name2` blocks a task until all named deps appear as `[x]` or `[-]` in the file. `_collect_completed_ids()` scans the full file for done/failed tasks. Two-pass in `read_queue_items()` — short-circuit if no `#needs:` present. Blocked tasks keep `QueueTask.blocked_reason != ""` and are skipped by `run_once()` (no `mark_done` → stays in queue for next cycle). Queue header shows `(N ausführbar, M blockiert)` when any tasks are blocked.

### Schedule tags (`#at:`/`#every:`)

Both reuse the existing retry primitive — no new modules, no scheduler tick. `#at:<timestamp>` accepts the same forms `_retry_is_due()` understands (`YYYY-MM-DDTHH:MM`, `YYYY-MM-DD HH:MM`, `HH:MM`); `read_queue_items()` filters the task out until the timestamp is reached, and the tag disappears on first fire via the `[x]` mark. Retry-annotation always wins over `#at:` (it's the active timing signal once a transient retry is set). `#every:<duration>` (units `s|m|h|d`, e.g. `#every:24h`, `#every:7d`) triggers a different completion path: `_completion_replacement()` rewrites the line as open with a fresh `<!-- retry: now+duration -->` annotation instead of `[x]`, and strips any stale `#at:` on the rewrite. Combinable: `#at:2026-05-17T22:00 #every:24h` = first fire at 22:00, then daily. Both `mark_done()` and `finalize_task_with_result()` call `_completion_replacement()`. Missed schedules replay automatically (retry-time in past = task due now). Queue file remains the single source of truth — adding/pausing schedules = editing `agent-queue.md`.

### Worktree isolation for `#parallel` (P1)

Opt-in via `#worktree` on the parent task — without the tag `run_parallel()` keeps the legacy in-place behaviour. When present:

1. Subtasks are still grouped by CWD as before. Each CWD group gets one `git worktree add --detach .worktrees/<parallel-XXXXXXXX> HEAD` under the group's base CWD. The 8-char id is `sha1(parent_task|group_key|idx)[:8]` — short on purpose to stay under Windows' 260-char `MAX_PATH` limit when nested in already-deep CWDs.
2. Pre-flight: the base CWD must be a clean git repo. Dirty trees or non-git dirs short-circuit the whole group with `provider_name="worktree"` errors before any subtask runs (`_is_clean_git_repo` returns `(ok, reason)`).
3. Each subtask in the group has its `cwd` rewritten to the worktree path. The original `cwd:` tag in the task text remains unchanged — only the dataclass field is mutated.
4. Cleanup on success uses `git worktree remove --force <path>`. On failure the worktree is **retained** and its absolute path is appended to every failed subtask's `error` string (`... [worktree retained: <path>]`) so the user can inspect.
5. `#keep-worktree` on the parent skips cleanup even when all subtasks succeed.
6. Doctor check `check_worktrees()` scans `ALLOWED_CWD_ROOTS` for orphaned `.worktrees/parallel-*` dirs and offers `git worktree prune` + `git worktree remove --force` via `--fix`.

`PARALLEL_WORKTREE_ROOT` constant (default `.worktrees`) controls the subdir name. The directory does **not** need to be `.gitignore`d — git itself never tracks it because each entry is a separate worktree, not file content.

### Tool Contracts (P3)

`policy.yaml` gained a `tool_contracts:` section so action budgets, stop conditions, and reporting paths live in one declarative place instead of scattered across `config.TOOL_*_*` constants:

```yaml
tool_contracts:
  dev-loop:
    budget:
      max_iterations: 20
      max_runtime_sec: 7200
    stop_conditions: [reviews_pass, capacity_exhausted]
    reporting_path: telegram+memory
  default:
    budget: {max_iterations: 20, max_runtime_sec: 3600}
    reporting_path: telegram+memory
```

`PolicyEngine.get_tool_contract(name)` resolves: specific entry → `default` entry (with `tool_name` rewritten) → empty `ToolContract` (all-None fields). The resolved contract is **never None** — callers can rely on the dataclass shape and check individual fields for None to decide between contract value vs. their existing `config.TOOL_*` constant.

`PolicyEngine` caches the parsed contracts under the same mtime check as the rest of `policy.yaml`, so hot-reload picks up edits without a restart. Doctor's `check_policy_file()` calls `_validate_tool_contracts()` and surfaces typos (`buget`, unknown budget keys, non-positive ints, etc.) as WARN-level Doctor output without failing the check — config typos shouldn't block the orchestrator from starting.

**Staged migration**: tools have not been migrated to read contracts yet. The API and schema are stable; switching a tool from `TOOL_MAX_ITERATIONS` to `policy.get_engine().get_tool_contract(self.name).max_iterations or TOOL_MAX_ITERATIONS` is a per-tool follow-up. Keep the fallback so the constant remains the authoritative default.

### `.env` comment stripping

`_normalize_dotenv_value()` requires whitespace before `#` for unquoted values (protects URLs/paths containing `#`); quoted values allow `#` anywhere after the closing quote.

### Process liveness watchdog

`providers/process_runner.py` `run_with_watchdog` replaces `subprocess.run` in all three CLI providers. Root cause it fixes: `subprocess.run(..., timeout=)` only kills the direct child; on Windows with `shell=True` the chain is `cmd.exe → python/node → grandchild`, the grandchild is orphaned, keeps the stdout pipes open, and `communicate()` blocks until the orphan finishes on its own (observed: nominal kill 900 s, return 1094 s) — so the old timeout was both an ineffective hang guard AND a harmful deadline (a slow-but-successful run got discarded as `error="timeout"`).

Design: `Popen` + TWO simultaneous reader threads (stdout + stderr — a single reader deadlocks on a full OS pipe buffer of the other stream) + stdin feeder, all daemon. The stdin feeder **verifies delivery** (`_StdinDelivery`, fail-closed): stdin is a buffered `TextIOWrapper`, so a failed tail flush truncates the END of the prompt — where `orchestrator._build_prompt` puts the task text — and the CLI then answers a context-only prompt with rc=0. Surfaced as `WatchdogResult.stdin_error` / `TimeoutExpired.stdin_error`; providers turn it into the bare code `stdin_incomplete`, but only inside the success branch and as an `rc == 0` last resort — at `rc != 0` a broken pipe is a symptom of the child dying, so the real error wins (see `components.md`). A 0.5 s watchdog loop fires on `idle_timeout` (no progress = hang) or `hard_timeout` (absolute backstop), then TREE-kills the process (Windows `taskkill /F /T`, fired twice as belt-and-suspenders; POSIX `killpg` SIGTERM→grace→SIGKILL with `start_new_session=True`) BEFORE joining readers — the kill closes the orphan FDs so readers get EOF and the join returns. Raises `subprocess.TimeoutExpired` with a `timeout_kind` attribute ("idle"|"hard").

**Tool-aware liveness for Claude (load-bearing, verified claude 2.1.158):** byte-silence is NOT a hang signal for Claude — a single blocking tool call holds stdout silent for the whole tool duration (verified: 12 s tool → 18.1 s inter-line gap, scales with tool duration). A naive byte watchdog would kill a productive pytest/build/install phase. **Critical schema detail:** there is NO top-level `{"type":"tool_use"}` event; matching on `evt["type"]` would be dead code (it would only ever see `assistant`/`user`/`result`, never flip `_tool_active` on). The real sequence is `system/init → rate_limit_event → assistant[content: thinking] → assistant[content: tool_use] → (stdout silence) → user[content: tool_result] → assistant[content: text] → result`. So Claude runs with `liveness_lines=True` and `_Liveness.on_event(evt, now)` inspects `message.content[]` (`_content_blocks`) and tracks a **set of open `tool_use` ids** (not a single boolean — Claude emits parallel `tool_use` blocks in one message and spawns `Task` subagents, so the first `tool_result` must not resume the timer while a sibling tool still runs): an `assistant` event adds each `tool_use` id, a `user` event discards each `tool_result`'s `tool_use_id`, the top-level `result` clears all; the idle timer is paused (`idle_for()==0`) while the set is non-empty. **Known limitation:** a tool that itself hangs forever (no `tool_result` ever arrives) keeps the set non-empty → only `hard_timeout` catches it, not the idle watchdog. Gemini/Codex have no structured tool signal → byte-only liveness with a conservative `CLI_IDLE_TIMEOUT_NO_LIVENESS_SEC` (1200 s) that exceeds the longest expected single tool phase; `hard_timeout` is the backstop if one buffers end-only. `#timeout:`/`timeout_minutes` now set the HARD backstop (90 min default), not an aggressive deadline.

idle-kill surfaces as `error="hang"` (vs `error="timeout"` for the hard backstop). The orchestrator tool loop requeues a hang with a short fixed backoff (`HANG_RETRY_BACKOFF_SEC`, NOT quota-reset-timed) via `mark_retry(hang_count=N)`, and after `MAX_HANG_RETRIES` (default 2) BLOCKS the task (finalizes + notifies) instead of looping forever. The hang counter persists in a `<!-- hang: N -->` queue-line comment (`queue_manager.extract_hang_count`, read from `QueueTask.raw_line`).

**Known limitations / open follow-ups (watchdog, reviewed 2026-05-30):**
- **`#parallel` path bypasses hang handling.** The parallel runner (`parallel_runner.py`) treats a subtask `error="hang"` like any failure and the parent is finalized as done; `hang_count`/BLOCK semantics apply only to the single-shot and tool paths. A chronically hanging `#parallel` subtask is re-run every cycle and absorbed into the aggregate. Fix vector: propagate `"hang"` through `run_parallel` and apply the same hang accounting.
- **`brainstorm` + `scientific_investigation` have no total-runtime deadline.** The `_runtime_deadline()` + `tool_runtime_exceeded` guard was applied to the 8 single-pipeline tools but not to these two multi-phase/multi-persona loops, whose summed phase timeouts are uncapped. Fix vector: add a deadline check between phases/iterations.
- **`_group_join_timeout_sec` sums subtask hard-timeouts.** With the 900 s→5400 s default bump, a 4-subtask CWD group yields a ~6 h thread-join cap; decouple the join cap from the hard backstop.
- **Gemini/Codex end-only buffering is an assumption.** The chunked `read1()` reader now registers activity on ANY byte (newline-less progress bursts no longer false-`hang`). The remaining gap: if a provider emits *zero* bytes for a whole long tool phase (true end-only buffering), the byte-only idle watchdog would still false-`hang` it after `CLI_IDLE_TIMEOUT_NO_LIVENESS_SEC` (1200 s); `hard_timeout` is the backstop. The actual flush behaviour of `gemini`/`codex exec` is not empirically verified.

### Tool total-runtime deadline

`BaseTool._runtime_deadline()` resolves `ToolContract.max_runtime_sec` (policy.yaml; fallback `TOOL_DEFAULT_MAX_RUNTIME_SEC=3600`) to a monotonic deadline checked at each iteration start of ALL three iterative loop tools (review-loop, dev-loop, test-loop) → bail with `error_code="tool_runtime_exceeded"` (partial output). This caps the SUM of all phases/iterations independently of the per-call hard backstop: without it, 20 iterations × multiple long phases (2 provider-calls each in test-loop) with a high `#timeout:` could bind dozens of hours of wall-clock. `BaseTool._phase_cap(task_timeout, phase_default)` enforces that a high task `#timeout:` is an upper deckel only — `min(task_timeout, phase_default)` never raises a phase above its `TOOL_*_TIMEOUT_SEC` constant (test-loop uses `_phase_cap(timeout, TOOL_FIX_TIMEOUT_SEC)`, not the old `timeout or TOOL_FIX_TIMEOUT_SEC`). **The orchestrator must treat `tool_runtime_exceeded` as terminal** (`orchestrator.py` tool-loop): it finalizes the task with the partial result rather than falling back to the next provider or `mark_retry`'ing — either path would restart the loop from iteration 1 with a FRESH `_runtime_deadline()` (3× the budget across the 3-provider chain, unbounded across re-polls), so the wall-clock bound only holds per single invocation otherwise.

### HTTP 429 resilience

When `cclimits` monitoring API returns 429, `limits.py` retries with backoff (5s/10s), then applies 3-tier fallback: (0) local JSONL via `claude-monitor` (`_get_claude_limits_from_local`, `CLAUDE_PLAN` env var, uses `token_counts.total_tokens`), (1) snapshot cache with estimated usage tracking (`_429_base_snapshot`, `_429_estimated_usage`), (2) optimistic cold-start. Polls back off to 5 minutes. `report_estimated_usage()` called after each task. State resets when 429 clears. Disk-cache (`--cache-ttl 600`) reduces normal API calls.

### 3-tier token estimation

`estimate_task_usage_pct()` in `limits.py` — (1) actual token counts from Claude JSON output, (2) text-based estimate from prompt/output char lengths, (3) duration heuristic fallback. Configured per provider via `ESTIMATE_TOKENS_PER_PCT`. In the 429-fallback path the headline estimate is split across Claude's 5h/7d windows by Phase-0-calibrated `tokens_per_pct` ratios (`ESTIMATE_TOKENS_PER_PCT_CLAUDE_WINDOWS`, `_estimate_window_usage_calibrated`) — the scalar cancels in `(pct × scalar) / window_tpp`, so the function's float return is unchanged; uncalibrated providers keep the reset-time heuristic.

### Goal-Adherence-Guards (Drift-Check)

`tools/review_loop.py` (Section A des Anti-Drift-Plans) implementiert ein zweistufiges Pattern, das auf andere Iterations-Loops (`brainstorm`, `critical_review`, später ggf. `security_audit`) portierbar ist:

1. **Scope-Guard im Stable-Prompt** (kostenlos, kein Cache-Bust): instruiert den Fix-/Execute-Agent explizit, off-topic Findings/Tasks zu skippen und als `SKIPPED (off-topic): <…>` zu markieren. Wirkt präventiv.
2. **Drift-Check als Mini-LLM-Call** (Trigger-basiert): zwischen Review und Fix wird ein read-only Provider-Call mit strenger Output-Form (`ON_TOPIC:` / `DRIFTED:`) gemacht. Bei `DRIFTED` wird eine Refocus-Warning in den **nächsten** Volatile-Block injiziert — der Loop wird **nicht** abgebrochen (Reviewer-LLM kann falsch liegen, harter Abort wäre over-correction). Drift-Call-Fehler sind non-fatal.

Trigger-Logik (Modus `auto`, default): `iteration >= 3 AND len(findings) > prev_count` ODER `iteration == max_iter // 2` (Halbzeit) ODER `iteration >= 5` (Sicherheitsnetz). Modi `always` / `skip` via `policy.yaml` `tool_phases.<tool>.drift_check_mode`. Implementierungsbausteine in `tools/review_loop.py`: `_DRIFT_CHECK_PROMPT`, `_parse_drift_check()`, `_should_drift_check()`, `_drift_check_mode()`-Methode auf der Tool-Klasse.

Anti-Pattern dabei vermieden: keine 1:1-Übernahme des dev-loop Resolution-Reviews (`RESOLVED/PARTIAL/UNRESOLVED`), weil review-loop-Tasks oft generisch sind ("review uncommitted changes") und die Resolution-Frage trivial RESOLVED wäre, sobald Findings clean sind. Drift-Check fragt stattdessen *zwischen* Iterationen "noch on-topic?", was zur review-loop-Semantik passt.

### Active-Run-Index (zentraler Live-State)

`tools/base_tool.py` `ActiveRunRegistry` hängt sich an `ToolTracer.emit()` und schreibt einen zentralen `logs/active_runs/<run_id>.json` Index. Schlüsseleigenschaften:

- **Zero-change in Tool-Files**: der Mirror ist Teil von `ToolTracer.emit()`. Tools, die schon die Action-Vocabulary (`run_start`/`iteration_start`/`subprocess_result`/`run_end`) verwenden, sind automatisch im Index sichtbar.
- **File-pro-Run statt zentraler JSONL**: vermeidet Lock-Contention zwischen Parallel-Runner-Worktrees; `os.replace()` für atomic write.
- **Token-Akkumulation via `tokens_delta`**: `subprocess_result`-Events tragen Per-Call-Tokens; der Index summiert in `tokens.{input,output,cache_creation,cache_read}`.
- **Stale + Lazy-Cleanup**: Records ohne Update >6h werden im Dashboard als `status="stale"` markiert; >24h werden beim nächsten `list_active()`-Read physisch entfernt (kein Daemon).
- **Test-Isolation**: `tests/conftest.py` `_isolate_active_runs_dir` Fixture (autouse) leitet `ACTIVE_RUNS_DIR` auf `tmp_path` um — verhindert Pollution des Produktiv-Verzeichnisses.

---

## Phase B Feature Flag — `CLAUDE_SESSION_ENABLED`

CLI-level conversation sessions on the Claude provider are gated by `config.CLAUDE_SESSION_ENABLED` (default **OFF**, opt-in via `.env`). When enabled:

- Tools that allocate a `SessionContext` get a UUID and pass `--session-id`/`--resume` on subsequent `claude --print` calls.
- Anthropic prompt-cache hits go from "static system-prompt only" to "full conversation history" — projected ~30-50 % token savings on multi-phase tools (dev-loop, review-loop).
- All UUIDs are registered in `logs/orchestrator-sessions.jsonl` (sidecar) so the heartbeat session-cleanup never deletes interactive Claude Code sessions.
- Fresh sessions every `cap=5` iterations cap conversation length (Sonnet 200 k context limit, Opus 1 M).
- On `error="session_missing"` (e.g. cleanup race) tools rollover and retry once.

**Rollback**: set `CLAUDE_SESSION_ENABLED=false` in `.env` and restart the orchestrator. ClaudeProvider then ignores `session_id`/`resume` parameters and falls back to today's stateless `claude --print` invocation. No code revert needed.

`config.ORCH_SESSION_RETENTION_DAYS=14` controls how long expired registry entries live before the cleanup deletes them.

Codex and Gemini providers also have CLI-level resume capabilities, but `supports_sessions=False` keeps them on stateless behaviour until empirical evidence justifies the implementation cost.

---

## Safety Rules (enforced in code) — details

- **Hard deny (Claude Code hook)**: `scripts/safety_hook.py` as `PreToolUse` hook blocks dangerous Bash commands before execution — works even in `--dangerously-skip-permissions` mode. Patterns defined in `config.SAFETY_DENY_PATTERNS`.
- **Soft deny (prompt injection)**: `SAFETY_RULES` text (auto-generated from same patterns) injected into all provider prompts via `SYSTEM_PROMPTS` — covers Gemini/Codex which have no hook system.
- **Blocked categories**: `rm -rf`, `git push --force/-f`, `git reset --hard`, `git clean -f`, `git checkout -- .`, `DROP/TRUNCATE TABLE`, `DELETE FROM` without WHERE, `format`/`mkfs`/`diskpart`, fork bombs, raw disk writes, credential exfiltration via curl/wget, Windows `del /s`, `rd /s /q`, `Remove-Item -Recurse -Force`.
- CWD validation against `ALLOWED_CWD_ROOTS` — rejects relative paths and parent escapes.
- Skill gating checks requirements (bins, env vars, OS, provider) before execution.
- Policy layer can block tasks pending Telegram approval.
