"""
AI Orchestrator - Main entry point.

Usage:
    python orchestrator.py              # Run queue once
    python orchestrator.py --watch      # Run continuously, auto-retry when usage resets
    python orchestrator.py --check-limits  # Show current provider limits
    python orchestrator.py --dry-run    # Parse tasks without executing
    python orchestrator.py --list-tools # Show available tools

Queue file: configured in config.py (default: Obsidian vault agent-queue.md)

Task format in agent-queue.md:
    - [ ] Task description
    - [ ] Task with provider tag #gemini
    - [ ] Task with vault ref [[Notiz Name]]
    - [ ] Code task cwd:/d/programmieren/projekt #timeout:10m #codex
    - [ ] Review und fixe Bugs #tool:review-loop cwd:/d/projekt
    - [ ] Tests fixen #tool:test-loop cwd:/d/projekt
"""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

# Ensure UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError for →, ✅, ❌, etc.)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timedelta

from logging_setup import setup_logging

from config import (
    GIT_AUTO_STASH,
    MAX_HANG_RETRIES,
    HANG_RETRY_BACKOFF_SEC,
    MAX_RETRIES_PER_PROVIDER,
    is_known_model_tag,
    model_id_for_provider,
    PROMPT_CURATED_MEMORY_TOKENS,
    PROMPT_DAILY_LOG_TOKENS,
    PROMPT_MEMORY_TOKENS,
    PROMPT_SKILL_TOKENS,
    PROMPT_WIKILINK_TOKENS,
    SLEEP_POLL_INTERVAL,
    STARTUP_DELAY_SEC,
    TASK_TIMEOUT_SEC,
    TRACK_FILE_CHANGES,
    get_system_prompt,
)
from dispatcher import select_provider, earliest_cooldown_reset, has_explicit_provider_tag, force_refresh_can_unblock
from limits import get_limits, set_queue_idle, set_paused, AllLimits, report_estimated_usage, estimate_task_usage_pct
from notifier import (
    notify_error,
    notify_providers_exhausted,
    notify_queue_complete,
    notify_task_done,
    notify_task_started,
    start_session,
)
from providers.base import RunResult
from skills import load_skill, check_requirements
from config import VAULT_PATH
import memory as memory_module
from queue_manager import (
    append_log,
    cleanup_done_tasks,
    ensure_queue_file,
    extract_cwd,
    extract_id_tag,
    extract_model_tag,
    extract_needs_tags,
    extract_pass_providers,
    extract_preapproved_actions,
    collect_file_context,
    extract_profile_tag,
    extract_second_opinion_alias,
    extract_shutdown_tag,
    extract_timeout,
    extract_hang_count,
    finalize_task_with_result,
    has_cwd_tag,
    mark_done,
    mark_retry,
    read_queue,
    read_queue_items,
    realign_stale_freshonly,
    strip_metadata_tags,
)
import replay
from telegram_listener import TelegramListener
from tools import extract_tool_tag, get_tool, list_tools


