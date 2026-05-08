"""
AI Orchestrator — Heartbeat Runner

Parses HEARTBEAT.md from the vault and runs scheduled checks at configurable
intervals. Sends Telegram notifications for non-empty results.

HEARTBEAT.md sections:
    ## Every 30 minutes   → interval_min = 30
    ## Every 2 hours      → interval_min = 120
    ## Daily (first run after 08:00) → daily_after = 480 (minute-of-day)

Items:
    - [ ] {description}

Built-in handlers matched by case-insensitive substring in item label:
    "queue"        → _check_queue_idle
    "git status"   → _check_git_status
    "disk space"   → _check_disk_space
    "check-limits" → _check_limits
    "summarize"    → _check_task_summary
    "stale branch" → _check_stale_branches
    "usage-suggest" → _check_usage_suggest
"""

import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Callable, Optional

from config import (
    ALLOWED_CWD_ROOTS,
    CAPACITY_LOG_FILE,
    CAPACITY_LOG_RETENTION_DAYS,
    HEARTBEAT_DISK_WARN_PCT,
    HEARTBEAT_FILE,
    HEARTBEAT_GIT_STALE_DAYS,
    HEARTBEAT_QUEUE_IDLE_HOURS,
    MIN_CAPACITY_PERCENT,
    VAULT_PATH,
)
from queue_manager import _write_bytes_atomic

logger = logging.getLogger(__name__)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class HeartbeatItem:
    label: str
    interval_min: int      # 0 = daily
    daily_after: int = 480 # minute-of-day threshold (for daily items)
    last_run: Optional[datetime] = None
    last_run_date: Optional[date] = None   # for daily items
    handler_key: str = ""


# ── Module-level state for queue-idle tracking ─────────────────────────────

_queue_empty_since: Optional[datetime] = None
_queue_empty_lock = threading.Lock()
_usage_suggest_thread: Optional[threading.Thread] = None


# ── Built-in handlers ─────────────────────────────────────────────────────────

def _check_queue_idle(queue_read_fn: Callable) -> Optional[str]:
    """Warn if the queue has been empty for >HEARTBEAT_QUEUE_IDLE_HOURS hours."""
    global _queue_empty_since

    try:
        tasks = queue_read_fn()
    except (OSError, ValueError):
        return None

    now = datetime.now()
    with _queue_empty_lock:
        if tasks:
            _queue_empty_since = None
            return None

        if _queue_empty_since is None:
            _queue_empty_since = now
            return None

        idle_hours = (now - _queue_empty_since).total_seconds() / 3600
        if idle_hours >= HEARTBEAT_QUEUE_IDLE_HOURS:
            return (
                f"Queue seit {idle_hours:.1f}h leer — "
                f"keine neuen Tasks seit {_queue_empty_since.strftime('%H:%M')}."
            )
    return None


def _check_git_status() -> Optional[str]:
    """Run git status --short in each ALLOWED_CWD_ROOTS dir."""
    results: list[str] = []
    roots = ALLOWED_CWD_ROOTS or []

    for root in roots:
        if not root.is_dir():
            continue
        try:
            r = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                lines = r.stdout.strip().splitlines()
                results.append(
                    f"{root.name}: {len(lines)} uncommitted change(s)\n"
                    + "\n".join(f"  {l}" for l in lines[:5])
                    + ("\n  ..." if len(lines) > 5 else "")
                )
        except (OSError, subprocess.TimeoutExpired):
            pass

    return "\n\n".join(results) if results else None


