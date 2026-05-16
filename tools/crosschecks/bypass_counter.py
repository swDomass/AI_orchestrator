"""Rate-limit counter for ``#cross-provider:none`` bypasses.

The plan (§2.1, K4) caps cross-provider Devil's-Advocate bypasses at 3 per
30 days (rolling). When the cap is reached, further bypasses require an
explicit Telegram-Policy approval via the existing ``PolicyEngine``.

State lives at ``{root_cwd}/.scientific-investigation/tool-bypass-counter.json``.
The file is JSON-only (not JSONL) because we read+rewrite atomically; the
"append-only" semantics from the audit-trail don't apply here — the counter
is operational state, not an audit anchor.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import TOOL_SI_BYPASS_LIMIT_PER_30_DAYS

logger = logging.getLogger(__name__)

COUNTER_FILE = ".scientific-investigation/tool-bypass-counter.json"
WINDOW_DAYS = 30


def _counter_path(root_cwd: Path) -> Path:
    path = root_cwd / COUNTER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load(root_cwd: Path) -> dict:
    path = _counter_path(root_cwd)
    if not path.exists():
        return {"bypasses": []}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("bypass_counter: %s unreadable, starting fresh: %s", path, exc)
        return {"bypasses": []}
    if not isinstance(data, dict) or not isinstance(data.get("bypasses"), list):
        return {"bypasses": []}
    return data


def _save(root_cwd: Path, data: dict) -> None:
    path = _counter_path(root_cwd)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _recent(data: dict, now: datetime | None = None) -> list[dict]:
    """Return entries within the rolling window. Skips malformed timestamps."""
    cutoff = (now or _utc_now()) - timedelta(days=WINDOW_DAYS)
    out: list[dict] = []
    for entry in data.get("bypasses", []):
        ts_raw = entry.get("at")
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            out.append(entry)
    return out


def recent_bypass_count(root_cwd: Path, now: datetime | None = None) -> int:
    """Number of bypass uses within the last 30 days (rolling)."""
    return len(_recent(_load(root_cwd), now=now))


def is_bypass_over_limit(root_cwd: Path, now: datetime | None = None) -> bool:
    """True iff the next bypass would exceed the rolling limit."""
    return recent_bypass_count(root_cwd, now=now) >= TOOL_SI_BYPASS_LIMIT_PER_30_DAYS


def record_bypass(
    root_cwd: Path,
    *,
    run_id: str,
    now: datetime | None = None,
) -> int:
    """Record one bypass use. Returns the new in-window count.

    The caller is responsible for first checking ``is_bypass_over_limit`` and
    routing through ``PolicyEngine`` when over the cap.
    """
    data = _load(root_cwd)
    ts = (now or _utc_now()).isoformat()
    data.setdefault("bypasses", []).append({"at": ts, "run_id": run_id})
    _save(root_cwd, data)
    return len(_recent(data, now=now))
