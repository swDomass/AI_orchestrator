"""Phase-1 Single-Source-of-Truth (SoTH) quota state file.

The orchestrator's cclimits background refresh loop calls
``write_quota_state`` after every successful poll, persisting the latest
per-window rate-limit snapshot (5h / 7d for Claude, plus Gemini/Codex)
to ``logs/cc_quota_state.json``. External, read-only consumers — the
Claude Code statusline and ``orchestrator.py --check-limits`` — read it
via ``read_quota_state`` instead of re-polling the rate-limited cclimits
endpoint (``api.anthropic.com/api/oauth/usage``).

The file is self-describing: it embeds the Phase-0 calibration constants
(``io_only`` model, per-window ``tokens_per_pct``) so a reader can
interpolate usage between polls without hard-coding the factors.

Design notes:
- **No operational coupling.** ``write_quota_state`` never raises; on any
  error it logs at DEBUG and returns False so the refresh loop is unaffected.
- **Atomic write** (temp file in the same dir + ``os.replace``) so a reader
  never observes a half-written file — mirrors ``idempotency.py``.
- **No import of ``limits``** (the writer hook lives in ``limits``); the
  ``AllLimits``/``ProviderLimits`` objects are read via attribute access to
  avoid a circular import.
- **Single-machine assumption** (same as Phase 0): the snapshot reflects the
  cclimits account, written by one orchestrator instance per host.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from config import (
    ESTIMATE_TOKENS_PER_PCT_CLAUDE_WINDOWS,
    QUOTA_CALIBRATION_MODEL,
)

if TYPE_CHECKING:
    from limits import AllLimits, ProviderLimits

logger = logging.getLogger(__name__)

# Bump when the JSON layout changes so readers can reject incompatible files.
SCHEMA_VERSION = 1

_PROVIDER_NAMES = ("claude", "gemini", "codex")


def _provider_to_dict(pl: "ProviderLimits", now: float) -> dict:
    """Serialise one ProviderLimits, adding reader-friendly ``used_pct`` and
    absolute window reset epochs (computed from ``resets_in_sec`` at write
    time, since WindowData only carries the relative offset)."""
    windows: dict[str, dict] = {}
    for name, w in (getattr(pl, "windows", None) or {}).items():
        remaining = float(getattr(w, "remaining_pct", 0.0))
        resets_in = int(getattr(w, "resets_in_sec", 0) or 0)
        windows[name] = {
            "remaining_pct": round(remaining, 4),
            "used_pct": round(100.0 - remaining, 4),
            "resets_in_sec": resets_in,
            "reset_at_epoch": round(now + resets_in, 0) if resets_in > 0 else 0,
        }
    remaining_pct = float(getattr(pl, "remaining_pct", 0.0))
    return {
        "available": bool(getattr(pl, "available", False)),
        "remaining_pct": round(remaining_pct, 4),
        "used_pct": round(100.0 - remaining_pct, 4),
        "resets_in_sec": int(getattr(pl, "resets_in_sec", 0) or 0),
        "reset_at_epoch": round(float(getattr(pl, "reset_at_epoch", 0.0)), 0),
        "error": getattr(pl, "error", "") or "",
        "windows": windows,
    }


def build_state(all_limits: "AllLimits", now: "float | None" = None) -> dict:
    """Build the SoTH state dict (pure, testable without disk I/O)."""
    now = time.time() if now is None else now
    return {
        "schema_version": SCHEMA_VERSION,
        "fetched_at_unix": round(now, 3),
        "fetched_at_utc": dt.datetime.fromtimestamp(
            now, dt.timezone.utc,
        ).isoformat(timespec="seconds"),
        "providers": {
            name: _provider_to_dict(getattr(all_limits, name), now)
            for name in _PROVIDER_NAMES
        },
        "calibration": {
            "model": QUOTA_CALIBRATION_MODEL,
            "tokens_per_pct": {"claude": dict(ESTIMATE_TOKENS_PER_PCT_CLAUDE_WINDOWS)},
            "note": (
                "io_only, conservative low-percentile; cache tokens excluded; "
                "single-machine"
            ),
        },
    }


def write_quota_state(all_limits: "AllLimits", path) -> bool:
    """Atomically write the SoTH quota state. Never raises.

    Returns True on success, False on any failure (logged at DEBUG so the
    cclimits refresh loop is never disrupted).
    """
    try:
        path = Path(path)
        content = json.dumps(build_state(all_limits), ensure_ascii=False, indent=2)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: "Path | None" = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", delete=False, dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", encoding="utf-8",
            ) as tmp:
                tmp_path = Path(tmp.name)
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, path)
            return True
        finally:
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
    except Exception as exc:
        logger.debug("write_quota_state failed: %s", exc)
        return False


def read_quota_state(path) -> "dict | None":
    """Read the SoTH quota state. Returns None if missing, empty, or corrupt.

    Read-only consumers (statusline, --check-limits) call this; it must be
    fast (single file read + json.loads) and total — never raise.
    """
    try:
        path = Path(path)
        if not path.exists() or path.stat().st_size == 0:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception as exc:
        logger.debug("read_quota_state failed: %s", exc)
        return None


def state_age_sec(state: dict, now: "float | None" = None) -> "float | None":
    """Seconds since the snapshot was fetched, or None if unparseable.

    Lets a reader decide whether to trust the file or fall back to its own
    source (e.g. statusline: prefer ccusage if the file is older than the
    cclimits poll interval).
    """
    try:
        fetched = float(state["fetched_at_unix"])
    except (KeyError, TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    return max(0.0, now - fetched)
