# Architecture — Patterns & Invariants

Cross-cutting patterns and feature flags. Linked from [CLAUDE.md](../../CLAUDE.md). Update both when behaviour changes.

---

## Key Patterns

### Singletons with threading

`PolicyEngine`, `UsageSuggester`, providers — each has own `_lock`, `threading.Event` for blocking operations, no global mutex.

### Provider-bound model tags

`#claude_opus`, `#gemini_flash`, `#codex_mini`, `#or_minimax_free`, `#or_glm`, `#vibe_small` etc. are resolved via `config.model_id_for_provider(tag, provider_name)`, which returns `None` if the tag doesn't belong to the target provider.

**Historical caveat, now closed:** this text used to warn that the *model* half only worked for 6 of the then-20 aliases, because `queue_manager.MODEL_TAG_RE` hard-coded a narrower list than `config.model_id_for_provider()` accepted. Fixed 2026-08-15 (`MODEL_TAG_RE` is now generated from `dispatcher._TAG_MAP`, same source as the provider half) and still true at 23 aliases after opencode's addition 2026-09-04 — see `queue_manager.py` in [components.md](components.md#queue_managerpy). Left here as a pointer rather than deleted outright, since a stale copy of the original claim was found lingering uncorrected in this exact paragraph for weeks after the fix landed — a caution about doc drift, not just about the tag mechanism. `#second_opinion:<alias>` is unaffected (own regex, own resolution). This prevents a Claude model ID from leaking to Gemini during provider fallback. The `_forced_model` attribute lives on `BaseProvider` (via `threading.local()`), applied to any provider. Orchestrator and `parallel_runner` set it per-task without a claude-only gate. Cross-provider tools (e.g. `critical_review.py` pass2) re-apply the tag against the pass2 provider separately — see `CriticalReviewTool.run()` around `pass2_provider.run(...)`.

### OpenRouter, Vibe and opencode never in fallback chain

`dispatcher._PRIORITY` deliberately omits `openrouter`, `vibe` and `opencode` so untagged tasks never accidentally route to a pay-per-token provider. All three are registered conditionally — OpenRouter on `OPENROUTER_API_KEY`, Vibe on the `vibe` binary being on `PATH`, opencode on `OpencodeProvider.is_available()` (exe resolved past the npm shim AND both required agents configured). opencode's omission is also Stufe 3 of its own plan, deliberately not built yet: the cause of an observed hang against it is still unmeasured, and `_PRIORITY` membership is exactly where an unattended run would hit that failure mode without a tag asking for it. Tag resolution uses `_providers.get(name)` (not `[name]`) so a tag for an unregistered provider silently falls back to the default chain (`_PRIORITY`, i.e. **claude → codex** since 2026-08-15 — gemini is registered but no longer in it) instead of raising KeyError — **except** for the two providers below, where that fall-through is the wrong answer.

### `_NO_FALLBACK_PROVIDERS` — providers that must not degrade into another provider

`dispatcher._NO_FALLBACK_PROVIDERS = {"vibe", "opencode"}` (renamed 2026-09-04 from `_REVIEWER_ONLY` when opencode joined — "reviewer-only" stopped describing a set containing a writing provider) carves the exception out of that silent fall-through. A `#vibe` or `#opencode`/`#opencode_*` tag with the provider unregistered yields **no provider at all** (`select_provider() → None`, task parked) rather than the next provider in the chain — `_tags_unregistered_no_fallback_provider(task)` returns the specific provider name, not just a bool, so the park log line can say which one.

The two members are in the set for DIFFERENT reasons, and both matter for understanding why this isn't just "pay-per-token providers stay barred":

* **vibe** — blast radius. Vibe's whole point is that it does not write; falling back would silently swap a non-writing reviewer for a file-writing executor. Answering "give me a second opinion without touching the files" with a file-writing executor is not the same task.
* **opencode** — tag intent. opencode DOES write, so blast radius is not the argument here. But the tag itself is usually the ask: avoiding Claude quota, or — for customer code — the only provider routed through a ZDR-guaranteed model (the handpicked `openrouter/zdr-review*` aliases). A silent fallback to Claude/Codex would spend the quota the tag was trying to avoid, or quietly drop the ZDR guarantee the task depended on.