def _check_session_cleanup() -> Optional[str]:
    """Delete orchestrator-created Claude session JSONL files older than
    ``ORCH_SESSION_RETENTION_DAYS`` from ``~/.claude/projects/**``.

    Uses the sidecar registry (``logs/orchestrator-sessions.jsonl``) as a
    whitelist — interactive Claude Code sessions in the same cwd are NEVER
    touched because they're not in the registry. Only registry entries that
    match an actual file on disk get deleted; registry entries with no file
    are pruned silently (file may have been manually cleaned).
    """
    try:
        from session_registry import prune_old
        from config import ORCH_SESSION_RETENTION_DAYS
    except ImportError:
        return None

    kept, expired = prune_old(ORCH_SESSION_RETENTION_DAYS)
    if not expired:
        return None

    projects_dir = Path(os.path.expanduser("~/.claude/projects"))
    deleted: list[str] = []
    failed: list[str] = []
    if projects_dir.exists():
        for entry in expired:
            uuid = entry.get("uuid")
            if not uuid:
                continue
            # Glob across all project subdirs since the encoded cwd path differs.
            matches = list(projects_dir.glob(f"**/{uuid}.jsonl"))
            for match in matches:
                try:
                    match.unlink()
                    deleted.append(uuid)
                except OSError as exc:
                    failed.append(f"{uuid}: {exc}")

    msg_parts: list[str] = []
    if deleted:
        msg_parts.append(f"Session-Cleanup: {len(deleted)} alte Sessions gelöscht")
    if failed:
        msg_parts.append(f"Fehler beim Löschen: {len(failed)} ({'; '.join(failed[:3])})")
    return " | ".join(msg_parts) if msg_parts else None


def _check_disk_space() -> Optional[str]:
    """Check disk usage for drives in ALLOWED_CWD_ROOTS."""
    checked_drives: set[str] = set()
    warnings: list[str] = []
    roots = ALLOWED_CWD_ROOTS or []

    for root in roots:
        if not root.exists():
            continue
        # Use drive letter on Windows, mount point on Unix
        drive = root.anchor
        if drive in checked_drives:
            continue
        checked_drives.add(drive)

        try:
            usage = shutil.disk_usage(str(root))
            free_pct = (usage.free / usage.total) * 100
            if free_pct < HEARTBEAT_DISK_WARN_PCT:
                free_gb = usage.free / (1024 ** 3)
                total_gb = usage.total / (1024 ** 3)
                warnings.append(
                    f"Drive {drive}: {free_pct:.1f}% frei "
                    f"({free_gb:.1f} GB / {total_gb:.1f} GB)"
                )
        except OSError:
            pass

    return "\n".join(warnings) if warnings else None


