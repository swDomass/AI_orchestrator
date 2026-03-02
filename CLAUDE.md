# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Obsidian-Projektdoku

`literal:/path/to/your/obsidian_vault\01_Tasks\01_Projekte\03_Unternehmung-Invest\AI-System-Intelligence-Orchestrator.md`

## Project

Autonomous task orchestrator routing work across Claude Code, Gemini CLI, and Codex CLI. Tasks come from an Obsidian vault Markdown queue (`99_System/AI/agent-queue.md`). Pure Python stdlib + pyyaml, Windows-first.

## Commands

```bash
# Run all tests (~207 tests, ~2s)
python -m pytest tests/ -q

# Run a single test file
python -m pytest tests/test_parallel_runner.py -v

# Run a single test
python -m pytest tests/test_queue_manager_regressions.py::test_extract_cwd_supports_spaces -v

# Install dependencies
pip install -r requirements.txt

# Validate setup (CLIs, vault, .env, Telegram, etc.)
python orchestrator.py --doctor
python orchestrator.py --doctor --fix --yes

# Other modes
python orchestrator.py                # single-shot: process queue once
python orchestrator.py --watch        # continuous mode with heartbeat
python orchestrator.py --dry-run      # parse queue without executing
python orchestrator.py --check-limits # show provider capacity
python orchestrator.py --list-tools   # show available #tool: handlers
python orchestrator.py --dashboard    # launch analytics web dashboard
```

## Architecture

**Execution flow**: Queue read → provider selection (fallback chain) → profile loading → policy check → skill gating → memory context injection → prompt building → provider execution → result persistence → heartbeat.

Key components:
- **`orchestrator.py`**: Main loop (`run_once`/`run_watch`), prompt building (`_build_prompt`), file change tracking via before/after snapshots
- **`dispatcher.py`**: Provider selection with fallback chain (Claude → Gemini → Codex), cooldown management, Claude model aliases (`#claude_haiku`, `#claude_sonnet`, `#claude_opus`)
- **`queue_manager.py`**: Obsidian MD queue parsing with sidecar `.lock` file locking (msvcrt on Windows, fcntl on Unix). Regex-based metadata extraction (`cwd:`, `#tool:`, `#agent:`, `#parallel`, `#claude_*`, etc.). UTF-8 with cp1252 fallback. Smart wikilink/file context injection with TF-IDF section extraction.
- **`providers/base.py`**: `BaseProvider` ABC with per-provider `_lock` for cooldown state and `threading.local()` for per-thread forced model
- **`policy.py`**: `PolicyEngine` singleton — AUTO/APPROVE/DENY classification from vault YAML, blocks on `threading.Event` for Telegram approval
- **`usage_suggester.py`**: `UsageSuggester` singleton — proactive task suggestions when provider capacity is underutilized. Same threading pattern as PolicyEngine
- **`memory.py`**: TF-IDF + temporal decay search over past task results stored in vault. Auto-archival after 180 days.
- **`heartbeat.py`**: Scheduled health checks with mtime-reloading config from vault `HEARTBEAT.md`. 7 built-in handlers: queue-idle, git-status, disk-space, check-limits, summarize, stale-branch, usage-suggest
- **`analytics.py`**: Parses task results, log files, queue events into `TaskRecord`/`LimitSnapshot`/`QueueEvent` dataclasses. Aggregation functions + `get_dashboard_data()` with 30s TTL cache
- **`dashboard.py`**: Standalone HTTP server (port 8411) serving Chart.js dashboard. `GET /` HTML, `GET /api/data` JSON. Auto-refresh 60s. Also launchable via `--dashboard` flag
- **`config.py`**: Centralized constants (~58), `.env` loader (no external dotenv), mtime-cached `SOUL.md` personality loader, Claude model aliases
- **`limits.py`**: `cclimits` wrapper for provider capacity checks, OAuth refresh handling
- **`logging_setup.py`**: Rotating file logger (5MB, 3 backups) + console output
- **`doctor.py`**: 15+ setup validation checks, `--fix`/`--yes` auto-repair mode
- **`shutdown.py`**: Shutdown state machine with countdown, cancellation via Telegram or new queue tasks

## Key Patterns

- **Singletons with threading**: `PolicyEngine`, `UsageSuggester`, providers — each has own `_lock`, `threading.Event` for blocking operations, no global mutex
- **Mtime-cached config**: Policy, profiles, SOUL.md, heartbeat all use `(mtime, content)` tuple caching for hot-reload in `--watch` mode
- **Token-budget injection**: `_build_prompt()` truncates skill/memory/wikilink context to `PROMPT_*_TOKENS` constants before assembly
- **Sidecar file locking**: `queue_manager.py` uses `.lock` file with platform-specific locking for multi-process safety

## Testing Conventions

- Tests use `unittest.mock.patch` and `pytest` fixtures (`tmp_path`, `monkeypatch`)
- Config module side-effects at import: mock `config._load_dotenv` when importing modules that depend on config
- All tests are synchronous (no async)
- Test files mirror source: `tests/test_<module>.py`

## Safety Rules (enforced in code)

- NEVER: `rm -rf`, `git push --force`, `git reset --hard`, `DROP TABLE`, `format`, `mkfs`
- CWD validation against `ALLOWED_CWD_ROOTS` — rejects relative paths and parent escapes
- Skill gating checks requirements (bins, env vars, OS, provider) before execution
- Policy layer can block tasks pending Telegram approval
