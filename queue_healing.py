"""
Queue healing (#38) — when tasks stay blocked indefinitely, the orchestrator
proposes unblock actions via Telegram instead of leaving them dead in the queue.

Triggers
--------

* A task is blocked by ``#needs:`` for more than ``HEAL_BLOCK_THRESHOLD_HOURS``.
* All other tasks have completed but this one + its dependencies are still open.
* A ``#needs:`` target has already failed — either dropped as ``[-]`` or
  finalized by the orchestrator as ``[x] … ❌ …`` — so the dep can never resolve
  on its own.

Actions (Telegram-asked)
------------------------

* ``/unblock <id>`` — treat any failed deps as done so the task can run.
* ``/drop <id>`` — finalize the blocked task as failed (releases its #needs slot
  for downstream tasks too).
* ``/retry <id>`` — reset a failed dep back to ``[ ]`` so the orchestrator picks
  it up again.

Each notification is rate-limited per (task_id, action) pair via the
notification ledger ``logs/queue-healing.jsonl`` (30-day retention) so we never
spam the same suggestion twice for the same task.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_LEDGER = Path(__file__).parent / "logs" / "queue-healing.jsonl"
HEAL_BLOCK_THRESHOLD_HOURS = 24
HEAL_LEDGER_RETENTION_DAYS = 30
HEAL_NOTIFY_COOLDOWN_HOURS = 24


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_ledger_path: Path = DEFAULT_LEDGER
_cleanup_last_date: date | None = None


def set_ledger_path(path: Path) -> None:
    global _ledger_path, _cleanup_last_date
    with _lock:
        _ledger_path = Path(path)
        _cleanup_last_date = None


def get_ledger_path() -> Path:
    return _ledger_path


def reset_for_tests() -> None:
    global _cleanup_last_date
    with _lock:
        _cleanup_last_date = None
        try:
            _ledger_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _read_ledger() -> list[dict]:
    if not _ledger_path.exists():
        return []
    out: list[dict] = []
    try:
        with open(_ledger_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.warning("queue-healing ledger read failed: %s", e)
    return out


def _append_ledger(entry: dict) -> None:
    _ledger_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(_ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("queue-healing ledger write failed: %s", e)


def _maybe_prune_locked() -> None:
    global _cleanup_last_date
    today = date.today()
    if _cleanup_last_date == today:
        return
    cutoff = datetime.now() - timedelta(days=HEAL_LEDGER_RETENTION_DAYS)
    if not _ledger_path.exists():
        _cleanup_last_date = today
        return
    kept: list[str] = []
    removed = 0
    try:
        with open(_ledger_path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    ts = datetime.strptime(obj["ts"], "%Y-%m-%dT%H:%M:%S")
                    if ts < cutoff:
                        removed += 1
                        continue
                except (json.JSONDecodeError, KeyError, ValueError):
                    pass
                kept.append(line)
    except OSError:
        _cleanup_last_date = today
        return

    if removed:
        _atomic_rewrite("\n".join(kept) + ("\n" if kept else ""))
    _cleanup_last_date = today


def _atomic_rewrite(content: str) -> None:
    _ledger_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, dir=_ledger_path.parent,
            prefix=f".{_ledger_path.name}.", suffix=".tmp", encoding="utf-8",
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, _ledger_path)
    except OSError as e:
        logger.warning("queue-healing rewrite failed: %s", e)
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _was_recently_notified(task_id: str, action: str) -> bool:
    """True if (task_id, action) was sent within HEAL_NOTIFY_COOLDOWN_HOURS."""
    cutoff = datetime.now() - timedelta(hours=HEAL_NOTIFY_COOLDOWN_HOURS)
    for entry in _read_ledger():
        if entry.get("task_id") != task_id or entry.get("action") != action:
            continue
        try:
            ts = datetime.strptime(entry["ts"], "%Y-%m-%dT%H:%M:%S")
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            return True
    return False


def record_notification(task_id: str, action: str, *, detail: str = "") -> None:
    with _lock:
        _append_ledger({
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "task_id": task_id,
            "action": action,
            "detail": detail,
        })
        _maybe_prune_locked()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HealCandidate:
    task_id: str
    task_text: str
    line_no: int
    reason: str           # human-readable why this candidate qualifies
    action: str           # "unblock" | "drop" | "retry"
    failed_deps: tuple[str, ...]   # which deps are in [-] state
    blocked_since_age_hours: float


# These regexes are kept local — queue_manager.OPEN_TASK_RE filters retry tags
# but we need to inspect finished lines too.
#
# A task counts as FAILED in two shapes, and both have to be recognised here or
# healing goes blind to exactly the tasks it exists for:
#   * ``- [-] …``            — dropped by the user (`/drop`), or a legacy failure.
#   * ``- [x] … ❌ ts (p)``  — the orchestrator gave up (see
#     queue_manager._collect_completed_ids). Checked off so it is never picked up
#     again, but it satisfies nothing.
_FAILED_TASK_RE = re.compile(r"^- \[-\] (.+?)\s*$", re.MULTILINE)
_DONE_TASK_RE = re.compile(r"^- \[x\] (.+?)\s*$", re.MULTILINE)
_FINISHED_TASK_RE = re.compile(r"^- \[[x-]\] (.+?)\s*$", re.MULTILINE)
_OPEN_TASK_RE = re.compile(r"^- \[ \] (.+?)(?:\s*<!--.*?-->)?\s*$", re.MULTILINE)


def _is_failed_line(line: str) -> bool:
    """True for both failure shapes: a `[-]` line, or a `[x]` line stamped ❌."""
    from queue_manager import line_is_failed
    return line.startswith("- [-]") or line_is_failed(line)


def _find_failed_ids(queue_content: str) -> set[str]:
    """Return the set of #id: values on task lines that failed (either shape)."""
    from queue_manager import extract_id_tag
    ids: set[str] = set()
    for m in _FINISHED_TASK_RE.finditer(queue_content):
        if not _is_failed_line(m.group(0)):
            continue
        tid = extract_id_tag(m.group(1))
        if tid:
            ids.add(tid)
    return ids