def _append_capacity_log(limits) -> None:
    """Append one line per provider/window to the persistent capacity log in the vault."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines: list[str] = []
        for name in ("claude", "gemini", "codex"):
            lim = getattr(limits, name, None)
            if lim is None:
                continue
            avail = "true" if lim.available else "false"
            if name == "gemini" and lim.windows:
                # Skip vertex duplicates; prefer gemini_2 (current gen), fallback to all
                non_vertex = {k: v for k, v in lim.windows.items() if "vertex" not in k}
                selected = non_vertex if non_vertex else lim.windows
                for wname, wdata in selected.items():
                    pct = f"{wdata.remaining_pct:.1f}"
                    w_avail = "true" if wdata.remaining_pct >= MIN_CAPACITY_PERCENT else "false"
                    lines.append(f"{ts} | {name}_{wname} | {pct} | {w_avail}")
            elif lim.windows:
                for wname, wdata in lim.windows.items():
                    pct = f"{wdata.remaining_pct:.1f}"
                    w_avail = "true" if wdata.remaining_pct >= MIN_CAPACITY_PERCENT else "false"
                    lines.append(f"{ts} | {name}_{wname} | {pct} | {w_avail}")
            else:
                pct = f"{lim.remaining_pct:.1f}" if lim.available else "-1.0"
                lines.append(f"{ts} | {name} | {pct} | {avail}")

        if not lines:
            return

        if not CAPACITY_LOG_FILE.exists():
            CAPACITY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CAPACITY_LOG_FILE.write_text(
                "# AI Provider Capacity Log\n"
                "<!-- appended by orchestrator heartbeat -->\n\n",
                encoding="utf-8",
            )

        with CAPACITY_LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        _cleanup_capacity_log()
    except Exception as e:
        logger.debug("capacity log append failed: %s", e)


# Module-level: track last cleanup date to run at most once per day
_last_capacity_cleanup: Optional[date] = None


def _cleanup_capacity_log() -> None:
    """Remove capacity log entries older than CAPACITY_LOG_RETENTION_DAYS.

    Runs at most once per calendar day. Preserves the header comment lines
    and rewrites the file in-place only when old entries are actually removed.
    """
    global _last_capacity_cleanup
    today = date.today()
    if _last_capacity_cleanup == today:
        return

    if not CAPACITY_LOG_FILE.exists():
        _last_capacity_cleanup = today
        return

    cutoff = datetime.now() - timedelta(days=CAPACITY_LOG_RETENTION_DAYS)
    try:
        text = CAPACITY_LOG_FILE.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)

        kept: list[str] = []
        removed = 0
        for line in lines:
            # Header / comment lines are always kept
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("<!--"):
                kept.append(line)
                continue
            # Data lines start with a timestamp: "YYYY-MM-DD HH:MM:SS | ..."
            ts_part = stripped.split("|", 1)[0].strip()
            try:
                ts = datetime.strptime(ts_part, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                kept.append(line)  # unparseable → keep safe
                continue
            if ts >= cutoff:
                kept.append(line)
            else:
                removed += 1

        if removed:
            _write_bytes_atomic(CAPACITY_LOG_FILE, "".join(kept).encode("utf-8"))
            logger.info(
                "capacity log pruned %d entries older than %d days",
                removed,
                CAPACITY_LOG_RETENTION_DAYS,
            )
    except Exception as e:
        logger.debug("capacity log cleanup failed: %s", e)
    finally:
        _last_capacity_cleanup = today


def _check_limits(get_limits_fn: Callable) -> Optional[str]:
    """Call get_limits() and return a formatted summary with per-window detail."""
    try:
        limits = get_limits_fn()
        parts: list[str] = []
        for name in ("claude", "gemini", "codex"):
            lim = getattr(limits, name)
            if lim.available:
                parts.append(f"{name}: {lim.remaining_pct:.0f}% remaining")
            else:
                parts.append(f"{name}: ❌ {lim.error or 'unavailable'}")
            for wname, wdata in sorted(lim.windows.items()):
                reset_min = wdata.resets_in_sec // 60
                parts.append(
                    f"  {wname}: {wdata.remaining_pct:.0f}% remaining, "
                    f"reset in {reset_min}m"
                )
            if name == "claude" and "seven_day" in lim.windows:
                try:
                    from usage_budget import compute_window_pace, format_pace_status
                    w = lim.windows["seven_day"]
                    pace = compute_window_pace(w.remaining_pct, w.resets_in_sec, 7)
                    parts.append(f"  {format_pace_status(pace)}")
                except (ImportError, OSError, KeyError, TypeError, ValueError):
                    pass
        _append_capacity_log(limits)
        return "\n".join(parts) if parts else None
    except Exception as e:
        logger.debug("check-limits failed: %s", e)
        return None


def _log_capacity() -> None:
    """Silently append a capacity snapshot to the persistent log. No Telegram output."""
    try:
        from limits import get_limits
        _append_capacity_log(get_limits())
    except Exception as e:
        logger.debug("log-capacity failed: %s", e)


def _check_task_summary(dispatcher: Optional[object] = None) -> Optional[str]:
    """Summarize yesterday's completed tasks from memory/task_results/."""
    try:
        from memory import _TASK_RESULTS_DIR, _parse_memory_file
        yesterday = (datetime.now().date() - timedelta(days=1))

        completed: list[str] = []
        if _TASK_RESULTS_DIR.exists():
            for path in _TASK_RESULTS_DIR.glob("*.md"):
                mem = _parse_memory_file(path)
                if mem and mem["timestamp"].date() == yesterday:
                    status = "✅" if mem["success"] else "❌"
                    completed.append(f"{status} {mem['task'][:60]}")

        if not completed:
            return f"Gestern ({yesterday}) keine abgeschlossenen Tasks."

        header = f"Gestern ({yesterday}) abgeschlossen ({len(completed)}):"
        return header + "\n" + "\n".join(completed[:20])

    except Exception as e:
        logger.debug("task-summary failed: %s", e)
        return None