For an unregistered `#or_*` tag the fall-through stays harmless — an executor is replaced by an executor, and the task still gets done. Parking is recoverable (install the CLI, set up the config, or retag), a stray write or a dropped ZDR guarantee is not. The guard runs only when no provider was explicitly forced, so `force_name` / an explicit `#claude` tag still wins.

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

`#id:name` tags a task with a unique ID. `#needs:name1,name2` blocks a task until all named deps are **satisfied**: `[x] … ✅ …` (succeeded) or `[-]` (cancelled by a human — either ticked off in Obsidian or dropped via `/drop`, which exists to release the downstream slot). `_collect_completed_ids()` scans the full file for them. Two-pass in `read_queue_items()` — short-circuit if no `#needs:` present. Blocked tasks keep `QueueTask.blocked_reason != ""` and are skipped by `run_once()` (no `mark_done` → stays in queue for next cycle). Queue header shows `(N ausführbar, M blockiert)` when any tasks are blocked.

### `#verify:` decides the final mark, not just the alarm

The three success paths finalize the queue line BEFORE running the task's `#verify:` outcome check, deliberately: a re-run on the next poll would alarm twice. Once the queue can express a failure, that ordering leaves a hole — `#verify:` is the one signal that says "the run was formally clean and the work did not happen" (measured 2026-09-03, reel task `njtaxr`: `exit_status ok`, artefact missing), and a `✅` on such a line releases every `#needs:` dependent. That is the same failure mode the terminal-failure state was built to remove, arriving through a different door.

`queue_manager.restamp_done_as_failed()` flips the already-written `✅` to `❌` when the check fails, and `orchestrator._restamp_after_failed_verify()` is paired with all three `_verify_task_result()` call sites (tool, `#parallel` with `success_all`, single-shot). This needs **no retry budget of its own** — the objection that kept the hole open in the first round. The line stays `[x]`, so nothing is requeued and the finalize-then-verify ordering is untouched; only the mark changes. A `#every:` task has no stamp to flip (it was rescheduled as `- [ ]`), which is the expected no-op.

`tests/test_orchestrator_tool_tasks.py::test_every_verify_site_restamps_on_failure` asserts the pairing structurally (3 verify sites, 3 restamps), because fixing two of three would leave the third silently stamping `✅` on a failed check.

**The "16 terminal paths" claim is now proven rather than asserted.** `test_every_finalization_states_its_verdict_explicitly` parses `orchestrator.py` and requires every call to `_finalize_task_with_result_checked` / `_mark_done_checked` to pass an explicit `failed=` argument — the default is `False`, so "forgot to decide" and "this is a success" were indistinguishable at the call site, which is exactly how a given-up task came to be stamped `✅`. `test_sixteen_terminal_failure_paths_are_covered` pins the partition (16 failure-stamping, 2 success), a deliberate tripwire: adding a terminal path is a decision about dependency release.

### Clean-worktree gate (`BaseTool.requires_clean_worktree`)

`#tool:dev-loop` does not start unless its `cwd:` is a git repository with a clean working tree. On a violation the task is never started: it is finalized terminally with `error_code="worktree_dirty"` (own taxonomy category `CAT_WORKTREE`) and the ❌ stamp, so it neither retries forever nor releases a `#needs:` dependent.

Measured 2026-09-03/04: `nightstash` ran 22:02–22:45 and left the repo on its own branch `night/stash-pruning`. Over two hours later `nightfloor` started in the same repo, and its Quality reviewer refused the output format — "Task und Working Tree passen nicht zusammen … Branch: night/stash-pruning ← nicht night/version-floor" — which tipped the run into `format_error`; three attempts later the retry budget was gone. The runs did not overlap, so this is a missing reset between *sequential* tasks, not a race. Every task order carried "start with `git switch -c night/xyz master`, abort on an unclean tree", but that lived in the PROMPT: a fail-open guard nothing enforced.

