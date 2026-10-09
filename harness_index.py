"""
AI Orchestrator — Harness index (P3, 2026-10-09)

Incremental SQLite index over five LOCAL sources, for the dashboard's "Harness"
tab (``GET /api/harness``, which reads nothing but this database):

1. Claude Code transcripts  ``HARNESS_CLAUDE_PROJECTS_DIR/<proj>/<session>.jsonl``
   plus subagent runs ``<proj>/<session>/subagents/agent-<id>.jsonl`` / ``.meta.json``
2. Codex rollouts           ``HARNESS_CODEX_SESSIONS_DIR/YYYY/MM/DD/rollout-*.jsonl``
3. opencode                 ``HARNESS_OPENCODE_DB`` (tables ``session`` and ``message``
                            only, opened ``mode=ro``; ``part`` holds the text and is
                            never read)
4. extern-voice ledger      ``HARNESS_EXTERN_LEDGER`` (JSONL)
5. harness-change markers   ``HARNESS_CHANGES_FILE`` (JSONL)

Usage::

    python -m harness_index --update     # one incremental run; exit 0 also when
                                         # another run holds the lock ("läuft bereits")

The orchestrator never runs this in-process: ``dashboard.start_autostart()``
starts a scheduler thread that launches ``run_update_subprocess()`` every
``HARNESS_UPDATE_INTERVAL_SEC`` — its own low-priority child process, killed by
handle (never by name) after ``HARNESS_LOCK_STALE_SEC``.

Invariants (each one is pinned by a test in ``tests/test_harness_index.py``):

* **Read-only towards every source.** The only files written are the SQLite
  database, its WAL/SHM files and the lock file next to it.
* **No text.** Stored are numbers, model names, types, timestamps, ids and the
  working directory only as ``sha256(cwd)[:12]`` plus its last path segment.
  Prompts, answers, thinking, titles, descriptions, notes, paths: never. Source
  file paths are stored as a hash (the Claude project directory name is the
  encoded full cwd). Exception by design: the harness-change markers' ``change``
  and ``expect`` sentences, which the user writes in order to see them on the
  dashboard (not ``source``).
* **Idempotent.** Every row has a natural key (``(file, message.id)`` for Claude —
  one API answer is split into up to four transcript lines with identical usage,
  counting lines would double to quadruple the usage; ``(sessionId, agentId)``
  for subagent runs; one row per Codex rollout carrying the LAST cumulative
  ``token_count``; ``message.id`` for opencode; ``(ts_local, voice, repo)`` for
  the ledger; ``(date, id)`` for markers). Re-reading anything never double counts.
* **Incremental.** Per file: size, mtime, offset of the end of the last complete
  line, and a head fingerprint (sha256 of the first ``min(256, size)`` bytes at
  the last read). Unchanged → not opened. Shrunk below the offset or head changed
  → read from the start (natural keys absorb it). A line without its newline is
  not consumed; the offset stays in front of it until it is complete.
* **Deleted sources keep their rows.** The heartbeat deletes orchestrator
  transcripts after 14 days (``heartbeat._check_session_cleanup``); afterwards
  this index is the only record of that time.
* **Never parallel to itself.** ``<db>.lock`` created with ``O_CREAT|O_EXCL``,
  holding pid and start time. A lock whose pid is dead or that is older than
  ``HARNESS_LOCK_STALE_SEC`` is taken over; a live one ends this run at once with
  exit 0 and "läuft bereits".
* **Local days.** Claude/Codex timestamps are UTC, the ledger carries an offset,
  opencode uses epoch ms, markers are local calendar days — every row's ``day``
  is the LOCAL date (``datetime.astimezone()``, no zoneinfo/tzdata needed).
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import importlib
import json
import math
import os
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import config

SCHEMA_VERSION = 1
HEAD_BYTES = 256
TAIL_BYTES = 256
# A complete-looking last line without newline is evaluated only once the file
# has not been written for this long (it may still be growing otherwise).
TAIL_QUIET_SEC = 60
# opencode rows are re-read with this overlap below the stored watermark, so a
# row committed in the same millisecond as the last one read is not lost; rows
# whose time_updated did not change are not counted as read.
OPENCODE_OVERLAP_MS = 5_000
# Upper bound for any stored name-like string (model, type, agent, status, …).
NAME_MAX = 80

_FAMILIES = ("opus", "sonnet", "haiku", "fable")
ORIGIN_INTERACTIVE = "interaktiv"
ORIGIN_ORCHESTRATOR = "orchestrator"
ORIGIN_UNKNOWN = "unbekannt"
_ENTRYPOINT_ORIGIN = {"cli": ORIGIN_INTERACTIVE, "sdk-cli": ORIGIN_ORCHESTRATOR}
_AGENT_TOOL_NAMES = ("Agent", "Task")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS file_state (
    source TEXT NOT NULL, file_key TEXT NOT NULL,
    size INTEGER, mtime REAL, offset INTEGER, head_len INTEGER, head_hash TEXT,
    tail_hash TEXT, info TEXT, last_read REAL,
    PRIMARY KEY (source, file_key));
CREATE TABLE IF NOT EXISTS claude_session (
    session_id TEXT PRIMARY KEY, entrypoint TEXT, origin TEXT,
    project TEXT, cwd_hash TEXT, file_key TEXT);
CREATE TABLE IF NOT EXISTS claude_msg (
    file_key TEXT NOT NULL, msg_id TEXT NOT NULL, session_id TEXT,
    kind TEXT, agent_id TEXT, origin TEXT, model TEXT, family TEXT,
    ts TEXT, day TEXT,
    input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER,
    project TEXT, cwd_hash TEXT,
    PRIMARY KEY (file_key, msg_id));
CREATE INDEX IF NOT EXISTS claude_msg_day ON claude_msg (day);
CREATE TABLE IF NOT EXISTS claude_cost (
    session_id TEXT PRIMARY KEY, file_key TEXT, cost_usd REAL,
    start_ms INTEGER, day TEXT, origin TEXT);
CREATE TABLE IF NOT EXISTS claude_agent_call (
    tool_use_id TEXT PRIMARY KEY, file_key TEXT, session_id TEXT, kind TEXT,
    origin TEXT, ts TEXT, day TEXT, tool_name TEXT, subagent_type TEXT, model_req TEXT);
CREATE TABLE IF NOT EXISTS claude_agent_meta (
    session_id TEXT NOT NULL, agent_id TEXT NOT NULL, file_key TEXT,
    tool_use_id TEXT, agent_type TEXT, model TEXT, spawn_depth INTEGER,
    request_shape TEXT, nested INTEGER, day TEXT, origin TEXT,
    PRIMARY KEY (session_id, agent_id));
CREATE TABLE IF NOT EXISTS codex_rollout (
    file_key TEXT PRIMARY KEY, session_id TEXT, thread_source TEXT, originator TEXT,
    model TEXT, project TEXT, cwd_hash TEXT, first_ts TEXT, day TEXT,
    tc_ts TEXT, input INTEGER, cached_input INTEGER, cache_write INTEGER,
    output INTEGER, reasoning INTEGER, total INTEGER,
    rl_ts TEXT, primary_used REAL, primary_window INTEGER, primary_resets INTEGER,
    secondary_used REAL, secondary_window INTEGER, secondary_resets INTEGER,
    plan_type TEXT);
CREATE TABLE IF NOT EXISTS oc_session (
    id TEXT PRIMARY KEY, parent_id TEXT, agent TEXT, model_id TEXT, provider_id TEXT,
    time_created INTEGER, time_updated INTEGER, day TEXT, cost REAL,
    t_input INTEGER, t_output INTEGER, t_reasoning INTEGER,
    t_cache_read INTEGER, t_cache_write INTEGER);
CREATE TABLE IF NOT EXISTS oc_message (
    id TEXT PRIMARY KEY, session_id TEXT, role TEXT, agent TEXT, model_id TEXT,
    provider_id TEXT, created_ms INTEGER, time_updated INTEGER, day TEXT, cost REAL,
    t_input INTEGER, t_output INTEGER, t_reasoning INTEGER,
    t_cache_read INTEGER, t_cache_write INTEGER, finish TEXT);
CREATE TABLE IF NOT EXISTS ledger (
    ts_local TEXT NOT NULL, voice TEXT NOT NULL, repo_hash TEXT NOT NULL,
    repo_name TEXT, status TEXT, blocked_until TEXT, tokens INTEGER, day TEXT,
    PRIMARY KEY (ts_local, voice, repo_hash));
CREATE TABLE IF NOT EXISTS marker (
    date TEXT NOT NULL, id TEXT NOT NULL, scope TEXT, change TEXT, expect TEXT,
    PRIMARY KEY (date, id));
CREATE TABLE IF NOT EXISTS index_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT,
    duration_sec REAL, files_read INTEGER, bytes_read INTEGER, lines_read INTEGER,
    lines_skipped INTEGER, errors TEXT, per_source TEXT, peak_rss_mb REAL, status TEXT);
"""


