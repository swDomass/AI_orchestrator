# AI Orchestrator ÔÇö Feature Roadmap

Inspired by patterns from OpenClaw and our own ideas.
Prioritized by usefulness, effort, and synergy with existing infrastructure
(Obsidian vault, Telegram, 3 CLI providers, queue system).

**Guiding principle ÔÇö Human-in-the-Loop**:
Maximum autonomy for routine work (file edits, git commits, tests, tool loops).
Telegram-based approval ONLY for irreversible/dangerous actions (push, publish,
delete outside CWD, CI changes). No approval fatigue ÔÇö blanket session approvals,
per-task pre-approval tags, and smart grouping. See Feature #9 for full design.

---

## Tier 1 ÔÇö High Impact, Builds on What We Have

| # | Feature | Status |
|---|---------|--------|
| 1 | Skills system + auto-discovery + gating | DONE |
| 2 | `--doctor` / onboarding command | DONE |
| 3 | Memory system with temporal decay | DONE |
| 4 | Heartbeat / proactive scheduled tasks | DONE |
| 10 | `#shutdown` — graceful OS shutdown via Telegram or queue tag | DONE |
| 20 | Increased Telegram output (3.5k) + context-aware AI chat | DONE |


## Tier 2 ÔÇö Strong Value, Moderate Effort

| # | Feature | Status |
|---|---------|--------|
| 5 | Selective skill/prompt injection | DONE |
| 6 | Execution profiles (`#agent:work`, `#agent:personal`) | DONE |
| 7 | Parallel sub-agent spawning | DONE |
| 8 | SOUL.md / personality-as-config | DONE |
| 9 | Execution policy + approval layer | DONE |
| 10b | Usage Suggester (proactive task suggestions) | DONE |

## Tier 3 ÔÇö Nice to Have, After Core Is Solid

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

## Tier 4 ÔÇö Overkill for Now

| # | Feature | Status |
|---|---------|--------|
| 15 | Gateway / WebSocket architecture | deferred |
| 16 | Multi-channel inbox (WhatsApp/Slack/Discord) | deferred |
| 17 | Voice / Canvas / Device nodes | deferred |

---

## Recommended Build Order

```
Phase 1 (foundations)     ÔåÆ Skills, --doctor, SOUL.md
Phase 2 (intelligence)    ÔåÆ Memory, Heartbeat, Selective injection
Phase 3 (power features)  ÔåÆ Profiles, Parallel spawning, Policy layer
Phase 4 (hardening)       ÔåÆ Docker sandbox, Tool policy, Dashboard
```

---

## Detailed Plans ÔÇö Tier 1 & Tier 2

### 1. Skills System + Auto-Discovery + Gating

**Goal**: Replace hardcoded `tools/registry.py` with a file-based skill discovery
system that reads from the Obsidian vault and repo-local directories.

**Existing infrastructure**:
- `99_System/AI/Skills/` already has 11 skills with `SKILL.md` files
- `tools/registry.py` currently does static tool registration
- `#tool:<name>` tags in queue items already route to tools

**Design**:

```
Skill resolution order (highest priority wins):
  1. Task CWD:    <cwd>/.orchestrator/skills/<name>/SKILL.md
  2. Repo-local:  ./skills/<name>/SKILL.md
  3. Vault:       99_System/AI/Skills/<name>/SKILL.md
  4. Bundled:     ./tools/<name>/  (current built-in tools)
```

**YAML parsing**: Use `pyyaml` (`pip install pyyaml`). Acceptable exception to the
no-external-deps rule ÔÇö YAML is used across Skills, Profiles, and Policy configs.
Add to `requirements.txt`.

**SKILL.md format** (YAML frontmatter + Markdown body):

```yaml
---
name: review-loop
description: Iterative code review with P1/P2/P3 findings
version: 1.0
requires:
  bins: []              # e.g. ["pytest", "docker"]
  env: []               # e.g. ["OPENAI_API_KEY"]
  os: []                # e.g. ["win32", "linux"]
  providers: []         # e.g. ["claude"] ÔÇö only run on these (task waits for reset)
tags: ["review", "quality"]
config:
  max_iterations: 10
  timeout_minutes: 20
---

## System Prompt Addition

You are performing an iterative code review...

## Steps

1. Run initial review, classify findings as P1/P2/P3
2. Fix P1 issues first, then P2
3. Re-review until clean or max iterations reached
```

**Implementation steps**:

1. Create `skills/discovery.py`:
   - `discover_skills(cwd, vault_path)` ÔåÆ scans all 4 locations
   - Returns `dict[str, SkillConfig]` with precedence applied
   - Parses YAML frontmatter from each `SKILL.md`
   - **Shadowing warning**: when a higher-priority location overrides a
     lower-priority skill of the same name, log a warning (e.g.
     "Skill 'review-loop' in repo-local shadows vault version")

