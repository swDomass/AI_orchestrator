"""CI-Watcher (P4) — turns failed GitHub-Action runs into queue items.

Polled from the heartbeat (`check-ci-failures` handler). For each repo in
``config.CI_WATCHER_REPOS`` it lists failed runs via `gh run list` and queues
a `#tool:dev-loop` task per new failure that wasn't already triaged.

Dedup strategy:
    - One queue item per (repo, headSha) — multiple failed checks on the same
      commit are bundled.
    - State file ``logs/ci-watcher-state.json`` remembers the per-repo set of
      queued commit SHAs so repeated heartbeats stay idempotent.
    - Cooldown of ``CI_WATCHER_QUEUE_COOLDOWN_HOURS`` blocks re-queueing the
      same SHA within the window even if the state file gets reset.

Failure modes are defensive: every gh error returns through the standard
(data, error) tuple in gh_helpers — handler never crashes the heartbeat.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from config import (
    CI_WATCHER_MAX_RUNS_PER_REPO,
    CI_WATCHER_QUEUE_COOLDOWN_HOURS,
    CI_WATCHER_REPO_PATHS,
    CI_WATCHER_REPOS,
    PR_BABYSITTER_GH_TIMEOUT_SEC,
)

logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).parent / "logs" / "ci-watcher-state.json"
_STATE_VERSION = 1


def _load_state() -> dict:
    """Read persisted state. Tolerates missing/corrupt file."""
    if not _STATE_FILE.exists():
        return {"version": _STATE_VERSION, "repos": {}}
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": _STATE_VERSION, "repos": {}}
        data.setdefault("version", _STATE_VERSION)
        data.setdefault("repos", {})
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ci-watcher: state unreadable (%s) — starting fresh", exc)
        return {"version": _STATE_VERSION, "repos": {}}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    except OSError as exc:
        logger.warning("ci-watcher: cannot persist state (%s)", exc)


def _within_cooldown(prev: dict, now: datetime, cooldown_hours: int) -> bool:
    last = prev.get("queued_at")
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return False
    return (now - last_dt) < timedelta(hours=cooldown_hours)


def _build_queue_line(repo: str, run: dict) -> str:
    """Compose a queue task line for a failed CI run."""
    branch = run.get("headBranch") or "unknown"
    sha = (run.get("headSha") or "")[:7]
    title = run.get("displayTitle") or run.get("name") or "CI failure"
    title_short = title[:80]
    cwd_part = ""
    local = CI_WATCHER_REPO_PATHS.get(repo)
    if local:
        cwd_part = f" cwd:{local}"
    return (
        f"CI failure ({repo}@{branch}, {sha}): {title_short}"
        f"{cwd_part} #tool:dev-loop"
    )


def sweep_once(
    repos: list[str] | None = None,
    *,
    now: datetime | None = None,
    cooldown_hours: int = CI_WATCHER_QUEUE_COOLDOWN_HOURS,
    list_failed_runs_fn: Callable | None = None,
    append_task_fn: Callable | None = None,
) -> dict:
    """Run one sweep. Returns a summary dict::

        {
          "checked_repos": int,
          "queued":  list[str],     # task lines appended
          "skipped": list[str],     # already-seen or cooldowned
          "errors":  list[str],     # gh / append errors
        }

    All dependencies are injectable so tests can drive the function without
    hitting `gh` or the queue file.
    """
    repos = repos if repos is not None else list(CI_WATCHER_REPOS)
    if list_failed_runs_fn is None:
        from gh_helpers import list_failed_runs as _lfr
        list_failed_runs_fn = _lfr
    if append_task_fn is None:
        from queue_manager import append_task as _append
        append_task_fn = _append

    now = now or datetime.now()
    state = _load_state()
    summary = {"checked_repos": 0, "queued": [], "skipped": [], "errors": []}

    for repo in repos:
        summary["checked_repos"] += 1
        runs, err = list_failed_runs_fn(
            repo,
            timeout_sec=PR_BABYSITTER_GH_TIMEOUT_SEC,
            limit=CI_WATCHER_MAX_RUNS_PER_REPO,
        )
        if err:
            summary["errors"].append(f"{repo}: {err}")
            continue

        repo_state = state["repos"].setdefault(repo, {})
        seen_shas = repo_state.setdefault("seen_shas", {})

        # Dedup multiple failed runs on the same commit by tracking per-SHA state.
        for run in runs:
            sha = (run.get("headSha") or "")[:40]
            if not sha:
                continue
            prev = seen_shas.get(sha, {})
            if prev and _within_cooldown(prev, now, cooldown_hours):
                summary["skipped"].append(f"{repo}@{sha[:7]}: cooldown")
                continue
            if prev and prev.get("queued_at"):
                summary["skipped"].append(f"{repo}@{sha[:7]}: already queued")
                continue

            queue_line = _build_queue_line(repo, run)
            if append_task_fn(queue_line):
                summary["queued"].append(queue_line)
                seen_shas[sha] = {
                    "queued_at": now.isoformat(timespec="seconds"),
                    "run_id": run.get("databaseId"),
                    "branch": run.get("headBranch"),
                }
            else:
                summary["errors"].append(f"{repo}@{sha[:7]}: queue append failed")

    state["checked_at"] = now.isoformat(timespec="seconds")
    _save_state(state)
    return summary