def _check_usage_suggest(queue_read_fn: Callable) -> Optional[str]:
    """Check if Claude limits are about to reset with unused capacity and suggest tasks."""
    global _usage_suggest_thread
    try:
        from usage_suggester import get_suggester

        # Run asynchronously so heartbeat never blocks the main watch loop.
        if _usage_suggest_thread and _usage_suggest_thread.is_alive():
            return None

        def _worker() -> None:
            try:
                result = get_suggester().check_and_suggest(queue_read_fn)
                if result:
                    logger.info("usage-suggest result: %s", result)
            except Exception as e:
                logger.debug("usage-suggest worker failed: %s", e)

        _usage_suggest_thread = threading.Thread(
            target=_worker,
            name="usage-suggest",
            daemon=True,
        )
        _usage_suggest_thread.start()
        return None
    except Exception as e:
        logger.debug("usage-suggest failed: %s", e)
        return None


# Transient = "could not verify, but ID is probably alive". Covers:
# - typed errors from providers (rate_limit, timeout, unreachable, session_missing)
# - raw stderr variants ("rate limit" with space, "429")
# - auth/credential errors → CLI lost OAuth, not a model issue (P2-4 from review)
_PROBE_TRANSIENT_KEYWORDS = (
    "rate_limit", "rate limit", "timeout", "timed out",
    "unreachable", "session_missing", "429",
    "auth", "login", "credential", "expired", "token",
)
# Dead = CLI explicitly rejected the model ID. Narrow keywords to avoid
# false positives (e.g. "unsupported encoding" should NOT count as dead).
_PROBE_DEAD_KEYWORDS = (
    "model not found", "unknown model", "model is deprecated",
    "invalid model", "model not supported", "no such model",
    "modell nicht gefunden", "modell nicht unterstützt",  # German variants
    "404",
)


def _probe_model(provider_name: str, model_id: str, timeout_sec: int = 30) -> tuple[bool, str]:
    """Send a minimal ping to verify the model ID is still served by the provider CLI.

    Returns (alive, detail). detail is empty when alive=True with no caveat;
    on alive=False it contains the raw provider error. Transient errors
    (rate limit, timeout, auth-expired) return alive=True with a "transient"
    detail so the caller can warn but not flag the ID as dead.

    Skips probing entirely when the provider is in cooldown — the cooldown
    indicates a recent failure, probing now would just stack another error.
    """
    try:
        from dispatcher import get_provider_by_name
    except ImportError:
        return True, "dispatcher unavailable — skipped"

    provider = get_provider_by_name(provider_name)
    if provider is None:
        return True, f"provider '{provider_name}' not initialised"

    if provider.is_cooling_down():
        return True, f"transient (provider in cooldown — {provider.cooldown_remaining_str()})"

    previous = getattr(provider, "_forced_model", None)
    setattr(provider, "_forced_model", model_id)
    try:
        result = provider.run("ping", timeout=timeout_sec, read_only=True)
    except Exception as exc:
        return True, f"transient probe error: {exc}"
    finally:
        setattr(provider, "_forced_model", previous)

    if result.success:
        return True, ""

    err = (result.error or "").lower()
    # Order matters: dead-keywords are more specific than transient ones, so if
    # an error contains BOTH (e.g. "rate limit on requested model"), we want
    # the dead-classification only when the dead-keyword is the model-rejection
    # phrase. Currently dead phrases all include "model" + qualifier, making
    # them unambiguous against transient single-word matches.
    if any(kw in err for kw in _PROBE_DEAD_KEYWORDS):
        return False, result.error[:200]

    if any(kw in err for kw in _PROBE_TRANSIENT_KEYWORDS):
        return True, f"transient ({result.error})"

    # Unknown error class — surface it but don't flag as dead
    return True, f"unclear ({result.error[:120]})"