2. Create `skills/gating.py`:
   - `check_requirements(skill)` ÔåÆ validates bins (shutil.which),
     env vars (os.environ), OS (sys.platform), provider availability
   - Returns `(available: bool, reasons: list[str])`
   - **Provider-locked skills** (`requires.providers`): if the required provider
     is rate-limited, the task waits for that provider to reset ÔÇö no fallback
     to other providers. Uses existing `mark_retry(task, reset_at)` mechanism.

3. Create `skills/loader.py`:
   - `load_skill(name)` ÔåÆ returns skill config + prompt content
   - Caches loaded skills per session

4. Migrate existing tools:
   - Convert `tools/review_loop.py` ÔåÆ `skills/review-loop/SKILL.md`
   - Convert `tools/test_loop.py` ÔåÆ `skills/test-loop/SKILL.md`
   - Keep Python implementation files alongside SKILL.md for complex logic
   - Update `tools/registry.py` to delegate to `skills/discovery.py`

5. Update `queue_manager.py`:
   - `#tool:<name>` tag resolution goes through skill discovery
   - Unknown skill names ÔåÆ `mark_done` with failure reason, notify via Telegram.
     Task is removed from queue (marked failed) so it doesn't block other tasks.

**Files to create**: `skills/__init__.py`, `skills/discovery.py`,
`skills/gating.py`, `skills/loader.py`
**Files to modify**: `tools/registry.py`, `queue_manager.py`, `config.py`

---

### 2. `--doctor` / Onboarding Command

**Goal**: Single command to validate the entire setup ÔÇö CLIs, auth, vault, Telegram,
skills prerequisites.

**Implementation steps**:

1. Add `doctor.py` module with check functions:

   ```python
   checks = [
       ("Claude CLI",    check_claude_cli),     # shutil.which("claude")
       ("Gemini CLI",    check_gemini_cli),      # shutil.which("gemini")
       ("Codex CLI",     check_codex_cli),       # shutil.which("codex")
       ("Node.js",       check_node),            # shutil.which("node")
       ("cclimits",      check_cclimits),        # subprocess cclimits --json
       ("Claude auth",   check_claude_auth),     # run claude --version or similar
       ("Vault path",    check_vault_path),      # os.path.isdir(VAULT_PATH)
       ("Queue file",    check_queue_file),      # os.path.isfile(QUEUE_FILE)
       ("Telegram bot",  check_telegram),        # GET /getMe with bot token
       ("Git",           check_git),             # shutil.which("git")
       ("Skills",        check_skills),          # discover + gate all skills
       (".env file",     check_dotenv),          # exists + has required keys
   ]
   ```

2. Each check returns `(status: pass|warn|fail, message: str)`

3. Output format:
   ```
   AI Orchestrator ÔÇö Doctor
   ========================
   [PASS] Claude CLI .......... claude 1.x at /usr/bin/claude
   [PASS] Gemini CLI .......... gemini 0.x at /usr/bin/gemini
   [FAIL] Codex CLI ........... not found in PATH
   [PASS] cclimits ............ cclimits --json OK
   [WARN] Telegram bot ........ token set but getMe returned 401
   [PASS] Vault path .......... C:\Users\you\obsidian_vault exists
   [PASS] Skills .............. 11 discovered, 9 available, 2 gated
   ```

4. Add `--doctor` flag to `orchestrator.py` argparse
5. Also run a subset of checks on `--watch` startup:
   - **Critical checks** (vault path, queue file, at least 1 provider): if any
     FAIL ÔåÆ refuse to start, print error, exit with non-zero code + Telegram warning
   - **Non-critical checks** (Telegram, git, skills): WARN only, continue startup

6. **Auto-fix mode** (`--doctor --fix`):
   - For each FAIL, offer a fix action if possible:
     - Missing CLI ÔåÆ suggest install command (e.g. `npm install -g @anthropic/claude-code`)
     - Missing `.env` ÔåÆ create template with placeholder keys
     - Missing queue file ÔåÆ `ensure_queue_file()` (already exists)
     - Missing vault dirs ÔåÆ `os.makedirs()`
   - Interactive: print the fix command, ask `Apply? [y/N]`, execute on confirmation
   - Non-interactive (`--doctor --fix --yes`): apply all fixes without prompting

**Files to create**: `doctor.py`
**Files to modify**: `orchestrator.py` (argparse)

---

### 3. Memory System with Temporal Decay

**Goal**: Persistent memory across runs ÔÇö store task results, error patterns,
and learned context. Use semantic search with temporal decay so recent
memories rank higher.

**Storage location**: `99_System/AI/memory/` in the Obsidian vault

**Design**:

```
99_System/AI/memory/
  Ôö£ÔöÇÔöÇ task_results/        # One .md per completed task
  Ôöé   ÔööÔöÇÔöÇ 2026-02-25_review-loop_projectX.md
  Ôö£ÔöÇÔöÇ error_patterns/      # Recurring errors + solutions
  Ôöé   ÔööÔöÇÔöÇ rate_limit_recovery.md
  Ôö£ÔöÇÔöÇ preferences/         # Learned user/project preferences
  Ôöé   ÔööÔöÇÔöÇ project_defaults.md
  ÔööÔöÇÔöÇ index.md             # Summary + links (auto-generated)
```