# ── small helpers ───────────────────────────────────────────────────────────


def _hash(value: str, n: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:n]


def _name(value: object) -> str | None:
    """A name-like field (model, type, status …) as a bounded string, else None."""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value)
    return text[:NAME_MAX] if text else None


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _err_text(exc: BaseException) -> str:
    """Error text for the index: never a path (OSError messages carry the file name)."""
    if isinstance(exc, OSError):
        return f"{type(exc).__name__} errno={exc.errno}"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _last_segment(path: object) -> str | None:
    """Last path segment of a Windows or POSIX path, no drive, no parents."""
    if not isinstance(path, str) or not path.strip():
        return None
    parts = [p for p in path.replace("\\", "/").split("/") if p and not p.endswith(":")]
    return parts[-1][:NAME_MAX] if parts else None


def _cwd_fields(cwd: object) -> tuple[str | None, str | None]:
    if not isinstance(cwd, str) or not cwd:
        return None, None
    return _last_segment(cwd), _hash(cwd)


def _local_day_from_iso(ts: object) -> str | None:
    """Local calendar day of an ISO timestamp (``Z``/offset → converted; naive = local)."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.date().isoformat()


def _local_day_from_ms(ms: object) -> str | None:
    value = _float(ms)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def model_family(model: object) -> str | None:
    """opus / sonnet / haiku / fable / andere; ``None`` for ``<synthetic>`` and empty."""
    if not isinstance(model, str) or not model or model == "<synthetic>":
        return None
    low = model.lower()
    for family in _FAMILIES:
        if family in low:
            return family
    return "andere"


def origin_of(entrypoint: object) -> str:
    if not isinstance(entrypoint, str) or not entrypoint:
        return ORIGIN_UNKNOWN
    return _ENTRYPOINT_ORIGIN.get(entrypoint, ORIGIN_UNKNOWN)


def _peak_rss_mb() -> float | None:
    """Peak resident memory of this process in MB, best effort (None if unknown)."""
    try:
        if os.name == "nt":
            return _peak_rss_mb_windows()
        resource = importlib.import_module("resource")  # POSIX only
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB, macOS bytes.
        return round(float(peak) / (1_048_576 if sys.platform == "darwin" else 1024), 1)
    except Exception:
        return None


def _peak_rss_mb_windows() -> float | None:
    wintypes = importlib.import_module("ctypes.wintypes")

    class _Counters(ctypes.Structure):
        _fields_ = (
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        )

    counters = _Counters()
    counters.cb = ctypes.sizeof(counters)
    windll = ctypes.windll  # type: ignore[attr-defined]
    handle = windll.kernel32.GetCurrentProcess()
    if windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return round(float(counters.PeakWorkingSetSize) / 1_048_576, 1)
    return None


# ── statistics ──────────────────────────────────────────────────────────────


@dataclass
class SourceStats:
    files_seen: int = 0
    files_read: int = 0
    files_skipped_window: int = 0
    bytes_read: int = 0
    lines_read: int = 0
    lines_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        if len(self.errors) < 20:
            self.errors.append(message[:300])

    def as_dict(self) -> dict:
        return {
            "files_seen": self.files_seen, "files_read": self.files_read,
            "files_skipped_window": self.files_skipped_window,
            "bytes_read": self.bytes_read, "lines_read": self.lines_read,
            "lines_skipped": self.lines_skipped, "errors": list(self.errors),
        }


# ── lock ────────────────────────────────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists. Unknown → True (the safe answer)."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    return _pid_alive_windows(pid) if os.name == "nt" else _pid_alive_posix(pid)


def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return bool(kernel32.GetLastError() == 5)  # ERROR_ACCESS_DENIED: it exists
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


def lock_path_for(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".lock")


class IndexLock:
    """Exclusive lock file next to the database (see module docstring)."""

    def __init__(self, path: Path, stale_sec: float) -> None:
        self.path = path
        self.stale_sec = stale_sec
        self.token = f"{os.getpid()}-{time.time():.6f}-{os.urandom(4).hex()}"
        self.taken_over: dict | None = None
        self.held_by: dict | None = None

    def _write_new(self) -> bool:
        payload = json.dumps({
            "pid": os.getpid(), "started": time.time(),
            "host": socket.gethostname(), "token": self.token,
        }).encode("utf-8")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True

    def _read(self) -> dict | None:
        try:
            raw = self.path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _is_stale(self, data: dict | None) -> bool:
        now = time.time()
        if data is None:
            # Unreadable/half-written: a live writer writes within microseconds of
            # creating the file, so after a minute this is a crashed creator.
            try:
                age = now - self.path.stat().st_mtime
            except OSError:
                return True
            return age > 60
        started = _float(data.get("started")) or 0.0
        pid = _int(data.get("pid")) or 0
        if now - started > self.stale_sec:
            return True
        return not _pid_alive(pid)

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(3):
            if self._write_new():
                return True
            data = self._read()
            if not self._is_stale(data):
                self.held_by = data
                return False
            self.taken_over = data or {}
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
        return False

    def release(self) -> None:
        data = self._read()
        if data is not None and data.get("token") == self.token:
            with contextlib.suppress(OSError):
                self.path.unlink()


# ── database ────────────────────────────────────────────────────────────────


class SchemaMismatchError(RuntimeError):
    pass


def open_index(db_path: Path) -> sqlite3.Connection:
    """Open (and create) the index for writing, WAL mode, schema checked."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    elif row[0] != str(SCHEMA_VERSION):
        conn.close()
        raise SchemaMismatchError(f"schema_version {row[0]} in {db_path}, expected {SCHEMA_VERSION}")
    return conn


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


# ── incremental JSONL reader ────────────────────────────────────────────────


@dataclass
class FileState:
    size: int = 0
    mtime: float = 0.0
    offset: int = 0
    head_len: int = 0
    head_hash: str = ""
    info: dict = field(default_factory=dict)
    # sha256 of the (up to) TAIL_BYTES bytes right before `offset`: catches a file
    # rewritten in place with the same head, where the old offset would land in
    # the middle of a new line and everything before it would be missed.
    tail_hash: str = ""


def _load_state(conn: sqlite3.Connection, source: str, file_key: str) -> FileState | None:
    row = conn.execute(
        "SELECT size, mtime, offset, head_len, head_hash, info, tail_hash FROM file_state "
        "WHERE source=? AND file_key=?", (source, file_key),
    ).fetchone()
    if row is None:
        return None
    try:
        info = json.loads(row[5]) if row[5] else {}
    except ValueError:
        info = {}
    return FileState(row[0] or 0, row[1] or 0.0, row[2] or 0, row[3] or 0, row[4] or "", info, row[6] or "")


def _save_state(conn: sqlite3.Connection, source: str, file_key: str, st: FileState) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO file_state(source, file_key, size, mtime, offset, head_len, "
        "head_hash, tail_hash, info, last_read) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (source, file_key, st.size, st.mtime, st.offset, st.head_len, st.head_hash, st.tail_hash,
         json.dumps(st.info, sort_keys=True), time.time()),
    )


def _head_hash(f, length: int) -> str:
    f.seek(0)
    return hashlib.sha256(f.read(length)).hexdigest()


def _tail_hash(f, offset: int) -> str:
    start = max(0, offset - TAIL_BYTES)
    f.seek(start)
    return hashlib.sha256(f.read(offset - start)).hexdigest()