**Scope is a property of the tool, not of the queue line.** `BaseTool.requires_clean_worktree` is False by default and True on `DevLoopTool`. Three reasons for keying it there rather than to a new opt-in tag:

* The queue is hand-written. A rule you must remember for every new task is weaker than one that is right on its own, and `#tool:dev-loop` is already written for its own reasons.
* dev-loop is precisely the tool that *produces* the diff its own reviewers judge, so a foreign working tree is not noise but a corrupted object under review. `review-loop` *consumes* an existing diff and is deliberately NOT gated — a clean tree there would be the opposite of what the tool is for. "Every writing tool" would therefore be the wrong rule.
* Legitimate tasks in permanently dirty repos keep working untouched: `nightlovelace` ran plain (no `#tool:` tag) in `haus`, explicitly ordered not to commit or write anything, and never reaches the gate.

`#allow-dirty` is the opt-out for a repo that always carries uncommitted work, so the escape hatch needs no code change.

**Two gaps in the enforcement were closed after an external review (2026-09-04); both were real.**

*No `cwd:` is now a refusal, not a skip.* "No cwd" does not mean "no repo": `providers/process_runner._spawn()` passes `cwd=None` straight to `Popen` (`cwd=cwd`, line 183), which inherits the orchestrator's own working directory — so a `dev-loop` task without a `cwd:` tag runs against whatever repo the orchestrator was started in, with the precondition unverifiable rather than absent. A guard that cannot check must not pass. `#allow-dirty` still waives it. Nine existing tests drive `#tool:dev-loop` without a cwd to exercise other mechanisms (retry counter, policy routing); they now switch the gate off explicitly via `_no_worktree_gate()` rather than satisfying it by accident.

*`#parallel` subtasks are gated where they actually run.* The parent stays exempt in `run_once()` — its own `#tool:` tag is not what executes — but `parallel_runner._run_single_subtask()` calls `_execute_tool_task()` directly, which made the subtasks the one route around the gate (the `#worktree` precheck only covers `#worktree` runs). The same `_worktree_gate_violation()` now runs per subtask before provider selection; a violation fails that subtask, and the parent is finalized `❌` through `failed=not success_all`. Under `#worktree` the subtask cwd has already been rewritten to the freshly created worktree, which is clean by construction.

**Terminal, not parked** — nothing cleans the tree on its own, so parking would re-check the same dirty tree on every poll, unattended and silent. **No automatic reset or branch switch**: a reset can destroy work from another session, while refusing to start makes the same mistake just as visible and costs nothing.

**Known limit, measured rather than assumed.** The gate catches a dirty tree, not a foreign branch. Of the three failed `nightfloor` attempts that night, the tree was actually dirty at exactly one (00:11 — proven by the `git stash create` snapshot the orchestrator takes before each task, which produces a commit only for a dirty tree; no snapshot exists for the 23:49 and 01:05 attempts). At the other two the tree was clean and only the branch was foreign. A branch check is not implementable generically: the orchestrator has no way to know which branch a task expects, since that appears only in the prompt text. The current branch is therefore named in the block message, so the human sees the state the repo was left in.

The check itself reuses `parallel_runner._is_clean_git_repo()` — one implementation for both the worktree path and this gate.

### Terminal failure state (`- [x] … ❌ …`)

The queue had no way to say "this went wrong": `mark_done()` and `finalize_task_with_result()` both wrote `- [x] <task> ✅ <ts> (<provider>)` unconditionally, and no writer anywhere in the repo produced any other terminal shape. Measured 2026-09-04 01:21: `#id:nightfloor` ended `exit_status: "error"`, `error_code: "format_error_blocked"` (`logs/runs.jsonl`) and its line read `✅ 2026-09-04 01:21 (claude+dev-loop)`. `_collect_completed_ids()` matched `[x]` **or** `[-]`, so the failure counted as satisfied, the dependent `#needs:…,nightfloor,…` shutdown task was released, and the machine powered off with the fix unwritten.