def _llm_check_for_newer_models() -> str:
    """Ask the best available provider whether the current aliases are stale.

    Cheap heuristic — runs a single short prompt against the highest-priority
    available provider. Output is plain text suggestions; never raises.

    Returns:
        ""                   — no provider available, "OK" response, or no relevant info
        "⚠️ LLM-Check failed: <error>" — call attempted but failed (visible to user)
        "<text>"             — actual suggestions to forward to Telegram
    """
    try:
        from datetime import date
        from config import (
            CLAUDE_MODEL_ALIASES,
            CODEX_MODEL_ALIASES,
            GEMINI_MODEL_ALIASES,
        )
        from dispatcher import select_provider
        from limits import get_limits
    except ImportError:
        return ""

    try:
        limits = get_limits()
        provider = select_provider("model-update-check", limits, tool_name=None)
        if provider is None:
            return ""

        ids_block = []
        for label, mapping in (
            ("Claude", CLAUDE_MODEL_ALIASES),
            ("Gemini", GEMINI_MODEL_ALIASES),
            ("Codex",  CODEX_MODEL_ALIASES),
        ):
            for tag, model_id in mapping.items():
                ids_block.append(f"  - {label}/{tag} = {model_id}")

        prompt = (
            f"Stand: {date.today().isoformat()}.\n"
            "Du prüfst eine Liste von KI-Modell-IDs auf Aktualität. "
            "Antworte NUR mit konkret bekannten Migrationen oder Empfehlungen — "
            "keine Spekulation, keine 'könnte sein'-Hinweise. "
            "Wenn alle IDs aktuell wirken, antworte EXAKT mit der zwei Zeichen 'OK' "
            "(ohne weitere Wörter, ohne Punkt).\n\n"
            "Hinterlegte IDs:\n" + "\n".join(ids_block) + "\n\n"
            "Format pro Eintrag (nur falls relevant): "
            "`<provider>/<tag>: <alte-id> → <neue-id> (<Grund/Quelle>)`"
        )
        result = provider.run(prompt, timeout=120, read_only=True)
        if not result.success:
            return f"⚠️ LLM-Check failed via {provider.name}: {result.error or 'unknown error'}"
        text = (result.output or "").strip()
        # Strict equality — "Okay, here are…" must NOT count as OK.
        if text.upper().rstrip(".").strip() == "OK":
            return ""
        # Truncate to keep Telegram message readable
        return text[:1500]
    except Exception as exc:
        logger.debug("LLM model-check failed: %s", exc)
        return f"⚠️ LLM-Check failed: {exc}"


def _check_model_updates() -> Optional[str]:
    """Verify all configured model aliases are still alive (CLI probe) and ask
    an LLM whether newer IDs are available. Designed for monthly heartbeat.

    Returns a Telegram-formatted summary on findings, or None when everything
    is current (no notification → no spam).
    """
    try:
        from config import (
            CLAUDE_MODEL_ALIASES,
            CODEX_MODEL_ALIASES,
            GEMINI_MODEL_ALIASES,
        )
    except ImportError:
        return None

    dead: list[str] = []
    flaky: list[str] = []

    for label, mapping in (
        ("claude", CLAUDE_MODEL_ALIASES),
        ("gemini", GEMINI_MODEL_ALIASES),
        ("codex",  CODEX_MODEL_ALIASES),
    ):
        for tag, model_id in mapping.items():
            alive, detail = _probe_model(label, model_id)
            if not alive:
                dead.append(f"❌ {label}/{tag} ({model_id}) — {detail}")
            elif detail and not detail.startswith("transient"):
                # Probe completed but with non-transient warning
                flaky.append(f"⚠️ {label}/{tag} ({model_id}) — {detail}")

    suggestions = _llm_check_for_newer_models()

    parts: list[str] = []
    if dead:
        parts.append("**Tote Model-IDs (Update nötig):**\n" + "\n".join(dead))
    if flaky:
        parts.append("**Auffällige IDs (manuell prüfen):**\n" + "\n".join(flaky))
    if suggestions:
        parts.append("**LLM-Hinweise zu möglichen neueren IDs:**\n" + suggestions)

    return "\n\n".join(parts) if parts else None