**Implementation steps**:

1. Create `memory.py` module:
   - `store_result(task, result, provider, duration, cwd)` ÔåÆ writes MD file
     - **Truncated summary only**: never store full provider output. The
       orchestrator uses a cheap LLM (Gemini ÔåÆ Codex ÔåÆ Haiku) to summarize the
       result into ~200-500 tokens before storing. If no LLM available, fall back
       to first 500 chars + last 200 chars of raw output.
   - `search_memory(query, top_k=5)` ÔåÆ semantic search + temporal decay
   - `get_context_for_task(task_text)` ÔåÆ returns relevant past results

2. Temporal decay scoring:
   ```python
   score = similarity * exp(-age_days / half_life)
   # half_life = 30 days (configurable)
   # 1 day old: 97% weight, 7 days: 79%, 30 days: 50%, 90 days: 12%
   ```

3. Search implementation (two options, start simple):
   - **Phase A**: Keyword search with TF-IDF (no deps, stdlib only)
   - **Phase B**: Smart Connections MCP if available, fall back to Phase A

4. Integration points:
   - `orchestrator.py`: after task completion, call `store_result()`
   - Prompt building (`orchestrator._build_prompt`): call `get_context_for_task()`
     and inject relevant memories. **The orchestrator decides** which memories are
     relevant (keyword matching + temporal decay scoring) ÔÇö the AI provider never
     sees the full memory pool, only pre-selected snippets.
   - Memory injection capped at ~2000 tokens to preserve context budget
   - **Generic tasks with no keyword matches**: inject the most recent memories
     from the same CWD/project as context. If CWD also doesn't match, inject
     the N most recent memories overall (project-relevance > recency > nothing).

5. Auto-cleanup: memories older than `MAX_MEMORY_AGE` (default 180 days)
   get archived to `memory/archive/`