def _find_completed_ids(queue_content: str) -> set[str]:
    """Return the #id: values that a dependency may treat as satisfied.

    Mirrors queue_manager._collect_completed_ids: a `[x]` line stamped ❌ is a
    failure, not a completion, and must not silence a healing proposal.
    """
    from queue_manager import extract_id_tag, line_is_failed
    ids: set[str] = set()
    for m in _DONE_TASK_RE.finditer(queue_content):
        if line_is_failed(m.group(1)):
            continue
        tid = extract_id_tag(m.group(1))
        if tid:
            ids.add(tid)
    return ids


def detect_candidates(
    queue_items: list,
    queue_content: str,
    *,
    now: datetime | None = None,
) -> list[HealCandidate]:
    """Scan the live queue items + raw content for healing candidates.

    Returns one HealCandidate per task that warrants an action proposal.
    """
    from queue_manager import extract_id_tag, extract_needs_tags
    now = now or datetime.now()
    failed_ids = _find_failed_ids(queue_content)
    completed_ids = _find_completed_ids(queue_content)

    eligible = [it for it in queue_items if not getattr(it, "blocked_reason", "")]
    candidates: list[HealCandidate] = []

    for item in queue_items:
        reason = getattr(item, "blocked_reason", "")
        if not reason:
            continue
        task_text = item.task_text
        task_id = extract_id_tag(task_text) or ""
        if not task_id:
            # Without an ID we can't issue /unblock /drop /retry
            continue
        needs = extract_needs_tags(task_text)
        unsatisfied = [dep for dep in needs if dep not in completed_ids]
        failed_deps = tuple(dep for dep in unsatisfied if dep in failed_ids)

        age_h = _block_age_hours(task_text, now=now)

        # Priority 1: dep is permanently failed → propose /unblock or /retry
        if failed_deps:
            candidates.append(HealCandidate(
                task_id=task_id,
                task_text=task_text[:200],
                line_no=getattr(item, "line_no", 0),
                reason=f"#needs:{','.join(failed_deps)} permanent gescheitert",
                action="unblock_or_retry",
                failed_deps=failed_deps,
                blocked_since_age_hours=age_h,
            ))
            continue

        # Priority 2: blocked >threshold and nothing else is eligible to unblock it
        if age_h >= HEAL_BLOCK_THRESHOLD_HOURS:
            others_running = any(
                extract_id_tag(it.task_text) in unsatisfied for it in eligible
            )
            if not others_running:
                candidates.append(HealCandidate(
                    task_id=task_id,
                    task_text=task_text[:200],
                    line_no=getattr(item, "line_no", 0),
                    reason=f"seit {age_h:.0f}h blockiert, "
                           f"keine offenen Tasks für: {','.join(unsatisfied)}",
                    action="drop_or_wait",
                    failed_deps=(),
                    blocked_since_age_hours=age_h,
                ))

    return candidates


