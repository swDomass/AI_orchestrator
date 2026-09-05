# AI Orchestrator — Feature Roadmap

Status ledger for the feature set, plus the design decisions behind it that are
not derivable from the code.

**What lives where**:
- *As-built behaviour* → [`docs/architecture/components.md`](docs/architecture/components.md) (per module) and [`docs/architecture/patterns.md`](docs/architecture/patterns.md) (cross-cutting patterns).
- *User-facing usage* → [`README.md`](README.md).
- *Original pre-implementation specs* for the shipped items → git history (`git log -p -- ROADMAP.md`). They recorded intent before the code existed and in several places no longer match what shipped (the skills migration, the doctor check list, the taxonomy categories). The decisions still worth having are summarised here; the stale step-by-step instructions were removed rather than left to contradict the code.

**Guiding principle — human-in-the-loop**: maximum autonomy for routine work
(file edits, git commits, tests, tool loops). Telegram approval ONLY for
irreversible or dangerous actions (push, publish, delete outside CWD, CI
changes). No approval fatigue — blanket session approvals, per-task
pre-approval tags, and smart grouping (see #9).

**Design constraint**: AI Orchestrator stays a *supervisor* over CLI providers,
not an agent runtime. Every item is a reliability lever, a UX shortcut into the
existing queue, or a cost/cache optimisation. The one item that touches agency —
#36 Skill Suggestion — is locked behind draft-only + pattern-gating + manual
activation.

---

## Tier 1 — High Impact, Builds on What We Have

| # | Feature | Status |
|---|---------|--------|
| 1 | Skills system + auto-discovery + gating | DONE |
| 2 | `--doctor` / onboarding command | DONE |
| 3 | Memory system with temporal decay | DONE |
| 4 | Heartbeat / proactive scheduled tasks | DONE |
| 10 | `#shutdown` — graceful OS shutdown via Telegram or queue tag | DONE |
| 20 | Increased Telegram output (3.5k) + context-aware AI chat | DONE |

## Tier 2 — Strong Value, Moderate Effort

| # | Feature | Status |
|---|---------|--------|
| 5 | Selective skill/prompt injection | DONE |
| 6 | Execution profiles (`#agent:work`, `#agent:personal`) | DONE |
| 7 | Parallel sub-agent spawning | DONE |
| 8 | SOUL.md / personality-as-config | DONE |
| 9 | Execution policy + approval layer | DONE |
| 10b | Usage Suggester (proactive task suggestions) | DONE |

## Tier 3 — Nice to Have, After Core Is Solid

| # | Feature | Status |
|---|---------|--------|
| 10 | Tool policy layering (global > profile > task) | DONE |
| 11 | Session management (Claude `--session-id` / `--resume` across tool phases) | DONE (opt-in via `CLAUDE_SESSION_ENABLED`) |
| 12 | Optional Docker sandbox | backlog |
| 13 | Dashboard / web UI | DONE |
| 14 | Plugin system (runtime-loadable handlers) | backlog |
| 15 | Obsidian CLI integration (search, tasks, backlinks) | backlog |
| 16 | Usage Budgeting & Pace Analysis (7d rolling) | DONE |
| 17 | Research-QA Tool (pre-implementation research) | DONE |
| 18 | Claude JSON token tracking for capacity estimation | DONE |
| 19 | Knowledge-Transfer Tool (cross-domain innovation) | DONE |
| 20 | Task dependencies (`#id:` / `#needs:`) — two-pass blocking resolution | DONE |
| 21 | Per-window capacity thresholds (Claude 5h/7d, Codex primary/secondary) | DONE |
| 22 | Heartbeat background thread (always-on scheduling during long tasks) | DONE |
| 23 | Dev-loop resilience (capacity guards, research-phase state caching) | DONE |
| 24 | Robustness hardening (narrow excepts, thread-safety, XSS-safe JS, CORS) | DONE |
| 25 | Critical-Review Tool (3-pass adversarial review, cross-provider) | DONE |
| 26 | Deep-Security-Audit Tool (6 expert personas + CISO synthesis + optional Round-Table) | DONE |
| 27 | Scientific-Investigation Tool (audit-trail pipeline, pre-registration, status tuple) | DONE (Plan v5, I0–I9) |
| 28 | Brainstorm Tool (domain-aware personas, cross-pollination, TF-IDF convergence) | DONE |

## Tier 4 — Overkill for Now

| # | Feature | Status |
|---|---------|--------|
| 15 | Gateway / WebSocket architecture | deferred |
| 16 | Multi-channel inbox (WhatsApp/Slack/Discord) | deferred |
| 17 | Voice / Canvas / Device nodes | deferred |

## Tier 5 — Reliability Wave (2026-05-16)

Synthesised from the three `*_FUTURE_IDEAS.md` notes, with critical
re-prioritisation. Guiding rule: **every item must strengthen the supervisor,
not pull the orchestrator toward agency.**

| # | Feature | Effort | Status |
|---|---------|--------|--------|
| 29 | Queue Linter (`--lint-queue`) | S | DONE |
| 30 | Replay JSONL (machine-readable run summaries) | M | DONE |
| 31 | Idempotency Keys (external triggers) | S | DONE |
| 32 | Telegram Slash-Commands (`/review`, `/dev`, `/security`, `/audit`, `/critique`, `/brainstorm`) | S | DONE |
| 33 | Schedule tags (`#at:` one-shot, `#every:` recurring) | S | DONE |
| 34 | Failure Taxonomy (built on #30) | S | DONE |
| 35 | Preflight Hooks per Tool | M | DONE |
| 36 | Skill Suggestion (draft-only, pattern-gated) | M | DONE |
| 37 | Progressive Skill Loading | M | DONE |
| 38 | Queue Healing (auto-unblock + Telegram-ask) | M | DONE |
| 39 | Goal-Adherence-Guard in review-loop (Scope-Guard + auto/always/skip drift-check) | S | DONE |
| 40 | Active-Runs Dashboard panel (live index + ToolTracer hook) | M | DONE |
| 41 | Cost-Cap (`ToolContract.max_cost_eur` + `budget_exceeded`) | M | deferred — see *Open items* |

**Effort legend**: S = ~1 day, M = 2-5 days, L = >1 week.

## Tier 6 — Operations Wave (2026-05 … 2026-07)

Delivered after Tier 5, mostly driven by real incidents rather than a plan
document. Numbered P1–P6 where they came from the Cherny/Steinberger adoption
plan (2026-05-18).

| # | Feature | Status |
|---|---------|--------|
| P1 | Worktree isolation for `#parallel` (`#worktree`, `#keep-worktree`) | DONE |
| P2 | PR-Babysitter — poll open PRs via `gh`, queue `dev-loop` fixes | DONE |
| P3 | Tool Contracts — per-tool budgets/stop conditions in `policy.yaml` | DONE (staged migration: tools still fall back to `config.TOOL_*`) |
| P4 | CI-Failure-Watcher heartbeat (`check-ci-failures`) | DONE |
| P5 | PR-Babysitter report-only mode + `/pr-fix` / `/pr-ignore` | DONE |
| P6 | Daily status recap via Telegram (`status-recap`) | DONE |
| 42 | Liveness/hang watchdog replacing wall-clock deadlines (`process_runner.py`) | DONE |
| 43 | stdin delivery verification (`stdin_incomplete`) | DONE |
| 44 | Quota calibration Phase 0 (telemetry) + Phase 1 (SoTH state file) | DONE |
| 45 | Quota Phase 2 — live between-poll estimate + auto-recalibration | DONE (flag-gated, `ORCH_QUOTA_LIVE_ESTIMATE`, default OFF) |
| 46 | Gemini dual-mode — HTTP REST API after the consumer CLI shutdown (2026-06-18) | DONE |
| 47 | OpenRouter provider — opt-in, pay-per-token, never in the fallback chain | DONE |
| 48 | External second opinion in `review-loop` (`#second_opinion:`), Gemini retired as reviewer | DONE |
| 49 | Mistral Vibe provider — second non-Claude voice, reviewer-only | DONE |
| 50 | Unattended `#tool:` reactivation — provider ceiling, uniform error classification, effective safety hook | DONE — **proven 2026-09-02**, see below |
| 51 | Snapshots into an own ref namespace (`refs/orchestrator-backup/`) + retention cap | DONE |
| 52 | opencode provider — third opt-in voice, tag-activated, capped by its own OpenRouter key | DONE (Stufe 1+2, 2026-09-04). Stufe 3 — joining `_PRIORITY` — deliberately **not** built: the cause of an observed hang is still unexplained. |

### #50 — Unattended `#tool:` reactivation (2026-08-15)

The orchestrator had run as a briefing runner only since 2026-04-03: not a single
`#tool:` task in that window. This wave rebuilt the guarantees an unattended tool
run needs, as four separate changes:

1. **Git state is frozen — and this half was rolled back two days later.** The
   original intent was that unattended runs leave work in the working tree and the
   user commits and pushes; `88a48ef` (2026-08-17) denied both `git commit` and
   `git push` in `config.SAFETY_DENY_PATTERNS`. `40c836f`, the same day, **dropped
   the commit half**: the hook is registered for every Bash call and never tested
   for "unattended", so it hit interactive sessions exactly as hard, with no way to
   override it from inside one — and a commit is local and revertible while a push
   is not. Today only `git push` is hard-denied; the only remaining lever against
   commits in a night run is `approve:` in `policy.yaml`. The prompt layer never
   restricted either (`SAFETY_RULES` mentions push only), so hook and prompt now
   agree.
2. **Provider ceiling.** `openrouter` and `vibe` are barred for tool tasks on the
   chain, on an explicit tag, and on tool-internal lookups; Gemini left the chain
   entirely.
3. **Uniform error classification.** All error handling goes through
   `providers.base.error_code_of()` / `is_transient()`, and a format break is a
   retryable `format_error` instead of a silently completed task.
4. **The safety hook actually blocks.** It had emitted an output shape Claude Code
   recognises as neither "block" nor "deny", so every block since it was written
   was a no-op.

**Why this was not DONE at first:** the queue contained **no `#tool:` task**, so none
of it had executed unattended even once. The path was repaired and tested, not
demonstrated, and the acceptance criterion was a first supervised run.

**Met on 2026-09-02 (recorded here 2026-09-05).** Counted in the vault queue: seven
completed `#tool:` lines — four in `agent-queue.md`, three in `agent-queue-erledigt.md`
— run unattended, among them the ruff/mypy introduction in this repo, the
`limits.py` version-floor fix and two cross-repo config/retry jobs. The machinery
held, and it also produced the first honest failures of the reactivated path, both
already fixed or tracked: `#id:nightfloor` burned three `format_error` retries into
`format_error_blocked` (two were parser strictness — fixed in `f9b29e0` — and the
third a *correct* refusal to emit a verdict for a working tree that did not match
the task), and `njtaxr` reported `ok` after 79 s having only started a renderer in
the background. The second is why `#verify:` became mandatory for night tasks whose
artefact lives outside git, and why a failed check now flips the mark to `❌`.

**Review coverage is partial.** The change set was reviewed by Mistral (`vibe`) —
four findings, all fixed, one (git aliases) refuted at the system level and
deliberately not built. **Codex could not run**: usage limit exhausted, free again
2026-08-20. So this diff has seen *one* external voice, not the usual two. It is
not "externally reviewed" in the sense the rest of this repo means it.

### #51 — Snapshot namespace + retention (2026-09-03)

`_git_snapshot()` ran before every non-read-only task in a git repo and wrote
its `git stash create` commit to `refs/stash` via `git stash store`. Two defects
in one line: it wrote into **the user's own workspace** (a `git stash pop` after
a nightly run pops an orchestrator snapshot, not your work), and **nothing ever
removed the entries** — a repo-wide search for `stash drop`/`clear`/`pop` outside
`tests/` returned nothing. Measured 2026-09-03: 11 entries had piled up and were
hand-archived to `refs/orchestrator-backup/<timestamp>`.

The snapshot now goes to `refs/orchestrator-backup/<timestamp>` via
`git update-ref`; `refs/stash` is never written. Creation uses an empty
`oldvalue` (create-only), so two snapshots in the same second in one repo cannot
overwrite each other — the second retries with a `_2` suffix.

**Why not "delete on task success".** The night tasks deliberately do not commit;
the changes sit in the working tree until the morning review. The snapshot is
therefore the *only* undo for them, and deleting it on success would destroy the
one artefact the feature exists to produce. Retention has to outlive a review
cycle, which makes the policy a **veto** structure rather than an LRU:

| Constant | Value | Reasoning |
|---|---|---|
| `GIT_SNAPSHOT_PROTECT_DAYS` | 14 | Veto over both caps. Covers a missed weekend plus a week away. Matches `ORCH_SESSION_RETENTION_DAYS`. |
| `GIT_SNAPSHOT_MAX_AGE_DAYS` | 30 | Age cap; the repo's existing retention convention (`MEMORY_DAILY_LOG_RETENTION_DAYS`, `QUEUE_EVENTS_LOG_RETENTION_DAYS`, `replay.py`). |
| `GIT_SNAPSHOT_MAX_COUNT` | 50 | Count cap, newest-first. Far above the measured 11-in-6-months rate; bounds a high-frequency repo rather than pruning routinely. |

Deletion rule: `age >= 14 d AND (age > 30 d OR outside the newest 50)`. In a
high-churn repo the veto can starve the count cap — deliberate; the undo
guarantee outranks tidiness, and the comment at `_prune_snapshot_refs` says so.
Pruning is scoped to `refs/orchestrator-backup/` (prefix re-checked per ref
before each delete) and to the repo the task runs in, and never raises.

The ref name and the restore command go to **both** stdout and the file log:
`git stash list` no longer shows the snapshot, so the ref name is the only way
to find it — and `run_orchestrator.ps1` starts `--watch` without stdout
redirection, where a `print` alone would be lost in exactly the unattended run
that needs it.

⚠️ **First run in this repo will prune the 11 hand-archived March refs.** They
are ~180 days old, so the age cap takes them on the next snapshot here. Each
deletion is logged with ref name *and* sha (recoverable via `git stash apply
<sha>` until `git gc` runs). To keep any of them, move them out of
`refs/orchestrator-backup/` first.

Ageing runs on `committerdate`, i.e. on the age of the *content*, not on when a
ref entered the namespace — the ref *names* carry the same March timestamps, so
name-based ageing would not help either. That is the general case behind the 11
refs: anything imported into the namespace from an older mechanism reads as
ancient on its first prune. Because a *routine* prune retires at most one ref
(snapshots accrue one per dirty run and cross the cap one at a time), deleting
**more than one ref in a single pass** is logged at `WARNING` — with all names
and shas, and the hint to move keepers out of the namespace — instead of
disappearing into the `INFO` housekeeping stream of an unattended 03:00 run.

---

## Open items

| Item | Why it is not done |
|---|---|
| #12 Docker sandbox | Weeks of work. Only worth it if untrusted code execution becomes routine. A narrow `#sandbox` profile is the acceptable smaller step. |
| #14 Plugin system | `tools/registry.py` is fine. Runtime-loadable handlers are a nice-to-have, not a need. |
| #15 Obsidian CLI integration | No concrete pain point yet; wikilink + file-context injection covers today's use. |
| #41 Cost-Cap | Bundled with a possible credit/API-provider migration. CLI subscriptions are rate-limit-bound, not $-metered, and the capacity side is already covered by the quota calibration (#44/#45). Open sub-questions: is `analytics._billing_cost_units` enough as a cost proxy, and how does `max_cost_*` combine with `max_iterations`/`max_runtime_sec`? Data keeps accumulating in `logs/runs.jsonl` + `quota-calibration.csv`, so delaying costs nothing. |
| `state.json` + session UUID persistence for capacity-resume | Today a resume starts a fresh UUID — a deliberate trade-off, not an oversight. |

### Known defects (tracked, unfixed)

Rebuilt 2026-08-15. The two entries this table carried before — `MODEL_TAG_RE`
covering 6 of 20 aliases, and the `RunResult` attribute crash in
`tools/security_audit.py` — are both **fixed** and were removed. The table
itself stays, because the register is what several other docs point at and
because these took its place. Full prose per item →
[README → Known Limitations](README.md#known-limitations).

Three more were closed later the same day, in the follow-up pass that finished
the two unification threads above: **the hang/format counter no longer resets on
a non-hang park** (`mark_retry()` preserves the marker when handed no count — it
is now the single writer, and only `hang`/`format_error` increment); **the last
two raw `get_provider_by_name()` call sites** (`tools/brainstorm.py`,
`tools/scientific_investigation_phase2.py`) went to `policy_provider_lookup()`;
and **`knowledge_transfer` + `brainstorm` joined the shared error classifier**.

A fourth was closed on 2026-08-17: **`realign_stale_freshonly()` no longer steps
over a slot whose grace window is still open.** A stale `#freshonly` task was
realigned to the next anchor strictly after `now`, so one day with the machine
off cost two days of automation — the day itself, plus the day the realign
skipped on the way back up. It now recovers the most recent anchor while that
one is still within grace (daily cadence only, window capped at half the
interval), and the realign is logged at INFO: the defect ran three times
(2026-07-23, 07-27, 08-17) without leaving a single trace.

A fifth and sixth were closed on 2026-09-02, both from the same root cause —
**a hand-copied provider list drifting from `dispatcher._TAG_MAP`, the same
pattern `MODEL_TAG_RE` already fixed on 2026-08-15**: `queue_linter.py`'s
unknown-model-alias-shape regex now derives its provider prefixes from
`queue_manager._MODEL_ALIAS_PREFIXES` (itself generated from `_TAG_MAP`)
instead of a hand-copied `claude|gemini|codex|or` list, so `#vibe_*` is
validated like every other provider's aliases; a `#vibe`/`#vibe_*` tag without
the `vibe` CLI on PATH now gets a `vibe_missing_cli` warning, worded
differently from the OpenRouter one because a missing Vibe binary *parks* the
task (`dispatcher._NO_FALLBACK_PROVIDERS`) instead of falling back to the default
chain. And `PASS_PROVIDER_TAG_RE` (`#pass1:`/`#pass2:`) now accepts `vibe` and
`openrouter` the same way, sourced from the same `_TAG_MAP` — chosen over
rejecting the two values outright because `_resolve_pass2_provider()` already
routed an arbitrary provider name through `policy_allows_provider()` /
`get_provider_by_name()` (see `test_pass2_tag_barred_by_policy_falls_back`);
the regex simply never produced `vibe`/`openrouter` for that already-built
path to receive. `select_provider()`'s own fail-open path (first row below) is
a separate, still-open item — untouched by this pass.

| Defect | Impact |
|---|---|
| `select_provider()` fail-open for a bare `#vibe`/`#openrouter` tag | The pay-per-token ceiling was made fail-closed in `policy_allows_provider()`/`dispatcher._allows()`, but not on the forced-provider path. With `policy.yaml` missing or unreadable, an explicitly tagged task still reaches the uncapped provider. **Halved on 2026-09-04** (`6702e13`): the *profile* branch now clears `_allows()` per candidate (`dispatcher.py:387-388`, own test); only the *tag/forced* branch remains, where `_allows()` is never consulted at all. Left open deliberately — the rest of the fix reworks ~10 routing tests. |
| `stdin_incomplete` requeues without bound | `<!-- hang: N -->` is the only persistent per-task counter and only `hang` + `format_error` *increment* it. Every other error code requeues without raising it. Count unbounded, rate still throttled by the 5-min cooldown. Pre-existing. Narrowed 2026-08-15: those parks no longer *reset* the counter either, so a task alternating between real failures and parks does reach the cap. |
| An unregistered value in `#pass1:`/`#pass2:` is still dropped silently | Narrowed 2026-09-02: `vibe` and `openrouter` are accepted now, but a typo or an unknown provider still fails twice over — `extract_pass_providers()` drops the pass, and `strip_metadata_tags()` leaves the tag in place, so it reaches the model as prompt text. Measured: `#pass1:claude #pass2:mistral` yields `{1: 'claude'}` and a prompt still ending in `#pass2:mistral`. `queue_linter.py` has no counterpart check. |
| Safety-hook residuals | `find . -exec git push`, `docker exec c git push`, `xargs git push` are not recognised (needs real argv parsing, not a regex). A heredoc body line starting with a git write command matches — a deliberate false positive in the safe direction. Hook covers Claude only; Codex relies on its own sandbox flag. |
| `tests/test_telegram_listener.py` order-dependent; `tests/test_usage_suggester.py` environment-dependent | Run with `-p no:randomly`. A red suite in a fresh worktree is more likely one of these two than a real regression. |
| `--lint-queue` is blind to a policy-barred provider tag | Added 2026-09-04. The linter checks CLI *registration* for `#vibe`/`#opencode`, never whether `policy.yaml` permits the provider. Measured: with `tool_providers.default: [claude, codex]` every `#opencode` line ended terminally with `provider_not_allowed` while the linter reported "no problems found". Needs the linter to load the policy, which it does not do today. |
| `AllLimits.opencode` missing from four display paths | Added 2026-09-04. `heartbeat.py:213`/`:316` and `telegram_listener.py:562`/`:579` still hand-count `("claude", "gemini", "codex")`. Observability only — every gate (`_limits_ok`, `earliest_reset_sec`, `any_available`, `has_transient_token_refresh`) and `--check-limits` do carry opencode. |
| `.dev-loop/` keyed by `cwd`, not by task | Added 2026-09-04. Two dev-loop tasks in one repo overwrite each other's `round-00N.md`, `summary.md` and `state.json`; the iteration counter restarts per run. `_task_hash()` exists but lives inside `state.json` rather than in the path. Costs traceability, not correctness. |

---

## Design decisions worth keeping

Condensed from the original per-feature specs. These are the choices that are
not obvious from reading the code.

### 1. Skills system
Resolution order is CWD-local → repo-local → vault → bundled, highest wins, and
a shadowed skill logs a warning rather than silently overriding. `pyyaml` is the
one accepted exception to the stdlib-only rule (skills, profiles and policy all
need YAML). Provider-locked skills (`requires.providers`) make the task *wait*
for that provider's reset instead of falling back — a skill that needs Claude is
not satisfied by Codex. *Not done as originally planned*: built-in tools were
NOT migrated into `SKILL.md` files; `tools/registry.py` remained the registry
and skills sit alongside it.

### 2. `--doctor`
One command that validates the whole setup, plus a critical subset on `--watch`
startup (vault, queue, ≥1 provider): FAIL there refuses to start, everything
else warns and continues. `--fix --yes` exists for unattended repair. The
current check list lives in [README](README.md#doctor---doctor) — it has grown
well past the original twelve.

### 3. Memory
`score = similarity × exp(-age_days / half_life)` with a 30-day half-life.
Stored results are **summaries, never full provider output**. The orchestrator —
not the model — decides which memories are relevant; the provider never sees the
memory pool. Compaction is user-initiated only.

### 4. Heartbeat
Local-first: `subprocess`/`shutil`/file mtimes wherever a check can be answered
without an LLM; an LLM only for genuine reasoning (e.g. "summarise yesterday").
If all providers are exhausted, an LLM-dependent check is skipped silently and
retried next interval — a heartbeat never blocks the queue. Items with an
interval ≥ 1 day persist their last run in `logs/heartbeat-state.json`, so a
`## Every 30 days` check does not re-fire on every restart.

### 5. Selective prompt injection
Everything injected is truncated to the *useful* block, never raw-dumped: a large
wikilink file contributes at most its share of ~1 500 tokens (7 500 chars), and often
less because TF-IDF section extraction cuts it further. Budgets per category are in
[README → Prompt Budget](README.md#prompt-budget-token-allocation).

### 6. Execution profiles
Profile settings win over global `policy.yaml`. That is intentional: a profile
is an explicit, named configuration the user wrote deliberately, the global
policy is the default. Multiple `#agent:` tags → first wins, with a warning.

### 7. Parallel sub-agents
Subtasks sharing a CWD run **sequentially within that group**, groups run in
parallel — no two threads write the same working tree. One subtask failing does
not stop the others; the parent gets one aggregated result. `#worktree` (P1)
later added true git-level isolation on top of this.

### 8. SOUL.md
Per-provider sections are re-applied on provider fallback: when a task rotates
from Claude to Codex mid-run, the prompt is rebuilt with the new provider's
section. Task text unchanged, system prompt swapped.

### 9. Policy + approvals
Three tiers (AUTO / APPROVE / DENY) with anti-fatigue measures baked in:
session-wide `/approve-all <category>`, per-task `#approve:push,publish`,
profile-level defaults, and grouping of N related actions into ONE request.
Pre-execution scanning is the primary defence; post-execution scanning of
provider output is an audit trail, not a guard — it cannot undo anything, but a
DENY-tier pattern appearing in output means the provider bypassed expectations,
which is worth knowing. Approval timeout pauses the task instead of skipping it.

### 10. `#shutdown`
Volatile intent only (`threading.Event`, no file persistence — a crash loses the
intent, which is the safe direction). Any incoming Telegram message cancels the
countdown, and the message is still processed normally. A new queue task during
the countdown aborts the shutdown. Double `#shutdown` is idempotent. A failed
`#shutdown` task still triggers the shutdown — the user asked for it *after that
task*, regardless of outcome.

### 29. Queue linter
Stays offline and instant: no LLM calls, no "will this task succeed?"
prediction. Exit codes 0/1/2 make it CI-wireable. *Planned but not implemented*:
tool/profile compatibility checks (e.g. `#tool:deep-security-audit` +
`#agent:readonly`). The `#vibe*` gap (unknown-alias shape + missing-CLI
warning) noted here before was closed 2026-09-02 — see the defect table above.

### 30. Replay JSONL
Path/offset references only — no full prompt or output text in the record, which
would bloat the file and duplicate the per-task result notes in the vault. This
is the data substrate everything analytical sits on (#34, cost forecast,
provider learning), which is why it came before them.

### 31. Idempotency keys
`sha256(source + canonical_payload + bucket_ts)` where `bucket_ts` is the
trigger's natural granularity (Telegram `message_id`, cron scheduled time,
webhook delivery id). Built *before* any cron/webhook trigger, because an
external trigger without dedup becomes a duplicate amplifier the first time the
watchdog restarts mid-write.

### 32. Slash-commands
No interactive wizard, no tag negotiation — put `#claude_opus` in the message
itself. Each command replies with the literal queue line it added so the user
can see and edit it.

### 33. Schedule tags
**Key insight**: a scheduler module was unnecessary. The existing retry
primitive (`<!-- retry: ... -->` + `_retry_is_due()`) already means "this line
becomes active at time T" — cron *is* retry with a future timestamp. So both
tags layer onto it: ~80 LOC instead of 250+, no new persistence, no tick loop,
and the queue file stays the single source of truth (pause a schedule by editing
Markdown). Missed runs replay automatically, which is right for maintenance
jobs — `#freshonly` + `#grace:` later carved out the exception for briefs whose
value expires.

### 34. Failure taxonomy
Deterministic mapping from `error_code` first, keyword heuristics only as a
fallback. The category list has grown past the original sketch — `taxonomy.py`
is authoritative (21 categories as of 2026-09-05; `verify_failed` and
`worktree_dirty` joined on 2026-09-04). Count it, do not quote this number:
`len(taxonomy.ALL_CATEGORIES)`.

### 35. Preflight hooks
Deterministic context collected *before* the LLM call is cheaper than having the
model rediscover basics every iteration. Hard constraint: a hook must be fast
(<5 s, enforced by a thread timeout with graceful skip) — a slow hook would gate
every task.

### 36. Skill suggestion
Never auto-activates. Drafts are inert until a human moves them from
`Skills-Drafts/` to `Skills/`. Gated on the same `(tool, cwd, task-shape)`
repeating N ≥ 3 times in 30 days, with a 90-day per-pattern cooldown — the
mitigation against skill bloat.

### 37. Progressive skill loading
An always-present one-line index of every skill (cheap, helps the model
self-route), full body only for the matched skill, and only the sections
matching the current phase. The `#tool:` tag stays authoritative — no
LLM-driven skill selection at runtime.

### 38. Queue healing
Detects blocked-over-24h, dead-end deps and failed (`[-]`) blockers, then *asks*
via Telegram (`/unblock`, `/drop`, `/retry`). No auto-unblocking — that would
break the supervisor model. Dead tasks silently rotting in a queue is a real
operational pain point that a batch runner would never address.

---

## Deferred — reconsider later

| Idea | Source | Why deferred |
|---|---|---|
| FTS5 memory backend | Hermes | Current corpus is small; TF-IDF + decay is fast enough. Revisit at >50 MB memory. |
| Channel adapter layer | Hermes + OpenClaw | YAGNI. Single channel (Telegram) doesn't justify the abstraction. Revisit when Discord/Slack is concretely on the roadmap. |
| Docker sandbox (general) | OpenClaw | Weeks of work. Revisit only if untrusted code execution becomes a routine use case. Narrow `#sandbox` profile for specific scenarios is acceptable earlier. |
| Telegram trust/pairing layer | OpenClaw | Over-engineered for single-user. Revisit if multi-user becomes a goal. Command-tiers (lax vs. strict commands) is the cheap subset. |
| Risk score per task | AI_orchestrator | Folded into #30 (replay record) + #35 (preflight signals). Standalone scoring layer not needed yet. |
| Task templates | AI_orchestrator | Folded into #32 (slash-commands). Same UX, less infra. |
| Human review pack | AI_orchestrator | Folded into #30. Replay record + a renderer covers it. |
| Local knowledge index per repo | AI_orchestrator | Folded into existing memory system + #35 preflight. Revisit if memory recall accuracy drops. |
| Task cost forecast | AI_orchestrator | Requires #30 data + several months of history. Premature without baseline. |
| Provider learning | AI_orchestrator | Same as cost forecast — needs #30 + history. |
| Plugin-style tool registration | Hermes | `tools/registry.py` is fine. Plugin discovery is a nice-to-have, not a need. |
| Script pre-processing (`#pre:`) | Hermes | Subsumed by #35 preflight hooks (per-tool deterministic context). |

---

## References

- [OpenClaw](https://github.com/openclaw/openclaw) — architecture patterns:
  SOUL.md, Skills, Memory, Heartbeat concepts
- `OPENCLAW_FUTURE_IDEAS.md`, `HERMES_AGENT_FUTURE_IDEAS.md`,
  `AI_ORCHESTRATOR_FUTURE_IDEAS.md` — local planning notes, not tracked in git
- Architecture: [`docs/architecture/components.md`](docs/architecture/components.md),
  [`docs/architecture/patterns.md`](docs/architecture/patterns.md)