def _iter_new_lines(
    path: Path,
    prev: FileState | None,
    stats: SourceStats,
    now: float,
) -> Iterator[tuple[bytes, FileState, bool]]:
    """Yield ``(line, state, reset)`` for every complete new line of ``path``.

    ``state`` is the new file state the caller saves after consuming the lines
    (its offset already sits behind the last complete line). ``reset`` is True
    when the file is read from the start because it shrank below the stored
    offset or its head changed. Nothing is yielded for an unchanged file.
    A trailing segment without newline is not consumed; it is yielded (without
    advancing the offset) only if it parses as JSON and the file has been quiet
    for ``TAIL_QUIET_SEC`` — natural keys make that re-evaluation harmless.
    """
    st = path.stat()
    size, mtime = st.st_size, st.st_mtime
    with path.open("rb") as f:
        reset = False
        offset = 0
        if prev is not None:
            same_head = size >= prev.head_len and _head_hash(f, prev.head_len) == prev.head_hash
            if same_head and size == prev.size and mtime == prev.mtime:
                return  # unchanged: not read at all
            if (
                not same_head or size < prev.offset
                or (prev.tail_hash and _tail_hash(f, prev.offset) != prev.tail_hash)
            ):
                reset = True
            else:
                offset = prev.offset
        head_len = min(HEAD_BYTES, size)
        new_state = FileState(
            size=size, mtime=mtime, offset=offset, head_len=head_len,
            head_hash=_head_hash(f, head_len),
            info={} if (reset or prev is None) else dict(prev.info),
        )
        stats.files_read += 1
        f.seek(offset)
        first = True
        for raw in f:
            if not raw.endswith(b"\n"):
                # incomplete last line: keep the offset in front of it
                stats.bytes_read += len(raw)
                if now - mtime >= TAIL_QUIET_SEC:
                    try:
                        json.loads(raw)
                    except ValueError:
                        pass
                    else:
                        stats.lines_read += 1
                        yield raw, new_state, reset and first
                        first = False
                break
            new_state.offset += len(raw)
            stats.bytes_read += len(raw)
            stats.lines_read += 1
            yield raw, new_state, reset and first
            first = False
        new_state.tail_hash = _tail_hash(f, new_state.offset)
        if first:
            # nothing yielded (e.g. only an incomplete tail): still hand out the state
            yield b"", new_state, reset


def _unchanged(path: Path, prev: FileState | None) -> bool:
    """True when the file is byte-for-byte where the last run left it (no BEGIN, no read)."""
    if prev is None:
        return False
    st = path.stat()
    if st.st_size != prev.size or st.st_mtime != prev.mtime or st.st_size < prev.head_len:
        return False
    with path.open("rb") as f:
        return _head_hash(f, prev.head_len) == prev.head_hash


def _parse_line(raw: bytes, needles: tuple[bytes, ...], stats: SourceStats) -> dict | None:
    """json.loads with a cheap pre-filter.

    A compact line (``"type":"`` present) that carries none of the needles is
    skipped unparsed. A line in any other format is parsed — the pre-filter is
    an acceleration, never a reason to miss a line. A complete line that does
    not parse to an object counts as skipped.
    """
    if not raw.strip():
        return None
    if needles and not any(n in raw for n in needles) and b'"type":"' in raw:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        stats.lines_skipped += 1
        return None
    if not isinstance(obj, dict):
        stats.lines_skipped += 1
        return None
    return obj


def _run_file(
    conn: sqlite3.Connection,
    source: str,
    file_key: str,
    path: Path,
    stats: SourceStats,
    *,
    handle: Callable[[dict, FileState, bool], None],
    needles: Callable[[FileState], tuple[bytes, ...]],
    on_reset: Callable[[], None] | None = None,
    on_done: Callable[[FileState], None] | None = None,
) -> None:
    """Read the new lines of one file inside ONE transaction (rows + state)."""
    prev = _load_state(conn, source, file_key)
    if _unchanged(path, prev):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        state: FileState | None = None
        now = time.time()
        for raw, state, reset in _iter_new_lines(path, prev, stats, now):
            if reset and on_reset is not None:
                on_reset()
            if not raw:
                continue
            obj = _parse_line(raw, needles(state), stats)
            if obj is None:
                continue
            try:
                handle(obj, state, reset)
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                stats.lines_skipped += 1
                stats.error(f"{source}: {type(e).__name__}")
        if state is not None:
            if on_done is not None:
                on_done(state)
            _save_state(conn, source, file_key, state)
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def _in_window(path: Path, cutoff: float, stats: SourceStats) -> bool:
    try:
        mtime = path.stat().st_mtime
    except OSError as e:
        stats.error(f"stat: {_err_text(e)}")
        return False
    if mtime < cutoff:
        stats.files_skipped_window += 1
        return False
    return True


# ── source 1: Claude Code transcripts ───────────────────────────────────────


def _claude_files(root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    if not root.is_dir():
        return [], [], []
    main = sorted(root.glob("*/*.jsonl"))
    sub = sorted(root.glob("*/*/subagents/agent-*.jsonl"))
    meta = sorted(root.glob("*/*/subagents/agent-*.meta.json"))
    return main, sub, meta


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _index_claude(conn: sqlite3.Connection, root: Path, cutoff: float, stats: SourceStats) -> None:
    main_files, sub_files, meta_files = _claude_files(root)
    for path in main_files:
        stats.files_seen += 1
        if _in_window(path, cutoff, stats):
            _guard_file(stats, path, _claude_main_file, conn, root, path, stats)
    for path in sub_files:
        stats.files_seen += 1
        if _in_window(path, cutoff, stats):
            _guard_file(stats, path, _claude_sub_file, conn, root, path, stats)
    for path in meta_files:
        stats.files_seen += 1
        if _in_window(path, cutoff, stats):
            _guard_file(stats, path, _claude_meta_file, conn, root, path, stats)


def _guard_file(stats: SourceStats, path: Path, work: Callable[..., None], *args: object) -> None:
    """Run one file's work; an I/O or database error is recorded, the run goes on."""
    try:
        work(*args)
    except (OSError, sqlite3.Error, UnicodeError) as e:
        stats.error(f"{_err_text(e)} in file #{_hash(str(path), 8)}")


def _claude_assistant(
    conn: sqlite3.Connection, obj: dict, stats: SourceStats, *, file_key: str, kind: str,
    agent_id: str | None, session_id: str | None, origin: str,
    project: str | None, cwd_hash: str | None,
) -> None:
    msg = obj.get("message")
    if not isinstance(msg, dict):
        stats.lines_skipped += 1
        return
    ts = _str(obj.get("timestamp"))
    day = _local_day_from_iso(ts)
    content = msg.get("content")
    if isinstance(content, list):
        for item in content:
            if (
                isinstance(item, dict) and item.get("type") == "tool_use"
                and item.get("name") in _AGENT_TOOL_NAMES and isinstance(item.get("id"), str)
            ):
                inp = _dict(item.get("input"))
                conn.execute(
                    "INSERT OR REPLACE INTO claude_agent_call(tool_use_id, file_key, session_id, kind, "
                    "origin, ts, day, tool_name, subagent_type, model_req) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (item["id"][:NAME_MAX], file_key, session_id, kind, origin, ts, day,
                     item.get("name"), _name(inp.get("subagent_type")),
                     _name(inp.get("model")) or "Standard"),
                )
    model = msg.get("model")
    family = model_family(model)
    if family is None:
        return  # <synthetic> and model-less lines carry no countable usage
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return
    msg_id = _str(msg.get("id")) or _str(obj.get("uuid"))
    if msg_id is None:
        stats.lines_skipped += 1
        return
    # Upsert with max(): the same message.id recurs in up to four lines with
    # identical usage (one API answer split per content block); should a later
    # line ever carry a larger (final) count, that one wins. Never a sum.
    conn.execute(
        "INSERT INTO claude_msg(file_key, msg_id, session_id, kind, agent_id, origin, model, family, "
        "ts, day, input, output, cache_read, cache_write, project, cwd_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(file_key, msg_id) DO UPDATE SET "
        "input=max(coalesce(input, 0), coalesce(excluded.input, 0)), "
        "output=max(coalesce(output, 0), coalesce(excluded.output, 0)), "
        "cache_read=max(coalesce(cache_read, 0), coalesce(excluded.cache_read, 0)), "
        "cache_write=max(coalesce(cache_write, 0), coalesce(excluded.cache_write, 0)), "
        "origin=excluded.origin",
        (file_key, msg_id[:NAME_MAX], session_id, kind, agent_id, origin, _name(model), family,
         ts, day, _int(usage.get("input_tokens")), _int(usage.get("output_tokens")),
         _int(usage.get("cache_read_input_tokens")), _int(usage.get("cache_creation_input_tokens")),
         project, cwd_hash),
    )


