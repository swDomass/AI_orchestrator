"""
Replay JSONL — machine-readable run summaries.

Every task run produces one JSONL line in ``logs/runs.jsonl`` that's sufficient
to (a) re-execute the same task deterministically, (b) classify failures
(see ``taxonomy.py``), and (c) feed analytics that today scrape logs.

Schema (one line per run)::

    {
      "run_id":      "uuid",
      "ts_start":    "2026-05-16T12:34:56",
      "ts_end":      "2026-05-16T12:48:12",
      "task_text":   "...",
      "task_id":     "...",
      "cwd":         "D:/programmieren/...",
      "provider":    "claude",
      "model":       "claude-opus-4-7",
      "tool":        "dev-loop",
      "profile":     "default",
      "prompt_hash": "sha256:abc...",
      "tokens": { "input": 1234, "output": 567,
                  "cache_creation": 8901, "cache_read": 23456 },
      "duration_sec": 796,
      "exit_status":  "ok | retry | error | blocked",
      "error_code":   null,
      "retry_count":  0,
      "needs_satisfied_by": ["task-id-1"],
      "log_refs":          ["logs/orchestrator.log:12345-12678"]
    }

Retention: ``logs/runs.jsonl`` keeps the current month; older lines are moved
to ``logs/runs-archive/{YYYY-MM}.jsonl.gz``. Archives older than
``ARCHIVE_RETENTION_DAYS`` are deleted.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)

DEFAULT_STORE = Path(__file__).parent / "logs" / "runs.jsonl"
DEFAULT_ARCHIVE_DIR = Path(__file__).parent / "logs" / "runs-archive"

ARCHIVE_AFTER_DAYS = 30
ARCHIVE_RETENTION_DAYS = 365

EXIT_OK = "ok"
EXIT_RETRY = "retry"
EXIT_ERROR = "error"
EXIT_BLOCKED = "blocked"
_VALID_EXIT_STATUS = {EXIT_OK, EXIT_RETRY, EXIT_ERROR, EXIT_BLOCKED}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_creation: int = 0
    cache_read: int = 0


@dataclass
class RunRecord:
    run_id: str
    ts_start: str
    ts_end: str
    task_text: str
    task_id: str
    cwd: str
    provider: str
    model: str
    tool: str
    profile: str
    prompt_hash: str
    tokens: TokenUsage
    duration_sec: float
    exit_status: str
    error_code: str | None = None
    retry_count: int = 0
    needs_satisfied_by: list[str] = field(default_factory=list)
    log_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_store_path: Path = DEFAULT_STORE
_archive_dir: Path = DEFAULT_ARCHIVE_DIR
_last_rotate_date: date | None = None


def set_store_path(path: Path, archive_dir: Path | None = None) -> None:
    """Override the store + archive paths (test helper)."""
    global _store_path, _archive_dir, _last_rotate_date
    with _lock:
        _store_path = Path(path)
        _archive_dir = Path(archive_dir) if archive_dir else _store_path.parent / "runs-archive"
        _last_rotate_date = None


def get_store_path() -> Path:
    return _store_path


def get_archive_dir() -> Path:
    return _archive_dir


def reset_for_tests() -> None:
    """Drop in-memory state and remove the JSONL + archive dir. Test-only."""
    global _last_rotate_date
    with _lock:
        _last_rotate_date = None
        try:
            _store_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.debug("reset_for_tests: store unlink failed: %s", e)
        if _archive_dir.exists():
            for archive_file in _archive_dir.iterdir():
                try:
                    archive_file.unlink()
                except OSError:
                    pass
            try:
                _archive_dir.rmdir()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def new_run_id() -> str:
    """Allocate a fresh UUID for a run."""
    return str(uuid.uuid4())


def now_iso() -> str:
    """ISO-8601 timestamp without microseconds, local time."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def prompt_hash(prompt: str) -> str:
    """sha256 hash with a stable ``sha256:`` prefix."""
    if not prompt:
        return ""
    digest = hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest}"


