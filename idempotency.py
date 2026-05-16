"""
Idempotency keys for external triggers (Telegram /task, future webhooks, cron).

Stores one JSONL entry per accepted trigger at ``logs/idempotency.jsonl``.
Key = sha256(source + canonical_payload + bucket_ts) — duplicates are dropped.

Retention: 30 days. Pruning runs lazily, at most once per calendar day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import hashlib
import json
import logging
import os
import sys
import tempfile
import threading

logger = logging.getLogger(__name__)

# Default store. Tests override via ``set_store_path`` or by monkey-patching.
DEFAULT_STORE = Path(__file__).parent / "logs" / "idempotency.jsonl"
RETENTION_DAYS = 30


@dataclass(frozen=True)
class IdempotencyEntry:
    key: str
    source: str
    bucket_ts: str
    recorded_at: str
    payload_hash: str


# Module-level state. ``_lock`` serializes file mutations; ``_cleanup_last_date``
# rate-limits pruning to once per day.
_lock = threading.Lock()
_cleanup_last_date: date | None = None
_store_path: Path = DEFAULT_STORE


def set_store_path(path: Path) -> None:
    """Override the JSONL store location. Used by tests."""
    global _store_path, _cleanup_last_date
    with _lock:
        _store_path = Path(path)
        _cleanup_last_date = None  # re-prune after a path swap


def get_store_path() -> Path:
    return _store_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_key(source: str, payload, bucket_ts) -> str:
    """Deterministic key over (source, canonicalized payload, bucket_ts).

    Args:
        source: trigger source name, e.g. 'telegram_slash', 'webhook', 'cron'.
        payload: any JSON-serializable value (dict, str, list, ...).
        bucket_ts: natural granularity for the trigger (message_id, scheduled time, ...).
    """
    canonical_payload = _canonicalize_payload(payload)
    bucket = str(bucket_ts)
    h = hashlib.sha256()
    h.update(source.encode("utf-8"))
    h.update(b"\x00")
    h.update(canonical_payload.encode("utf-8"))
    h.update(b"\x00")
    h.update(bucket.encode("utf-8"))
    return h.hexdigest()


def check_and_record(source: str, payload, bucket_ts) -> bool:
    """Record a new trigger if it hasn't been seen.

    Returns:
        True if the trigger is new (and was recorded). False if a duplicate
        (already on file) — the caller should skip the side effect.
    """
    key = compute_key(source, payload, bucket_ts)
    payload_hash = _payload_hash(payload)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with _lock:
        if _key_exists(key):
            return False
        _append_entry({
            "key": key,
            "source": source,
            "bucket_ts": str(bucket_ts),
            "recorded_at": now,
            "payload_hash": payload_hash,
        })
        _maybe_prune_locked()
    return True


def is_recorded(source: str, payload, bucket_ts) -> bool:
    """Check whether a key is already on file without recording it."""
    key = compute_key(source, payload, bucket_ts)
    with _lock:
        return _key_exists(key)


def prune_old(max_age_days: int = RETENTION_DAYS) -> int:
    """Remove entries older than max_age_days. Returns count of pruned entries."""
    with _lock:
        return _prune_locked(max_age_days)


def reset_for_tests() -> None:
    """Clear in-memory state and delete the store file. Test-only helper."""
    global _cleanup_last_date
    with _lock:
        _cleanup_last_date = None
        try:
            _store_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.debug("reset_for_tests: unlink failed: %s", e)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _canonicalize_payload(payload) -> str:
    """Stable JSON serialization. Dict keys are sorted; str is round-tripped
    through json.dumps to escape control chars consistently."""
    if isinstance(payload, str):
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _payload_hash(payload) -> str:
    return hashlib.sha256(_canonicalize_payload(payload).encode("utf-8")).hexdigest()[:16]


def _ensure_dir() -> None:
    _store_path.parent.mkdir(parents=True, exist_ok=True)


def _read_entries() -> list[dict]:
    if not _store_path.exists():
        return []
    out: list[dict] = []
    try:
        with open(_store_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # Tolerate corrupt lines — skip and continue.
                    continue
    except OSError as e:
        logger.warning("idempotency store read failed: %s", e)
    return out


def _key_exists(key: str) -> bool:
    """Linear scan; the JSONL stays small (entries auto-pruned at 30d)."""
    if not _store_path.exists():
        return False
    try:
        with open(_store_path, encoding="utf-8") as f:
            for line in f:
                if f'"key": "{key}"' in line or f'"key":"{key}"' in line:
                    return True
    except OSError as e:
        logger.warning("idempotency store scan failed: %s", e)
    return False


def _append_entry(entry: dict) -> None:
    _ensure_dir()
    try:
        with open(_store_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("idempotency store append failed: %s", e)


def _maybe_prune_locked() -> None:
    global _cleanup_last_date
    today = date.today()
    if _cleanup_last_date == today:
        return
    try:
        _prune_locked(RETENTION_DAYS)
    finally:
        _cleanup_last_date = today


def _prune_locked(max_age_days: int) -> int:
    if not _store_path.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=max_age_days)
    kept: list[str] = []
    removed = 0
    try:
        with open(_store_path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    ts = datetime.strptime(obj["recorded_at"], "%Y-%m-%dT%H:%M:%S")
                    if ts < cutoff:
                        removed += 1
                        continue
                except (json.JSONDecodeError, KeyError, ValueError):
                    # Keep malformed lines — better to err on the side of
                    # not deleting unknown data.
                    pass
                kept.append(line)
    except OSError as e:
        logger.warning("idempotency store prune-read failed: %s", e)
        return 0

    if removed == 0:
        return 0

    _atomic_rewrite("\n".join(kept) + ("\n" if kept else ""))
    return removed


def _atomic_rewrite(content: str) -> None:
    """Replace store content atomically via temp file in the same directory."""
    _ensure_dir()
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            dir=_store_path.parent,
            prefix=f".{_store_path.name}.",
            suffix=".tmp",
            encoding="utf-8",
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, _store_path)
    except OSError as e:
        logger.warning("idempotency store rewrite failed: %s", e)
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