def _check_stale_branches() -> Optional[str]:
    """Warn about branches with last commit >HEARTBEAT_GIT_STALE_DAYS days old."""
    results: list[str] = []
    roots = ALLOWED_CWD_ROOTS or []

    for root in roots:
        if not root.is_dir():
            continue
        try:
            r = subprocess.run(
                ["git", "branch", "--list", "--sort=-creatordate",
                 "--format=%(refname:short) %(creatordate:relative)"],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if r.returncode != 0 or not r.stdout.strip():
                continue

            stale: list[str] = []
            for line in r.stdout.strip().splitlines():
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                branch, age_str = parts
                # Detect stale: "N weeks ago", "N months ago"
                m = re.search(r"(\d+)\s+(week|month)", age_str)
                if m:
                    n = int(m.group(1))
                    unit = m.group(2)
                    days = n * 7 if unit == "week" else n * 30
                    if days > HEARTBEAT_GIT_STALE_DAYS:
                        stale.append(f"  {branch}: {age_str}")

            if stale:
                results.append(f"{root.name} — stale branches:\n" + "\n".join(stale[:10]))

        except (OSError, subprocess.TimeoutExpired):
            pass

    return "\n\n".join(results) if results else None


# ── Handler dispatch table ────────────────────────────────────────────────────

HANDLER_KEYS: list[tuple[str, str]] = [
    ("queue",            "_check_queue_idle"),
    ("git status",       "_check_git_status"),
    ("disk space",       "_check_disk_space"),
    ("check-limits",     "_check_limits"),
    ("log-capacity",     "_log_capacity"),
    ("summarize",        "_check_task_summary"),
    ("stale branch",     "_check_stale_branches"),
    ("usage-suggest",    "_check_usage_suggest"),
    ("session-cleanup",  "_check_session_cleanup"),
    ("model-check",      "_check_model_updates"),
]


def _match_handler_key(label: str) -> str:
    """Return the first matching handler key for a label (case-insensitive)."""
    label_lower = label.lower()
    for keyword, handler_key in HANDLER_KEYS:
        if keyword in label_lower:
            return handler_key
    return ""


# ── HEARTBEAT.md parser ───────────────────────────────────────────────────────

_INTERVAL_RE = re.compile(
    r"^##\s+Every\s+(\d+)\s+(minutes?|hours?|days?)\s*$", re.IGNORECASE
)
_DAILY_RE = re.compile(
    r"^##\s+Daily.*?(\d{1,2}):(\d{2})", re.IGNORECASE
)
_ITEM_RE = re.compile(r"^-\s+\[\s*\]\s+(.+)$")


def _parse_heartbeat_md(content: str) -> list[HeartbeatItem]:
    """Parse HEARTBEAT.md sections into HeartbeatItem list."""
    items: list[HeartbeatItem] = []
    current_interval_min = 0
    current_daily_after = 0
    is_daily = False

    for line in content.splitlines():
        # Section heading: ## Every N minutes/hours/days
        m = _INTERVAL_RE.match(line)
        if m:
            n = int(m.group(1))
            unit = m.group(2).lower()
            if "hour" in unit:
                n *= 60
            elif "day" in unit:
                n *= 1440  # 24 * 60
            # Skip nonsensical "Every 0 X" sections — without this the items
            # underneath would inherit interval_min=0 and be treated as daily,
            # which is almost certainly NOT what the user meant.
            if n <= 0:
                logger.warning("HEARTBEAT.md: ignoring section '%s' (interval must be > 0)", line.strip())
                current_interval_min = -1   # sentinel — items with this are dropped
                is_daily = False
                continue
            current_interval_min = n
            is_daily = False
            continue

        # Section heading: ## Daily ...
        m = _DAILY_RE.match(line)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2))
            current_daily_after = hour * 60 + minute
            current_interval_min = 0
            is_daily = True
            continue

        # Task item: - [ ] description
        m = _ITEM_RE.match(line)
        if m:
            # Drop items in invalid sections (current_interval_min == -1)
            if current_interval_min == -1 and not is_daily:
                continue
            label = m.group(1).strip()
            handler_key = _match_handler_key(label)
            items.append(HeartbeatItem(
                label=label,
                interval_min=current_interval_min,
                daily_after=current_daily_after if is_daily else 0,
                handler_key=handler_key,
            ))

    return items


# Items with interval >= this threshold persist their last_run timestamp
# across orchestrator restarts. Without this, a `## Every 30 days` item would
# run on every --watch restart (which can cost real LLM tokens). Short-interval
# items are not persisted because they run frequently anyway and the persistence
# I/O would dominate.
_PERSIST_INTERVAL_THRESHOLD_MIN = 1440  # 1 day

# State file lives next to the orchestrator; intentionally NOT in the vault.
_HEARTBEAT_STATE_FILE = Path(__file__).parent / "logs" / "heartbeat-state.json"