def fmt_time(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def _get_next_retry_sec(limits: AllLimits) -> int:
    """Calculate seconds until next retry based on limits and cooldowns."""
    limit_sec = limits.earliest_reset_sec()
    cooldown_sec = earliest_cooldown_reset()

    if cooldown_sec is not None:
        # If API says "available" (default 3600 fallback in limits.py) but we have cooldowns,
        # prefer the potentially shorter cooldown time.
        if limit_sec == 3600:
            return int(cooldown_sec)
        return int(min(limit_sec, cooldown_sec))
    
    return limit_sec


def _rate_limit_cooldown_sec(limits: AllLimits, provider_name: str) -> int:
    """Choose a bounded cooldown after a provider rate-limit error."""
    lim = getattr(limits, provider_name, None)
    if lim is None:
        return 5 * 60

    reset_sec = int(getattr(lim, "resets_in_sec", 0) or 0)
    if reset_sec <= 0:
        return 5 * 60

    return max(60, min(reset_sec, 30 * 60))



def _snapshot_dir(cwd: str) -> dict[str, tuple[float, int]]:
    """Recursively snapshot files as {relative_path: (mtime, size)}."""
    snapshot: dict[str, tuple[float, int]] = {}
    try:
        for root, _dirs, files in os.walk(cwd):
            for name in files:
                path = os.path.join(root, name)
                try:
                    stat = os.stat(path)
                    rel = os.path.relpath(path, cwd)
                    snapshot[rel] = (stat.st_mtime, stat.st_size)
                except OSError:
                    pass
    except OSError:
        pass
    return snapshot


def _diff_snapshot(
    before: dict[str, tuple[float, int]],
    after: dict[str, tuple[float, int]],
) -> str:
    """Compare two snapshots, return formatted summary of changes."""
    created = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(
        name for name in set(before) & set(after) if before[name] != after[name]
    )

    if not created and not deleted and not modified:
        return ""

    lines: list[str] = []
    if created:
        lines.append(f"Created ({len(created)}): {', '.join(created)}")
    if deleted:
        lines.append(f"Deleted ({len(deleted)}): {', '.join(deleted)}")
    if modified:
        lines.append(f"Modified ({len(modified)}): {', '.join(modified)}")
    return "\n".join(lines)


def _is_git_repo(cwd: str) -> bool:
    """Check if cwd is inside a git work tree."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


def _git_snapshot(cwd: str, is_git: bool | None = None) -> str | None:
    """Create a non-destructive git stash snapshot as rollback point.

    Uses `git stash create` + `git stash store` so the current worktree is not
    modified before the task runs.
    """
    if not GIT_AUTO_STASH or not cwd:
        return None
    if not (is_git if is_git is not None else _is_git_repo(cwd)):
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    msg = f"orchestrator-backup-{timestamp}"
    try:
        create = subprocess.run(
            ["git", "stash", "create", msg],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if create.returncode != 0:
            return None

        stash_commit = create.stdout.strip()
        if not stash_commit:
            return None

        store = subprocess.run(
            ["git", "stash", "store", "-m", msg, stash_commit],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if store.returncode == 0:
            print(f"  [safety] Git Snapshot gespeichert (nicht-destruktiv): {msg}")
            return msg
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _git_diff_summary(cwd: str) -> str:
    """Get a git change summary including untracked files."""
    parts: list[str] = []
    try:
        tracked = subprocess.run(
            ["git", "diff", "HEAD", "--stat"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        if tracked.returncode == 0 and tracked.stdout.strip():
            parts.append(tracked.stdout.strip())

        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        if untracked.returncode == 0 and untracked.stdout.strip():
            files = [line.strip() for line in untracked.stdout.splitlines() if line.strip()]
            if files:
                preview = ", ".join(files[:10])
                if len(files) > 10:
                    preview += f", ... (+{len(files) - 10} mehr)"
                parts.append(f"Untracked ({len(files)}): {preview}")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "\n".join(parts)


def _get_change_summary(cwd: str | None, snap_before: dict | None, is_git: bool = False) -> str:
    """Build a change summary from git diff or file snapshots."""
    if not cwd or not TRACK_FILE_CHANGES:
        return ""

    # Prefer git diff for git repos
    if is_git:
        return _git_diff_summary(cwd)

    # Fall back to file snapshot diff
    if snap_before is not None:
        snap_after = _snapshot_dir(cwd)
        return _diff_snapshot(snap_before, snap_after)

    return ""


def _truncate_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to approximately max_tokens words."""
    words = text.split()
    if len(words) <= max_tokens:
        return text
    return " ".join(words[:max_tokens]) + "\n...[truncated]"


def _build_prompt(
    task: str,
    provider_name: str,
    skill_name: str | None = None,
    memory_context: str = "",
) -> str:
    """Build final prompt with selective injection and token budget management.

    Components (in order):
    1. Core system prompt (SOUL.md base + provider override) — always included
    2. Skill body — only when skill_name is provided
    3. Curated MEMORY.md (layer 1) — long-term patterns, always loaded
    4. Daily log today+yesterday (layer 2) — recent temporal context
    5. TF-IDF memory matches (layer 3) — relevant deep history
    6. File/wikilink context — budget-capped
    7. The task itself, under a "## Aufgabe" heading — ALWAYS LAST

    Step 7 is load-bearing, not cosmetic. Until 2026-07-25 the task text rode along
    inside step 6 (inject_file_context returns "task + blocks"), which put the
    instruction at ~62 % of the prompt and ended the prompt with whatever files the
    task happened to reference. Three morning-brief runs died that way: a clean run,
    exit 0, subtype "success" — and an answer of "I see your configuration but no
    concrete task". Keep the task last and clearly delimited.
    """
    from skills import build_index, load_skill, progressive_body

    # Strip routing tags only from the queue task text, not from injected file contents.
    clean_task = strip_metadata_tags(task)

    # 1. Core prompt (always)
    core = get_system_prompt(provider_name)

    # 2. Skill INDEX (always-present, cheap self-routing context) + matched body
    skill_index = build_index(vault_path=VAULT_PATH)
    skill_prompt = ""
    if skill_name:
        skill = load_skill(skill_name, vault_path=VAULT_PATH)
        if skill and skill.prompt:
            # Lazy section selection isn't requested at the queue layer; tools
            # that want per-phase narrowing pass phase= themselves.
            body = progressive_body(skill)
            skill_prompt = _truncate_tokens(body, PROMPT_SKILL_TOKENS)

    # 3. Curated MEMORY.md (layer 1 — long-term patterns)
    curated = memory_module.get_curated_memory()
    if curated:
        curated = _truncate_tokens(curated, PROMPT_CURATED_MEMORY_TOKENS)

    # 4. Daily log (layer 2 — today + yesterday)
    daily = memory_module.get_daily_context()
    if daily:
        daily = _truncate_tokens(daily, PROMPT_DAILY_LOG_TOKENS)

    # 5. TF-IDF memory context (layer 3 — pre-filtered by get_context_for_task)
    mem_block = _truncate_tokens(memory_context, PROMPT_MEMORY_TOKENS) if memory_context else ""

    # 6. Wikilink / file context (budget-capped); ~5 chars per token.
    # Blocks only — the task text is appended separately in step 7 below.
    max_wiki_chars = PROMPT_WIKILINK_TOKENS * 5
    wiki_ctx = collect_file_context(clean_task, max_chars=max_wiki_chars)

    # Assemble
    parts: list[str] = []
    if core:
        parts.append(core)
    if skill_index:
        parts.append(skill_index)
    if skill_prompt:
        parts.append(f"## Skill: {skill_name}\n{skill_prompt}")
    if curated:
        parts.append(f"## Langzeit-Kontext\n{curated}")
    if daily:
        parts.append(f"## Heutiger Verlauf\n{daily}")
    if mem_block:
        parts.append(f"## Relevanter vergangener Kontext\n{mem_block}")
    if wiki_ctx:
        parts.append(f"## Referenzierte Dateien\n{wiki_ctx}")
    # 7. The task LAST — see the docstring for why this position is load-bearing.
    # A queue line consisting only of routing tags strips down to nothing; emitting a
    # bare "## Aufgabe" heading would recreate the very state this fix removes (context
    # with no instruction), just from a different cause. Say so instead of faking one.
    if clean_task:
        parts.append(f"## Aufgabe\n{clean_task}")
    else:
        print("  [prompt] WARNUNG: Task-Text ist nach dem Strippen der Tags leer")
        parts.append("## Aufgabe\n(LEER — die Queue-Zeile enthielt nur Metadaten-Tags)")

    return "\n\n".join(p for p in parts if p)


def _run_with_retry(
    provider,
    task: str,
    prompt: str,
    cwd: str | None,
    timeout: int,
    pause_event: threading.Event | None = None,
) -> tuple:
    """
    Run task on provider with retries. Returns (result, exhausted).
    exhausted=True means all retries failed.
    """
    if MAX_RETRIES_PER_PROVIDER <= 0:
        return RunResult(success=False, error="no retries configured"), True

    for attempt in range(MAX_RETRIES_PER_PROVIDER):
        if pause_event and pause_event.is_set():
            return RunResult(success=False, error="paused"), False

        result = provider.run(prompt, cwd=cwd, timeout=timeout)

        if result.success:
            return result, False

        # "hang" (idle-kill) like "timeout": no provider fallback — a frozen
        # process is not a "different provider would help" case.
        # "stdin_incomplete": retrying the SAME oversized prompt down the SAME
        # pipe is the least likely thing to work, and each attempt burns a full
        # prompt (~26k cache_creation tokens in the 2026-07-20 incident). Bail
        # out of the in-run backoff; the task keeps its past retry marker and is
        # picked up again on the next poll within the grace window.
        if result.error in ("rate_limit", "unreachable", "timeout", "hang", "stdin_incomplete"):
            return result, True

        if attempt < MAX_RETRIES_PER_PROVIDER - 1:
            # Exponential backoff: 10s, 20s, 40s...
            wait = 10 * (2 ** attempt)
            print(f"  Retry {attempt + 1}/{MAX_RETRIES_PER_PROVIDER} in {wait}s...")
            slept = 0
            while slept < wait:
                if pause_event and pause_event.is_set():
                    return RunResult(success=False, error="paused"), False
                chunk = min(1, wait - slept)
                time.sleep(chunk)
                slept += chunk

    return result, True


@dataclass
class ToolTaskExecutionOutcome:
    success: bool
    finalized: bool
    retryable: bool = False
    error: str = ""
    error_code: str = ""
    output: str = ""


@dataclass
class _RunSpan:
    """Per-task run telemetry, emitted to replay JSONL when the iteration ends.

    Default exit_status is ERROR — branches that succeed or retry must override.
    """
    run_id: str
    ts_start: datetime
    task_text: str
    task_id: str = ""
    cwd: str = ""
    provider: str = ""
    model: str = ""
    tool: str = ""
    profile: str = ""
    prompt: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    exit_status: str = replay.EXIT_ERROR
    error_code: str | None = None
    retry_count: int = 0
    needs_satisfied_by: list[str] = None  # type: ignore[assignment]
    emitted: bool = False

    def __post_init__(self) -> None:
        if self.needs_satisfied_by is None:
            self.needs_satisfied_by = []

    def ok(self, **fields) -> None:
        self.exit_status = replay.EXIT_OK
        self.error_code = None
        self._merge(fields)

    def retry(self, code: str | None, **fields) -> None:
        self.exit_status = replay.EXIT_RETRY
        self.error_code = code
        self._merge(fields)

    def error(self, code: str | None, **fields) -> None:
        self.exit_status = replay.EXIT_ERROR
        self.error_code = code
        self._merge(fields)

    def blocked(self, code: str = "dep_unsatisfied", **fields) -> None:
        self.exit_status = replay.EXIT_BLOCKED
        self.error_code = code
        self._merge(fields)

    def _merge(self, fields: dict) -> None:
        for k, v in fields.items():
            if hasattr(self, k):
                setattr(self, k, v)

    def emit(self) -> None:
        """Append the record to replay JSONL. Idempotent — emits at most once."""
        if self.emitted:
            return
        self.emitted = True
        try:
            record = replay.build_record(
                run_id=self.run_id,
                ts_start=self.ts_start,
                task_text=self.task_text[:500],
                task_id=self.task_id,
                cwd=self.cwd,
                provider=self.provider,
                model=self.model,
                tool=self.tool,
                profile=self.profile,
                prompt=self.prompt,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                cache_creation_input_tokens=self.cache_creation_input_tokens,
                cache_read_input_tokens=self.cache_read_input_tokens,
                exit_status=self.exit_status,
                error_code=self.error_code,
                retry_count=self.retry_count,
                needs_satisfied_by=self.needs_satisfied_by,
            )
            replay.append_run(record)
        except Exception as e:  # noqa: BLE001 — telemetry must never break the loop
            import logging as _logging
            _logging.getLogger(__name__).debug("replay emit failed: %s", e)



def _mark_done_checked(
    task: str,
    provider: str,
    *,
    queue_line_no: int | None = None,
    subtasks: tuple[str, ...] | None = None,
) -> bool:
    """Mark task done and return False if queue mutation failed."""
    if mark_done(task, provider, line_no=queue_line_no, subtasks=subtasks):
        return True
    msg = "Queue-Update fehlgeschlagen: Task konnte nicht als erledigt markiert werden"
    print(f"  ❌ {msg}")
    append_log(msg)
    notify_error(task, provider, msg)
    return False


def _finalize_task_with_result_checked(
    task: str,
    result: str,
    provider: str,
    *,
    queue_line_no: int | None = None,
    subtasks: tuple[str, ...] | None = None,
) -> bool:
    """Atomically persist result + done status and return False on queue mutation failure."""
    if finalize_task_with_result(task, result, provider, line_no=queue_line_no, subtasks=subtasks):
        return True
    msg = "Queue-Update fehlgeschlagen: Ergebnis+Status konnten nicht atomar persistiert werden"
    print(f"  ❌ {msg}")
    append_log(msg)
    notify_error(task, provider, msg)
    return False


def _mark_retry_checked(
    task: str,
    retry_at: str,
    provider: str = "queue",
    *,
    queue_line_no: int | None = None,
    subtasks: tuple[str, ...] | None = None,
) -> bool:
    """Mark task for retry and return False if queue mutation failed."""
    if mark_retry(task, retry_at, line_no=queue_line_no, subtasks=subtasks):
        return True
    msg = f"Queue-Update fehlgeschlagen: Task konnte nicht für Retry ({retry_at}) markiert werden"
    print(f"  ❌ {msg}")
    append_log(msg)
    notify_error(task, provider, msg)
    return False


def _execute_tool_task(
    task: str,
    tool_name: str,
    provider,
    cwd: str | None,
    timeout: int | None = None,
    queue_line_no: int | None = None,
    subtasks: tuple[str, ...] | None = None,
    memory_context: str = "",
    skip_queue: bool = False,
) -> ToolTaskExecutionOutcome:
    """Execute a tool-based task and report whether the queue item was finalized."""
    tool = get_tool(tool_name)
    if not tool:
        msg = f"Tool nicht gefunden: {tool_name}"
        print(f"  ❌ Unbekanntes Tool: {tool_name}")
        if not skip_queue:
            append_log(f"Unbekanntes Tool: {tool_name}")
            notify_error(task, provider.name if provider else "unknown", msg)
            finalized = _mark_done_checked(task, "failed", queue_line_no=queue_line_no, subtasks=subtasks)
        else:
            finalized = False
        return ToolTaskExecutionOutcome(success=False, finalized=finalized, error=msg)

    # Gating check: verify skill requirements are met
    skill = load_skill(tool_name, cwd=Path(cwd) if cwd else None, vault_path=VAULT_PATH)
    if skill:
        available, reasons = check_requirements(skill)
        if not available:
            msg = f"Skill '{tool_name}' Anforderungen nicht erfüllt: {'; '.join(reasons)}"
            print(f"  ❌ {msg}")
            if not skip_queue:
                append_log(msg)
                notify_error(task, provider.name, msg)
                finalized = _mark_done_checked(task, "failed", queue_line_no=queue_line_no, subtasks=subtasks)
            else:
                finalized = False
            return ToolTaskExecutionOutcome(success=False, finalized=finalized, error=msg)

    # Safety: snapshot before execution
    tool_is_read_only = getattr(tool, "read_only", False)
    is_git = bool(cwd) and _is_git_repo(cwd)
    snap_before = _snapshot_dir(cwd) if cwd and TRACK_FILE_CHANGES else None
    if cwd and not tool_is_read_only:
        _git_snapshot(cwd, is_git=is_git)

    print(f"  → Tool: {tool.name} ({tool.description})")
    clean_task = strip_metadata_tags(task)
    # Extract pass-provider tags from raw task (before strip removes them)
    pass_providers = extract_pass_providers(task)
    # Second-opinion (review-loop opt-in): pass raw alias; the tool resolves
    # it via dispatcher.get_provider_by_name so other tools stay unaffected.
    second_opinion_alias = extract_second_opinion_alias(task)
    _tool_start = time.time()
    tool_result = tool.run(
        clean_task, provider, cwd=cwd, timeout=timeout,
        memory_context=memory_context, pass_providers=pass_providers,
        second_opinion_alias=second_opinion_alias,
    )
    _tool_duration = time.time() - _tool_start

    # Track estimated usage for 429 capacity estimation
    if (tool_result.error_code or "") not in ("rate_limit", "unreachable"):
        report_estimated_usage(provider.name, estimate_task_usage_pct(
            _tool_duration,
            input_tokens=tool_result.input_tokens,
            output_tokens=tool_result.output_tokens,
            prompt_text=clean_task,
            output_text=tool_result.output,
            provider=provider.name,
        ))

    # Safety: build change summary
    change_summary = _get_change_summary(cwd, snap_before, is_git=is_git)
    if change_summary:
        print(f"  [safety] Änderungen:\n{change_summary}")

    provider_tool = f"{provider.name}+{tool.name}"

    if tool_result.success:
        print(f"  ✅ Tool erledigt ({tool_result.iterations} Iteration(en))")
        if not skip_queue:
            if not _finalize_task_with_result_checked(
                task,
                tool_result.output,
                provider_tool,
                queue_line_no=queue_line_no,
                subtasks=subtasks,
            ):
                return ToolTaskExecutionOutcome(
                    success=False,
                    finalized=False,
                    error="queue_update_failed",
                    output=tool_result.output,
                )
            memory_module.store_result(
                task, tool_result.output, provider_tool, _tool_duration, cwd=cwd, success=True,
                input_tokens=tool_result.input_tokens,
                output_tokens=tool_result.output_tokens,
                cache_creation_input_tokens=tool_result.cache_creation_input_tokens,
                cache_read_input_tokens=tool_result.cache_read_input_tokens,
            )
            append_log(f"Tool {tool.name} erledigt via {provider.name} ({tool_result.iterations}x): {task[:60]}")
            notify_task_done(task, provider_tool, tool_result.output, change_summary=change_summary)
        return ToolTaskExecutionOutcome(
            success=True,
            finalized=not skip_queue,
            output=tool_result.output,
        )
    else:
        print(f"  ⚠️ Tool beendet: {tool_result.error}")
        if tool_result.retryable:
            if not skip_queue:
                append_log(f"Tool {tool.name} transienter Fehler via {provider.name}: {tool_result.error}")
                notify_error(task, f"{provider.name}+{tool.name}", tool_result.error)
            return ToolTaskExecutionOutcome(
                success=False,
                finalized=False,
                retryable=True,
                error=tool_result.error,
                error_code=tool_result.error_code or tool_result.error,
                output=tool_result.output,
            )

        if not skip_queue:
            if not _finalize_task_with_result_checked(
                task,
                tool_result.output,
                provider_tool,
                queue_line_no=queue_line_no,
                subtasks=subtasks,
            ):
                return ToolTaskExecutionOutcome(
                    success=False,
                    finalized=False,
                    error="queue_update_failed",
                    error_code=tool_result.error_code,
                    output=tool_result.output,
                )
            memory_module.store_result(
                task, tool_result.output or tool_result.error, provider_tool,
                _tool_duration, cwd=cwd, success=False,
                input_tokens=tool_result.input_tokens,
                output_tokens=tool_result.output_tokens,
                cache_creation_input_tokens=tool_result.cache_creation_input_tokens,
                cache_read_input_tokens=tool_result.cache_read_input_tokens,
            )
            append_log(f"Tool {tool.name} Fehler: {tool_result.error}")
            notify_error(task, f"{provider.name}+{tool.name}", tool_result.error)
        return ToolTaskExecutionOutcome(
            success=False,
            finalized=not skip_queue,
            error=tool_result.error,
            error_code=tool_result.error_code,
            output=tool_result.output,
        )


def run_once(dry_run: bool = False, pause_event: threading.Event | None = None) -> bool | None:
    """
    Process all open tasks in the queue once.
    Returns True if all tasks were completed, False if stopped early,
    None if all tasks were blocked by dependencies (#needs:).
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    # Archive old memories once per cycle (silent, never blocks)
    try:
        archived = memory_module.archive_old_memories()
        if archived:
            _log.debug("Archived %d old memories", archived)
    except (OSError, ImportError):
        pass

    # Move old completed tasks to erledigt.md once per cycle (silent, never blocks)
    try:
        moved = cleanup_done_tasks()
        if moved:
            _log.debug("Moved %d done task(s) to erledigt.md", moved)
    except (OSError, ImportError):
        pass

    # Realign stale #freshonly tasks (e.g. a daily brief whose slot was missed while
    # the orchestrator was off) to their next anchored slot BEFORE reading — so they
    # are filtered out this cycle instead of firing late at the wrong time of day.
    # Skipped under dry_run: it mutates the queue, and dry_run must only parse.
    if not dry_run:
        try:
            realigned = realign_stale_freshonly()
            if realigned:
                _log.debug("Realigned %d stale #freshonly task(s) to next slot", realigned)
        except (OSError, ValueError) as e:
            _log.warning("realign_stale_freshonly failed: %s", e)

    task_items = read_queue_items()
    if not task_items:
        print("Queue leer - nichts zu tun.")
        return True

    print(f"\n{'='*60}")
    blocked_count = sum(1 for t in task_items if getattr(t, "blocked_reason", ""))
    eligible = len(task_items) - blocked_count
    suffix = f" ({eligible} ausführbar, {blocked_count} blockiert)" if blocked_count else ""
    print(f"Queue: {len(task_items)} offene Task(s){suffix}")
    print(f"{'='*60}")

    for i, queue_task in enumerate(task_items, 1):
        if pause_event and pause_event.is_set():
            print("\n[pause] Queue-Verarbeitung pausiert.")
            append_log("Queue-Verarbeitung pausiert")
            return False

        task = queue_task.task_text
        task_subtasks: tuple[str, ...] | None = getattr(queue_task, "subtasks", None)  # getattr for test-mock compat

        # Replay-Telemetrie: pro Task allokieren. Wird im finally am Ende der
        # Iteration emittiert; Branches setzen den Status via span.ok/retry/...
        _span = _RunSpan(
            run_id=replay.new_run_id(),
            ts_start=datetime.now(),
            task_text=task,
            task_id=extract_id_tag(task) or "",
        )

        # Dependency check — skip blocked tasks without marking them done
        blocked_reason = getattr(queue_task, "blocked_reason", "")
        if blocked_reason:
            print(f"\n[{i}/{len(task_items)}] Task: {task[:80]}{'...' if len(task) > 80 else ''}")
            print(f"  [blocked] {blocked_reason} — übersprungen")
            _span.blocked("dep_unsatisfied", needs_satisfied_by=extract_needs_tags(task))
            _span.emit()
            continue

        print(f"\n[{i}/{len(task_items)}] Task: {task[:80]}{'...' if len(task) > 80 else ''}")

        # --- Feature 6: Load execution profile ---
        profile_name: str | None = None
        try:
            from profiles import load_profile, get_default_profile
            profile_name = extract_profile_tag(task)
            if profile_name:
                profile = load_profile(profile_name, VAULT_PATH)
                if profile is None:
                    print(f"  [profile] Warnung: Profil '{profile_name}' nicht gefunden, verwende Default")
                    profile = get_default_profile()
                else:
                    print(f"  [profile] {profile.name} (providers: {profile.providers})")
            else:
                profile = get_default_profile()
        except Exception as e:
            _log.warning("profile loading failed: %s", e)
            profile = None

        # Extract task metadata
        cwd_tag_present = has_cwd_tag(task)
        cwd = extract_cwd(task)

        # Profile timeout overrides task timeout
        if profile and profile.timeout_minutes > 0:
            timeout = profile.timeout_minutes * 60
        else:
            timeout = extract_timeout(task, default=TASK_TIMEOUT_SEC)

        tool_timeout = extract_timeout(task, default=0) or None
        tool_name = extract_tool_tag(task)

        model_tag = extract_model_tag(task)
        if model_tag and not is_known_model_tag(model_tag):
            _log.warning("Unknown model tag #%s — ignored, using default model", model_tag)

        # Strict mode: when provider/model is explicitly specified, no fallback
        provider_is_forced = has_explicit_provider_tag(task)

        # Populate span basics now that we have all metadata
        _span.cwd = cwd or ""
        _span.tool = tool_name or ""
        _span.profile = profile.name if profile else ""

        # Feature 6: denied_skills check
        if tool_name and profile and tool_name in profile.denied_skills:
            msg = f"Tool '{tool_name}' durch Profil '{profile.name}' gesperrt (denied_skills)"
            print(f"  ❌ {msg}")
            if not dry_run:
                append_log(msg)
                notify_error(task, "profile", msg)
                if not _mark_done_checked(task, "profile-denied", queue_line_no=queue_task.line_no, subtasks=task_subtasks):
                    _span.error("profile_denied")
                    _span.emit()
                    return False
            _span.error("profile_denied")
            _span.emit()
            continue

        # Feature 6: allowed_skills whitelist check
        if tool_name and profile and profile.allowed_skills and tool_name not in profile.allowed_skills:
            msg = f"Tool '{tool_name}' nicht in allowed_skills von Profil '{profile.name}'"
            print(f"  ❌ {msg}")
            if not dry_run:
                append_log(msg)
                notify_error(task, "profile", msg)
                if not _mark_done_checked(task, "profile-denied", queue_line_no=queue_task.line_no, subtasks=task_subtasks):
                    _span.error("profile_denied")
                    _span.emit()
                    return False
            _span.error("profile_denied")
            _span.emit()
            continue

        if cwd_tag_present and cwd is None:
            msg = "Ungültiges cwd:-Tag (Verzeichnis fehlt oder ist nicht erlaubt) - Task wird nicht ausgeführt"
            print(f"  ❌ {msg}")
            if dry_run:
                continue
            append_log(msg)
            notify_error(task, "queue", msg)
            if not _mark_done_checked(task, "invalid-cwd", queue_line_no=queue_task.line_no, subtasks=task_subtasks):
                _span.error("cwd_invalid")
                _span.emit()
                return False
            _span.error("cwd_invalid")
            _span.emit()
            continue

        if cwd:
            print(f"  [cwd] {cwd}")
        if timeout != TASK_TIMEOUT_SEC:
            print(f"  [timeout] {fmt_time(timeout)}")
        if tool_name:
            print(f"  [tool] {tool_name}")
        if model_tag:
            print(f"  [model-tag] #{model_tag}")

        # --- Feature 10: detect #shutdown tag ---
        task_has_shutdown = extract_shutdown_tag(task)

        # Dry-run
        if dry_run:
            limits = get_limits()
            provider = select_provider(task, limits, profile=profile, strict=provider_is_forced, tool_name=tool_name)
            memory_context = memory_module.get_context_for_task(task, cwd=cwd)
            prompt = _build_prompt(
                task,
                provider.name if provider else "claude",
                skill_name=tool_name,
                memory_context=memory_context,
            )
            print(f"  [DRY-RUN] Provider: {provider.name if provider else 'KEINER VERFÜGBAR'}")
            print(f"  [DRY-RUN] Tool: {tool_name or 'keins (single-shot)'}")
            if profile_name:
                print(f"  [DRY-RUN] Profil: {profile_name}")
            print(f"  [DRY-RUN] Memory: {len(memory_context)} Zeichen ({memory_context.count(chr(10)+chr(10))+1 if memory_context else 0} Einträge)")
            print(f"  [DRY-RUN] Prompt-Länge: {len(prompt)} Zeichen (~{len(prompt.split())} Tokens)")
            if task_has_shutdown:
                print(f"  [DRY-RUN] #shutdown erkannt → Shutdown nach diesem Task")
            continue

        # Get current limits
        print("  Prüfe Usage-Limits (cclimits)...")
        limits = get_limits()

        # Fetch memory context once (same for all provider fallbacks)
        memory_context = memory_module.get_context_for_task(task, cwd=cwd)

        # --- Feature 9: Policy check ---
        try:
            from policy import get_engine, TIER_DENY, TIER_APPROVE, _TIER_ORDER, reason_matches_preapproval
            engine = get_engine()

            # Build profile policy once; used for both parent task and subtasks
            profile_policy = profile.policy if profile else {}

            # Check parent task
            clean_task_for_policy = strip_metadata_tags(task)
            verdict, reasons_list = engine.check_task(
                clean_task_for_policy,
                profile_rules=profile_policy or None,
            )
            reasons = set(reasons_list)

            # Check subtasks (if any)
            if getattr(queue_task, "subtasks", None):
                for st in task_subtasks:
                    st_verdict, st_reasons = engine.check_task(
                        strip_metadata_tags(st),
                        profile_rules=profile_policy or None,
                    )
                    # Lower index means higher priority (DENY < APPROVE < AUTO)
                    if _TIER_ORDER.index(st_verdict) < _TIER_ORDER.index(verdict):
                        verdict = st_verdict
                    for r in st_reasons:
                        reasons.add(r)

            if verdict == TIER_DENY:
                msg = f"Task gesperrt (DENY-Policy): {'; '.join(reasons)}"
                print(f"  ❌ {msg}")
                append_log(msg)
                notify_error(task, "policy", msg)
                if not _mark_done_checked(task, "policy-denied", queue_line_no=queue_task.line_no, subtasks=task_subtasks):
                    _span.error("policy_denied")
                    _span.emit()
                    return False
                _span.error("policy_denied")
                _span.emit()
                continue

            if verdict == TIER_APPROVE:
                preapproved = extract_preapproved_actions(task)
                unapproved = [
                    r for r in reasons
                    if not any(reason_matches_preapproval(r, cat) for cat in preapproved)
                    and not engine.is_preapproved(r)
                ]
                if unapproved:
                    response = engine.request_approval(task, unapproved)
                    if response == "denied":
                        print("  ❌ Genehmigung abgelehnt — Task bleibt in Queue.")
                        append_log(f"Genehmigung abgelehnt für Task: {task[:60]}")
                        reset_at = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
                        if not _mark_retry_checked(task, reset_at, queue_line_no=queue_task.line_no, subtasks=task_subtasks):
                            _span.error("approval_denied")
                            _span.emit()
                            return False
                        _span.retry("approval_denied")
                        _span.emit()
                        return False
                    elif response == "timeout":
                        print("  ⏱ Genehmigung timeout — Task bleibt in Queue.")
                        append_log(f"Genehmigung timeout für Task: {task[:60]}")
                        reset_at = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
                        if not _mark_retry_checked(task, reset_at, queue_line_no=queue_task.line_no, subtasks=task_subtasks):
                            _span.error("approval_timeout")
                            _span.emit()
                            return False
                        _span.retry("approval_timeout")
                        _span.emit()
                        return False
                    elif response == "skipped":
                        print("  ⏭ Genehmigung übersprungen — riskante Aktion blockiert, Task bleibt in Queue.")
                        append_log(f"Genehmigung übersprungen für Task: {task[:60]}")
                        reset_at = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
                        if not _mark_retry_checked(task, reset_at, queue_line_no=queue_task.line_no, subtasks=task_subtasks):
                            _span.error("approval_skipped")
                            _span.emit()
                            return False
                        _span.retry("approval_skipped")
                        _span.emit()
                        return False
                    # "approved" → continue
        except ImportError:
            pass
        except Exception as e:
            _log.warning("policy check failed: %s", e)

        # --- Feature 7: Parallel sub-agent spawning ---
        if getattr(queue_task, "subtasks", None):
            print(f"  [parallel] {len(task_subtasks)} Subtask(s)")
            notify_task_started(task, "parallel")
            _span.provider = "parallel"
            try:
                from parallel_runner import run_parallel, format_parallel_result
                results = run_parallel(
                    task,
                    task_subtasks,
                    limits,
                    memory_context=memory_context,
                    pause_event=pause_event,
                    profile=profile,
                )
                aggregated = format_parallel_result(results)
                success_all = all(r.success for r in results)
                provider_tag = "parallel"
                status_str = "✅" if success_all else "⚠️"
                print(f"  {status_str} Parallel abgeschlossen ({len(results)} Subtasks)")
                if not _finalize_task_with_result_checked(
                    task, aggregated, provider_tag, queue_line_no=queue_task.line_no, subtasks=task_subtasks
                ):
                    _span.error("queue_update_failed")
                    _span.emit()
                    return False
                memory_module.store_result(task, aggregated, provider_tag, 0.0, cwd=cwd, success=success_all)
                append_log(f"Parallel-Task erledigt: {task[:60]}")
                notify_task_done(task, provider_tag, aggregated)
                if success_all:
                    _span.ok()
                else:
                    _span.error("parallel_subtask_failure")
            except Exception as e:
                msg = f"Parallel-Ausführung fehlgeschlagen: {e}"
                print(f"  ❌ {msg}")
                append_log(msg)
                notify_error(task, "parallel", msg)
                retry_at = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
                if not _mark_retry_checked(task, retry_at, "parallel", queue_line_no=queue_task.line_no, subtasks=task_subtasks):
                    _span.error("queue_update_failed")
                    _span.emit()
                    return False
                print(f"  [parallel] Task bleibt in Queue (Retry um ~{retry_at[-5:]})")
                _span.retry("tool_internal_error")
                _span.emit()
                return False

            _span.emit()

            # Feature 10: trigger shutdown after this task if tagged
            if task_has_shutdown:
                from shutdown import request_shutdown
                if request_shutdown():
                    print("  [shutdown] #shutdown erkannt → Shutdown ausstehend")
                return False
            continue

        # Tool-based task (iterative loop)
        if tool_name:
            tried_providers: set[str] = set()
            tool_retry_count = 0
            tool_token_refreshed = False
            while True:
                provider = select_provider(task, limits, exclude=tried_providers, profile=profile, strict=provider_is_forced, tool_name=tool_name)
                if provider is None:
                    # Boot-race recovery (mirrors the single-shot path): on the FIRST
                    # selection, if the provider this task can route to is only blocked
                    # by an in-flight OAuth token refresh, wait for it once via a
                    # synchronous force_refresh before parking. Gated to the first
                    # selection (empty tried_providers) + a one-shot flag so real
                    # exhaustion / mid-loop rotation never loops force-refreshing.
                    if (not tried_providers and not tool_token_refreshed
                            and force_refresh_can_unblock(task, limits, strict=provider_is_forced)):
                        tool_token_refreshed = True
                        print("  [limits] Provider unreachable (Token wird erneuert) → force-refresh + Retry")
                        append_log("Provider unreachable wegen Token-Refresh → force_refresh der Limits")
                        limits = get_limits(force_refresh=True)
                        continue
                    earliest = _get_next_retry_sec(limits)
                    reset_dt = datetime.now() + timedelta(seconds=earliest)
                    reset_at_display = reset_dt.strftime("%H:%M")
                    reset_at_marker = reset_dt.strftime("%Y-%m-%d %H:%M")
                    msg = f"Alle Provider voll/unreachable → Task wartet bis ~{reset_at_display}"
                    print(f"  {msg}")
                    append_log(msg)
                    if not _mark_retry_checked(task, reset_at_marker, queue_line_no=queue_task.line_no, subtasks=task_subtasks):
                        _span.error("queue_update_failed", retry_count=tool_retry_count)
                        _span.emit()
                        return False
                    notify_providers_exhausted(fmt_time(earliest))
                    _span.retry("provider_unreachable", retry_count=tool_retry_count)
                    _span.emit()
                    return False

                print(f"  → Provider: {provider.name}")
                if not tried_providers:
                    notify_task_started(task, provider.name)
                model_id = model_id_for_provider(model_tag, provider.name)
                previous_forced_model = getattr(provider, "_forced_model", None)
                setattr(provider, "_forced_model", model_id)
                _span.provider = provider.name
                _span.model = model_id or ""
                try:
                    outcome = _execute_tool_task(
                        task,
                        tool_name,
                        provider,
                        cwd,
                        timeout=tool_timeout,
                        queue_line_no=queue_task.line_no,
                        subtasks=task_subtasks,
                        memory_context=memory_context,
                    )
                finally:
                    setattr(provider, "_forced_model", previous_forced_model)

                if outcome.success:
                    _span.ok(retry_count=tool_retry_count)
                    break
                if outcome.finalized:
                    _span.error(outcome.error_code or "tool_internal_error", retry_count=tool_retry_count)
                    break

                if not outcome.retryable:
                    print("  ❌ Tool-Task nicht finalisiert (Queue-Update-Fehler). Task bleibt offen.")
                    append_log("Tool-Task nicht finalisiert wegen Queue-Update-Fehler")
                    _span.error("queue_update_failed", retry_count=tool_retry_count)
                    _span.emit()
                    return False

                # Capacity exhausted mid-loop: suspend task, don't try other providers
                if outcome.error_code == "capacity_exhausted":
                    limits = get_limits()
                    earliest = _get_next_retry_sec(limits)
                    reset_dt = datetime.now() + timedelta(seconds=earliest)
                    reset_at_marker = reset_dt.strftime("%Y-%m-%d %H:%M")
                    msg = (
                        f"Kapazität erschöpft während Tool-Ausführung "
                        f"→ Suspend bis ~{reset_dt.strftime('%H:%M')}"
                    )
                    print(f"  ⏸ {msg}")
                    append_log(msg)
                    _mark_retry_checked(task, reset_at_marker, queue_line_no=queue_task.line_no, subtasks=task_subtasks)
                    _span.retry("capacity_exhausted", retry_count=tool_retry_count)
                    _span.emit()
                    return False

                # Total-runtime deadline hit: the tool's max_runtime_sec wall-clock
                # budget is exhausted. This is TERMINAL — do NOT fall back to the
                # next provider (each provider would start the loop from iteration 1
                # with a FRESH deadline → 3× budget) and do NOT mark_retry (the next
                # poll would re-run with a fresh deadline → unbounded). Finalize with
                # the partial result so the wall-clock bound actually holds.
                if outcome.error_code == "tool_runtime_exceeded":
                    msg = (
                        f"Tool-Gesamt-Laufzeit-Limit ({provider.name}/{tool_name}) erreicht "
                        f"→ Task abgeschlossen mit Teilergebnis (kein Provider-Fallback)"
                    )
                    if cwd:
                        msg += f" | Teilarbeit ggf. in {cwd}/.{tool_name}/"
                    print(f"  ⏱ {msg}")
                    append_log(msg)
                    notify_error(task, f"{provider.name}+{tool_name}", msg)
                    _finalize_task_with_result_checked(
                        task, outcome.output or msg, f"{provider.name}+{tool_name}",
                        queue_line_no=queue_task.line_no, subtasks=task_subtasks,
                    )
                    _span.error("tool_runtime_exceeded", retry_count=tool_retry_count)
                    break

                tried_providers.add(provider.name)
                tool_retry_count += 1
                if outcome.error_code == "unreachable":
                    provider.set_cooldown()
                elif outcome.error_code == "rate_limit":
                    limits = get_limits(force_refresh=True)
                    provider.set_cooldown(_rate_limit_cooldown_sec(limits, provider.name))
                elif outcome.error_code in ("timeout", "hang"):
                    pass  # not a provider-capacity problem → no cooldown
                elif outcome.error_code != "":
                    provider.set_cooldown(5 * 60)

                # Hang (idle-kill): the process froze, not a capacity issue. Do NOT
                # take the quota-reset retry path (that would re-run the same hanging
                # task forever). Requeue with a short backoff up to MAX_HANG_RETRIES,
                # then BLOCK the task so it stops looping silently.
                if outcome.error_code == "hang":
                    hang_count = extract_hang_count(getattr(queue_task, "raw_line", "")) + 1
                    if hang_count > MAX_HANG_RETRIES:
                        msg = (
                            f"Tool-Hang ({provider.name}/{tool_name}) zum {hang_count}. Mal "
                            f"→ Task blockiert (kein weiterer Retry)"
                        )
                        print(f"  🚫 {msg}")
                        append_log(msg)
                        notify_error(task, f"{provider.name}+{tool_name}", msg)
                        _finalize_task_with_result_checked(
                            task, msg, f"{provider.name}+{tool_name}",
                            queue_line_no=queue_task.line_no, subtasks=task_subtasks,
                        )
                        _span.error("hang_blocked", retry_count=tool_retry_count)
                        break
                    reset_dt = datetime.now() + timedelta(seconds=HANG_RETRY_BACKOFF_SEC)
                    reset_at_marker = reset_dt.strftime("%Y-%m-%d %H:%M")
                    msg = (
                        f"Tool-Hang ({provider.name}/{tool_name}) #{hang_count} "
                        f"→ Requeue um ~{reset_dt.strftime('%H:%M')}"
                    )
                    print(f"  {msg}")
                    append_log(msg)
                    if not mark_retry(
                        task, reset_at_marker, line_no=queue_task.line_no,
                        subtasks=task_subtasks, hang_count=hang_count,
                    ):
                        _span.error("queue_update_failed", retry_count=tool_retry_count)
                        _span.emit()
                        return False
                    _span.retry("hang", retry_count=tool_retry_count)
                    break

                # Timeout: task-complexity issue — don't fall back to other providers.
                # Falling back risks the next provider failing non-retryably, which would
                # finalize the task as [-] and incorrectly satisfy #needs: dependencies.
                if outcome.error_code == "timeout":
                    earliest = _get_next_retry_sec(limits)
                    reset_dt = datetime.now() + timedelta(seconds=earliest)
                    reset_at_marker = reset_dt.strftime("%Y-%m-%d %H:%M")
                    msg = (
                        f"Tool-Timeout ({provider.name}/{tool_name})"
                        f" → Task wartet bis ~{reset_dt.strftime('%H:%M')}"
                    )
                    if cwd:
                        msg += f" | Teilarbeit ggf. in {cwd}/.{tool_name}/"
                    print(f"  {msg}")
                    append_log(msg)
                    if not _mark_retry_checked(task, reset_at_marker, queue_line_no=queue_task.line_no, subtasks=task_subtasks):
                        _span.error("queue_update_failed", retry_count=tool_retry_count)
                        _span.emit()
                        return False
                    _span.retry("timeout", retry_count=tool_retry_count)
                    break

                if provider_is_forced:
                    # Strict mode: no fallback, retry later
                    earliest = _get_next_retry_sec(limits)
                    reset_dt = datetime.now() + timedelta(seconds=earliest)
                    reset_at_marker = reset_dt.strftime("%Y-%m-%d %H:%M")
                    msg = f"Provider {provider.name} erzwungen aber nicht verfügbar → Retry um ~{reset_dt.strftime('%H:%M')}"
                    print(f"  {msg}")
                    append_log(msg)
                    if not _mark_retry_checked(task, reset_at_marker, queue_line_no=queue_task.line_no, subtasks=task_subtasks):
                        _span.error("queue_update_failed", retry_count=tool_retry_count)
                        _span.emit()
                        return False
                    _span.retry(outcome.error_code or "rate_limit", retry_count=tool_retry_count)
                    break

                print(f"  Task bleibt in Queue - versuche nächsten Provider ({outcome.error_code or outcome.error})...")

            _span.emit()

            # Feature 10: trigger shutdown after tool task if tagged
            if task_has_shutdown:
                from shutdown import request_shutdown
                if request_shutdown():
                    print("  [shutdown] #shutdown erkannt → Shutdown ausstehend")
                return False
            continue

        # Safety: snapshot before execution
        is_git = bool(cwd) and _is_git_repo(cwd)
        snap_before = _snapshot_dir(cwd) if cwd and TRACK_FILE_CHANGES else None
        if cwd:
            _git_snapshot(cwd, is_git=is_git)

        # Standard single-shot task with provider fallback in the same run
        tried_providers: set[str] = set()
        single_shot_success = False
        single_shot_retry_count = 0
        single_shot_token_refreshed = False
        while True:
            if pause_event and pause_event.is_set():
                print("\n[pause] Queue-Verarbeitung pausiert.")
                append_log("Queue-Verarbeitung pausiert")
                _span.retry("paused", retry_count=single_shot_retry_count)
                _span.emit()
                return False

            provider = select_provider(task, limits, exclude=tried_providers, profile=profile, strict=provider_is_forced, tool_name=tool_name)

            if provider is None:
                if not tried_providers:
                    # Boot-race recovery: a strict/forced task can hit an expired
                    # OAuth token that the background limits thread is still
                    # refreshing (preliminary snapshot). Wait for that refresh once
                    # via a synchronous force_refresh before giving up — mirrors the
                    # tool-path's rate_limit handling. Scoped to the provider this task
                    # can actually route to (force_refresh_can_unblock), so genuine
                    # exhaustion of the forced provider still falls straight through to
                    # the retry path. Bounded to a single attempt (no endless loop).
                    if not single_shot_token_refreshed and force_refresh_can_unblock(
                        task, limits, strict=provider_is_forced
                    ):
                        single_shot_token_refreshed = True
                        print("  [limits] Provider unreachable (Token wird erneuert) → force-refresh + Retry")
                        append_log("Provider unreachable wegen Token-Refresh → force_refresh der Limits")
                        limits = get_limits(force_refresh=True)
                        continue
                    earliest = _get_next_retry_sec(limits)
                    reset_dt = datetime.now() + timedelta(seconds=earliest)
                    reset_at_display = reset_dt.strftime("%H:%M")
                    reset_at_marker = reset_dt.strftime("%Y-%m-%d %H:%M")
                    msg = f"Alle Provider voll/unreachable → Task wartet bis ~{reset_at_display}"
                    print(f"  {msg}")
                    append_log(msg)
                    if not _mark_retry_checked(task, reset_at_marker, queue_line_no=queue_task.line_no, subtasks=task_subtasks):
                        _span.error("queue_update_failed")
                        _span.emit()
                        return False
                    notify_providers_exhausted(fmt_time(earliest))
                    _span.retry("provider_unreachable")
                    _span.emit()
                    return False

                print("  Keine weiteren Provider verfügbar - Task bleibt in Queue.")
                append_log(f"Keine weiteren Provider verfügbar für Task: {task[:60]}")
                _span.retry("provider_unreachable", retry_count=single_shot_retry_count)
                break

            print(f"  → Provider: {provider.name}")
            if not tried_providers:
                notify_task_started(task, provider.name)
            model_id = model_id_for_provider(model_tag, provider.name)
            previous_forced_model = getattr(provider, "_forced_model", None)
            setattr(provider, "_forced_model", model_id)
            _span.provider = provider.name
            _span.model = model_id or ""
            try:
                prompt = _build_prompt(task, provider.name, memory_context=memory_context)
                _span.prompt = prompt
                start_time = time.time()
                result, _exhausted = _run_with_retry(
                    provider, task, prompt, cwd, timeout, pause_event=pause_event
                )
            finally:
                setattr(provider, "_forced_model", previous_forced_model)

            # Track estimated usage for 429 capacity estimation
            _task_duration = time.time() - start_time
            if result.error not in ("rate_limit", "unreachable", "paused"):
                report_estimated_usage(provider.name, estimate_task_usage_pct(
                    _task_duration,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    prompt_text=prompt,
                    output_text=result.output,
                    provider=provider.name,
                ))

            if result.error == "paused":
                print("\n[pause] Queue-Verarbeitung pausiert.")
                append_log("Queue-Verarbeitung pausiert")
                _span.retry("paused", retry_count=single_shot_retry_count)
                _span.emit()
                return False

            if result.success:
                duration = _task_duration
                print(f"  ✅ Erledigt ({len(result.output)} Zeichen Output)")
                change_summary = _get_change_summary(cwd, snap_before, is_git=is_git)
                if change_summary:
                    print(f"  [safety] Änderungen:\n{change_summary}")
                if not _finalize_task_with_result_checked(
                    task,
                    result.output,
                    provider.name,
                    queue_line_no=queue_task.line_no,
                    subtasks=task_subtasks,
                ):
                    _span.error("queue_update_failed", retry_count=single_shot_retry_count)
                    _span.emit()
                    return False
                memory_module.store_result(
                    task, result.output, provider.name, duration, cwd=cwd, success=True,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cache_creation_input_tokens=result.cache_creation_input_tokens,
                    cache_read_input_tokens=result.cache_read_input_tokens,
                )
                append_log(f"Task erledigt via {provider.name}: {task[:60]}")
                notify_task_done(task, provider.name, result.output, change_summary=change_summary)
                single_shot_success = True
                _span.ok(
                    retry_count=single_shot_retry_count,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cache_creation_input_tokens=result.cache_creation_input_tokens,
                    cache_read_input_tokens=result.cache_read_input_tokens,
                )
                break

            tried_providers.add(provider.name)
            single_shot_retry_count += 1
            error = result.error
            print(f"  ❌ Fehler: {error}")

            # Hang (idle-kill): the process froze, not a capacity issue. Do NOT
            # rotate providers / cooldown forever — that re-runs the same hanging
            # task endlessly. Requeue with a short backoff up to MAX_HANG_RETRIES,
            # then BLOCK the task so it stops looping silently (mirrors the
            # tool-path hang handling; spec §4.1 / README "then the task is BLOCKED").
            if error == "hang":
                hang_count = extract_hang_count(getattr(queue_task, "raw_line", "")) + 1
                if hang_count > MAX_HANG_RETRIES:
                    msg = (
                        f"Hang ({provider.name}) zum {hang_count}. Mal "
                        f"→ Task blockiert (kein weiterer Retry)"
                    )
                    print(f"  🚫 {msg}")
                    append_log(msg)
                    notify_error(task, provider.name, msg)
                    _finalize_task_with_result_checked(
                        task, msg, provider.name,
                        queue_line_no=queue_task.line_no, subtasks=task_subtasks,
                    )
                    _span.error("hang_blocked", retry_count=single_shot_retry_count)
                    break
                reset_dt = datetime.now() + timedelta(seconds=HANG_RETRY_BACKOFF_SEC)
                reset_at_marker = reset_dt.strftime("%Y-%m-%d %H:%M")
                msg = (
                    f"Hang ({provider.name}) #{hang_count} "
                    f"→ Requeue um ~{reset_dt.strftime('%H:%M')}"
                )
                print(f"  {msg}")
                append_log(msg)
                notify_error(task, provider.name, error)
                if not mark_retry(
                    task, reset_at_marker, line_no=queue_task.line_no,
                    subtasks=task_subtasks, hang_count=hang_count,
                ):
                    _span.error("queue_update_failed", retry_count=single_shot_retry_count)
                    _span.emit()
                    return False
                _span.retry("hang", retry_count=single_shot_retry_count)
                break

            # NOTE on "stdin_incomplete" (prompt not fully delivered, see
            # providers/process_runner._feed_stdin): deliberately NOT special-
            # cased here. It takes the generic else-branch below — 5-min
            # provider cooldown + rotation — which is bounded and self-healing:
            # this run rotates and breaks without touching the queue line; on a
            # LATER poll, with every provider still cooled, select_provider
            # returns None and the `if not tried_providers` branch parks the
            # task via mark_retry until the cooldowns expire.
            # An earlier attempt to skip cooldown+rotation (a local pipe fault
            # says nothing about provider health, and all providers share the
            # feeder) removed that throttle without replacing it, producing an
            # unbounded 30-second retry loop. Rotating costs one prompt per
            # provider; looping forever costs everything. If this is revisited,
            # it needs the `hang` treatment: a persistent counter in the queue
            # line, backoff, and BLOCK after N — not a bare `break`.
            if error == "rate_limit":
                limits = get_limits(force_refresh=True)
                lim = getattr(limits, provider.name)
                provider_reset = fmt_time(lim.resets_in_sec) if lim.resets_in_sec else "unbekannt"
                msg = f"{provider.name} rate-limit → reset in {provider_reset}"
                append_log(msg)
                provider.set_cooldown(_rate_limit_cooldown_sec(limits, provider.name))
            elif error == "unreachable":
                provider.set_cooldown()
                msg = f"{provider.name} nicht erreichbar → Cooldown 30min"
                append_log(msg)
            elif error == "timeout":
                msg = f"{provider.name} Timeout nach {fmt_time(timeout)} — Task zu komplex; #timeout:Xm in Task hinzufügen"
                append_log(msg)
                print(f"  ⏱ {msg}")
                # No cooldown: timeout is a task-complexity issue, not a provider health issue
            else:
                msg = f"{provider.name} Fehler nach {MAX_RETRIES_PER_PROVIDER} Versuchen: {error}"
                append_log(msg)
                provider.set_cooldown(5 * 60)

            notify_error(task, provider.name, error)

            if provider_is_forced:
                # Strict mode: no fallback, retry later
                earliest = _get_next_retry_sec(limits)
                reset_dt = datetime.now() + timedelta(seconds=earliest)
                reset_at_marker = reset_dt.strftime("%Y-%m-%d %H:%M")
                msg = f"Provider {provider.name} erzwungen aber nicht verfügbar → Retry um ~{reset_dt.strftime('%H:%M')}"
                print(f"  {msg}")
                append_log(msg)
                if not _mark_retry_checked(task, reset_at_marker, queue_line_no=queue_task.line_no, subtasks=task_subtasks):
                    _span.error("queue_update_failed", retry_count=single_shot_retry_count)
                    _span.emit()
                    return False
                _span.retry(error or "rate_limit", retry_count=single_shot_retry_count)
                break

            print("  Task bleibt in Queue - versuche nächsten Provider...")

        # Emit replay record for the single-shot run
        if not _span.emitted:
            if single_shot_success:
                # _span.ok() already called inside the success branch
                _span.emit()
            else:
                # Fell out of the while loop without explicit retry/error tag —
                # default to error with the last observed code (best-effort).
                if _span.exit_status == replay.EXIT_ERROR and _span.error_code is None:
                    _span.error_code = "provider_unreachable"
                _span.emit()

        # Feature 10: trigger shutdown after single-shot task if tagged
        if task_has_shutdown:
            from shutdown import request_shutdown
            if request_shutdown():
                print("  [shutdown] #shutdown erkannt → Shutdown ausstehend")
            return False

    if dry_run:
        print("\n[DRY-RUN] Keine Tasks ausgeführt.")
        return True

    # All tasks blocked by #needs: dependencies — signal caller to wait longer
    if eligible == 0 and blocked_count > 0:
        print(f"\nAlle {blocked_count} Task(s) blockiert (warte auf Abhängigkeiten).")
        return None

    remaining = read_queue()
    if not remaining:
        print("\n✅ Alle Tasks erledigt!")
        append_log("Alle Tasks erledigt.")
        notify_queue_complete(0)
        return True

    print(f"\n{len(remaining)} Task(s) noch offen.")
    notify_queue_complete(len(remaining))
    return False


def run_watch(dry_run: bool = False) -> None:
    """Continuously process queue, sleeping when all providers are exhausted."""
    from doctor import run_startup_checks
    from heartbeat import HeartbeatRunner, _log_capacity, start_heartbeat_thread
    if not run_startup_checks():
        print("CRITICAL: Startup checks failed. Run --doctor to see details.")
        sys.exit(1)

    set_queue_idle(False)  # reset any stale idle state from a previous run
    print("Orchestrator gestartet (--watch Modus). Ctrl+C zum Beenden.")
    append_log("Orchestrator gestartet (watch)")
    start_session()

    # Startup delay: wait for provider tokens to renew
    if STARTUP_DELAY_SEC > 0:
        print(f"\n[startup] Warte {fmt_time(STARTUP_DELAY_SEC)} vor Queue-Verarbeitung (Token-Erneuerung)...")
        append_log(f"Startup-Delay: {fmt_time(STARTUP_DELAY_SEC)}")
        slept = 0
        while slept < STARTUP_DELAY_SEC:
            time.sleep(min(10, STARTUP_DELAY_SEC - slept))
            slept += min(10, STARTUP_DELAY_SEC - slept)
            remaining = STARTUP_DELAY_SEC - slept
            if remaining > 0:
                print(f"  [startup] noch {fmt_time(int(remaining))}...", end="\r")
        print(f"[startup] Delay abgeschlossen, starte Queue-Verarbeitung.")

    pause_event = threading.Event()
    listener = TelegramListener(pause_event)
    listener.start()

    heartbeat = HeartbeatRunner()

    # Write a fresh capacity snapshot right after startup so the dashboard
    # timeline is current from the first second. _log_capacity() reads from
    # the bg-daemon in-memory cache — no extra cclimits call.
    try:
        _log_capacity()
    except Exception:
        pass  # non-critical: dashboard will get fresh data on next heartbeat

    # Run heartbeat in a background thread so scheduled checks (log-capacity,
    # usage-suggest, check-limits, etc.) fire on time even when the main thread
    # is blocked for hours inside a long-running task.
    _hb_stop = threading.Event()
    start_heartbeat_thread(heartbeat, read_queue, _hb_stop, pause_event=pause_event)

    def _cleanup():
        listener.stop()
        _hb_stop.set()

    try:
        while True:
            # Honour /pause command from Telegram
            if pause_event.is_set():
                print("\n[pause] Orchestrator pausiert. Warte auf /resume...")
                set_paused(True)   # stop bg cclimits polling while paused
                try:
                    while pause_event.is_set():
                        time.sleep(5)
                finally:
                    set_paused(False)   # resume → bg thread refreshes immediately
                print("[pause] Fortgesetzt.")
                continue

            tasks = read_queue()
            if not tasks:
                # Feature 10: if shutdown pending and queue drained, start countdown
                try:
                    from shutdown import execute_shutdown, shutdown_pending as _sp
                    if _sp.is_set() and not pause_event.is_set():
                        print("\n[shutdown] Queue leer + #shutdown gesetzt → Countdown startet")
                        execute_shutdown(cleanup_cb=_cleanup)
                        # No return here! Continue the loop so we can resume if cancelled.
                        continue
                except Exception:
                    pass

                set_queue_idle(True)   # reduce cclimits polling to 10 min while idle
                print("\nQueue leer. Warte auf neue Tasks (alle 5min prüfen)...")
                heartbeat.run_due(read_queue)
                time.sleep(SLEEP_POLL_INTERVAL)
                continue

            set_queue_idle(False)  # task found → wake bg thread for fresh limits check
            done = run_once(dry_run=dry_run, pause_event=pause_event)

            # Run heartbeat checks after each queue cycle
            heartbeat.run_due(read_queue)

            if pause_event.is_set():
                continue

            if dry_run:
                return

            # Feature 10: check shutdown after each run_once cycle
            try:
                from shutdown import execute_shutdown, shutdown_pending as _sp
                if _sp.is_set() and not pause_event.is_set():
                    print("\n[shutdown] #shutdown gesetzt → Countdown startet")
                    execute_shutdown(cleanup_cb=_cleanup)
                    # No return here!
                    continue
            except Exception:
                pass

            if done is True:
                print("\nQueue abgearbeitet. Warte auf neue Tasks...")
                time.sleep(60)
                continue

            if done is None:
                # All tasks blocked by #needs: dependencies — wait like idle queue
                print(f"\nAlle Tasks blockiert. Prüfe erneut in {fmt_time(SLEEP_POLL_INTERVAL)}...")
                time.sleep(SLEEP_POLL_INTERVAL)
                continue

            print("\nPrüfe Reset-Zeiten...")
            limits = get_limits(force_refresh=True)

            if limits.any_available():
                # Providers are available — failure was task-specific (tool error,
                # format mismatch, etc.), not capacity exhaustion.  Retry quickly
                # so remaining queue tasks are not blocked for 50 minutes.
                sleep_sec = 30
                print(f"Provider verfügbar — kurze Pause ({sleep_sec}s) vor nächstem Versuch")
            else:
                sleep_sec = _get_next_retry_sec(limits)
                sleep_sec = min(sleep_sec, SLEEP_POLL_INTERVAL * 10)

            # Ensure minimal sleep to prevent busy loops
            sleep_sec = max(5, sleep_sec)

            wake_at = (datetime.now() + timedelta(seconds=sleep_sec)).strftime("%H:%M:%S")
            print(f"Schlafe {fmt_time(sleep_sec)} → Neuversuch um {wake_at}")
            append_log(f"Schlafe {fmt_time(sleep_sec)} → Neuversuch um {wake_at}")

            slept = 0
            while slept < sleep_sec:
                if pause_event.is_set():
                    break  # Wake up immediately to honour /pause
                chunk = min(SLEEP_POLL_INTERVAL, sleep_sec - slept)
                time.sleep(chunk)
                slept += chunk
                remaining = sleep_sec - slept
                if remaining > 0:
                    print(f"  ... noch {fmt_time(int(remaining))}", end="\r")

            print()
    finally:
        _hb_stop.set()
        listener.stop()


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="AI Task Orchestrator")
    parser.add_argument("--watch", "-w", action="store_true",
                        help="Läuft kontinuierlich, retried automatisch")
    parser.add_argument("--check-limits", action="store_true",
                        help="Zeigt aktuelle Usage-Limits")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validiert Tasks ohne auszuführen")
    parser.add_argument("--list-tools", action="store_true",
                        help="Zeigt verfügbare Tools")
    parser.add_argument("--dashboard", action="store_true",
                        help="Startet das Analytics-Dashboard im Browser")
    parser.add_argument("--doctor", action="store_true",
                        help="Validiert das gesamte Setup")
    parser.add_argument("--fix", action="store_true",
                        help="Auto-fixe Probleme (mit --doctor)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Nicht-interaktiver Fix (mit --doctor --fix)")
    parser.add_argument("--lint-queue", action="store_true",
                        help="Validiert agent-queue.md ohne Ausführung")
    args = parser.parse_args()

    if args.lint_queue:
        from queue_linter import run_lint
        sys.exit(run_lint())

    if args.doctor:
        from doctor import run_doctor
        sys.exit(0 if run_doctor(fix=args.fix, yes=args.yes) else 1)

    if args.dashboard:
        from dashboard import start_server
        start_server()
        return

    ensure_queue_file()

    if args.list_tools:
        print("\nVerfügbare Tools:")
        for name, desc in list_tools().items():
            print(f"  #tool:{name:15} → {desc}")
        return

    if args.check_limits:
        limits = get_limits()
        print("\nAktuelle Usage-Limits:")
        for name in ("claude", "gemini", "codex"):
            lim = getattr(limits, name)
            status = f"{lim.remaining_pct:.1f}% remaining" if lim.available else f"❌ {lim.error}"
            reset = f", reset in {fmt_time(lim.resets_in_sec)}" if lim.resets_in_sec else ""
            print(f"  {name:8}: {status}{reset}")
            for wname, wdata in sorted(lim.windows.items()):
                print(f"    {wname:20}: {wdata.remaining_pct:.1f}% remaining, reset in {fmt_time(wdata.resets_in_sec)}")
            if name == "claude" and "seven_day" in lim.windows:
                from usage_budget import compute_window_pace, format_pace_status
                w = lim.windows["seven_day"]
                pace = compute_window_pace(w.remaining_pct, w.resets_in_sec, 7)
                print(f"    {format_pace_status(pace)}")
        return

    if args.dry_run:
        run_once(dry_run=True)
        return

    start_session()

    if args.watch:
        try:
            run_watch()
        except KeyboardInterrupt:
            print("\n\nOrchestrator gestoppt.")
            append_log("Orchestrator manuell gestoppt.")
            notify_queue_complete(len(read_queue()))
    else:
        run_once()


if __name__ == "__main__":
    main()
