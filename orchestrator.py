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
    MAX_RETRIES_PER_PROVIDER,
    PROMPT_MEMORY_TOKENS,
    PROMPT_SKILL_TOKENS,
    PROMPT_WIKILINK_TOKENS,
    SLEEP_POLL_INTERVAL,
    TASK_TIMEOUT_SEC,
    TRACK_FILE_CHANGES,
    get_system_prompt,
)
from dispatcher import select_provider, earliest_cooldown_reset
from limits import get_limits, AllLimits
from notifier import (
    notify_error,
    notify_providers_exhausted,
    notify_queue_complete,
    notify_task_done,
    start_session,
)
from providers.base import RunResult
from skills import load_skill, check_requirements
from config import VAULT_PATH
import memory as memory_module
from queue_manager import (
    append_log,
    ensure_queue_file,
    extract_cwd,
    extract_preapproved_actions,
    extract_profile_tag,
    extract_shutdown_tag,
    extract_timeout,
    finalize_task_with_result,
    has_cwd_tag,
    inject_file_context,
    mark_done,
    mark_retry,
    read_queue,
    read_queue_items,
    strip_metadata_tags,
)
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
            cwd=cwd, capture_output=True, text=True, timeout=5,
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
            cwd=cwd, capture_output=True, text=True, timeout=30,
        )
        if create.returncode != 0:
            return None

        stash_commit = create.stdout.strip()
        if not stash_commit:
            return None

        store = subprocess.run(
            ["git", "stash", "store", "-m", msg, stash_commit],
            cwd=cwd, capture_output=True, text=True, timeout=30,
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
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        if tracked.returncode == 0 and tracked.stdout.strip():
            parts.append(tracked.stdout.strip())

        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
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
    3. Relevant past context (memory) — budget-capped
    4. File/wikilink context + task text — budget-capped
    """
    from skills import load_skill

    # Strip routing tags only from the queue task text, not from injected file contents.
    clean_task = strip_metadata_tags(task)

    # 1. Core prompt (always)
    core = get_system_prompt(provider_name)

    # 2. Skill body (only when #tool: present)
    skill_prompt = ""
    if skill_name:
        skill = load_skill(skill_name, vault_path=VAULT_PATH)
        if skill and skill.prompt:
            skill_prompt = _truncate_tokens(skill.prompt, PROMPT_SKILL_TOKENS)

    # 3. Memory context (pre-filtered by get_context_for_task)
    mem_block = _truncate_tokens(memory_context, PROMPT_MEMORY_TOKENS) if memory_context else ""

    # 4. Wikilink / file context (budget-capped); ~5 chars per token
    max_wiki_chars = PROMPT_WIKILINK_TOKENS * 5
    wiki_ctx = inject_file_context(clean_task, max_chars=max_wiki_chars)

    # Assemble
    parts: list[str] = []
    if core:
        parts.append(core)
    if skill_prompt:
        parts.append(f"## Skill: {skill_name}\n{skill_prompt}")
    if mem_block:
        parts.append(f"## Relevanter vergangener Kontext\n{mem_block}")
    parts.append(wiki_ctx)

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

        if result.error in ("rate_limit", "unreachable"):
            return result, True

        if attempt < MAX_RETRIES_PER_PROVIDER - 1:
            wait = 2 ** attempt
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



def _mark_done_checked(task: str, provider: str, *, queue_line_no: int | None = None) -> bool:
    """Mark task done and return False if queue mutation failed."""
    if mark_done(task, provider, line_no=queue_line_no):
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
) -> bool:
    """Atomically persist result + done status and return False on queue mutation failure."""
    if finalize_task_with_result(task, result, provider, line_no=queue_line_no):
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
) -> bool:
    """Mark task for retry and return False if queue mutation failed."""
    if mark_retry(task, retry_at, line_no=queue_line_no):
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
            finalized = _mark_done_checked(task, "failed", queue_line_no=queue_line_no)
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
                finalized = _mark_done_checked(task, "failed", queue_line_no=queue_line_no)
            else:
                finalized = False
            return ToolTaskExecutionOutcome(success=False, finalized=finalized, error=msg)

    # Safety: snapshot before execution
    is_git = bool(cwd) and _is_git_repo(cwd)
    snap_before = _snapshot_dir(cwd) if cwd and TRACK_FILE_CHANGES else None
    if cwd:
        _git_snapshot(cwd, is_git=is_git)

    print(f"  → Tool: {tool.name} ({tool.description})")
    clean_task = strip_metadata_tags(task)
    _tool_start = time.time()
    tool_result = tool.run(clean_task, provider, cwd=cwd, timeout=timeout, memory_context=memory_context)
    _tool_duration = time.time() - _tool_start

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
            ):
                return ToolTaskExecutionOutcome(
                    success=False,
                    finalized=False,
                    error="queue_update_failed",
                )
            memory_module.store_result(
                task, tool_result.output, provider_tool, _tool_duration, cwd=cwd, success=True
            )
            append_log(f"Tool {tool.name} erledigt via {provider.name} ({tool_result.iterations}x): {task[:60]}")
            notify_task_done(task, provider_tool, tool_result.output, change_summary=change_summary)
        return ToolTaskExecutionOutcome(success=True, finalized=not skip_queue)
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
            )

        if not skip_queue:
            if not _finalize_task_with_result_checked(
                task,
                tool_result.output,
                provider_tool,
                queue_line_no=queue_line_no,
            ):
                return ToolTaskExecutionOutcome(
                    success=False,
                    finalized=False,
                    error="queue_update_failed",
                    error_code=tool_result.error_code,
                )
            memory_module.store_result(
                task, tool_result.output or tool_result.error, provider_tool,
                _tool_duration, cwd=cwd, success=False,
            )
            append_log(f"Tool {tool.name} Fehler: {tool_result.error}")
            notify_error(task, f"{provider.name}+{tool.name}", tool_result.error)
        return ToolTaskExecutionOutcome(
            success=False,
            finalized=not skip_queue,
            error=tool_result.error,
            error_code=tool_result.error_code,
        )


def run_once(dry_run: bool = False, pause_event: threading.Event | None = None) -> bool:
    """
    Process all open tasks in the queue once.
    Returns True if all tasks were completed, False if stopped early.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    # Archive old memories once per cycle (silent, never blocks)
    try:
        archived = memory_module.archive_old_memories()
        if archived:
            _log.debug("Archived %d old memories", archived)
    except Exception:
        pass

    task_items = read_queue_items()
    if not task_items:
        print("Queue leer - nichts zu tun.")
        return True

    print(f"\n{'='*60}")
    print(f"Queue: {len(task_items)} offene Task(s)")
    print(f"{'='*60}")

    for i, queue_task in enumerate(task_items, 1):
        if pause_event and pause_event.is_set():
            print("\n[pause] Queue-Verarbeitung pausiert.")
            append_log("Queue-Verarbeitung pausiert")
            return False

        task = queue_task.task_text
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

        # Feature 6: denied_skills check
        if tool_name and profile and tool_name in profile.denied_skills:
            msg = f"Tool '{tool_name}' durch Profil '{profile.name}' gesperrt (denied_skills)"
            print(f"  ❌ {msg}")
            if not dry_run:
                append_log(msg)
                notify_error(task, "profile", msg)
                if not _mark_done_checked(task, "profile-denied", queue_line_no=queue_task.line_no):
                    return False
            continue

        # Feature 6: allowed_skills whitelist check
        if tool_name and profile and profile.allowed_skills and tool_name not in profile.allowed_skills:
            msg = f"Tool '{tool_name}' nicht in allowed_skills von Profil '{profile.name}'"
            print(f"  ❌ {msg}")
            if not dry_run:
                append_log(msg)
                notify_error(task, "profile", msg)
                if not _mark_done_checked(task, "profile-denied", queue_line_no=queue_task.line_no):
                    return False
            continue

        if cwd_tag_present and cwd is None:
            msg = "Ungültiges cwd:-Tag (Verzeichnis fehlt oder ist nicht erlaubt) - Task wird nicht ausgeführt"
            print(f"  ❌ {msg}")
            if dry_run:
                continue
            append_log(msg)
            notify_error(task, "queue", msg)
            if not _mark_done_checked(task, "invalid-cwd", queue_line_no=queue_task.line_no):
                return False
            continue

        if cwd:
            print(f"  [cwd] {cwd}")
        if timeout != TASK_TIMEOUT_SEC:
            print(f"  [timeout] {fmt_time(timeout)}")
        if tool_name:
            print(f"  [tool] {tool_name}")

        # --- Feature 10: detect #shutdown tag ---
        task_has_shutdown = extract_shutdown_tag(task)

        # Dry-run
        if dry_run:
            limits = get_limits()
            provider = select_provider(task, limits, profile=profile)
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
                for st in queue_task.subtasks:
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
                if not _mark_done_checked(task, "policy-denied", queue_line_no=queue_task.line_no):
                    return False
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
                        if not _mark_retry_checked(task, reset_at, queue_line_no=queue_task.line_no):
                            return False
                        return False
                    elif response == "timeout":
                        print("  ⏱ Genehmigung timeout — Task bleibt in Queue.")
                        append_log(f"Genehmigung timeout für Task: {task[:60]}")
                        reset_at = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
                        if not _mark_retry_checked(task, reset_at, queue_line_no=queue_task.line_no):
                            return False
                        return False
                    elif response == "skipped":
                        print("  ⏭ Genehmigung übersprungen — riskante Aktion blockiert, Task bleibt in Queue.")
                        append_log(f"Genehmigung übersprungen für Task: {task[:60]}")
                        reset_at = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
                        if not _mark_retry_checked(task, reset_at, queue_line_no=queue_task.line_no):
                            return False
                        return False
                    # "approved" → continue
        except ImportError:
            pass
        except Exception as e:
            _log.warning("policy check failed: %s", e)

        # --- Feature 7: Parallel sub-agent spawning ---
        if getattr(queue_task, "subtasks", None):
            print(f"  [parallel] {len(queue_task.subtasks)} Subtask(s)")
            try:
                from parallel_runner import run_parallel, format_parallel_result
                results = run_parallel(
                    task,
                    queue_task.subtasks,
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
                    task, aggregated, provider_tag, queue_line_no=queue_task.line_no
                ):
                    return False
                memory_module.store_result(task, aggregated, provider_tag, 0.0, cwd=cwd, success=success_all)
                append_log(f"Parallel-Task erledigt: {task[:60]}")
                notify_task_done(task, provider_tag, aggregated)
            except Exception as e:
                msg = f"Parallel-Ausführung fehlgeschlagen: {e}"
                print(f"  ❌ {msg}")
                append_log(msg)
                notify_error(task, "parallel", msg)
                if not _mark_done_checked(task, "parallel-error", queue_line_no=queue_task.line_no):
                    return False

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
            while True:
                provider = select_provider(task, limits, exclude=tried_providers, profile=profile)
                if provider is None:
                    earliest = _get_next_retry_sec(limits)
                    reset_dt = datetime.now() + timedelta(seconds=earliest)
                    reset_at_display = reset_dt.strftime("%H:%M")
                    reset_at_marker = reset_dt.strftime("%Y-%m-%d %H:%M")
                    msg = f"Alle Provider voll/unreachable → Task wartet bis ~{reset_at_display}"
                    print(f"  {msg}")
                    append_log(msg)
                    if not _mark_retry_checked(task, reset_at_marker, queue_line_no=queue_task.line_no):
                        return False
                    notify_providers_exhausted(fmt_time(earliest))
                    return False

                print(f"  → Provider: {provider.name}")
                outcome = _execute_tool_task(
                    task,
                    tool_name,
                    provider,
                    cwd,
                    timeout=tool_timeout,
                    queue_line_no=queue_task.line_no,
                    memory_context=memory_context,
                )

                if outcome.success or outcome.finalized:
                    break

                if not outcome.retryable:
                    print("  ❌ Tool-Task nicht finalisiert (Queue-Update-Fehler). Task bleibt offen.")
                    append_log("Tool-Task nicht finalisiert wegen Queue-Update-Fehler")
                    return False

                tried_providers.add(provider.name)
                if outcome.error_code == "unreachable":
                    provider.set_cooldown()
                elif outcome.error_code not in ("rate_limit", ""):
                    provider.set_cooldown(5 * 60)

                print(f"  Task bleibt in Queue - versuche nächsten Provider ({outcome.error_code or outcome.error})...")

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
        while True:
            if pause_event and pause_event.is_set():
                print("\n[pause] Queue-Verarbeitung pausiert.")
                append_log("Queue-Verarbeitung pausiert")
                return False

            provider = select_provider(task, limits, exclude=tried_providers, profile=profile)

            if provider is None:
                if not tried_providers:
                    earliest = _get_next_retry_sec(limits)
                    reset_dt = datetime.now() + timedelta(seconds=earliest)
                    reset_at_display = reset_dt.strftime("%H:%M")
                    reset_at_marker = reset_dt.strftime("%Y-%m-%d %H:%M")
                    msg = f"Alle Provider voll/unreachable → Task wartet bis ~{reset_at_display}"
                    print(f"  {msg}")
                    append_log(msg)
                    if not _mark_retry_checked(task, reset_at_marker, queue_line_no=queue_task.line_no):
                        return False
                    notify_providers_exhausted(fmt_time(earliest))
                    return False

                print("  Keine weiteren Provider verfügbar - Task bleibt in Queue.")
                append_log(f"Keine weiteren Provider verfügbar für Task: {task[:60]}")
                break

            print(f"  → Provider: {provider.name}")
            prompt = _build_prompt(task, provider.name, memory_context=memory_context)
            start_time = time.time()
            result, _exhausted = _run_with_retry(
                provider, task, prompt, cwd, timeout, pause_event=pause_event
            )

            if result.error == "paused":
                print("\n[pause] Queue-Verarbeitung pausiert.")
                append_log("Queue-Verarbeitung pausiert")
                return False

            if result.success:
                duration = time.time() - start_time
                print(f"  ✅ Erledigt ({len(result.output)} Zeichen Output)")
                change_summary = _get_change_summary(cwd, snap_before, is_git=is_git)
                if change_summary:
                    print(f"  [safety] Änderungen:\n{change_summary}")
                if not _finalize_task_with_result_checked(
                    task,
                    result.output,
                    provider.name,
                    queue_line_no=queue_task.line_no,
                ):
                    return False
                memory_module.store_result(
                    task, result.output, provider.name, duration, cwd=cwd, success=True
                )
                append_log(f"Task erledigt via {provider.name}: {task[:60]}")
                notify_task_done(task, provider.name, result.output, change_summary=change_summary)
                single_shot_success = True
                break

            tried_providers.add(provider.name)
            error = result.error
            print(f"  ❌ Fehler: {error}")

            if error == "rate_limit":
                lim = getattr(limits, provider.name)
                provider_reset = fmt_time(lim.resets_in_sec) if lim.resets_in_sec else "unbekannt"
                msg = f"{provider.name} rate-limit → reset in {provider_reset}"
                append_log(msg)
            elif error == "unreachable":
                provider.set_cooldown()
                msg = f"{provider.name} nicht erreichbar → Cooldown 30min"
                append_log(msg)
            else:
                msg = f"{provider.name} Fehler nach {MAX_RETRIES_PER_PROVIDER} Versuchen: {error}"
                append_log(msg)
                provider.set_cooldown(5 * 60)

            notify_error(task, provider.name, error)
            print("  Task bleibt in Queue - versuche nächsten Provider...")

        # Feature 10: trigger shutdown after single-shot task if tagged
        if task_has_shutdown:
            from shutdown import request_shutdown
            if request_shutdown():
                print("  [shutdown] #shutdown erkannt → Shutdown ausstehend")
            return False

    if dry_run:
        print("\n[DRY-RUN] Keine Tasks ausgeführt.")
        return True

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
    from heartbeat import HeartbeatRunner
    if not run_startup_checks():
        print("CRITICAL: Startup checks failed. Run --doctor to see details.")
        sys.exit(1)

    print("Orchestrator gestartet (--watch Modus). Ctrl+C zum Beenden.")
    append_log("Orchestrator gestartet (watch)")
    start_session()

    pause_event = threading.Event()
    listener = TelegramListener(pause_event)
    listener.start()

    heartbeat = HeartbeatRunner()

    def _cleanup():
        listener.stop()

    try:
        while True:
            # Honour /pause command from Telegram
            if pause_event.is_set():
                print("\n[pause] Orchestrator pausiert. Warte auf /resume...")
                while pause_event.is_set():
                    time.sleep(5)
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
                        return
                except Exception:
                    pass

                print("\nQueue leer. Warte auf neue Tasks (alle 60s prüfen)...")
                heartbeat.run_due(read_queue)
                time.sleep(60)
                continue

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
                    return
            except Exception:
                pass

            if done:
                print("\nQueue abgearbeitet. Warte auf neue Tasks...")
                time.sleep(60)
                continue

            print("\nPrüfe Reset-Zeiten...")
            limits = get_limits()
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
    parser.add_argument("--doctor", action="store_true",
                        help="Validiert das gesamte Setup")
    parser.add_argument("--fix", action="store_true",
                        help="Auto-fixe Probleme (mit --doctor)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Nicht-interaktiver Fix (mit --doctor --fix)")
    args = parser.parse_args()

    if args.doctor:
        from doctor import run_doctor
        sys.exit(0 if run_doctor(fix=args.fix, yes=args.yes) else 1)

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