def _load_heartbeat_state() -> dict:
    """Load persisted last_run timestamps for long-interval items.

    Returns: {label: {"last_run": iso8601, "last_run_date": iso8601-date}} or {}.
    Tolerates missing/corrupt file — never raises.
    """
    if not _HEARTBEAT_STATE_FILE.exists():
        return {}
    try:
        import json
        return json.loads(_HEARTBEAT_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("heartbeat-state.json unreadable, ignoring: %s", e)
        return {}


def _save_heartbeat_state(state: dict) -> None:
    """Persist long-interval last_run state. Best-effort — never raises."""
    try:
        import json
        _HEARTBEAT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HEARTBEAT_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("heartbeat-state.json write failed: %s", e)


# ── HeartbeatRunner ───────────────────────────────────────────────────────────

class HeartbeatRunner:
    """Loads HEARTBEAT.md, reloads on mtime change, runs due items."""

    def __init__(self) -> None:
        self._items: list[HeartbeatItem] = []
        self._mtime: float = 0.0
        self._lock = threading.Lock()  # prevents concurrent run_due() calls
        self._reload_if_changed()
        self._restore_persistent_state()

    def _is_persistent(self, item: HeartbeatItem) -> bool:
        """True for items whose last_run survives orchestrator restarts.

        Daily items (interval_min == 0) and long-interval items (>= 1 day)
        qualify. Short intervals stay in-memory only.
        """
        return item.interval_min == 0 or item.interval_min >= _PERSIST_INTERVAL_THRESHOLD_MIN

    def _restore_persistent_state(self) -> None:
        """Hydrate long-interval items' last_run from disk."""
        state = _load_heartbeat_state()
        if not state:
            return
        for item in self._items:
            if not self._is_persistent(item):
                continue
            entry = state.get(item.label)
            if not isinstance(entry, dict):
                continue
            try:
                last_run = entry.get("last_run")
                if last_run:
                    item.last_run = datetime.fromisoformat(last_run)
                last_date = entry.get("last_run_date")
                if last_date:
                    item.last_run_date = date.fromisoformat(last_date)
            except (ValueError, TypeError):
                logger.debug("heartbeat-state: bad entry for '%s'", item.label)

    def _persist_state(self) -> None:
        """Write current persistent items' last_run to disk."""
        state: dict = {}
        for item in self._items:
            if not self._is_persistent(item) or item.last_run is None:
                continue
            entry = {"last_run": item.last_run.isoformat()}
            if item.last_run_date:
                entry["last_run_date"] = item.last_run_date.isoformat()
            state[item.label] = entry
        if state:
            _save_heartbeat_state(state)

    def _reload_if_changed(self) -> None:
        """Reload HEARTBEAT.md if file has changed since last load."""
        if not HEARTBEAT_FILE.exists():
            return
        try:
            mtime = HEARTBEAT_FILE.stat().st_mtime
            if mtime == self._mtime:
                return
            content = HEARTBEAT_FILE.read_text(encoding="utf-8")
            new_items = _parse_heartbeat_md(content)

            # Preserve last_run state for items that survived a reload
            old_by_label = {i.label: i for i in self._items}
            for item in new_items:
                if item.label in old_by_label:
                    item.last_run = old_by_label[item.label].last_run
                    item.last_run_date = old_by_label[item.label].last_run_date

            self._items = new_items
            self._mtime = mtime
            # Restore persistent state for items that didn't survive in-memory
            # (e.g. user added a new monthly item after the last reload).
            self._restore_persistent_state()
            logger.debug("HEARTBEAT.md loaded: %d items", len(self._items))
        except Exception as e:
            logger.warning("Failed to load HEARTBEAT.md: %s", e)

    def due_items(self) -> list[HeartbeatItem]:
        """Return items that are due for execution."""
        self._reload_if_changed()
        now = datetime.now()
        due: list[HeartbeatItem] = []

        for item in self._items:
            if item.interval_min == 0:
                # Daily item
                today = now.date()
                minute_of_day = now.hour * 60 + now.minute
                if item.last_run_date == today:
                    continue  # already ran today
                if minute_of_day < item.daily_after:
                    continue  # not yet reached the daily threshold
                due.append(item)
            else:
                # Periodic item
                if item.last_run is None:
                    due.append(item)
                    continue
                elapsed_min = (now - item.last_run).total_seconds() / 60
                if elapsed_min >= item.interval_min:
                    due.append(item)

        return due

    def run_due(
        self,
        queue_read_fn: Callable,
        dispatcher: Optional[object] = None,
    ) -> None:
        """Run all due items, send Telegram notifications for non-empty results.

        Never raises — all errors are caught and logged.
        Safe for concurrent calls: returns immediately if already running.
        """
        if not self._lock.acquire(blocking=False):
            return  # bg thread or main loop already running — skip
        try:
            self._run_due_locked(queue_read_fn, dispatcher)
        finally:
            self._lock.release()

    def _run_due_locked(
        self,
        queue_read_fn: Callable,
        dispatcher: Optional[object] = None,
    ) -> None:
        """Inner implementation of run_due — must be called with _lock held."""
        self._reload_if_changed()
        due = self.due_items()
        if not due:
            return

        try:
            from notifier import send_message
        except Exception:
            send_message = None

        any_persistent_ran = False
        for item in due:
            try:
                result = self._run_item(item, queue_read_fn, dispatcher)
                now = datetime.now()
                item.last_run = now
                item.last_run_date = now.date()
                if self._is_persistent(item):
                    any_persistent_ran = True

                if result:
                    logger.info("Heartbeat [%s]: %s", item.label, result[:1000])
                    msg = f"🫀 Heartbeat — {item.label}\n\n{result}"
                    if send_message:
                        try:
                            send_message(msg)
                        except Exception as e:
                            logger.warning("Heartbeat notify failed: %s", e)
            except Exception as e:
                logger.warning("Heartbeat item '%s' failed: %s", item.label, e)

        # Persist state at most once per run_due() call, only when needed
        if any_persistent_ran:
            self._persist_state()

    def _run_item(
        self,
        item: HeartbeatItem,
        queue_read_fn: Callable,
        dispatcher: Optional[object],
    ) -> Optional[str]:
        """Dispatch to the correct handler."""
        key = item.handler_key

        if key == "_check_queue_idle":
            return _check_queue_idle(queue_read_fn)
        elif key == "_check_git_status":
            return _check_git_status()
        elif key == "_check_disk_space":
            return _check_disk_space()
        elif key == "_check_limits":
            try:
                from limits import get_limits
                return _check_limits(get_limits)
            except ImportError:
                return None
        elif key == "_log_capacity":
            _log_capacity()
            return None
        elif key == "_check_task_summary":
            return _check_task_summary(dispatcher)
        elif key == "_check_stale_branches":
            return _check_stale_branches()
        elif key == "_check_usage_suggest":
            return _check_usage_suggest(queue_read_fn)
        elif key == "_check_session_cleanup":
            return _check_session_cleanup()
        elif key == "_check_model_updates":
            return _check_model_updates()
        else:
            logger.debug("No handler for heartbeat item: %s", item.label)
            return None


def start_heartbeat_thread(
    heartbeat: HeartbeatRunner,
    queue_read_fn: Callable,
    stop_event: threading.Event,
    poll_sec: int = 60,
    pause_event: Optional[threading.Event] = None,
) -> threading.Thread:
    """Start a daemon thread that calls heartbeat.run_due() every *poll_sec* seconds.

    This ensures scheduled checks (log-capacity, usage-suggest, etc.) fire on time
    even when the main thread is blocked for hours inside a long-running task.  
    The thread is safe to run alongside the existing run_due() calls in the main loop
    because HeartbeatRunner.run_due() is protected by a non-blocking lock.      
    """
    def _loop() -> None:
        while not stop_event.wait(timeout=poll_sec):
            try:
                # Honour pause_event if provided
                if pause_event and pause_event.is_set():
                    continue
                heartbeat.run_due(queue_read_fn)
            except Exception:
                logger.debug("heartbeat bg thread error", exc_info=True)        

    t = threading.Thread(target=_loop, name="heartbeat-bg", daemon=True)        
    t.start()
    logger.debug("Heartbeat background thread started (poll=%ds)", poll_sec)    
    return t
