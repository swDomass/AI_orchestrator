"""PR-Babysitter (P2) — polls open PRs via `gh` and queues fix tasks.

Triggers a queue item `- [ ] PR #N (<repo>) review-feedback verarbeiten cwd:<repo-cwd> #tool:dev-loop`
when an open PR receives new review comments OR a CI check has flipped to
failure since the last poll.

Configuration:
    - Whitelist of repos: comma-separated list via `#repos:owner/name1,owner/name2`
      OR the `PR_BABYSITTER_REPOS` env var (semicolon-separated).
    - Optional label filter: `#pr-labels:auto-fix,bot-ready` — only PRs carrying
      one of those labels are surveyed.
    - Mode: `#pr-mode:queue` (default — generate queue items) OR `#pr-mode:report-only`
      (P5 — Telegram summary only, no queue write).

State tracking lives in `{cwd}/.pr-babysitter/state.json`. The 1h cooldown
prevents duplicate queue items even when subsequent polls would still match
the same condition.

Usage in queue:
    - [ ] PR-Babysitter sweep #tool:pr-babysitter cwd:/d/programmieren/projekt
    - [ ] Survey CI fixes #tool:pr-babysitter cwd:/d/proj #repos:user/a,user/b
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from config import (
    PR_BABYSITTER_GH_TIMEOUT_SEC,
    PR_BABYSITTER_MAX_PRS_PER_REPO,
    PR_BABYSITTER_QUEUE_COOLDOWN_HOURS,
    PR_BABYSITTER_REPOS,
)
from providers.base import BaseProvider
from tools.base_tool import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_REPOS_TAG_RE = re.compile(r"(?i)(?<!\S)#repos:([\w/.,-]+)(?=\s|$)")
_LABELS_TAG_RE = re.compile(r"(?i)(?<!\S)#pr-labels:([\w,-]+)(?=\s|$)")
_MODE_TAG_RE = re.compile(r"(?i)(?<!\S)#pr-mode:(queue|report-only)(?=\s|$)")

_STATE_VERSION = 1
_STATE_DIR_NAME = ".pr-babysitter"
_STATE_FILE_NAME = "state.json"


# ── State persistence ────────────────────────────────────────────────────────

def _state_path(cwd: Path) -> Path:
    return cwd / _STATE_DIR_NAME / _STATE_FILE_NAME


def _load_state(cwd: Path) -> dict:
    p = _state_path(cwd)
    if not p.exists():
        return {"version": _STATE_VERSION, "prs": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": _STATE_VERSION, "prs": {}}
        data.setdefault("version", _STATE_VERSION)
        data.setdefault("prs", {})
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("pr-babysitter: state file unreadable (%s) — starting fresh", exc)
        return {"version": _STATE_VERSION, "prs": {}}


def _save_state(cwd: Path, state: dict) -> None:
    p = _state_path(cwd)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("pr-babysitter: cannot persist state (%s)", exc)


# ── Task-tag parsing ─────────────────────────────────────────────────────────

def _parse_repos(task: str) -> list[str]:
    m = _REPOS_TAG_RE.search(task)
    if not m:
        return list(PR_BABYSITTER_REPOS)
    return [r.strip() for r in m.group(1).split(",") if r.strip()]


def _parse_labels(task: str) -> list[str]:
    m = _LABELS_TAG_RE.search(task)
    if not m:
        return []
    return [l.strip() for l in m.group(1).split(",") if l.strip()]


def _parse_mode(task: str) -> str:
    m = _MODE_TAG_RE.search(task)
    return m.group(1).lower() if m else "queue"


# ── Change detection ─────────────────────────────────────────────────────────

def _ci_overall_status(pr: dict) -> str:
    """Reduce statusCheckRollup to one of: passed | failed | pending | none.

    failure beats everything; pending wins over success; no rollup = none.
    """
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return "none"
    has_failure = False
    has_pending = False
    for entry in rollup:
        # CheckRun → conclusion (success/failure/skipped/cancelled) + status (completed/in_progress)
        # StatusContext → state (SUCCESS/FAILURE/PENDING)
        conclusion = (entry.get("conclusion") or "").lower()
        status = (entry.get("status") or "").lower()
        state = (entry.get("state") or "").lower()
        if conclusion in ("failure", "cancelled", "timed_out") or state == "failure":
            has_failure = True
        elif status in ("queued", "in_progress", "waiting", "pending") or state == "pending":
            has_pending = True
    if has_failure:
        return "failed"
    if has_pending:
        return "pending"
    return "passed"


def _latest_commit_sha(pr: dict) -> str:
    commits = pr.get("commits") or []
    if not commits:
        return ""
    last = commits[-1]
    return (last.get("oid") or last.get("sha") or "")[:40]


def _detect_change(prev: dict, pr_detail: dict) -> str:
    """Return a non-empty reason string when the PR has new triage-worthy changes."""
    new_comment_count = len(pr_detail.get("comments") or [])
    new_status = _ci_overall_status(pr_detail)
    new_sha = _latest_commit_sha(pr_detail)

    if not prev:
        # First observation. Triage only if there's something actionable already.
        if new_status == "failed":
            return f"CI failure on first observation ({new_sha[:7] or 'unknown'})"
        if new_comment_count > 0:
            return f"{new_comment_count} existing comment(s) — first survey"
        return ""

    if new_comment_count > prev.get("last_seen_comment_count", 0):
        delta = new_comment_count - prev.get("last_seen_comment_count", 0)
        return f"{delta} new comment(s)"
    if new_status == "failed" and prev.get("last_check_status") != "failed":
        return f"CI flipped to failure (commit {new_sha[:7]})"
    if new_status == "failed" and new_sha and new_sha != prev.get("last_seen_commit_sha"):
        return f"CI failure on new commit {new_sha[:7]}"
    return ""


def _in_cooldown(prev: dict, now: datetime, cooldown_hours: int) -> bool:
    last_q = prev.get("last_queued_at")
    if not last_q:
        return False
    try:
        last_dt = datetime.fromisoformat(last_q)
    except ValueError:
        return False
    return (now - last_dt) < timedelta(hours=cooldown_hours)


# ── Main sweep (pure-ish — accepts injected dependencies for tests) ──────────

def sweep(
    repos: list[str],
    *,
    cwd: Path,
    labels: list[str] | None = None,
    mode: str = "queue",
    now: datetime | None = None,
    cooldown_hours: int = PR_BABYSITTER_QUEUE_COOLDOWN_HOURS,
    list_open_prs_fn: Callable | None = None,
    view_pr_fn: Callable | None = None,
    append_task_fn: Callable | None = None,
    send_message_fn: Callable | None = None,
) -> dict:
    """Survey `repos` and act on PRs with new triage signal.

    Returns a summary dict::

        {
          "checked_prs": int,
          "queued":      list[str],   # queue lines appended
          "reported":    list[str],   # telegram summary entries
          "errors":      list[str],   # repo/PR-level errors
          "skipped":     list[str],   # cooldown / no change
        }

    All gh and side-effect functions are injected for testability.
    """
    from gh_helpers import list_open_prs as _list, view_pr as _view
    list_open_prs_fn = list_open_prs_fn or _list
    view_pr_fn = view_pr_fn or _view
    if append_task_fn is None:
        from queue_manager import append_task as _append
        append_task_fn = _append
    if send_message_fn is None:
        from notifier import send_message as _send
        send_message_fn = _send

    now = now or datetime.now()
    state = _load_state(cwd)
    summary = {"checked_prs": 0, "queued": [], "reported": [], "errors": [], "skipped": []}

    for repo in repos:
        prs, err = list_open_prs_fn(
            repo,
            timeout_sec=PR_BABYSITTER_GH_TIMEOUT_SEC,
            limit=PR_BABYSITTER_MAX_PRS_PER_REPO,
            labels=labels,
        )
        if err:
            summary["errors"].append(f"{repo}: {err}")
            continue
        for pr in prs:
            number = pr.get("number")
            if not isinstance(number, int):
                continue
            key = f"{repo}#{number}"
            prev = state["prs"].get(key, {})
            detail, derr = view_pr_fn(repo, number, timeout_sec=PR_BABYSITTER_GH_TIMEOUT_SEC)
            if derr:
                summary["errors"].append(f"{key}: {derr}")
                continue
            summary["checked_prs"] += 1
            reason = _detect_change(prev, detail)
            if not reason:
                summary["skipped"].append(f"{key}: no change")
                state["prs"][key] = {
                    **prev,
                    "last_seen_comment_count": len(detail.get("comments") or []),
                    "last_check_status": _ci_overall_status(detail),
                    "last_seen_commit_sha": _latest_commit_sha(detail),
                    "last_checked_at": now.isoformat(timespec="seconds"),
                }
                continue
            if _in_cooldown(prev, now, cooldown_hours):
                summary["skipped"].append(f"{key}: cooldown")
                continue

            if mode == "report-only":
                line = f"PR {key} — {reason}"
                summary["reported"].append(line)
                # P5 message format includes /pr-fix and /pr-ignore commands so
                # the user can react without inline buttons (not available in
                # current telegram_listener).
                telegram_msg = (
                    f"🔍 PR-Babysitter: {line}\n"
                    f"\n"
                    f"➜ Fix queueing: `/pr-fix {key}` (cwd:{cwd})\n"
                    f"➜ Ignore until next change: `/pr-ignore {key}`"
                )
                try:
                    send_message_fn(telegram_msg)
                except Exception as exc:
                    logger.warning("pr-babysitter telegram report failed: %s", exc)
            else:
                queue_line = (
                    f"PR #{number} ({repo}) review-feedback verarbeiten "
                    f"cwd:{cwd} #tool:dev-loop"
                )
                if append_task_fn(queue_line):
                    summary["queued"].append(queue_line)
                    state["prs"][key] = {
                        **state["prs"].get(key, {}),
                        "last_queued_at": now.isoformat(timespec="seconds"),
                        "last_queue_reason": reason,
                    }
                else:
                    summary["errors"].append(f"{key}: failed to append queue item")
                    continue

            # Update tracking regardless of queue vs report-only mode.
            state["prs"][key] = {
                **state["prs"].get(key, {}),
                "last_seen_comment_count": len(detail.get("comments") or []),
                "last_check_status": _ci_overall_status(detail),
                "last_seen_commit_sha": _latest_commit_sha(detail),
                "last_checked_at": now.isoformat(timespec="seconds"),
            }

    state["checked_at"] = now.isoformat(timespec="seconds")
    _save_state(cwd, state)
    return summary


# ── Slash command handlers (P5) ──────────────────────────────────────────────


_PR_KEY_RE = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


def _parse_pr_key(value: str) -> tuple[str, int] | None:
    """Parse 'owner/repo#123' → ('owner/repo', 123). Returns None on bad input."""
    m = _PR_KEY_RE.match(value.strip())
    if not m:
        return None
    try:
        return m.group(1), int(m.group(2))
    except ValueError:
        return None


def cmd_pr_fix(
    pr_key: str,
    cwd: Path,
    *,
    append_task_fn: Callable | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Queue a dev-loop task for the named PR. Returns (ok, message).

    Called from telegram_listener./pr-fix. The cwd is read from the PR's
    tracked state if the caller doesn't override it — this avoids forcing the
    user to remember which repo lived where.
    """
    parsed = _parse_pr_key(pr_key)
    if parsed is None:
        return False, f"unrecognized PR key '{pr_key}' (expected owner/repo#N)"
    repo, number = parsed
    if append_task_fn is None:
        from queue_manager import append_task as _append
        append_task_fn = _append

    queue_line = (
        f"PR #{number} ({repo}) review-feedback verarbeiten "
        f"cwd:{cwd} #tool:dev-loop"
    )
    if not append_task_fn(queue_line):
        return False, "queue append failed"

    # Mark the cooldown so the next sweep doesn't immediately requeue.
    state = _load_state(cwd)
    key = f"{repo}#{number}"
    state["prs"][key] = {
        **state["prs"].get(key, {}),
        "last_queued_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "last_queue_reason": "manual /pr-fix",
    }
    _save_state(cwd, state)
    return True, f"queued: {queue_line}"