def _block_age_hours(task_text: str, *, now: datetime | None = None) -> float:
    """Best-effort: age since the most recent retry marker. When no marker is
    present, falls back to ``HEAL_BLOCK_THRESHOLD_HOURS - 1`` so callers don't
    eagerly heal first-poll-blocked tasks.
    """
    now = now or datetime.now()
    m = re.search(r"<!--\s*retry:\s*([^>]+?)\s*-->", task_text)
    if not m:
        return HEAL_BLOCK_THRESHOLD_HOURS - 1.0  # never reached threshold alone
    raw = m.group(1).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            ts = datetime.strptime(raw, fmt)
            return max(0.0, (now - ts).total_seconds() / 3600)
        except ValueError:
            continue
    return HEAL_BLOCK_THRESHOLD_HOURS - 1.0


# ---------------------------------------------------------------------------
# Notification + heartbeat entry point
# ---------------------------------------------------------------------------

def format_proposal(candidate: HealCandidate) -> str:
    """Return a Telegram-friendly proposal message for one candidate."""
    if candidate.action == "unblock_or_retry":
        deps = ", ".join(f"`{d}`" for d in candidate.failed_deps)
        return (
            f"🩺 Queue-Healing — Task `{candidate.task_id}` blockiert\n\n"
            f"Grund: {candidate.reason}\n"
            f"Failed deps: {deps}\n\n"
            f"Actions:\n"
            f"  /unblock {candidate.task_id} — Failed deps als erledigt behandeln, Task ausführen\n"
            f"  /retry {' '.join(candidate.failed_deps)} — Failed deps zurücksetzen, neu versuchen\n"
            f"  /drop {candidate.task_id} — Task aufgeben"
        )
    return (
        f"🩺 Queue-Healing — Task `{candidate.task_id}` ohne Fortschritt\n\n"
        f"Grund: {candidate.reason}\n\n"
        f"Actions:\n"
        f"  /unblock {candidate.task_id} — Manuell freischalten\n"
        f"  /drop {candidate.task_id} — Task aufgeben"
    )


def heal_once(
    queue_read_items_fn: Callable,
    queue_read_content_fn: Callable,
    notify_fn: Callable[[str], None] | None = None,
) -> list[HealCandidate]:
    """Detect candidates and (optionally) notify via Telegram.

    Returns the list of candidates considered. Notifications are deduplicated
    via the ledger so the same (task_id, action) is never proposed twice within
    HEAL_NOTIFY_COOLDOWN_HOURS.
    """
    try:
        items = queue_read_items_fn()
        content = queue_read_content_fn()
    except Exception as exc:
        logger.warning("queue-healing read failed: %s", exc)
        return []

    candidates = detect_candidates(items, content)
    for c in candidates:
        if _was_recently_notified(c.task_id, c.action):
            continue
        if notify_fn is not None:
            try:
                notify_fn(format_proposal(c))
            except Exception as exc:
                logger.warning("queue-healing notify failed: %s", exc)
                continue
        record_notification(c.task_id, c.action, detail=c.reason)
    return candidates


# ---------------------------------------------------------------------------
# Mutating actions — used by Telegram /unblock /drop /retry
# ---------------------------------------------------------------------------

