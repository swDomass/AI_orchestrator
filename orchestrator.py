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
import os
import subprocess
import sys
import threading
import time

# Ensure UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError for →, ✅, ❌, etc.)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timedelta

from config import (
    GIT_AUTO_STASH,
    MAX_RETRIES_PER_PROVIDER,
    SLEEP_POLL_INTERVAL,
    SYSTEM_PROMPTS,
    TASK_TIMEOUT_SEC,
    TRACK_FILE_CHANGES,
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
from queue_manager import (
    append_log,
    append_result,
    ensure_queue_file,
    extract_cwd,
    extract_timeout,
    inject_file_context,
    mark_done,
    mark_retry,
    read_queue,
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
    """Walk top-level files (1 level deep) and record {filename: (mtime, size)}."""
    snapshot: dict[str, tuple[float, int]] = {}
    try:
        for entry in os.scandir(cwd):
            try:
                stat = entry.stat()
                snapshot[entry.name] = (stat.st_mtime, stat.st_size)
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
    """Get git diff --stat summary for the working directory."""
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--stat"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


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


def _build_prompt(task: str, provider_name: str) -> str:
    """Build final prompt: system prompt + file context + task (without metadata tags)."""
    # Strip routing tags only from the queue task text, not from injected file contents.
    clean_task = strip_metadata_tags(task)
    clean = inject_file_context(clean_task)

    system = SYSTEM_PROMPTS.get(provider_name, "")
    if system:
        return f"{system}\n\n{clean}"
    return clean


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


def _execute_tool_task(task: str, tool_name: str, provider, cwd: str | None) -> bool:
    """Execute a tool-based task (iterative loop). Returns True on success."""
    tool = get_tool(tool_name)
    if not tool:
        print(f"  ❌ Unbekanntes Tool: {tool_name}")
        append_log(f"Unbekanntes Tool: {tool_name}")
        notify_error(task, provider.name, f"Tool nicht gefunden: {tool_name}")
        return False

    # Safety: snapshot before execution
    is_git = bool(cwd) and _is_git_repo(cwd)
    snap_before = _snapshot_dir(cwd) if cwd and TRACK_FILE_CHANGES else None
    if cwd:
        _git_snapshot(cwd, is_git=is_git)

    print(f"  → Tool: {tool.name} ({tool.description})")
    clean_task = strip_metadata_tags(task)
    tool_result = tool.run(clean_task, provider, cwd=cwd)

    # Safety: build change summary
    change_summary = _get_change_summary(cwd, snap_before, is_git=is_git)
    if change_summary:
        print(f"  [safety] Änderungen:\n{change_summary}")

    provider_tool = f"{provider.name}+{tool.name}"

    if tool_result.success:
        print(f"  ✅ Tool erledigt ({tool_result.iterations} Iteration(en))")
        mark_done(task, provider_tool)
        append_result(task, tool_result.output, provider_tool)
        append_log(f"Tool {tool.name} erledigt via {provider.name} ({tool_result.iterations}x): {task[:60]}")
        notify_task_done(task, provider_tool, tool_result.output, change_summary=change_summary)
        return True
    else:
        print(f"  ⚠️ Tool beendet: {tool_result.error}")
        append_result(task, tool_result.output, provider_tool)
        append_log(f"Tool {tool.name} Fehler: {tool_result.error}")
        notify_error(task, f"{provider.name}+{tool.name}", tool_result.error)
        # Mark done even on failure (to prevent infinite retries of broken tools)
        mark_done(task, provider_tool)
        return False


def run_once(dry_run: bool = False, pause_event: threading.Event | None = None) -> bool:
    """
    Process all open tasks in the queue once.
    Returns True if all tasks were completed, False if stopped early.
    """
    tasks = read_queue()
    if not tasks:
        print("Queue leer - nichts zu tun.")
        return True

    print(f"\n{'='*60}")
    print(f"Queue: {len(tasks)} offene Task(s)")
    print(f"{'='*60}")

    for i, task in enumerate(tasks, 1):
        if pause_event and pause_event.is_set():
            print("\n[pause] Queue-Verarbeitung pausiert.")
            append_log("Queue-Verarbeitung pausiert")
            return False

        print(f"\n[{i}/{len(tasks)}] Task: {task[:80]}{'...' if len(task) > 80 else ''}")

        # Extract task metadata
        cwd = extract_cwd(task)
        timeout = extract_timeout(task, default=TASK_TIMEOUT_SEC)
        tool_name = extract_tool_tag(task)

        if cwd:
            print(f"  [cwd] {cwd}")
        if timeout != TASK_TIMEOUT_SEC:
            print(f"  [timeout] {fmt_time(timeout)}")
        if tool_name:
            print(f"  [tool] {tool_name}")

        # Dry-run
        if dry_run:
            limits = get_limits()
            provider = select_provider(task, limits)
            prompt = _build_prompt(task, provider.name if provider else "claude")
            print(f"  [DRY-RUN] Provider: {provider.name if provider else 'KEINER VERFÜGBAR'}")
            print(f"  [DRY-RUN] Tool: {tool_name or 'keins (single-shot)'}")
            print(f"  [DRY-RUN] Prompt-Länge: {len(prompt)} Zeichen")
            continue

        # Get current limits
        print("  Prüfe Usage-Limits (cclimits)...")
        limits = get_limits()

        # Tool-based task (iterative loop)
        if tool_name:
            provider = select_provider(task, limits)
            if provider is None:
                earliest = _get_next_retry_sec(limits)
                reset_at = (datetime.now() + timedelta(seconds=earliest)).strftime("%H:%M")
                msg = f"Alle Provider voll/unreachable → Task wartet bis ~{reset_at}"
                print(f"  {msg}")
                append_log(msg)
                mark_retry(task, reset_at)
                notify_providers_exhausted(fmt_time(earliest))
                return False
            print(f"  → Provider: {provider.name}")
            _execute_tool_task(task, tool_name, provider, cwd)
            continue

        # Safety: snapshot before execution
        is_git = bool(cwd) and _is_git_repo(cwd)
        snap_before = _snapshot_dir(cwd) if cwd and TRACK_FILE_CHANGES else None
        if cwd:
            _git_snapshot(cwd, is_git=is_git)

        # Standard single-shot task with provider fallback in the same run
        tried_providers: set[str] = set()
        while True:
            if pause_event and pause_event.is_set():
                print("\n[pause] Queue-Verarbeitung pausiert.")
                append_log("Queue-Verarbeitung pausiert")
                return False

            provider = select_provider(task, limits, exclude=tried_providers)

            if provider is None:
                if not tried_providers:
                    earliest = _get_next_retry_sec(limits)
                    reset_at = (datetime.now() + timedelta(seconds=earliest)).strftime("%H:%M")
                    msg = f"Alle Provider voll/unreachable → Task wartet bis ~{reset_at}"
                    print(f"  {msg}")
                    append_log(msg)
                    mark_retry(task, reset_at)
                    notify_providers_exhausted(fmt_time(earliest))
                    return False

                print("  Keine weiteren Provider verfügbar - Task bleibt in Queue.")
                append_log(f"Keine weiteren Provider verfügbar für Task: {task[:60]}")
                break

            print(f"  → Provider: {provider.name}")
            prompt = _build_prompt(task, provider.name)
            result, _exhausted = _run_with_retry(
                provider, task, prompt, cwd, timeout, pause_event=pause_event
            )

            if result.error == "paused":
                print("\n[pause] Queue-Verarbeitung pausiert.")
                append_log("Queue-Verarbeitung pausiert")
                return False

            if result.success:
                print(f"  ✅ Erledigt ({len(result.output)} Zeichen Output)")
                change_summary = _get_change_summary(cwd, snap_before, is_git=is_git)
                if change_summary:
                    print(f"  [safety] Änderungen:\n{change_summary}")
                mark_done(task, provider.name)
                append_result(task, result.output, provider.name)
                append_log(f"Task erledigt via {provider.name}: {task[:60]}")
                notify_task_done(task, provider.name, result.output, change_summary=change_summary)
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
    print("Orchestrator gestartet (--watch Modus). Ctrl+C zum Beenden.")
    append_log("Orchestrator gestartet (watch)")
    start_session()

    pause_event = threading.Event()
    listener = TelegramListener(pause_event)
    listener.start()

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
                print("\nQueue leer. Warte auf neue Tasks (alle 60s prüfen)...")
                time.sleep(60)
                continue

            done = run_once(dry_run=dry_run, pause_event=pause_event)

            if pause_event.is_set():
                continue

            if dry_run:
                return

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
    parser = argparse.ArgumentParser(description="AI Task Orchestrator")
    parser.add_argument("--watch", "-w", action="store_true",
                        help="Läuft kontinuierlich, retried automatisch")
    parser.add_argument("--check-limits", action="store_true",
                        help="Zeigt aktuelle Usage-Limits")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validiert Tasks ohne auszuführen")
    parser.add_argument("--list-tools", action="store_true",
                        help="Zeigt verfügbare Tools")
    args = parser.parse_args()

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
