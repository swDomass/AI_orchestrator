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
import sys
import time
from datetime import datetime, timedelta

from config import SLEEP_POLL_INTERVAL, MAX_RETRIES_PER_PROVIDER, SYSTEM_PROMPTS, TASK_TIMEOUT_SEC
from dispatcher import select_provider
from limits import get_limits
from notifier import (
    notify_error,
    notify_providers_exhausted,
    notify_queue_complete,
    notify_task_done,
    start_session,
)
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


def _build_prompt(task: str, provider_name: str) -> str:
    """Build final prompt: system prompt + file context + task (without metadata tags)."""
    enriched = inject_file_context(task)
    clean = strip_metadata_tags(enriched)

    system = SYSTEM_PROMPTS.get(provider_name, "")
    if system:
        return f"{system}\n\n{clean}"
    return clean


def _run_with_retry(provider, task: str, prompt: str, cwd: str | None, timeout: int) -> tuple:
    """
    Run task on provider with retries. Returns (result, exhausted).
    exhausted=True means all retries failed.
    """
    for attempt in range(MAX_RETRIES_PER_PROVIDER):
        result = provider.run(prompt, cwd=cwd, timeout=timeout)

        if result.success:
            return result, False

        if result.error in ("rate_limit", "unreachable"):
            return result, True

        if attempt < MAX_RETRIES_PER_PROVIDER - 1:
            wait = 2 ** attempt
            print(f"  Retry {attempt + 1}/{MAX_RETRIES_PER_PROVIDER} in {wait}s...")
            time.sleep(wait)

    return result, True


def _execute_tool_task(task: str, tool_name: str, provider, cwd: str | None) -> bool:
    """Execute a tool-based task (iterative loop). Returns True on success."""
    tool = get_tool(tool_name)
    if not tool:
        print(f"  ❌ Unbekanntes Tool: {tool_name}")
        append_log(f"Unbekanntes Tool: {tool_name}")
        notify_error(task, provider.name, f"Tool nicht gefunden: {tool_name}")
        return False

    print(f"  → Tool: {tool.name} ({tool.description})")
    clean_task = strip_metadata_tags(task)
    tool_result = tool.run(clean_task, provider, cwd=cwd)

    if tool_result.success:
        print(f"  ✅ Tool erledigt ({tool_result.iterations} Iteration(en))")
        mark_done(task, f"{provider.name}+{tool.name}")
        append_result(task, tool_result.output, f"{provider.name}+{tool.name}")
        append_log(f"Tool {tool.name} erledigt via {provider.name} ({tool_result.iterations}x): {task[:60]}")
        notify_task_done(task, f"{provider.name}+{tool.name}", tool_result.output)
        return True
    else:
        print(f"  ⚠️ Tool beendet: {tool_result.error}")
        append_result(task, tool_result.output, f"{provider.name}+{tool.name}")
        append_log(f"Tool {tool.name} Fehler: {tool_result.error}")
        notify_error(task, f"{provider.name}+{tool.name}", tool_result.error)
        # Mark done even on failure (to prevent infinite retries of broken tools)
        mark_done(task, f"{provider.name}+{tool.name}")
        return False


def run_once(dry_run: bool = False) -> bool:
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
        provider = select_provider(task, limits)

        if provider is None:
            earliest = limits.earliest_reset_sec()
            reset_at = (datetime.now() + timedelta(seconds=earliest)).strftime("%H:%M")
            msg = f"Alle Provider voll/unreachable → Task wartet bis ~{reset_at}"
            print(f"  {msg}")
            append_log(msg)
            mark_retry(task, reset_at)
            notify_providers_exhausted(fmt_time(earliest))
            return False

        print(f"  → Provider: {provider.name}")

        # Tool-based task (iterative loop)
        if tool_name:
            _execute_tool_task(task, tool_name, provider, cwd)
            continue

        # Standard single-shot task
        prompt = _build_prompt(task, provider.name)
        result, exhausted = _run_with_retry(provider, task, prompt, cwd, timeout)

        if result.success:
            print(f"  ✅ Erledigt ({len(result.output)} Zeichen Output)")
            mark_done(task, provider.name)
            append_result(task, result.output, provider.name)
            append_log(f"Task erledigt via {provider.name}: {task[:60]}")
            notify_task_done(task, provider.name, result.output)

        else:
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
            continue

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

    while True:
        tasks = read_queue()
        if not tasks:
            print("\nQueue leer. Warte auf neue Tasks (alle 60s prüfen)...")
            time.sleep(60)
            continue

        done = run_once(dry_run=dry_run)

        if dry_run:
            return

        if done:
            print("\nQueue abgearbeitet. Warte auf neue Tasks...")
            time.sleep(60)
            continue

        print("\nPrüfe Reset-Zeiten...")
        limits = get_limits()
        sleep_sec = limits.earliest_reset_sec()
        sleep_sec = min(sleep_sec, SLEEP_POLL_INTERVAL * 10)

        wake_at = (datetime.now() + timedelta(seconds=sleep_sec)).strftime("%H:%M:%S")
        print(f"Schlafe {fmt_time(sleep_sec)} → Neuversuch um {wake_at}")
        append_log(f"Schlafe {fmt_time(sleep_sec)} → Neuversuch um {wake_at}")

        slept = 0
        while slept < sleep_sec:
            chunk = min(SLEEP_POLL_INTERVAL, sleep_sec - slept)
            time.sleep(chunk)
            slept += chunk
            remaining = sleep_sec - slept
            if remaining > 0:
                print(f"  ... noch {fmt_time(int(remaining))}", end="\r")

        print()


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