def append_run(record: RunRecord) -> bool:
    """Append a single run record. Returns True on success."""
    if record.exit_status not in _VALID_EXIT_STATUS:
        logger.warning("append_run: invalid exit_status %r — coerced to %r",
                       record.exit_status, EXIT_ERROR)
        record.exit_status = EXIT_ERROR

    line = json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":"))
    with _lock:
        _ensure_dirs()
        try:
            with open(_store_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            logger.warning("replay append failed: %s", e)
            return False
        _maybe_rotate_locked()
    return True


def read_runs(
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    include_archive: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """Read run records, optionally filtered by time window.

    Returns dicts (not RunRecord) so callers don't break when the schema grows.
    """
    out: list[dict] = []
    sources: list[Path] = []

    if _store_path.exists():
        sources.append(_store_path)

    if include_archive and _archive_dir.exists():
        sources.extend(sorted(_archive_dir.glob("*.jsonl.gz")))

    for src in sources:
        for rec in _iter_records(src):
            ts_raw = rec.get("ts_start", "")
            if since or until:
                try:
                    ts = datetime.strptime(ts_raw, "%Y-%m-%dT%H:%M:%S")
                except (ValueError, TypeError):
                    continue
                if since and ts < since:
                    continue
                if until and ts > until:
                    continue
            out.append(rec)
            if limit and len(out) >= limit:
                return out
    return out


def rotate_now(max_age_days: int = ARCHIVE_AFTER_DAYS) -> tuple[int, int]:
    """Force a rotation pass. Returns (archived, archive_files_pruned)."""
    with _lock:
        return _rotate_locked(max_age_days)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    _store_path.parent.mkdir(parents=True, exist_ok=True)


def _iter_records(path: Path) -> Iterator[dict]:
    try:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.debug("replay read %s failed: %s", path, e)


def _maybe_rotate_locked() -> None:
    global _last_rotate_date
    today = date.today()
    if _last_rotate_date == today:
        return
    try:
        _rotate_locked(ARCHIVE_AFTER_DAYS)
    finally:
        _last_rotate_date = today


def _rotate_locked(max_age_days: int) -> tuple[int, int]:
    """Archive records older than max_age_days. Returns (archived, pruned)."""
    archived = 0
    pruned = 0
    if not _store_path.exists():
        return archived, pruned

    cutoff = datetime.now() - timedelta(days=max_age_days)
    kept_lines: list[str] = []
    archive_buckets: dict[str, list[str]] = {}

    try:
        with open(_store_path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                    ts = datetime.strptime(obj["ts_start"], "%Y-%m-%dT%H:%M:%S")
                except (json.JSONDecodeError, KeyError, ValueError):
                    kept_lines.append(stripped)
                    continue

                if ts < cutoff:
                    bucket = ts.strftime("%Y-%m")
                    archive_buckets.setdefault(bucket, []).append(stripped)
                    archived += 1
                else:
                    kept_lines.append(stripped)
    except OSError as e:
        logger.warning("replay rotate read failed: %s", e)
        return archived, pruned

    if archive_buckets:
        _archive_dir.mkdir(parents=True, exist_ok=True)
        for bucket, lines in archive_buckets.items():
            archive_file = _archive_dir / f"{bucket}.jsonl.gz"
            try:
                existing = b""
                if archive_file.exists():
                    with gzip.open(archive_file, "rb") as gz:
                        existing = gz.read()
                with gzip.open(archive_file, "wb") as gz:
                    if existing:
                        gz.write(existing)
                        if not existing.endswith(b"\n"):
                            gz.write(b"\n")
                    gz.write(("\n".join(lines) + "\n").encode("utf-8"))
            except OSError as e:
                logger.warning("replay archive write %s failed: %s", archive_file, e)

    if archived:
        _atomic_rewrite("\n".join(kept_lines) + ("\n" if kept_lines else ""))

    # Prune archive files older than ARCHIVE_RETENTION_DAYS
    if _archive_dir.exists():
        prune_cutoff = datetime.now() - timedelta(days=ARCHIVE_RETENTION_DAYS)
        for archive_file in _archive_dir.glob("*.jsonl.gz"):
            try:
                mtime = datetime.fromtimestamp(archive_file.stat().st_mtime)
                if mtime < prune_cutoff:
                    archive_file.unlink()
                    pruned += 1
            except OSError:
                continue

    return archived, pruned


def _atomic_rewrite(content: str) -> None:
    _ensure_dirs()
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
        logger.warning("replay rewrite failed: %s", e)
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Helper for orchestrator integration
# ---------------------------------------------------------------------------

def build_record(
    *,
    run_id: str,
    ts_start: datetime,
    ts_end: datetime | None = None,
    task_text: str,
    task_id: str = "",
    cwd: str | None = "",
    provider: str = "",
    model: str = "",
    tool: str = "",
    profile: str = "",
    prompt: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    exit_status: str = EXIT_OK,
    error_code: str | None = None,
    retry_count: int = 0,
    needs_satisfied_by: Iterable[str] | None = None,
    log_refs: Iterable[str] | None = None,
) -> RunRecord:
    """Construct a RunRecord from orchestrator-side primitives.

    Keeps orchestrator.py free of dataclass plumbing — callers pass kwargs.
    """
    end = ts_end or datetime.now()
    duration = (end - ts_start).total_seconds()
    return RunRecord(
        run_id=run_id,
        ts_start=ts_start.strftime("%Y-%m-%dT%H:%M:%S"),
        ts_end=end.strftime("%Y-%m-%dT%H:%M:%S"),
        task_text=task_text,
        task_id=task_id or "",
        cwd=str(cwd) if cwd else "",
        provider=provider or "",
        model=model or "",
        tool=tool or "",
        profile=profile or "",
        prompt_hash=prompt_hash(prompt),
        tokens=TokenUsage(
            input=int(input_tokens or 0),
            output=int(output_tokens or 0),
            cache_creation=int(cache_creation_input_tokens or 0),
            cache_read=int(cache_read_input_tokens or 0),
        ),
        duration_sec=round(float(duration), 1),
        exit_status=exit_status if exit_status in _VALID_EXIT_STATUS else EXIT_ERROR,
        error_code=error_code,
        retry_count=int(retry_count or 0),
        needs_satisfied_by=list(needs_satisfied_by or []),
        log_refs=list(log_refs or []),
    )
