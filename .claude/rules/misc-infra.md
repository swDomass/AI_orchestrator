---
paths:
  - "notifier.py"
  - "telegram_listener.py"
  - "idempotency.py"
  - "session_registry.py"
  - "replay.py"
  - "taxonomy.py"
  - "preflight.py"
  - "skill_suggester.py"
  - "skills/index.py"
  - "gh_helpers.py"
  - "ci_watcher.py"
  - "parallel_runner.py"
  - "run_orchestrator.ps1"
---
# Infrastruktur: Telegram, Replay, Taxonomy & mehr

Ausgelagert aus CLAUDE.md am 2026-09-21; laedt automatisch bei Zugriff auf notifier.py, telegram_listener.py, idempotency.py, session_registry.py, replay.py, taxonomy.py, preflight.py, skill_suggester.py, skills/index.py, gh_helpers.py, ci_watcher.py, parallel_runner.py, run_orchestrator.ps1.

- **`notifier.py`** — Telegram notifications, 3500-char truncation
- **`telegram_listener.py`** — Bot listener, `/chat` AI mode, slash tool-commands (`/review` `/security` `/audit` `/dev` `/critique` `/brainstorm`), `/approve` SI-Manager routing; P5 `/pr-fix <owner/repo#N>` + `/pr-ignore <owner/repo#N>` for PR-Babysitter report-only mode
- **`idempotency.py`** — Duplicate-trigger dedup (JSONL store, sha256 keys, 30-day retention)
- **`session_registry.py`** — Append-only JSONL whitelist of orchestrator-created Claude session UUIDs
- **`replay.py`** — Machine-readable run summaries (`logs/runs.jsonl`), one record per task end (ok/retry/error/blocked), 30-day rotation → gzip archive
- **`taxonomy.py`** — Classifies replay records into **22** failure categories (rate_limit, timeout, auth_error, model_refusal, stdin_incomplete, etc.) for analytics + retry logic. It was 19 until 2026-09-04, when `verify_failed` (`6c8479c`) and `worktree_dirty` (`e73ac19`) joined; both are deliberately their own category rather than `tool_internal_error`, because each names a failure the machinery cannot see — "the run was clean and the WORK did not happen", and "an environment precondition was violated, so the task never started". `verify_missing` joined 2026-09-16, split out of `verify_failed` for the same reason one level up: "no check ever ran at all" (a `#verify:` script that does not exist — a queue/config defect, 8× since 2026-09-05 read as "task did not deliver") is not the same failure as "the check ran and the artefact is missing". Counted 2026-09-16: `len(taxonomy.ALL_CATEGORIES) == 22`
- **`preflight.py`** — Per-tool deterministic context collectors (git status, manifests, file histograms), 5s timeout, day-cached at `{cwd}/.<tool>/preflight-{date}.md`
- **`skill_suggester.py`** — Pattern-gated draft generator: N>=3 occurrences of (tool, cwd, task-shape) in 30 days → SKILL.md draft to `Skills-Drafts/`, 90-day cooldown, never auto-activates
- **`skills/index.py`** — Progressive skill loading: always-present INDEX block + lazy section extraction by phase name
- **`gh_helpers.py`** — Thin subprocess wrapper around `gh` CLI (typed errors: `gh_not_found`/`gh_auth`/`gh_timeout`/`gh_not_found_repo`); shared by PR-Babysitter (P2/P5) and CI-Watcher (P4)
- **`ci_watcher.py`** — P4 CI-Failure-Watcher: `sweep_once()` lists failed GitHub-Action runs per repo, dedups by `(repo, headSha)`, queues `#tool:dev-loop` task per new failure, persistent state in `logs/ci-watcher-state.json`
- **`parallel_runner.py`** — P1 worktree-isolation helpers (`_is_clean_git_repo`, `_create_worktree`, `_remove_worktree`) — one worktree per CWD group, retained on failure, removed on success unless `#keep-worktree`
- **`run_orchestrator.ps1`** — Crash-resistant watchdog (PS 5.1+7+), exponential backoff, Telegram alerts
- **Worktree isolation (P1)** — `#worktree` on parent → one `git worktree add --detach` per CWD group; subtask cwd rewritten; failed groups retain the worktree (path appended to error)