def _claude_main_file(conn: sqlite3.Connection, root: Path, path: Path, stats: SourceStats) -> None:
    rel = _rel(root, path)
    file_key = _hash("claude:" + rel, 20)
    stem_session = path.stem

    def needles(state: FileState) -> tuple[bytes, ...]:
        base = (b'"type":"assistant"', b'"type":"cost-state"')
        return base if "entrypoint" in state.info else (*base, b'"entrypoint"')

    def handle(obj: dict, state: FileState, _reset: bool) -> None:
        info = state.info
        if "entrypoint" not in info and "entrypoint" in obj:
            info["entrypoint"] = _name(obj.get("entrypoint"))
            project, cwd_hash = _cwd_fields(obj.get("cwd"))
            info["project"], info["cwd_hash"] = project, cwd_hash
        session_id = _str(obj.get("sessionId")) or stem_session
        origin = origin_of(info.get("entrypoint"))
        kind = obj.get("type")
        if kind == "assistant":
            _claude_assistant(conn, obj, stats, file_key=file_key, kind="main", agent_id=None,
                              session_id=session_id[:NAME_MAX], origin=origin,
                              project=info.get("project"), cwd_hash=info.get("cwd_hash"))
        elif kind == "cost-state":
            cost = _float(obj.get("totalCostUSD"))
            start_ms = _int(obj.get("startTime"))
            conn.execute(
                "INSERT OR REPLACE INTO claude_cost(session_id, file_key, cost_usd, start_ms, day, origin) "
                "VALUES (?,?,?,?,?,?)",
                (session_id[:NAME_MAX], file_key, cost, start_ms, _local_day_from_ms(start_ms), origin),
            )

    def on_done(state: FileState) -> None:
        info = state.info
        conn.execute(
            "INSERT OR REPLACE INTO claude_session(session_id, entrypoint, origin, project, cwd_hash, file_key) "
            "VALUES (?,?,?,?,?,?)",
            (stem_session[:NAME_MAX], info.get("entrypoint"), origin_of(info.get("entrypoint")),
             info.get("project"), info.get("cwd_hash"), file_key),
        )

    _run_file(conn, "claude", file_key, path, stats, handle=handle, needles=needles, on_done=on_done)


def _parent_origin(conn: sqlite3.Connection, session_id: str) -> str | None:
    row = conn.execute("SELECT origin, entrypoint FROM claude_session WHERE session_id=?", (session_id,)).fetchone()
    if row is None or row[1] is None:
        return None
    return str(row[0])


def _claude_sub_file(conn: sqlite3.Connection, root: Path, path: Path, stats: SourceStats) -> None:
    rel = _rel(root, path)
    file_key = _hash("claude:" + rel, 20)
    parent_session = path.parent.parent.name[:NAME_MAX]
    agent_id = path.name.removeprefix("agent-").removesuffix(".jsonl")[:NAME_MAX]
    parent = _parent_origin(conn, parent_session)

    def needles(_state: FileState) -> tuple[bytes, ...]:
        return (b'"type":"assistant"',)

    def handle(obj: dict, state: FileState, _reset: bool) -> None:
        info = state.info
        if "entrypoint" not in info and "entrypoint" in obj:
            info["entrypoint"] = _name(obj.get("entrypoint"))
            project, cwd_hash = _cwd_fields(obj.get("cwd"))
            info["project"], info["cwd_hash"] = project, cwd_hash
        if obj.get("type") != "assistant":
            return
        # The subagent's share stays "subagent", attributed to the PARENT's
        # entrypoint; the file's own field only when the parent is unknown.
        origin = parent or origin_of(obj.get("entrypoint") or info.get("entrypoint"))
        _claude_assistant(conn, obj, stats, file_key=file_key, kind="subagent", agent_id=agent_id,
                          session_id=parent_session, origin=origin,
                          project=info.get("project"), cwd_hash=info.get("cwd_hash"))

    _run_file(conn, "claude", file_key, path, stats, handle=handle, needles=needles)


def _claude_meta_file(conn: sqlite3.Connection, root: Path, path: Path, stats: SourceStats) -> None:
    rel = _rel(root, path)
    file_key = _hash("claude:" + rel, 20)
    st = path.stat()
    prev = _load_state(conn, "claude-meta", file_key)
    if prev is not None and prev.size == st.st_size and prev.mtime == st.st_mtime:
        return
    stats.files_read += 1
    raw = path.read_bytes()
    stats.bytes_read += len(raw)
    stats.lines_read += 1
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    session_id = path.parent.parent.name[:NAME_MAX]
    agent_id = path.name.removeprefix("agent-").removesuffix(".meta.json")[:NAME_MAX]
    conn.execute("BEGIN IMMEDIATE")
    try:
        if isinstance(data, dict):
            conn.execute(
                "INSERT OR REPLACE INTO claude_agent_meta(session_id, agent_id, file_key, tool_use_id, "
                "agent_type, model, spawn_depth, request_shape, nested, day, origin) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, agent_id, file_key, _name(data.get("toolUseId")), _name(data.get("agentType")),
                 _name(data.get("model")) or "Standard", _int(data.get("spawnDepth")),
                 _name(data.get("requestShape")), 1 if data.get("parentAgentId") else 0,
                 datetime.fromtimestamp(st.st_mtime).date().isoformat(),
                 _parent_origin(conn, session_id) or ORIGIN_UNKNOWN),
            )
        else:
            stats.lines_skipped += 1
        _save_state(conn, "claude-meta", file_key, FileState(st.st_size, st.st_mtime, st.st_size))
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


# ── source 2: Codex rollouts ────────────────────────────────────────────────

_CODEX_FIELDS = (
    "session_id", "thread_source", "originator", "model", "project", "cwd_hash", "first_ts", "day",
    "tc_ts", "input", "cached_input", "cache_write", "output", "reasoning", "total",
    "rl_ts", "primary_used", "primary_window", "primary_resets",
    "secondary_used", "secondary_window", "secondary_resets", "plan_type",
)


def _index_codex(conn: sqlite3.Connection, root: Path, cutoff: float, stats: SourceStats) -> None:
    if not root.is_dir():
        return
    for path in sorted(root.glob("**/rollout-*.jsonl")):
        stats.files_seen += 1
        if _in_window(path, cutoff, stats):
            _guard_file(stats, path, _codex_file, conn, root, path, stats)


def _codex_file(conn: sqlite3.Connection, root: Path, path: Path, stats: SourceStats) -> None:
    file_key = _hash("codex:" + _rel(root, path), 20)
    existing = conn.execute(
        f"SELECT {', '.join(_CODEX_FIELDS)} FROM codex_rollout WHERE file_key=?", (file_key,),
    ).fetchone()
    row: dict = dict(zip(_CODEX_FIELDS, existing, strict=True)) if existing else {}

    def on_reset() -> None:
        row.clear()  # the file's content is the truth: start the one row afresh

    def needles(_state: FileState) -> tuple[bytes, ...]:
        return (b'"type":"session_meta"', b'"type":"turn_context"', b'"type":"token_count"')

    def handle(obj: dict, _state: FileState, _reset: bool) -> None:
        kind = obj.get("type")
        payload = _dict(obj.get("payload"))
        ts = _str(obj.get("timestamp"))
        if kind == "session_meta":
            row["session_id"] = _name(payload.get("id"))
            row["thread_source"] = _name(payload.get("thread_source"))
            row["originator"] = _name(payload.get("originator"))
            row["project"], row["cwd_hash"] = _cwd_fields(payload.get("cwd"))
            first = _str(payload.get("timestamp")) or ts
            row["first_ts"] = first
            row["day"] = _local_day_from_iso(first)
        elif kind == "turn_context":
            if payload.get("model") is not None:
                row["model"] = _name(payload.get("model"))
        elif kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                # LAST cumulative value wins — never a sum over events.
                total = info["total_token_usage"]
                row["tc_ts"] = ts
                row["input"] = _int(total.get("input_tokens"))
                row["cached_input"] = _int(total.get("cached_input_tokens"))
                row["cache_write"] = _int(total.get("cache_write_input_tokens"))
                row["output"] = _int(total.get("output_tokens"))
                row["reasoning"] = _int(total.get("reasoning_output_tokens"))
                row["total"] = _int(total.get("total_tokens"))
            limits = payload.get("rate_limits")
            if isinstance(limits, dict):
                row["rl_ts"] = ts
                for key in ("primary", "secondary"):
                    win = _dict(limits.get(key))
                    row[f"{key}_used"] = _float(win.get("used_percent"))
                    row[f"{key}_window"] = _int(win.get("window_minutes"))
                    row[f"{key}_resets"] = _int(win.get("resets_at"))
                row["plan_type"] = _name(limits.get("plan_type"))

    def on_done(_state: FileState) -> None:
        if not row:
            return
        if row.get("day") is None:
            row["day"] = _local_day_from_iso(row.get("tc_ts"))
        conn.execute(
            f"INSERT OR REPLACE INTO codex_rollout(file_key, {', '.join(_CODEX_FIELDS)}) "
            f"VALUES (?, {', '.join('?' for _ in _CODEX_FIELDS)})",
            (file_key, *(row.get(k) for k in _CODEX_FIELDS)),
        )

    _run_file(conn, "codex", file_key, path, stats, handle=handle, needles=needles,
              on_reset=on_reset, on_done=on_done)


