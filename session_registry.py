"""Sidecar registry for orchestrator-created Claude session UUIDs.

Append-only JSONL at ``ORCH_SESSION_REGISTRY``. One entry per session:
    {"uuid": "...", "tool": "dev-loop", "cwd": "/path", "created_at": "ISO"}

Used as a whitelist by the heartbeat session-cleanup so we never accidentally
delete an interactive Claude Code session that happens to live in the same
``~/.claude/projects/<cwd>/`` directory.

Concurrency: a thread lock guards writes. Multi-process safety is best-effort
(append-only JSONL with line-buffered writes — partial-line corruption is
self-healing because the parser skips malformed lines).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from config import ORCH_SESSION_REGISTRY

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


def register_session(uuid: str, tool: str, cwd: str) -> None:
    """Append a new orchestrator-created session to the registry.

    Failure modes are logged but never raise — registration is best-effort
    and a missed entry only means the heartbeat won't auto-clean that
    session (manual cleanup possible).
    """
    entry = {
        "uuid": uuid,
        "tool": tool,
        "cwd": cwd,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with _LOCK:
            ORCH_SESSION_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
            with ORCH_SESSION_REGISTRY.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Session registry append failed for %s: %s", uuid, exc)


def list_sessions() -> list[dict]:
    """Return all registered sessions. Skips malformed lines silently."""
    if not ORCH_SESSION_REGISTRY.exists():
        return []
    entries: list[dict] = []
    try:
        with _LOCK, ORCH_SESSION_REGISTRY.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("Session registry read failed: %s", exc)
    return entries


def is_orchestrator_session(uuid: str) -> bool:
    """True if uuid was created by the orchestrator (in the registry)."""
    return any(e.get("uuid") == uuid for e in list_sessions())


def prune_old(retention_days: int) -> tuple[list[dict], list[dict]]:
    """Split registry into (kept, expired) entries by age.

    Rewrites the registry in-place with only ``kept`` entries; returns
    ``expired`` so the caller can delete the corresponding session JSONL
    files in ``~/.claude/projects/**``.
    """
    cutoff = datetime.now() - timedelta(days=retention_days)
    entries = list_sessions()
    kept: list[dict] = []
    expired: list[dict] = []
    for entry in entries:
        try:
            created = datetime.fromisoformat(entry.get("created_at", ""))
        except ValueError:
            # Malformed timestamp: treat as expired so we don't accumulate
            # broken entries forever.
            expired.append(entry)
            continue
        if created < cutoff:
            expired.append(entry)
        else:
            kept.append(entry)

    if expired:
        try:
            with _LOCK, ORCH_SESSION_REGISTRY.open("w", encoding="utf-8") as f:
                for entry in kept:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("Session registry rewrite failed: %s", exc)

    return kept, expired