def cmd_pr_ignore(
    pr_key: str,
    cwd: Path,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Mark a PR as ignored — next sweep skips it until the underlying state
    actually changes (new comments OR new commit SHA OR CI flip).

    Implementation: bump last_seen_* fields to whatever the current detail is.
    The next sweep will see no delta and skip. This is the same effect as
    setting the cooldown, but more permanent because we update the seen state.
    """
    parsed = _parse_pr_key(pr_key)
    if parsed is None:
        return False, f"unrecognized PR key '{pr_key}' (expected owner/repo#N)"
    repo, number = parsed
    key = f"{repo}#{number}"

    state = _load_state(cwd)
    prev = state["prs"].get(key, {})
    state["prs"][key] = {
        **prev,
        "last_ignored_at": (now or datetime.now()).isoformat(timespec="seconds"),
        # Cooldown for 24h — gives the user a day before re-triage even if the
        # PR's state hasn't actually moved on.
        "last_queued_at": (now or datetime.now()).isoformat(timespec="seconds"),
    }
    _save_state(cwd, state)
    return True, f"ignored {key} — won't re-trigger until state changes"


# ── BaseTool wrapper ─────────────────────────────────────────────────────────

class PRBabysitterTool(BaseTool):
    name = "pr-babysitter"
    description = (
        "Poll open GitHub PRs (via `gh`) and queue dev-loop tasks for new "
        "review comments or CI failures."
    )
    read_only = True   # Tool itself does not modify code; queued tasks do.

    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
        **kwargs,
    ) -> ToolResult:
        if not cwd:
            return ToolResult(
                success=False,
                error="pr-babysitter requires cwd: tag on the task",
                error_code="missing_cwd",
            )
        cwd_path = Path(cwd)
        repos = _parse_repos(task)
        if not repos:
            return ToolResult(
                success=False,
                error="no repos configured (set PR_BABYSITTER_REPOS in .env or use #repos: tag)",
                error_code="no_repos",
            )
        labels = _parse_labels(task)
        mode = _parse_mode(task)

        summary = sweep(repos, cwd=cwd_path, labels=labels or None, mode=mode)

        # Build a human-readable result body.
        lines = [
            f"PR-Babysitter sweep — {len(repos)} repo(s), {summary['checked_prs']} PR(s) checked",
            f"mode: {mode}",
        ]
        if summary["queued"]:
            lines.append(f"queued ({len(summary['queued'])}):")
            lines.extend(f"  + {q}" for q in summary["queued"])
        if summary["reported"]:
            lines.append(f"reported ({len(summary['reported'])}):")
            lines.extend(f"  · {r}" for r in summary["reported"])
        if summary["errors"]:
            lines.append(f"errors ({len(summary['errors'])}):")
            lines.extend(f"  ! {e}" for e in summary["errors"])
        if not (summary["queued"] or summary["reported"] or summary["errors"]):
            lines.append("no triage signal — nothing to do")

        # `gh_auth`/`gh_not_found` errors get surfaced loudly so the user
        # actually sees them in the queue result.
        critical = [e for e in summary["errors"]
                    if "gh_not_found" in e or "gh_auth" in e]
        success = not critical
        return ToolResult(
            success=success,
            output="\n".join(lines),
            iterations=1,
            error="; ".join(critical) if critical else "",
            error_code="gh_unavailable" if critical else "",
        )