# ── source 3: opencode.db ───────────────────────────────────────────────────


def _open_opencode_ro(path: Path) -> sqlite3.Connection:
    """Read-only, WAL-aware. Never ``immutable=1``: that ignores the WAL and
    returns stale data while opencode is running."""
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _index_opencode(conn: sqlite3.Connection, path: Path, cutoff: float, stats: SourceStats) -> None:
    if not path.is_file():
        return
    stats.files_seen += 1
    before = stats.lines_read
    src = _open_opencode_ro(path)
    try:
        floor_ms = int(cutoff * 1000)
        _opencode_sessions(conn, src, floor_ms, stats)
        _opencode_messages(conn, src, floor_ms, stats)
    finally:
        src.close()
    if stats.lines_read > before:
        stats.files_read += 1


def _watermark(conn: sqlite3.Connection, key: str, floor_ms: int) -> int:
    stored = _meta_get(conn, key)
    try:
        value = int(stored) if stored is not None else 0
    except ValueError:
        value = 0
    return max(floor_ms, value - OPENCODE_OVERLAP_MS)


def _opencode_sessions(conn: sqlite3.Connection, src: sqlite3.Connection, floor_ms: int, stats: SourceStats) -> None:
    since = _watermark(conn, "oc_session_watermark", floor_ms)
    # Explicit column list: title, directory, slug, summary_diffs are never read.
    cur = src.execute(
        "SELECT id, parent_id, agent, model, time_created, time_updated, cost, tokens_input, "
        "tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write "
        "FROM session WHERE time_updated >= ? ORDER BY time_updated", (since,),
    )
    newest = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in cur:
            sid, parent, agent, model_raw, created, updated = row[:6]
            known = conn.execute("SELECT time_updated FROM oc_session WHERE id=?", (sid,)).fetchone()
            newest = updated if newest is None or (updated or 0) > newest else newest
            if known is not None and known[0] == updated:
                continue
            stats.lines_read += 1
            model_id = provider_id = None
            if isinstance(model_raw, str) and model_raw:
                try:
                    model_obj = json.loads(model_raw)
                except ValueError:
                    model_obj = None
                if isinstance(model_obj, dict):
                    model_id, provider_id = _name(model_obj.get("id")), _name(model_obj.get("providerID"))
                else:
                    stats.lines_skipped += 1
            conn.execute(
                "INSERT OR REPLACE INTO oc_session(id, parent_id, agent, model_id, provider_id, time_created, "
                "time_updated, day, cost, t_input, t_output, t_reasoning, t_cache_read, t_cache_write) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_name(sid), _name(parent), _name(agent), model_id, provider_id, _int(created),
                 _int(updated), _local_day_from_ms(created), _float(row[6]),
                 *(_int(v) for v in row[7:12])),
            )
        if newest is not None:
            _meta_set(conn, "oc_session_watermark", str(newest))
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def _opencode_messages(conn: sqlite3.Connection, src: sqlite3.Connection, floor_ms: int, stats: SourceStats) -> None:
    since = _watermark(conn, "oc_message_watermark", floor_ms)
    cur = src.execute(
        "SELECT id, session_id, time_created, time_updated, data FROM message "
        "WHERE time_updated >= ? ORDER BY time_updated", (since,),
    )
    newest = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        for mid, sid, created, updated, data in cur:
            newest = updated if newest is None or (updated or 0) > newest else newest
            known = conn.execute("SELECT time_updated FROM oc_message WHERE id=?", (mid,)).fetchone()
            if known is not None and known[0] == updated:
                continue
            stats.lines_read += 1
            stats.bytes_read += len(data) if isinstance(data, (str, bytes)) else 0
            try:
                obj = json.loads(data)
            except (TypeError, ValueError):
                obj = None
            if not isinstance(obj, dict):
                # remembered as seen (role NULL), so it is not re-read every run
                stats.lines_skipped += 1
                conn.execute(
                    "INSERT OR REPLACE INTO oc_message(id, session_id, time_updated) VALUES (?,?,?)",
                    (_name(mid), _name(sid), _int(updated)),
                )
                continue
            role = _name(obj.get("role"))
            tokens = _dict(obj.get("tokens"))
            cache = _dict(tokens.get("cache"))
            times = _dict(obj.get("time"))
            created_ms = _int(times.get("created")) or _int(created)
            conn.execute(
                "INSERT OR REPLACE INTO oc_message(id, session_id, role, agent, model_id, provider_id, "
                "created_ms, time_updated, day, cost, t_input, t_output, t_reasoning, t_cache_read, "
                "t_cache_write, finish) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_name(mid), _name(sid), role, _name(obj.get("agent") or obj.get("mode")),
                 _name(obj.get("modelID")), _name(obj.get("providerID")), created_ms, _int(updated),
                 _local_day_from_ms(created_ms), _float(obj.get("cost")),
                 _int(tokens.get("input")), _int(tokens.get("output")), _int(tokens.get("reasoning")),
                 _int(cache.get("read")), _int(cache.get("write")), _name(obj.get("finish"))),
            )
        if newest is not None:
            _meta_set(conn, "oc_message_watermark", str(newest))
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


# ── sources 4 + 5: ledger and markers ───────────────────────────────────────


def _index_ledger(conn: sqlite3.Connection, path: Path, stats: SourceStats) -> None:
    if not path.is_file():
        return
    stats.files_seen += 1

    def handle(obj: dict, _state: FileState, _reset: bool) -> None:
        ts = obj.get("ts_local")
        voice = _name(obj.get("voice"))
        if not isinstance(ts, str) or not voice:
            stats.lines_skipped += 1
            return
        repo = _str(obj.get("repo")) or ""
        tokens = obj.get("tokens")
        conn.execute(
            "INSERT OR REPLACE INTO ledger(ts_local, voice, repo_hash, repo_name, status, blocked_until, "
            "tokens, day) VALUES (?,?,?,?,?,?,?,?)",
            (ts[:40], voice, _hash(repo), _last_segment(repo), _name(obj.get("status")),
             _name(obj.get("blocked_until")), _int(tokens), _local_day_from_iso(ts)),
        )

    def run() -> None:
        _run_file(conn, "ledger", "ledger", path, stats, handle=handle, needles=lambda _s: ())

    _guard_file(stats, path, run)


def _index_markers(conn: sqlite3.Connection, path: Path, stats: SourceStats) -> None:
    """Markers are small and user-edited: a changed file replaces the table
    (a corrected date must not leave a ghost line). A deleted file keeps it."""
    if not path.is_file():
        return
    stats.files_seen += 1
    st = path.stat()
    prev = _load_state(conn, "markers", "markers")
    raw = path.read_bytes()
    head = hashlib.sha256(raw).hexdigest()
    if prev is not None and prev.size == st.st_size and prev.mtime == st.st_mtime and prev.head_hash == head:
        return
    stats.files_read += 1
    stats.bytes_read += len(raw)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM marker")
        for line in raw.splitlines():
            if not line.strip():
                continue
            stats.lines_read += 1
            try:
                obj = json.loads(line)
            except ValueError:
                stats.lines_skipped += 1
                continue
            if not isinstance(obj, dict) or not isinstance(obj.get("date"), str) or not obj.get("id"):
                stats.lines_skipped += 1
                continue
            try:
                date = datetime.strptime(obj["date"][:10], "%Y-%m-%d").date().isoformat()
            except ValueError:
                stats.lines_skipped += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO marker(date, id, scope, change, expect) VALUES (?,?,?,?,?)",
                (date, _name(obj.get("id")), _name(obj.get("scope")),
                 str(obj.get("change") or "")[:300], str(obj.get("expect") or "")[:300]),
            )
        _save_state(conn, "markers", "markers", FileState(st.st_size, st.st_mtime, st.st_size, 0, head))
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