A terminal failure now stamps **❌ instead of ✅** on an otherwise unchanged `- [x]` line. Four constraints pick that shape, and each rules out an alternative:

* **It must not be picked up again.** `OPEN_TASK_RE` only matches `- [ ]`, so the checkbox has to stay `[x]`. Leaving the line open would re-burn the full runtime every following night — the retry cap had just been reached.
* **It must not satisfy `#needs:`.** `_collect_completed_ids()` skips a `[x]` line carrying the failure stamp. `[-]` deliberately keeps satisfying: in Obsidian that symbol means *cancelled*, and `queue_healing.apply_drop()` writes it precisely to unblock downstream tasks.
* **It must not disturb Obsidian.** The queue file is a vault note rendered by the Tasks plugin. A vault-wide census (2026-09-03) found exactly four status symbols (`x`, `-`, ` `, `/`), and the plugin classifies an unregistered symbol as type **TODO** — a `- [!]` would have made every failed task reappear as *open* in every task query in the vault. So the verdict rides on the mark, not on the checkbox.
* **A human must see it while skimming.** ❌ against ✅ in the same column is the whole point.

`queue_manager.line_is_failed()` is the single reader of that shape. It anchors on the **complete stamp** (` ❌ YYYY-MM-DD HH:MM (provider)` at end of line), not on the bare emoji, so a task whose description merely contains ❌ cannot fake a verdict. `_DONE_TASK_TS_RE` accepts both marks, so failures are archived to `agent-queue-erledigt.md` on the same 48 h clock instead of piling up in the live queue. `#every:` tasks are unaffected — `_completion_replacement()` reschedules them instead of stamping, and a recurring line never becomes `[x]`, so it never satisfied a dependency anyway.

**Two things about the stamp that an external review (2026-09-04) put a finger on.**

