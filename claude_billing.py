"""Billing path of the Claude provider: API credits vs. subscription.

Single place that knows whether Claude CLI runs go over the monthly API credits
(``ORCH_CLAUDE_API_KEY``) or over the subscription. Holds

* the block state after a billing/auth failure (``logs/claude-api-state.json``),
  including the one-notification-per-episode marker,
* the ledger of message IDs of API-billed runs (``logs/claude-api-runs.jsonl``),
  which quota calibration / the local 429 fallback use to drop those entries,
* ``child_env()``, the only place that turns the key into ``ANTHROPIC_API_KEY``.

The key never appears in logs, exception text or files. Reads of missing/corrupt
files behave like "no state"; nothing here raises to the caller, except
``child_env()`` without a configured key (ValueError).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# Guards the state file and the ledger (sub-tasks run in threads).
_LOCK = threading.Lock()

_REASON_TEXT = {
    "credit": "Guthaben leer oder Limit erreicht",
    "auth": "API-Schlüssel ungültig",
    "rate": "API-Ratenlimit der Console-Organisation erreicht",
}


def key_configured() -> bool:
    return bool(config.ORCH_CLAUDE_API_KEY)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
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
    finally:
        if tmp_path is not None and tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()


_mem_state: list[dict | None] = [None]  # mirror only if writing the state file failed


def _read_state() -> dict | None:
    """Current block state: the in-memory mirror if the last write failed (it is
    newer than any file left on disk, e.g. an expired one on a full disk), else
    the file. None when neither exists. Caller holds _LOCK."""
    if _mem_state[0] is not None:
        return _mem_state[0]
    try:
        data = json.loads(Path(config.CLAUDE_API_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _blocked_until(state: dict | None) -> float:
    if not state:
        return 0.0
    try:
        return float(state.get("blocked_until") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def api_active(now: float | None = None) -> bool:
    """True when the key is set and no block is in force."""
    if not key_configured():
        return False
    now = time.time() if now is None else now
    with _LOCK:
        state = _read_state()
    return state is None or _blocked_until(state) <= now


def child_env() -> dict[str, str]:
    """Full environment copy for the Claude subprocess incl. ANTHROPIC_API_KEY."""
    if not key_configured():
        raise ValueError("ORCH_CLAUDE_API_KEY is not configured")
    env = dict(os.environ)
    env["ANTHROPIC_API_KEY"] = config.ORCH_CLAUDE_API_KEY
    return env


def trip(reason: str, now: float | None = None) -> None:
    """Block the API path for ORCH_CLAUDE_API_RETRY_HOURS (reason ``"rate"``:
    ORCH_CLAUDE_API_RATE_RETRY_MIN minutes); notify once per episode.

    An episode ends with ``clear()``. A repeated trip (e.g. the single retry after
    the block expired failed again, or a rate block followed by a credit block)
    keeps ``notified`` and sends nothing. A shorter block (rate, 60 min) never
    shortens a longer one still in force (credit/auth, 24 h): the later of both
    ``blocked_until`` wins, and so does that block's ``reason``.
    """
    now = time.time() if now is None else now
    if reason == "rate":
        until = now + config.ORCH_CLAUDE_API_RATE_RETRY_MIN * 60
    else:
        until = now + config.ORCH_CLAUDE_API_RETRY_HOURS * 3600
    with _LOCK:
        previous = _read_state()
        previous_until = _blocked_until(previous)
        if previous_until > until:
            until = previous_until
            reason = str((previous or {}).get("reason") or reason)
        logger.info(
            "Claude API path blocked (reason=%s) until %s",
            reason, datetime.fromtimestamp(until).isoformat(timespec="seconds"),
        )
        already_notified = bool(previous and previous.get("notified"))
        state = {
            "blocked_until": until,
            "reason": reason,
            "since": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "notified": True,
        }
        # `since` of a running episode stays the episode start.
        if previous and previous.get("since"):
            state["since"] = previous["since"]
        # notified is stored even if Telegram fails: no endless retries.
        try:
            _atomic_write(
                Path(config.CLAUDE_API_STATE_FILE),
                json.dumps(state, ensure_ascii=False, indent=2),
            )
            _mem_state[0] = None
        except OSError as exc:
            _mem_state[0] = state  # keep the block + notified marker in memory
            logger.warning("Claude API state not written (%s) - block kept in memory",
                           type(exc).__name__)
    if already_notified:
        return
    until_txt = datetime.fromtimestamp(until).strftime("%d.%m. %H:%M")
    headline = "Claude-Guthaben nicht nutzbar" if reason != "rate" else "Claude-API gedrosselt"
    text = (
        f"💳 {headline} ({_REASON_TEXT.get(reason, _REASON_TEXT['credit'])}). "
        f"Der Orchestrator nutzt bis {until_txt} das Abo und versucht es dann erneut. "
        "Dieser Hinweis kommt nur einmal, bis das Guthaben wieder läuft."
    )
    # A failed notice must never break a run.
    with contextlib.suppress(Exception):
        import notifier  # noqa: PLC0415 - lazy: avoid import cycles

        notifier.send_message(text)


def clear(now: float | None = None) -> None:
    """After a successful API run: drop block state and the notified marker,
    but only when no block is in force any more.

    A run started in parallel that ends successfully *after* another run's
    ``trip()`` must not delete the fresh block together with ``notified`` (the
    next trip would send a second Telegram message). The episode therefore ends
    with the first successful API run after the block expired.
    """
    now = time.time() if now is None else now
    with _LOCK:
        if _blocked_until(_read_state()) > now:
            return
        _mem_state[0] = None
        try:
            Path(config.CLAUDE_API_STATE_FILE).unlink()
        except OSError:  # includes "already gone": nothing to log then
            return
    logger.info("Claude API block cleared after successful API run")


_in_flight = [0]  # mutable cell: no ``global`` statement


@contextlib.contextmanager
def api_run_in_flight():
    """Count a running API-billed CLI run (thread-safe, decremented in ``finally``)."""
    with _LOCK:
        _in_flight[0] += 1
    try:
        yield
    finally:
        with _LOCK:
            _in_flight[0] -= 1


def api_runs_in_flight() -> int:
    with _LOCK:
        return _in_flight[0]


def _read_ledger_lines() -> list[tuple[float, str, dict]]:
    """Parsed ledger rows as (ts, raw_line, row); unparsable lines are skipped."""
    rows: list[tuple[float, str, dict]] = []
    try:
        text = Path(config.CLAUDE_API_RUNS_FILE).read_text(encoding="utf-8")
    except OSError:
        return rows
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            ts = float(row["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(row, dict):
            rows.append((ts, raw, row))
    return rows


def record_api_run(session_id: str, message_ids: list[str], now: float | None = None) -> None:
    """Append one API-billed run to the ledger; rows older than the calibration
    window are dropped (file rewritten atomically only when something is dropped)."""
    ids = [m for m in message_ids if m]
    if not ids:
        return
    now = time.time() if now is None else now
    cutoff = now - config.QUOTA_RECALIBRATE_WINDOW_DAYS * 86400
    line = json.dumps(
        {"ts": now, "session_id": session_id, "message_ids": ids}, ensure_ascii=False,
    )
    path = Path(config.CLAUDE_API_RUNS_FILE)
    with _LOCK:
        try:
            rows = _read_ledger_lines()
            try:
                total = sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())
            except OSError:
                total = 0
            kept = [raw for ts, raw, _ in rows if ts >= cutoff]
            if len(kept) == total:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            else:
                _atomic_write(path, "\n".join([*kept, line]) + "\n")
        except OSError:
            pass


_SESSION_ID_RE = re.compile(r"[0-9a-fA-F-]{8,}")


def transcript_message_ids(session_id: str) -> list[str]:
    """Message IDs from the session transcripts of the Claude CLI.

    Reads ``CLAUDE_PROJECTS_DIR/*/<sid>.jsonl`` and ``.../<sid>/subagents/*.jsonl``
    (subagent messages are not in the stream, see ADR 005). Missing files and
    broken lines are ignored; an invalid ``session_id`` yields ``[]`` (never part
    of a glob pattern).
    """
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return []
    root = Path(config.CLAUDE_PROJECTS_DIR)
    ids: list[str] = []
    try:
        files = [*root.glob(f"*/{session_id}.jsonl"), *root.glob(f"*/{session_id}/subagents/*.jsonl")]
    except OSError:
        return []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            try:
                evt = json.loads(raw)
            except ValueError:
                continue
            msg = evt.get("message") if isinstance(evt, dict) else None
            mid = msg.get("id") if isinstance(msg, dict) else None
            if isinstance(mid, str) and mid and (evt.get("type") == "assistant" or mid.startswith("msg_")):
                ids.append(mid)
    return list(dict.fromkeys(ids))


def api_message_ids() -> set[str]:
    """Message IDs of all API-billed runs in the ledger."""
    with _LOCK:
        rows = _read_ledger_lines()
    ids: set[str] = set()
    for _ts, _raw, row in rows:
        mids = row.get("message_ids")
        if isinstance(mids, list):
            ids.update(m for m in mids if isinstance(m, str) and m)
    return ids


def filter_api_entries(entries: list) -> list:
    """Drop entries whose ``message_id`` belongs to an API-billed run.

    Entries without a message_id stay. An empty ledger returns ``entries`` as is.
    """
    ids = api_message_ids()
    if not ids:
        return entries
    return [e for e in entries if getattr(e, "message_id", None) not in ids]