# ── run ─────────────────────────────────────────────────────────────────────


@dataclass
class Sources:
    db: Path
    claude: Path
    codex: Path
    opencode: Path
    ledger: Path
    changes: Path
    window_days: int
    stale_sec: int

    @classmethod
    def from_config(cls) -> Sources:
        return cls(
            db=Path(config.HARNESS_DB_FILE), claude=Path(config.HARNESS_CLAUDE_PROJECTS_DIR),
            codex=Path(config.HARNESS_CODEX_SESSIONS_DIR), opencode=Path(config.HARNESS_OPENCODE_DB),
            ledger=Path(config.HARNESS_EXTERN_LEDGER), changes=Path(config.HARNESS_CHANGES_FILE),
            window_days=int(config.HARNESS_WINDOW_DAYS), stale_sec=int(config.HARNESS_LOCK_STALE_SEC),
        )


def _record_run(conn: sqlite3.Connection, *, started: float, finished: float,
                totals: dict, errors: dict, per_source: dict, peak: float | None, status: str) -> None:
    conn.execute(
        "INSERT INTO index_runs(started, finished, duration_sec, files_read, bytes_read, lines_read, "
        "lines_skipped, errors, per_source, peak_rss_mb, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.fromtimestamp(started).isoformat(timespec="seconds"),
         datetime.fromtimestamp(finished).isoformat(timespec="seconds"),
         round(finished - started, 3), totals["files_read"], totals["bytes_read"],
         totals["lines_read"], totals["lines_skipped"], json.dumps(errors),
         json.dumps(per_source), peak, status),
    )


def _index_all(conn: sqlite3.Connection, sources: Sources) -> dict[str, SourceStats]:
    per_source = {name: SourceStats() for name in ("claude", "codex", "opencode", "ledger", "markers")}
    cutoff = (datetime.now() - timedelta(days=sources.window_days)).timestamp()
    steps: list[tuple[str, Callable[[], None]]] = [
        ("claude", lambda: _index_claude(conn, sources.claude, cutoff, per_source["claude"])),
        ("codex", lambda: _index_codex(conn, sources.codex, cutoff, per_source["codex"])),
        ("opencode", lambda: _index_opencode(conn, sources.opencode, cutoff, per_source["opencode"])),
        ("ledger", lambda: _index_ledger(conn, sources.ledger, per_source["ledger"])),
        ("markers", lambda: _index_markers(conn, sources.changes, per_source["markers"])),
    ]
    for name, step in steps:
        try:
            step()
        except Exception as e:  # one broken source must not stop the others
            per_source[name].error(_err_text(e))
    return per_source


def run_update(sources: Sources | None = None, *, out: Callable[[str], None] = print) -> dict:
    """One incremental run. Returns a summary dict; ``status`` is ``ok``,
    ``partial`` (a source reported errors), ``busy`` (another live run holds
    the lock) or ``error`` (the index itself could not be opened)."""
    sources = sources or Sources.from_config()
    def say(message: str) -> None:  # an orphaned child's broken stdout must not abort a run
        with contextlib.suppress(Exception):
            out(message)

    lock = IndexLock(lock_path_for(sources.db), sources.stale_sec)
    if not lock.acquire():
        holder = lock.held_by or {}
        say(f"Harness-Index läuft bereits (pid {holder.get('pid', '?')}) — dieser Lauf endet ohne Arbeit.")
        return {"status": "busy", "held_by": holder.get("pid")}
    try:
        if lock.taken_over is not None:
            say(f"Veralteten Lock übernommen (pid {lock.taken_over.get('pid', '?')}).")
        started = time.time()
        try:
            conn = open_index(sources.db)
        except (sqlite3.Error, SchemaMismatchError, OSError) as e:
            say(f"Harness-Index nicht nutzbar: {_err_text(e)}")
            return {"status": "error", "error": _err_text(e)}
        try:
            per_source = _index_all(conn, sources)
            finished = time.time()
            totals = {
                "files_read": sum(s.files_read for s in per_source.values()),
                "bytes_read": sum(s.bytes_read for s in per_source.values()),
                "lines_read": sum(s.lines_read for s in per_source.values()),
                "lines_skipped": sum(s.lines_skipped for s in per_source.values()),
            }
            errors = {k: v.errors for k, v in per_source.items() if v.errors}
            status = "partial" if errors else "ok"
            peak = _peak_rss_mb()
            detail = {k: v.as_dict() for k, v in per_source.items()}
            try:
                _record_run(conn, started=started, finished=finished, totals=totals, errors=errors,
                            per_source=detail, peak=peak, status=status)
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error as e:  # the data is committed; only the run log is missing
                say(f"index_runs nicht geschrieben: {_err_text(e)}")
        finally:
            conn.close()
        summary = {
            "status": status, "duration_sec": round(finished - started, 3), **totals,
            "errors": errors, "peak_rss_mb": peak, "per_source": detail,
        }
        say(
            f"Harness-Index {status}: {totals['files_read']} Dateien, {totals['lines_read']} Zeilen, "
            f"{totals['bytes_read'] / 1_048_576:.1f} MB gelesen, {totals['lines_skipped']} übersprungen, "
            f"{summary['duration_sec']:.1f} s, Spitze {peak if peak is not None else '?'} MB"
        )
        for name, errs in errors.items():
            say(f"  Fehler {name}: {'; '.join(errs[:3])}")
        return summary
    finally:
        lock.release()


def run_update_subprocess(*, timeout: float | None = None) -> dict:
    """Run ``python -m harness_index --update`` as a low-priority child process.

    Working directory = the directory of ``config.py`` (the watchdog does not
    necessarily start in the repo). On timeout the child is killed through its
    own Popen handle — never by name. Never raises for a failed child; an error
    to start it is returned in ``error``.
    """
    cmd = [sys.executable, "-X", "utf8", "-m", "harness_index", "--update"]
    cwd = Path(config.__file__).resolve().parent
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    started = time.monotonic()
    try:
        proc = subprocess.Popen(  # fixed argv, own interpreter
            cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs,
        )
    except OSError as e:
        return {"returncode": None, "error": f"Start fehlgeschlagen: {type(e).__name__}: {e}"}
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()  # our own child, by handle
        out, err = proc.communicate()
        return {
            "returncode": proc.returncode, "timed_out": True,
            "duration_sec": round(time.monotonic() - started, 1),
            "error": f"Zeitgrenze {timeout} s überschritten, Kindprozess beendet",
        }
    text = out.decode("utf-8", "replace").strip()
    result = {
        "returncode": proc.returncode, "timed_out": False,
        "duration_sec": round(time.monotonic() - started, 1),
        "stdout": text[-2000:],
    }
    if proc.returncode != 0:
        result["error"] = f"Exit {proc.returncode}: {err.decode('utf-8', 'replace').strip()[-300:]}"
    return result


# ── read side: the dashboard's /api/harness ────────────────────────────────

# Harness-change marker → KPI. `expect` is free text, so the mapping is a FIXED
# keyword table, visible here: a keyword found in `expect` (case-insensitive)
# links the marker to a daily series; a marker without any keyword gets only its
# vertical line, never a guessed metric. All series are per DAY — the index has
# no loop/pass identifier (that is the excluded phase 2).
MARKER_KPIS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("externe aufrufe",), "ledger_calls", "Externe Aufrufe je Tag (Ledger)"),
    (("fehlversuche",), "ledger_failures", "Externe Fehlversuche je Tag (Ledger, Status ≠ ok)"),
    (("opencode-kosten", "kosten je pass"), "opencode_cost", "opencode-Kosten je Tag (Katalogpreis, USD)"),
    (("sessions je pass",), "opencode_sessions", "opencode-Sitzungen je Tag"),
)
MARKER_WINDOWS = (3, 7, 14)
HARNESS_READ_TIMEOUT_SEC = 0.5     # sqlite busy timeout of the read connection
HARNESS_QUERY_BUDGET_SEC = 3.0     # hard cap for all queries of one request
CATEGORIES = ("interaktiv", "orchestrator", "subagent", "unbekannt")
_COUNTERS = ("input", "output", "cache_read", "cache_write")