def _promote_failed_line(line: str) -> str:
    """Rewrite one failed task line into a satisfying completion.

    Two things have to happen, and doing only the first was a defect: the checkbox
    becomes `[x]`, AND a ❌ stamp is traded for ✅. `apply_drop()` writes
    `- [-] … ❌ ts (drop via queue-healing)`, so promoting the checkbox alone
    produced `- [x] … ❌ …` — which `_collect_completed_ids()` reads as a FAILURE.
    `/unblock` would then have left the dependent blocked, i.e. done nothing at all.

    `rpartition` is what keeps this safe on a description containing ❌: the last
    occurrence is the stamp's, because the stamp is anchored at end of line.
    """
    from queue_manager import TASK_DONE_MARK, TASK_FAILED_MARK, line_is_failed
    promoted = line.replace("- [-]", "- [x]", 1) if line.startswith("- [-]") else line
    if line_is_failed(promoted):
        head, _, tail = promoted.rpartition(TASK_FAILED_MARK)
        promoted = head + TASK_DONE_MARK + tail
    return promoted


def apply_unblock(task_id: str) -> tuple[bool, str]:
    """Mark each failed dep of the task as completed (-> [x] ✅). Idempotent."""
    from queue_manager import _apply_update, extract_id_tag, extract_needs_tags

    def transform(content: str) -> str | None:
        target_needs: list[str] = []
        # Find the open task with this id and read its needs
        for m in _OPEN_TASK_RE.finditer(content):
            if extract_id_tag(m.group(1)) == task_id:
                target_needs = extract_needs_tags(m.group(1))
                break
        if not target_needs:
            return None

        new = content
        changed = False
        failed_ids_in_content = _find_failed_ids(new)
        for dep_id in target_needs:
            if dep_id not in failed_ids_in_content:
                continue
            # Promote any failed task with this id (either shape) to a completion
            def _promote(m_inner: re.Match[str], dep=dep_id) -> str:
                line = m_inner.group(0)
                if not _is_failed_line(line):
                    return line
                inner_id = extract_id_tag(m_inner.group(1))
                if inner_id == dep:
                    return _promote_failed_line(line)
                return line

            new = _FINISHED_TASK_RE.sub(_promote, new)
            changed = True
        return new if changed else None

    if _apply_update(transform):
        return True, f"unblocked deps for {task_id}"
    return False, f"no failed deps to unblock for {task_id}"


def apply_drop(task_id: str) -> tuple[bool, str]:
    """Finalize a blocked task as failed ([-])."""
    from queue_manager import _apply_update, extract_id_tag

    def transform(content: str) -> str | None:
        new_lines: list[str] = []
        changed = False
        for line in content.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            m = _OPEN_TASK_RE.match(body)
            if m and extract_id_tag(m.group(1)) == task_id:
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                new_lines.append(
                    body.replace("- [ ]", "- [-]", 1) + f" ❌ {stamp} (drop via queue-healing)\n"
                )
                changed = True
            else:
                new_lines.append(line)
        return "".join(new_lines) if changed else None

    if _apply_update(transform):
        return True, f"dropped {task_id}"
    return False, f"task {task_id} not found in open queue"


def apply_retry_dep(dep_ids: list[str]) -> tuple[bool, str]:
    """Reset failed dep tasks back to ``- [ ]`` so they get re-attempted."""
    from queue_manager import _apply_update, extract_id_tag

    targets = {d.lower() for d in dep_ids if d}
    if not targets:
        return False, "no dep ids provided"

    def transform(content: str) -> str | None:
        new = content
        changed = False
        def _reset(m: re.Match[str]) -> str:
            line = m.group(0)
            # Only failed lines are resettable — a successful `[x]` must never be
            # reopened by a /retry that names it.
            if not _is_failed_line(line):
                return line
            inner_id = extract_id_tag(m.group(1))
            if inner_id in targets:
                # Strip ONLY the validated trailing stamp. The former
                # `re.sub(r"\s*❌\s+.*$", "", line)` cut at the FIRST ❌, so a task
                # whose own text contains the emoji lost its instruction and its
                # #id: — `- [x] Replace ❌ with ✅ #id:a ❌ 2026-… (claude)` became
                # `- [ ] Replace`, and the reopened task was unrunnable.
                from queue_manager import strip_failure_stamp
                stripped = strip_failure_stamp(line)
                marker = "- [-]" if stripped.startswith("- [-]") else "- [x]"
                return stripped.replace(marker, "- [ ]", 1)
            return line

        new = _FINISHED_TASK_RE.sub(_reset, new)
        changed = new != content
        return new if changed else None

    if _apply_update(transform):
        return True, f"reset deps: {', '.join(sorted(targets))}"
    return False, "no matching failed deps found"