*The shape is forgeable, and that is accepted.* `line_is_failed()` recognises a SHAPE — a hand-ticked `[x]` line whose description happens to end in `❌ YYYY-MM-DD HH:MM (text)` cannot be told from one the orchestrator wrote. Distinguishing them needs a second marker in the line, which this repo has already weighed and rejected once (it splits the queue's only persistent state across two markers every rewrite must keep in sync). It stays because the direction of the error is safe: a false positive can only **withhold** a dependency — blocked, visible, and reported by `queue_healing` after 24 h — never release one, and releasing one wrongly is the disaster this mechanism exists to prevent. Measured read-only against the live `agent-queue.md` on 2026-09-04: 15 finished lines, **all 15** carrying a real orchestrator `✅` stamp, **zero** hand-ticked lines and zero lines of the failure shape. The exposure is a shape nothing in the file has.

*Stripping the stamp must be anchored, not greedy.* `re.sub(r"\s*❌\s+.*$", …)` cuts at the FIRST ❌, so `/retry` on `- [x] Replace ❌ with ✅ #id:a ❌ 2026-… (claude)` produced `- [ ] Replace` — instruction and `#id:` gone, the reopened task unrunnable. `queue_manager.strip_failure_stamp()` removes only the validated trailing stamp; `_promote_failed_line()` uses `rpartition()` for the same reason (the last ❌ is the stamp's, because the stamp is anchored at end of line).

`queue_healing.py` is the only other component that reads task status. `_find_failed_ids()` recognises both failure shapes (so a failed dep still produces an `/unblock`/`/retry` proposal), `_find_completed_ids()` excludes the ❌ stamp (so a failure cannot silence the proposal), `apply_unblock()` promotes a failed line to a satisfying one — checkbox to `[x]` **and** ❌ to ✅, because doing only the first turned `apply_drop()`'s `- [-] … ❌ …` into `- [x] … ❌ …`, which reads as a failure: `/unblock` reported success and left the dependent blocked. `apply_retry_dep()` reopens a failed line as `- [ ]` (stamp stripped, description intact) while refusing to reopen a *successful* task.

### Schedule tags (`#at:`/`#every:`)

Both reuse the existing retry primitive — no new modules, no scheduler tick. `#at:<timestamp>` accepts the same forms `_retry_is_due()` understands (`YYYY-MM-DDTHH:MM`, `YYYY-MM-DD HH:MM`, `HH:MM`); `read_queue_items()` filters the task out until the timestamp is reached, and the tag disappears on first fire via the `[x]` mark. Retry-annotation always wins over `#at:` (it's the active timing signal once a transient retry is set). `#every:<duration>` (units `s|m|h|d`, e.g. `#every:24h`, `#every:7d`) triggers a different completion path: `_completion_replacement()` rewrites the line as open with a fresh `<!-- retry: now+duration -->` annotation instead of `[x]`, and strips any stale `#at:` on the rewrite. Combinable: `#at:2026-05-17T22:00 #every:24h` = first fire at 22:00, then daily. Both `mark_done()` and `finalize_task_with_result()` call `_completion_replacement()`. Missed schedules replay automatically (retry-time in past = task due now). Queue file remains the single source of truth — adding/pausing schedules = editing `agent-queue.md`.

### Keine HTML-Kommentare im Task-Body

`OPEN_TASK_RE` (`^- \[ \] (.+?)(?:\s*<!--.*?-->)?\s*$`) duldet Kommentare nur am Zeilenende — dort schreibt `queue_manager` die `retry`/`hang`-Marker. Steht einer mitten im Text **und** endet die Zeile auf `-->` (bei jeder `#every:`-Task der Fall, sobald ein Retry-Marker angehängt ist), spannt die non-greedy Gruppe vom ersten `<!--` bis zum letzten `-->`: der Provider bekommt einen mitten im Satz abgeschnittenen Prompt, antwortet plausibel und der Lauf gilt als `success` — und beim Zurückschreiben der erledigten Zeile ist der verschluckte Bereich inklusive `#every:`/`#at:`/Modell-Tag **dauerhaft aus der Datei gelöscht**, der Recurring-Task also tot. Die stdin-Detektion (`stdin_incomplete`) greift nicht: der Prompt erreicht den Provider vollständig, verstümmelt wurde er schon beim Parsen. Live passiert am Task „Daily-Activity-Synthese" (23.+24.07.2026, zwei Tage stiller Ausfall).

`--lint-queue` meldet das seit 2026-07-24 als `html_comment_in_body`, einen nicht als Marker lesbaren Kommentar am Zeilenende als `html_comment_trailing` (beide error). Der Check ist der einzige, der die **rohe** Zeile sieht — `task_text` stammt aus demselben abschneidenden Regex und ist für den Defekt blind; `_iter_open_tasks` reicht die Rohzeile deshalb als viertes Tupel-Element durch. Das Marker-Muster wird aus `RETRY_TAG_RE`/`HANG_COUNT_RE` komponiert statt kopiert, damit Linter und Parser nicht auseinanderdriften. Kommentar-förmige Inhalte (Block-Marker, Beispiele) gehören in die Skill-Datei, auf die die Queue-Zeile verweist.

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

idle-kill surfaces as `error="hang"` (vs `error="timeout"` for the hard backstop). The orchestrator tool loop requeues a hang with a short fixed backoff (`HANG_RETRY_BACKOFF_SEC`, NOT quota-reset-timed) via `mark_retry(hang_count=N)`, and after `MAX_HANG_RETRIES` (default 2) BLOCKS the task (finalizes + notifies) instead of looping forever. The hang counter persists in a `<!-- hang: N -->` queue-line comment (`queue_manager.extract_hang_count`, read from `QueueTask.raw_line`). `error_code="format_error"` shares that counter — it is the queue's only per-task counter — so it counts **fruitless attempts, not hangs**: two format errors followed by a first genuine hang block at 3. That cap is intended (three dead attempts on one task is what it is for), but it means the message must not claim an ordinal it does not have, hence `… — 3. erfolgloser Versuch (Hang/Format-Fehler zusammen gezählt)`. A second marker was weighed and rejected: it would split the queue's only persistent state across two markers every rewrite has to keep in sync, and would raise the unattended budget from 3 dead attempts to 5.

**`mark_retry()` is the single writer of that marker, and "no count" means preserve.** Every park rebuilds the queue line from scratch, so the counter only survives if the rebuild carries it. It did not until 2026-08-15: `mark_retry()` emitted the marker only when handed a `hang_count`, and `_mark_retry_checked()` — the helper behind the capacity, timeout, strict-mode, approval and parallel-error parks — never passed one, so each of those silently reset the count to 0 and a task alternating between format errors and capacity parks requeued forever. The distinction is drawn by what the park says about the *task*: `hang` and `format_error` pass `previous + 1` because they are unsuccessful attempts at it; everything else passes `None` and the existing value is carried through unchanged, because capacity, a cooldown or a quota reset says nothing about the task. `hang_count=0` clears explicitly (nothing uses it); success clears implicitly, since `finalize_task_with_result()` rewrites the line without the marker — including the `#every:` requeue, where a fresh start is the intent. Keeping the rule inside `mark_retry()` rather than at each caller is deliberate: the alternative is a cross-cutting obligation on the queue's only persistent state, and that is exactly the invariant that was already broken. `_replace_open_task_line()` therefore accepts a *builder* as well as a finished string, so the old count is read from the same line that gets overwritten — line numbers shift while a task runs, and reading it in the caller would read it off whatever moved into the remembered slot.

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

- **Hard deny (Claude Code hook)**: `scripts/safety_hook.py` as `PreToolUse` hook blocks dangerous Bash commands before execution — works even in `--dangerously-skip-permissions` mode. Patterns defined in `config.SAFETY_DENY_PATTERNS`. The git patterns are anchored to `config._CMD_START`, so they fire only where a command really starts: line start, after `;`/`&`/`|`/newline, after `(` or a backtick, after a shell interpreter's `-c`/`-Command` argument, and after the command-taking builtins `eval`/`exec` (both prefixes compose, covering `bash -c "eval 'git push'"`). That is what keeps `python -c "…git push…"`, `grep -r 'git push'` and comments out. Accepted residuals: a command handed to a non-shell wrapper (`find . -exec …`, `docker exec …`, `xargs …`) is not recognised, since telling those apart needs real argv parsing.
- **Soft deny (prompt injection)**: `SAFETY_RULES` text (auto-generated from the same patterns) reaches a provider through `config.get_system_prompt()`. **Two limits, both measured 2026-09-05, because "injected into all provider prompts" was wrong in both directions.** (1) `SYSTEM_PROMPTS` has keys for `claude`, `codex`, `gemini` and `opencode` only — Claude *does* get the rules (on top of its hook), while `vibe` and `openrouter` fall through `.get(name, "")` and get **nothing**; harmless for `vibe` (`--disabled-tools "*"` or read-only tools) but worth knowing for `openrouter`. (2) **A `SOUL.md` replaces this layer wholesale**: `get_system_prompt()` returns `soul["base"] + soul[provider]` and never looks at `SYSTEM_PROMPTS`, so with a SOUL present the `SAFETY_RULES` constant reaches nobody and the effective soft layer is whatever safety text the SOUL's `base` carries — for *every* provider, `vibe` and `openrouter` included, which is why they end up better covered with a SOUL than without one. Nothing keeps a SOUL-side copy in sync with the constant; when `SAFETY_RULES` changes, the SOUL must be re-read by hand. The hard-deny hook is unaffected by all of this.
- **Blocked categories**: `rm -rf`, `git push --force/-f`, `git reset --hard`, `git clean -f`, `git checkout -- .`, `DROP/TRUNCATE TABLE`, `DELETE FROM` without WHERE, `format`/`mkfs`/`diskpart`, fork bombs, raw disk writes, credential exfiltration via curl/wget, Windows `del /s`, `rd /s /q`, `Remove-Item -Recurse -Force`.
- CWD validation against `ALLOWED_CWD_ROOTS` — rejects relative paths and parent escapes.
- Skill gating checks requirements (bins, env vars, OS, provider) before execution.
- Policy layer can block tasks pending Telegram approval.