def marker_kpis(expect: object) -> list[tuple[str, str]]:
    """The (kpi key, label) pairs a marker's ``expect`` text names, in table order."""
    text = str(expect or "").lower()
    return [(key, label) for words, key, label in MARKER_KPIS if any(w in text for w in words)]


def marker_window(series: dict[str, float], marker_day: str, n: int, today: str) -> dict:
    """Before/after comparison around a marker: n days each, the marker day in neither.

    ``before`` = [D-n, D-1], ``after`` = [D+1, D+n]; only completed days (before
    ``today``) count for "after", so a window still running is reported as
    ``complete=False`` with ``after_days`` < n. Averages are per day over the
    days counted; a missing day in ``series`` is 0.
    """
    d = datetime.strptime(marker_day, "%Y-%m-%d").date()
    last_full = datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=1)
    before = [(d - timedelta(days=i)).isoformat() for i in range(n, 0, -1)]
    after_all = [(d + timedelta(days=i)).isoformat() for i in range(1, n + 1)]
    after = [x for x in after_all if datetime.strptime(x, "%Y-%m-%d").date() <= last_full]
    before_sum = sum(series.get(x, 0) for x in before)
    after_sum = sum(series.get(x, 0) for x in after)
    before_avg = before_sum / len(before) if before else None
    after_avg = after_sum / len(after) if after else None
    delta = None
    if before_avg not in (None, 0) and after_avg is not None:
        delta = round((after_avg - before_avg) / before_avg * 100, 1)
    return {
        "n": n, "before_days": before, "after_days": after, "complete": len(after) == n,
        "before_sum": round(before_sum, 6), "after_sum": round(after_sum, 6),
        "before_avg": None if before_avg is None else round(before_avg, 6),
        "after_avg": None if after_avg is None else round(after_avg, 6),
        "delta_pct": delta,
    }


def _unavailable(reason: str) -> dict:
    return {"harness": {"available": False, "reason": reason}}


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=HARNESS_READ_TIMEOUT_SEC)
    try:
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _category(kind: str | None, origin: str | None) -> str:
    if kind == "subagent":
        return "subagent"
    return origin if origin in ("interaktiv", "orchestrator") else "unbekannt"


def _days_between(first: str, last: str) -> list[str]:
    a = datetime.strptime(first, "%Y-%m-%d").date()
    b = datetime.strptime(last, "%Y-%m-%d").date()
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def _codex_window(used: float | None, minutes: int | None, resets: int | None, now: float) -> dict | None:
    if used is None and minutes is None:
        return None
    expired = resets is not None and resets < now
    return {
        "used_pct": used, "free_pct": None if used is None else round(100.0 - used, 1),
        "window_minutes": minutes, "resets_at": resets, "expired": bool(expired),
    }


def dashboard_payload(db_path: Path | str | None = None, *, days: int = 30, window: int = 7,
                      now: datetime | None = None) -> dict:
    """Everything the "Harness" tab draws, read from the index ONLY. Never raises.

    Read-only connection (``mode=ro``, ``query_only``), busy timeout
    ``HARNESS_READ_TIMEOUT_SEC`` and a hard query budget, because the dashboard
    server is single-threaded: a missing, empty, locked, corrupt or
    other-schema database answers ``{"harness": {"available": false, ...}}``
    quickly instead of raising or waiting.
    """
    path = Path(db_path if db_path is not None else config.HARNESS_DB_FILE)
    if not path.is_file():
        return _unavailable("Index noch nicht gelaufen (keine Datei)")
    days = max(1, min(int(days), 365))
    window = window if window in MARKER_WINDOWS else 7
    now = now or datetime.now()
    deadline = time.monotonic() + HARNESS_QUERY_BUDGET_SEC
    try:
        conn = _connect_read_only(path)
    except sqlite3.Error as e:
        return _unavailable(f"Index nicht lesbar: {_err_text(e)}")
    try:
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1_000)
        return {"harness": _read_payload(conn, days=days, window=window, now=now)}
    except _NotReadyError as e:
        return _unavailable(str(e))
    except sqlite3.Error as e:
        return _unavailable(f"Index nicht lesbar: {_err_text(e)}")
    except Exception as e:
        return _unavailable(f"Index-Auswertung fehlgeschlagen: {_err_text(e)}")
    finally:
        conn.close()


class _NotReadyError(Exception):
    pass


def _read_payload(conn: sqlite3.Connection, *, days: int, window: int, now: datetime) -> dict:
    try:
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            raise _NotReadyError("Index noch nicht gelaufen (leere Datenbank)") from e
        raise
    if version is None or version[0] != str(SCHEMA_VERSION):
        raise _NotReadyError(
            f"Index-Schema {version[0] if version else '?'} passt nicht (erwartet {SCHEMA_VERSION}) — "
            "neuer Indexlauf nötig")
    run = conn.execute(
        "SELECT finished, duration_sec, lines_read, lines_skipped, status, errors, files_read, bytes_read "
        "FROM index_runs ORDER BY id DESC LIMIT 1").fetchone()
    if run is None:
        raise _NotReadyError("Index noch nicht gelaufen (kein abgeschlossener Lauf)")
    today = now.date().isoformat()
    first = (now.date() - timedelta(days=days - 1)).isoformat()
    day_list = _days_between(first, today)
    try:
        finished = datetime.fromisoformat(run[0])
        age = max(0, int((now - finished).total_seconds()))
    except (TypeError, ValueError):
        age = None
    try:
        errors = json.loads(run[5] or "{}")
    except ValueError:
        errors = {}
    payload: dict = {
        "available": True, "generated_at": now.isoformat(timespec="seconds"),
        "days": days, "window": window, "day_list": day_list,
        "last_run": {"finished": run[0], "age_sec": age, "duration_sec": run[1], "lines_read": run[2],
                     "lines_skipped": run[3], "status": run[4], "errors": errors,
                     "files_read": run[6], "bytes_read": run[7]},
    }
    payload["usage"] = _read_usage(conn, first)
    payload["agent_calls"] = _read_agent_calls(conn, first)
    payload["cost"] = _read_costs(conn, first)
    payload["quota"] = _read_codex_quota(conn, now)
    payload["extern"] = _read_extern(conn, first)
    payload["markers"] = _read_markers(conn, window=window, today=today)
    return payload


def _read_usage(conn: sqlite3.Connection, first: str) -> dict:
    by_day: dict[str, dict[str, dict[str, int]]] = {}
    totals = {c: dict.fromkeys((*_COUNTERS, "total", "messages"), 0) for c in (*CATEGORIES, "all")}
    sub_by_origin = {"interaktiv": 0, "orchestrator": 0, "unbekannt": 0}
    for day, kind, origin, inp, out, cr, cw, n in conn.execute(
        "SELECT day, kind, origin, sum(coalesce(input,0)), sum(coalesce(output,0)), "
        "sum(coalesce(cache_read,0)), sum(coalesce(cache_write,0)), count(*) "
        "FROM claude_msg WHERE day >= ? GROUP BY day, kind, origin", (first,),
    ):
        cat = _category(kind, origin)
        values = dict(zip(_COUNTERS, (inp, out, cr, cw), strict=True))
        cell = by_day.setdefault(day, {}).setdefault(cat, dict.fromkeys((*_COUNTERS, "total"), 0))
        for key, v in values.items():
            cell[key] += v
            totals[cat][key] += v
            totals["all"][key] += v
        s = sum(values.values())
        cell["total"] += s
        totals[cat]["total"] += s
        totals["all"]["total"] += s
        totals[cat]["messages"] += n
        totals["all"]["messages"] += n
        if cat == "subagent":
            sub_by_origin[origin if origin in sub_by_origin else "unbekannt"] += s
    all_total = totals["all"]["total"]
    all_io = totals["all"]["input"] + totals["all"]["output"]
    sub = totals["subagent"]
    families: dict[str, dict[str, dict[str, int]]] = {}
    for kind, origin, family, tok, out, n in conn.execute(
        "SELECT kind, origin, family, sum(coalesce(input,0)+coalesce(output,0)+coalesce(cache_read,0)"
        "+coalesce(cache_write,0)), sum(coalesce(output,0)), count(*) FROM claude_msg WHERE day >= ? "
        "GROUP BY kind, origin, family", (first,),
    ):
        for cat in (_category(kind, origin), "all"):
            cell = families.setdefault(cat, {}).setdefault(family or "andere", {"tokens": 0, "output": 0, "messages": 0})
            cell["tokens"] += tok
            cell["output"] += out
            cell["messages"] += n
    return {
        "by_day": by_day, "totals": totals, "families": families,
        "subagent_share_pct": round(sub["total"] / all_total * 100, 1) if all_total else None,
        "subagent_share_io_pct": round((sub["input"] + sub["output"]) / all_io * 100, 1) if all_io else None,
        "subagent_by_origin": sub_by_origin,
    }


