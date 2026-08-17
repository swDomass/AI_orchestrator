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
| 50 | Unattended `#tool:` reactivation — git-state freeze, provider ceiling, uniform error classification, effective safety hook | **Built, not yet proven** — see below |

### #50 — Unattended `#tool:` reactivation (2026-08-15)

The orchestrator had run as a briefing runner only since 2026-04-03: not a single
`#tool:` task in that window. This wave rebuilt the guarantees an unattended tool
run needs, as four separate changes:

1. **Git state is frozen.** Unattended runs leave work in the working tree; the
   user commits and pushes. Enforced by `config.SAFETY_DENY_PATTERNS` (hook, Claude
   path) and by `approve:` entries in `policy.yaml`. *Not* by the prompt layer —
   `SAFETY_RULES` mentions push only.
2. **Provider ceiling.** `openrouter` and `vibe` are barred for tool tasks on the
   chain, on an explicit tag, and on tool-internal lookups; Gemini left the chain
   entirely.
3. **Uniform error classification.** All error handling goes through
   `providers.base.error_code_of()` / `is_transient()`, and a format break is a
   retryable `format_error` instead of a silently completed task.
4. **The safety hook actually blocks.** It had emitted an output shape Claude Code
   recognises as neither "block" nor "deny", so every block since it was written
   was a no-op.

**Why this is not DONE:** the queue contains **no `#tool:` task**, so none of it has
executed unattended even once. The path is repaired and tested, not demonstrated.
The honest status is "ready for a first supervised `#tool:` run", and that run is
the acceptance criterion.

**Review coverage is partial.** The change set was reviewed by Mistral (`vibe`) —
four findings, all fixed, one (git aliases) refuted at the system level and
deliberately not built. **Codex could not run**: usage limit exhausted, free again
2026-08-20. So this diff has seen *one* external voice, not the usual two. It is
not "externally reviewed" in the sense the rest of this repo means it.

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

| Defect | Impact |
|---|---|
| `select_provider()` fail-open for a bare `#vibe`/`#openrouter` tag | The pay-per-token ceiling was made fail-closed in `policy_allows_provider()`/`dispatcher._allows()`, but not on the forced-provider path. With `policy.yaml` missing or unreadable, an explicitly tagged task still reaches the uncapped provider. Left open deliberately — the fix reworks ~10 routing tests. |
| `stdin_incomplete` requeues without bound | `<!-- hang: N -->` is the only persistent per-task counter and only `hang` + `format_error` *increment* it. Every other error code requeues without raising it. Count unbounded, rate still throttled by the 5-min cooldown. Pre-existing. Narrowed 2026-08-15: those parks no longer *reset* the counter either, so a task alternating between real failures and parks does reach the cap. |
| `realign_stale_freshonly()` skips a slot inside its grace window | A stale `#freshonly` task realigns strictly after now, so today's still-valid slot is skipped. Morning brief fails silently — no log line, no `runs.jsonl` record. Observed 2026-07-23 and 2026-07-27. |
| Safety-hook residuals | `find . -exec git push`, `docker exec c git push`, `xargs git push` are not recognised (needs real argv parsing, not a regex). A heredoc body line starting with a git write command matches — a deliberate false positive in the safe direction. Hook covers Claude only; Codex relies on its own sandbox flag. |
| `#pass1:`/`#pass2:` accept only `claude`, `gemini`, `codex` | Own regex, predates the opt-in providers. `#pass2:vibe`/`#pass2:openrouter` are ignored rather than rejected. Use `#second_opinion:`. |
| `queue_linter.py` does not validate `#vibe*` tags | Its unknown-alias regex enumerates `claude\|gemini\|codex\|or` only, and there is no "CLI missing" warning to match the OpenRouter one. Linter-only since 2026-08-15 — runtime resolves the alias correctly. |
| `tests/test_telegram_listener.py` order-dependent; `tests/test_usage_suggester.py` environment-dependent | Run with `-p no:randomly`. A red suite in a fresh worktree is more likely one of these two than a real regression. |

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
Everything injected is truncated to the *useful* block, never raw-dumped: a
10 000-token wikilink file contributes the 500–1000 tokens that matter. Budgets
per category are in [README → Prompt Budget](README.md#prompt-budget-token-allocation).

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
`#agent:readonly`), plus the `#vibe*` gaps noted in the defect table above.

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
is authoritative (19 categories today).

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