6. **Compaction triggers** (user-initiated, never automatic):
   - `#comp_week` tag in queue or Telegram ÔåÆ summarize the past 7 days of
     task results into a single weekly summary file, delete originals
   - `#comp_month` tag ÔåÆ same for the past 30 days
   - Before executing: send Telegram preview ("Compacting 42 task results
     from last week into summary. Proceed? /approve or /deny")
   - Only compact after user confirmation
   - Summary format: one `.md` file per period, grouped by project/CWD,
     with key outcomes and error patterns preserved

**Files to create**: `memory.py`
**Files to modify**: `orchestrator.py`, `queue_manager.py`, `config.py`

---

### 4. Heartbeat / Proactive Scheduled Tasks

**Goal**: In `--watch` mode, periodically evaluate a checklist and execute
proactive maintenance tasks.

**Config location**: `99_System/AI/HEARTBEAT.md` in the vault

**HEARTBEAT.md format**:

```markdown
# Heartbeat Checks

## Every 30 minutes
- [ ] Check if queue has been empty for >2 hours ÔåÆ notify via Telegram
- [ ] Check git status in active project dirs ÔåÆ warn about uncommitted changes

## Every 2 hours
- [ ] Run `--check-limits` and log to memory
- [ ] Check disk space on project drives

## Daily (first run after 08:00)
- [ ] Summarize yesterday's completed tasks ÔåÆ post to Telegram
- [ ] Check for stale branches (>7 days) in project repos
```

**Implementation steps**:

1. Create `heartbeat.py` module:
   - Parse `HEARTBEAT.md` for check items with frequency tags
   - `HeartbeatRunner` class with `last_run` tracking per item
   - `should_run(item)` ÔåÆ checks frequency vs last execution

2. Integration with `--watch` loop:
   - After each queue poll cycle, check if any heartbeat items are due
   - Execute due items as lightweight tasks (shorter timeout, no git snapshot)
   - Log results to memory system (feature #3)

3. Heartbeat items are NOT queue tasks ÔÇö they run in a separate lightweight
   path, don't modify the queue file, and use Telegram for output only

4. **Execution strategy ÔÇö local-first**:
   - **Prefer local/stdlib execution** wherever possible: `subprocess` for git
     status, `shutil.disk_usage()` for disk space, file mtime checks for
     staleness, `read_queue()` for queue monitoring. No LLM needed for these.
   - **LLM fallback** only when the check requires reasoning (e.g. "summarize
     yesterday's tasks"). Provider priority for heartbeat: Gemini ÔåÆ Codex ÔåÆ
     Claude with `--model haiku`. Use cheapest/fastest provider first.
   - **If all providers exhausted**: skip the LLM-dependent heartbeat item
     silently. It will be retried at the next interval. Never block the main
     queue for a heartbeat.

5. **Standalone operation**: Heartbeat works without the Memory system (Feature #3).
   If Memory is not implemented yet, skip memory-dependent checks (e.g. "summarize
   yesterday's tasks") and only run local checks. No hard dependency.

6. **Shutdown interaction**: If `shutdown_pending` is set and a heartbeat check is
   due, the heartbeat still runs first ÔÇö shutdown countdown is paused until the
   heartbeat completes. Heartbeats are fast (local checks: <1s, LLM checks: <30s),
   so the delay is negligible.

**Files to create**: `heartbeat.py`
**Files to modify**: `orchestrator.py` (watch loop), `config.py`

---

### 5. Selective Skill/Prompt Injection

**Goal**: Only inject relevant skill prompts per task instead of the full
system prompt. Saves context window budget.

**Current state**: `config.py` has a monolithic `SAFETY_RULES` string injected
into every task regardless of content.

**Design**:

1. Split system prompt into layers:
   - **Core** (~200 tokens): Always injected ÔÇö safety rules, CWD, identity
   - **Skill-specific** (variable): Only when `#tool:` tag matches
   - **Context** (variable): Memory results, wikilink content

2. Skill prompt comes from `SKILL.md` body (below frontmatter)

3. Budget allocation:
   ```
   Total context budget: ~8000 tokens for injected content
   Core prompt:         ~200 tokens  (always)
   Memory context:      ~2000 tokens (from feature #3)
   Wikilink context:    ~3000 tokens (existing feature)
   Skill prompt:        ~2000 tokens (only matched skill)
   Remaining:           ~800 tokens  (buffer)
   ```

4. If multiple skills match (future: `#tool:review-loop,test-loop`),
   budget is split proportionally

5. **Aggressive token saving ÔÇö truncate to useful blocks**:
   - All injected content (wikilinks, memory, skill prompts) is truncated to
     only the useful/relevant sections, never raw-dumped in full.
   - **Wikilink files**: extract only sections relevant to the task (heading
     matching, keyword proximity). If no match, take the first N lines as summary.
   - **Memory results**: already pre-filtered by orchestrator (see Feature #3).
   - **Skill prompts**: trim examples/steps that don't apply to the current task.
   - **Principle**: save tokens wherever possible without compromising quality.
     A 10,000-token wikilink file should never be injected as-is ÔÇö extract the
     500-1000 tokens that actually matter.

**Implementation steps**:

1. Refactor `config.py`: split `SAFETY_RULES` into `CORE_PROMPT` + skill prompts
2. Update prompt assembly in `orchestrator.py`:
   - `build_prompt(task, skill, memory_context, wikilink_context)`
   - Token counting with simple `len(text.split())` heuristic
   - Truncate ALL categories to budget ÔÇö no category gets a free pass
   - Smart truncation: extract relevant blocks, not just `text[:limit]`
3. Each provider's `run()` method receives the assembled prompt

**Files to modify**: `config.py`, `orchestrator.py`, `queue_manager.py` (inject_file_context), providers

---

### 6. Execution Profiles (`#agent:<name>`)

**Goal**: Named configs that bundle provider order, allowed roots, tools,
timeouts, and sandbox settings. Routed via `#agent:work` tag in queue.

**Config location**: `99_System/AI/profiles/` or `config.py`

**Profile format** (YAML):

```yaml
# profiles/work.yaml
name: work
providers: [claude, gemini]       # provider priority order
allowed_roots:
  - D:\programmieren\work
  - D:\projekte
allowed_skills: [review-loop, test-loop]
denied_skills: [deploy]
timeout_minutes: 10
sandbox: off                       # off | ro | rw
safety_level: strict               # strict | standard | yolo
```

**Implementation steps**:

1. Create `profiles.py`:
   - `load_profile(name)` ÔåÆ reads YAML from vault or repo (uses `pyyaml`,
     same dependency as Skills)
   - `ProfileConfig` dataclass with all fields + defaults
   - Default profile = current hardcoded config values

2. Update `queue_manager.py`:
   - Parse `#agent:<name>` tag from task line
   - **If multiple `#agent:` tags**: first one wins, others are ignored.
     Log a warning so the user knows.
   - Pass profile to dispatcher and orchestrator

3. Update `dispatcher.py`:
   - `select_provider()` respects profile's provider order
   - Profile's `allowed_roots` overrides global config

4. Update `orchestrator.py`:
   - Timeout, safety level, sandbox from profile
   - Skill filtering via `allowed_skills` / `denied_skills`

5. **Profile vs global policy**: Profile settings win over global `policy.yaml`.
   A profile with `safety_level: yolo` can override DENY rules from global policy.
   This is intentional ÔÇö profiles are explicit, named configurations that the user
   creates with full awareness. The global policy is the default, profiles are
   the override.

**Files to create**: `profiles.py`, example profile YAMLs
**Files to modify**: `queue_manager.py`, `dispatcher.py`, `orchestrator.py`

---

### 7. Parallel Sub-Agent Spawning

**Goal**: Allow a single task to fan out subtasks across multiple providers
simultaneously.

**Queue syntax**:

```markdown
- [ ] Review, test, and document project X #parallel
  - review code #claude #tool:review-loop
  - run tests #codex #tool:test-loop
  - update README #gemini
```

**Implementation steps**:

1. Update `queue_manager.py`:
   - Detect `#parallel` tag + indented sub-items
   - Parse into `ParallelTask` with list of `SubTask` objects

2. Create `parallel_runner.py`:
   - `run_parallel(subtasks)` ÔåÆ launches each in a thread
   - Each thread uses `dispatcher.select_provider()` with forced provider
   - Collects `RunResult` from each, waits for all to finish
   - Aggregates results into single output

3. Thread safety:
   - Provider cooldown locks already exist (BaseProvider._lock)
   - File locking on queue already exists
   - **No simultaneous file access**: subtasks that share the same CWD
     are NOT allowed to run in parallel. The parallel runner must validate
     this at parse time:
     - If all subtasks have distinct `cwd:` tags ÔåÆ run in parallel
     - If any two subtasks share a CWD (or have no CWD, defaulting to the
       parent task's CWD) ÔåÆ run them sequentially within that CWD group,
       parallel across groups
   - This prevents merge conflicts, file corruption, and race conditions

4. Failure handling:
   - If one subtask fails, others continue
   - Aggregated result shows per-subtask status
   - Failed subtasks can be retried individually

5. **Completion**: single `mark_done` on the parent task with aggregated output
   from all subtasks. One entry under `## Ergebnisse` with per-subtask sections:
   ```
   ### Review, test, and document project X
   **Subtask 1** (claude, review-loop): PASS ÔÇö 3 findings fixed
   **Subtask 2** (codex, test-loop): PASS ÔÇö 12/12 tests green
   **Subtask 3** (gemini): FAIL ÔÇö rate limited
   ```

**Files to create**: `parallel_runner.py`
**Files to modify**: `queue_manager.py`, `orchestrator.py`

---

### 8. SOUL.md / Personality-as-Config

**Goal**: Move system prompts from `config.py` to editable Markdown in the vault.

**Location**: `99_System/AI/SOUL.md`

**Format**:

```markdown
# AI Orchestrator ÔÇö Soul

## Identity
You are an autonomous task executor working inside an Obsidian vault.
You execute tasks from a queue with careful attention to safety.

## Safety Rules
- Never delete files without explicit instruction
- Never force-push to any branch
- Always create a git snapshot before modifying code
- ...

## Communication Style
- Be concise, report results not process
- Use German for Telegram notifications
- Include file paths and line numbers in code references

## Per-Provider Overrides

### Claude
- You have full tool access (Read/Write/Edit/Bash/Glob/Grep)

### Gemini
- You are in yolo mode, auto-approve all actions

### Codex
- You are in full-auto exec mode
```

**Implementation steps**:

1. Update `config.py`:
   - `load_soul(vault_path)` ÔåÆ reads and parses `SOUL.md`
   - Falls back to current hardcoded `SAFETY_RULES` if file missing
   - Caches content, reloads on file change (mtime check)

2. Support per-provider sections:
   - Parse `### <ProviderName>` headers
   - Merge base soul + provider-specific section
   - **On provider fallback** (rate limit mid-task): rebuild the prompt with the
     new provider's soul section before retrying. The task text stays the same,
     only the system prompt changes.

3. Update provider `run()` methods to use loaded soul

**Files to modify**: `config.py`, `orchestrator.py`
**Files to create**: `99_System/AI/SOUL.md` in vault

---

### 9. Execution Policy + Approval Layer (Human-in-the-Loop)

**Goal**: Maximum autonomy for routine work, Telegram-based approval ONLY for
genuinely dangerous or irreversible actions. No approval fatigue.

**Core philosophy ÔÇö 3 tiers**:

```
AUTO (no confirmation needed ÔÇö the 95% case):
  - Read/write/edit files in allowed CWD roots
  - git add, commit, branch, checkout, stash
  - Run tests, linters, formatters
  - Install dev dependencies (npm install, pip install)
  - Create/edit Obsidian notes
  - All tool-loop iterations (review, test)

APPROVE (Telegram confirmation required ÔÇö rare, irreversible):
  - git push (any remote)
  - npm publish / pypi upload / docker push
  - Delete files outside task CWD
  - Modify CI/CD configs (.github/workflows, Dockerfile)
  - Run database migrations
  - Send emails / post to external APIs
  - Any command matching custom regex patterns

DENY (always blocked ÔÇö catastrophic):
  - rm -rf / or equivalent
  - git push --force to main/master
  - DROP TABLE / DROP DATABASE
  - Format disk / kill system processes
  - Disable security features
```

**Key design decisions to avoid approval fatigue**:

1. **Blanket approvals per session**: User can reply `/approve-all push`
   to auto-approve all git pushes for the current session. Resets on restart.

2. **Per-task pre-approval via queue tags**: Tasks can declare expected
   risky actions upfront:
   ```markdown
   - [ ] Deploy feature X #approve:push,publish cwd:/d/project
   ```
   These are approved when the task is queued ÔÇö no runtime interruption.

3. **Profile-level defaults**: The `work` profile might auto-approve
   `git push` but require approval for `npm publish`. The `readonly`
   profile blocks all writes.

4. **Smart grouping**: If a task triggers 5 file deletions, send ONE
   approval request listing all files, not 5 separate messages.

5. **Approval timeout**: 10 min default ÔåÆ deny + pause task (not skip).
   Task stays in queue for retry after user reviews.

**Telegram approval UX**:

```
­ƒöÆ Approval required

Task: "Deploy feature X"
Action: git push origin feature/x

Reply:
  /approve       ÔÇö allow this action
  /approve-all push ÔÇö allow all pushes this session
  /deny          ÔÇö block and pause task
  /skip          ÔÇö block this action, continue task
```

**Policy config** (YAML, in vault or profile):

```yaml
# 99_System/AI/policy.yaml  (or per-profile)
auto:
  - "git add *"
  - "git commit *"
  - "pytest *"
  - "npm install *"
  - "pip install *"

approve:
  - pattern: "git push"
    message: "Push to {remote}/{branch}"
  - pattern: "rm -rf"
    message: "Recursive delete: {path}"
  - pattern: "npm publish"
  - pattern: "docker push"

deny:
  - "git push --force (main|master)"
  - "rm -rf /"
  - "DROP (TABLE|DATABASE)"

session_preapprovals: []  # populated at runtime via /approve-all
```

**Implementation steps**:

1. Create `policy.py`:
   - `PolicyEngine` loads rules from profile + global policy.yaml
   - Three-tier classification: `check(cmd)` ÔåÆ `auto | approve | deny`
   - Pattern matching via regex, with variable extraction for messages
   - Session state: `preapprovals: set[str]` (e.g. {"push", "publish"})

2. Create approval flow in `telegram_listener.py`:
   - New commands: `/approve`, `/approve-all <category>`, `/deny`, `/skip`
   - `request_approval(action, context)` ÔåÆ sends message, blocks on
     `threading.Event` with timeout
   - Smart grouping: buffer multiple approval requests within 2s window,
     send as single message

3. Queue tag parsing in `queue_manager.py`:
   - `#approve:push,publish` ÔåÆ pre-approve these categories for task

4. Integration in `orchestrator.py` ÔÇö **pre-execution is top priority**:
   - **Pre-execution** (primary defense): scan task text, skill config, and
     `#approve:` tags for risky patterns BEFORE sending to any provider.
     - DENY matches ÔåÆ reject task immediately, notify via Telegram, mark failed
     - APPROVE matches without pre-approval ÔåÆ request Telegram confirmation,
       block until approved/denied/timeout
     - AUTO matches ÔåÆ proceed silently
   - **Post-execution** (audit trail): scan provider output for commands that
     were actually run. Can't undo damage, but:
     - Log all detected risky commands to audit log
     - Notify via Telegram if a DENY-tier pattern appears in output
       (indicates the provider bypassed expectations ÔÇö important to know)
     - This is secondary effort ÔÇö implement after pre-execution is solid

5. Logging:
   - All approval decisions logged to `memory/audit_log.md`
   - Format: `[timestamp] APPROVED/DENIED/AUTO action (by: user/policy/timeout)`
   - Post-execution findings: `[timestamp] DETECTED action in output (task: ...)`

**Files to create**: `policy.py`, `99_System/AI/policy.yaml`
**Files to modify**: `orchestrator.py`, `telegram_listener.py`, `queue_manager.py`

---

### 10. `#shutdown` ÔÇö Graceful OS Shutdown via Telegram or Queue Tag

**Goal**: Allow the user to trigger a safe computer shutdown by typing `#shutdown`
in a Telegram message or embedding it as a tag in a queue task. If the app is in
standby (queue empty), it proactively starts a countdown and asks for confirmation
via Telegram. Any incoming reply cancels the shutdown.

**Trigger sources**:

| Source | Example | Behavior |
|---|---|---|
| Telegram message | `"done for today #shutdown"` | Sets pending flag, replies "Shutdown scheduled." |
| Queue task tag | `- [ ] Build project X #shutdown` | After THIS task completes, triggers countdown |
| Queue drains | `shutdown_pending` set, queue empties | Proactively starts countdown immediately |

**State machine**:

```
IDLE
  Ôöé  user sends #shutdown (Telegram or queue tag)
  Ôû╝
SHUTDOWN_PENDING  ÔöÇÔöÇ task in progress? wait for it to finish
  Ôöé               ÔöÇÔöÇ queue empty? start countdown immediately
  Ôû╝
COUNTDOWN (60s)   ÔöÇÔöÇ any incoming Telegram message ÔåÆ IDLE (cancelled)
  Ôöé
  Ôû╝
EXECUTE OS shutdown
```

**Key design decisions**:

1. **Queue task with `#shutdown` and more tasks after it**: Shutdown starts immediately
   after that specific task. Remaining queue items are skipped but stay in the queue
   for the next orchestrator session. If countdown is cancelled, the watch loop resumes
   and remaining tasks are processed normally.

2. **Cancellation ÔÇö any reply**: During countdown, any incoming message from the
   authorized chat (command or plain text) sets `shutdown_cancel_event`. The listener
   still processes the message normally (e.g. `/status` still shows status), but the
   countdown is also cancelled. `/cancel-shutdown` is an explicit command for this.

3. **Countdown notification**: Single message: "Shutting down in 60s. Send any message
   to cancel." No intermediate countdown messages. Followed by "Shutdown cancelled."
   or "Shutting down now."

4. **New task during countdown aborts shutdown**: If a new task arrives in the queue
   while the countdown is running (Flow C), the countdown aborts, `shutdown_pending`
   is cleared, and the orchestrator processes the new task normally.

5. **Double `#shutdown` is idempotent**: If `shutdown_pending` is already set, a second
   `#shutdown` (from any source) is silently ignored ÔÇö no timer reset, no duplicate
   notification.

6. **Task failure still triggers shutdown**: If a `#shutdown`-tagged task fails
   (provider exhausted, error, `mark_retry`), the shutdown still triggers. The user
   asked for shutdown after that task, regardless of outcome.

7. **Shared module**: `_execute_shutdown()` and related logic live in a new
   `shutdown.py` module that both `orchestrator.py` and `telegram_listener.py` import.
   This avoids circular imports and callback wiring.

8. **Pause overrides shutdown**: If `/pause` is active, the shutdown countdown does
   not start. `shutdown_pending` stays set but waits. Once `/resume` is sent,
   the countdown begins (or the next task runs first if queue is non-empty).

9. **Telegram notification failure**: If `notify_shutdown_pending()` fails to send,
   the countdown proceeds silently. The user may not see it, but the intent was clear.

10. **Volatile flag only**: `shutdown_pending` is a `threading.Event` ÔÇö session-only.
    If the orchestrator crashes or is killed, the shutdown intent is lost. No file-based
    persistence.

11. **All messages in English**: Shutdown notifications use English consistently
    ("Shutting down in 60s...", "Shutdown cancelled.", "Shutting down now.").

12. **Cleanup before OS shutdown**: Before calling `subprocess.run(SHUTDOWN_COMMAND)`,
    run cleanup: stop TelegramListener, call `notify_queue_complete()`, flush logs,
    `append_log("Shutdown initiated.")`. The OS may kill the process mid-cleanup,
    so order matters ÔÇö Telegram notification first, log flush last.

**Shutdown flows**:

```
Flow A ÔÇö Queue task has #shutdown:
  1. extract_shutdown(task) ÔåÆ True; #shutdown stripped from prompt
  2. Task runs normally
  3. After mark_done: run_once() returns early (skips remaining tasks), sets shutdown_pending
  4. run_watch() detects shutdown_pending ÔåÆ calls _execute_shutdown()

Flow B ÔÇö Telegram #shutdown while task is running:
  1. Listener detects #shutdown, sets shutdown_pending
  2. Replies: "Shutdown scheduled after current task completes."
  3. Orchestrator checks shutdown_pending after task finishes ÔåÆ _execute_shutdown()

Flow C ÔÇö Telegram #shutdown while standby (queue empty):
  1. Listener sets shutdown_pending
  2. Spawns background thread ÔåÆ _execute_shutdown() (countdown starts immediately)

Flow D ÔÇö Queue drains while shutdown_pending is set:
  1. run_watch() empty-queue branch checks shutdown_pending.is_set()
  2. Calls _execute_shutdown() proactively
```

**Countdown implementation** (in `shutdown.py`):

```python
import subprocess, threading
from config import SHUTDOWN_COMMAND, SHUTDOWN_DELAY_SEC
from notifier import (notify_shutdown_pending, notify_shutdown_cancelled,
                      notify_shutdown_executing, notify_queue_complete)
from queue_manager import read_queue, append_log

# Module-level state
shutdown_pending = threading.Event()
shutdown_cancel  = threading.Event()

def request_shutdown() -> bool:
    """Set shutdown_pending. Returns False if already pending (idempotent)."""
    if shutdown_pending.is_set():
        return False
    shutdown_pending.set()
    return True

def cancel_shutdown() -> None:
    shutdown_cancel.set()

def execute_shutdown(delay_sec: int = SHUTDOWN_DELAY_SEC,
                     cleanup_cb: Callable | None = None) -> None:
    """Countdown, then OS shutdown. Blocks for delay_sec."""
    notify_shutdown_pending(delay_sec)
    cancelled = shutdown_cancel.wait(timeout=delay_sec)
    if cancelled:
        shutdown_cancel.clear()
        shutdown_pending.clear()
        notify_shutdown_cancelled()
        return
    # Cleanup before OS kills us ÔÇö order matters (Telegram first, logs last)
    notify_shutdown_executing()
    if cleanup_cb:
        cleanup_cb()                    # stop listener, flush state
    notify_queue_complete(len(read_queue()))
    append_log("Shutdown initiated by #shutdown.")
    subprocess.run(SHUTDOWN_COMMAND)
```

**New task aborts countdown** (in `shutdown.py`):

```python
def check_queue_abort() -> bool:
    """Call during countdown polling. If new tasks appeared, abort."""
    if read_queue():
        shutdown_cancel.set()
        return True
    return False
```

The countdown loop polls `shutdown_cancel` in short intervals (e.g. 5s chunks)
and calls `check_queue_abort()` each iteration, so a new task arriving during
Flow C cancels the shutdown within ~5s.

**Pause interaction** (in `orchestrator.py`):

```python
# In run_watch(), before calling execute_shutdown():
if shutdown_pending.is_set() and not pause_event.is_set():
    execute_shutdown(cleanup_cb=lambda: listener.stop())
# If paused, shutdown waits ÔÇö checked again after /resume
```

**Any-reply cancellation in listener**:

```python
def _handle_message(self, msg: dict) -> None:
    # Cancel any pending shutdown on ANY incoming message
    if shutdown_pending.is_set():
        cancel_shutdown()
    # ... normal command/chat processing continues
```

**Config** (in `config.py`):

```python
import sys
SHUTDOWN_DELAY_SEC = 60
SHUTDOWN_COMMAND = ["shutdown", "/s", "/t", "0"] if sys.platform == "win32" \
                   else ["sudo", "shutdown", "-h", "now"]
```

**Implementation steps**:

1. **Create `shutdown.py`** (new shared module):
   - Module-level `shutdown_pending` and `shutdown_cancel` Events
   - `request_shutdown()` ÔÇö idempotent, returns False if already pending
   - `cancel_shutdown()` ÔÇö sets cancel event
   - `execute_shutdown(delay_sec, cleanup_cb)` ÔÇö countdown in 5s chunks,
     checks `check_queue_abort()` each chunk, runs cleanup + OS command
   - `check_queue_abort()` ÔÇö returns True if queue has new tasks

2. **`config.py`**: add `SHUTDOWN_DELAY_SEC = 60` and platform-aware `SHUTDOWN_COMMAND`

3. **`queue_manager.py`**:
   - Add `extract_shutdown(task: str) -> bool` ÔÇö detects `#shutdown` tag
   - Extend `strip_metadata_tags()` to also remove `#shutdown`

4. **`notifier.py`**:
   - Add `notify_shutdown_pending(delay_sec)` ÔÇö "Shutting down in {delay_sec}s. Send any message to cancel."
   - Add `notify_shutdown_cancelled()` ÔÇö "Shutdown cancelled."
   - Add `notify_shutdown_executing()` ÔÇö "Shutting down now."

5. **`telegram_listener.py`**:
   - Import `request_shutdown`, `cancel_shutdown`, `shutdown_pending` from `shutdown`
   - In `_handle_message`: if `shutdown_pending.is_set()`, call `cancel_shutdown()`
   - Detect `#shutdown` in plain text ÔåÆ call `request_shutdown()`; if idle,
     spawn thread calling `execute_shutdown()`
   - Add `/cancel-shutdown` command (also covered by any-reply, but explicit)

6. **`orchestrator.py`**:
   - Import `shutdown_pending`, `execute_shutdown`, `request_shutdown` from `shutdown`
   - In `run_once()`: after `mark_done`/`mark_retry`, if task had `#shutdown` ÔåÆ
     `request_shutdown()`, return early (skip remaining tasks)
   - In `run_watch()` after `run_once()`: if `shutdown_pending.is_set()` and
     not `pause_event.is_set()` ÔåÆ `execute_shutdown(cleanup_cb=...)`
   - In `run_watch()` empty-queue branch: same check
   - Cleanup callback: `listener.stop()`, flush logs

**Files to create**: `shutdown.py`

**Files to modify**: `config.py`, `queue_manager.py`, `notifier.py`,
`telegram_listener.py`, `orchestrator.py`

**Verification**:

1. Queue tag: Add `- [ ] Echo hello #shutdown` to queue ÔåÆ task completes ÔåÆ Telegram
   countdown message ÔåÆ send any message ÔåÆ "Shutdown cancelled." ÔåÆ queue resumes.
2. Telegram trigger (standby): Send `#shutdown` while queue is empty ÔåÆ countdown
   starts immediately.
3. Proactive standby: Set `shutdown_pending` before queue drains ÔåÆ orchestrator
   triggers countdown automatically when queue empties.
4. Task with followers: Add `#shutdown` task with tasks below it ÔåÆ shutdown starts
   after the tagged task, remaining tasks stay in queue.
5. Windows OS command: Confirm `shutdown /s /t 0` fires (test with a long delay first
   to verify cancellation).

---

## References

- [OpenClaw](https://github.com/openclaw/openclaw) ÔÇö Architecture patterns,
  SOUL.md, Skills, Memory, Heartbeat concepts
- Existing vault skills: `99_System/AI/Skills/` (11 skills with SKILL.md)
- Existing tools: `tools/registry.py`, `tools/review_loop.py`, `tools/test_loop.py`