def _read_agent_calls(conn: sqlite3.Connection, first: str) -> dict:
    rows = [
        {"subagent_type": t or "?", "model": m or "Standard", "calls": n}
        for t, m, n in conn.execute(
            "SELECT subagent_type, model_req, count(*) FROM claude_agent_call WHERE day >= ? "
            "GROUP BY subagent_type, model_req ORDER BY count(*) DESC, subagent_type", (first,))
    ]
    by_origin = dict(conn.execute(
        "SELECT origin, count(*) FROM claude_agent_call WHERE day >= ? GROUP BY origin", (first,)).fetchall())
    tool_use = sum(r["calls"] for r in rows)
    meta = conn.execute("SELECT count(*) FROM claude_agent_meta WHERE day >= ?", (first,)).fetchone()[0]
    return {"rows": rows, "by_origin": by_origin, "tool_use_count": tool_use, "meta_count": meta,
            "difference": tool_use - meta}


def _read_costs(conn: sqlite3.Connection, first: str) -> dict:
    claude = dict(conn.execute(
        "SELECT day, round(sum(cost_usd), 4) FROM claude_cost WHERE day >= ? GROUP BY day", (first,)).fetchall())
    claude_by_origin = dict(conn.execute(
        "SELECT origin, round(sum(cost_usd), 4) FROM claude_cost WHERE day >= ? GROUP BY origin",
        (first,)).fetchall())
    opencode = dict(conn.execute(
        "SELECT day, round(sum(cost), 6) FROM oc_message WHERE role='assistant' AND day >= ? GROUP BY day",
        (first,)).fetchall())
    return {"claude_by_day": claude, "claude_by_origin": claude_by_origin, "opencode_by_day": opencode}


def _read_codex_quota(conn: sqlite3.Connection, now: datetime) -> dict:
    row = conn.execute(
        "SELECT rl_ts, primary_used, primary_window, primary_resets, secondary_used, secondary_window, "
        "secondary_resets, plan_type FROM codex_rollout WHERE rl_ts IS NOT NULL ORDER BY rl_ts DESC LIMIT 1",
    ).fetchone()
    if row is None:
        return {"codex": None}
    epoch = now.timestamp()
    return {"codex": {
        "ts": row[0], "plan_type": row[7],
        "primary": _codex_window(row[1], row[2], row[3], epoch),
        "secondary": _codex_window(row[4], row[5], row[6], epoch),
    }}


def _read_extern(conn: sqlite3.Connection, first: str) -> dict:
    ledger_by_day: dict[str, dict[str, int]] = {}
    status_counts: dict[str, dict[str, int]] = {}
    for day, voice, status, n in conn.execute(
        "SELECT day, voice, coalesce(status, '?'), count(*) FROM ledger WHERE day >= ? "
        "GROUP BY day, voice, status", (first,),
    ):
        ledger_by_day.setdefault(day, {})
        ledger_by_day[day][voice] = ledger_by_day[day].get(voice, 0) + n
        status_counts.setdefault(voice, {})[status] = n + status_counts.get(voice, {}).get(status, 0)
    total, nulls = conn.execute(
        "SELECT count(*), sum(CASE WHEN tokens IS NULL THEN 1 ELSE 0 END) FROM ledger WHERE day >= ?",
        (first,)).fetchone()
    oc_models = [
        {"model": m or "?", "sessions": s, "messages": n, "cost": c}
        for m, s, n, c in conn.execute(
            "SELECT model_id, count(DISTINCT session_id), count(*), round(sum(coalesce(cost,0)), 6) "
            "FROM oc_message WHERE role='assistant' AND day >= ? GROUP BY model_id ORDER BY 4 DESC", (first,))
    ]
    oc_sessions: dict[str, dict[str, int]] = {}
    for day, child, n in conn.execute(
        "SELECT day, parent_id IS NOT NULL, count(*) FROM oc_session WHERE day >= ? GROUP BY 1, 2", (first,),
    ):
        oc_sessions.setdefault(day, {"top": 0, "child": 0})["child" if child else "top"] += n
    codex: dict[str, dict[str, dict[str, int]]] = {}
    for day, source, n, tok in conn.execute(
        "SELECT day, coalesce(thread_source, '?'), count(*), sum(coalesce(total, 0)) FROM codex_rollout "
        "WHERE day >= ? GROUP BY 1, 2", (first,),
    ):
        codex.setdefault(day, {})[source] = {"rollouts": n, "tokens": tok}
    return {
        "ledger_by_day": ledger_by_day, "ledger_status": status_counts,
        "ledger_total": total or 0, "ledger_tokens_null": nulls or 0,
        "opencode_by_model": oc_models, "opencode_sessions_by_day": oc_sessions, "codex_by_day": codex,
    }


def _kpi_series(conn: sqlite3.Connection, key: str, since: str) -> dict[str, float]:
    queries = {
        "ledger_calls": "SELECT day, count(*) FROM ledger WHERE day >= ? GROUP BY day",
        "ledger_failures": "SELECT day, count(*) FROM ledger WHERE day >= ? AND coalesce(status,'') != 'ok' "
                           "GROUP BY day",
        "opencode_cost": "SELECT day, sum(coalesce(cost,0)) FROM oc_message WHERE role='assistant' AND day >= ? "
                         "GROUP BY day",
        "opencode_sessions": "SELECT day, count(*) FROM oc_session WHERE day >= ? GROUP BY day",
    }
    return {d: float(v or 0) for d, v in conn.execute(queries[key], (since,))}


def _read_markers(conn: sqlite3.Connection, *, window: int, today: str) -> list[dict]:
    markers = conn.execute("SELECT date, id, scope, change, expect FROM marker ORDER BY date, id").fetchall()
    if not markers:
        return []
    since = (datetime.strptime(markers[0][0], "%Y-%m-%d").date() - timedelta(days=max(MARKER_WINDOWS))).isoformat()
    series_cache: dict[str, dict[str, float]] = {}
    out = []
    for date, mid, scope, change, expect in markers:
        rows = []
        for key, label in marker_kpis(expect):
            if key not in series_cache:
                series_cache[key] = _kpi_series(conn, key, since)
            rows.append({"kpi": key, "label": label,
                         **marker_window(series_cache[key], date, window, today)})
        out.append({"date": date, "id": mid, "scope": scope, "change": change, "expect": expect,
                    "kpis": rows})
    return out


def _lower_priority() -> None:
    """Lower our own CPU (and on Windows I/O) priority.

    POSIX: ``os.nice(10)`` from inside the child — the parent could only do it
    through ``preexec_fn``, which is not thread-safe. Windows: the parent already
    starts us with BELOW_NORMAL_PRIORITY_CLASS, which leaves the I/O priority
    normal; PROCESS_MODE_BACKGROUND_BEGIN (settable only by a process on itself)
    lowers CPU, I/O and memory priority, so a first run over gigabytes does not
    compete with a running task's disk access.
    """
    if os.name == "nt":
        with contextlib.suppress(Exception):
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00100000)  # PROCESS_MODE_BACKGROUND_BEGIN
        return
    if hasattr(os, "nice"):
        with contextlib.suppress(OSError):
            os.nice(10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harness index (SQLite) over local transcripts and logs")
    parser.add_argument("--update", action="store_true", help="run one incremental update")
    args = parser.parse_args(argv)
    if not args.update:
        parser.print_help()
        return 0
    _lower_priority()
    summary = run_update()
    return 1 if summary.get("status") == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
