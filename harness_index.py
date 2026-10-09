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
  working directory only as ``sha256(cwd)[:12]`` — no folder name, not even the
  last path segment (K9, Korrekturrunde 1). Prompts, answers, thinking, titles,
  descriptions, notes, paths: never. Source file paths are stored as a hash (the
  Claude project directory name is the encoded full cwd). Error texts in
  ``index_runs`` are the exception type only (plus ``errno`` for an OSError); the
  full message goes to stderr, which the scheduler writes to the orchestrator
  log. Exception by design: the harness-change markers' ``change`` and
  ``expect`` sentences, which the user writes in order to see them on the
  dashboard (not ``source``).
* **Idempotent.** Every row has a natural key, and re-reading anything never
  double counts:
  - Claude: ``message.id`` alone, across ALL files. One API answer is split into
    up to four transcript lines, and the same id also appears in several files
    (measured by the Auftraggeber: 101 ids, all of them in subagent transcripts
    of the same parent session; main+subagent and main+main: none). The lines of
    one id do NOT all carry the same usage (measured: in 57 % of the ids at least
    one counter differs, streaming), so every counter keeps its ``max()``; should
    a main-session file ever carry the id, it wins the attribution.
  - ``(sessionId, agentId)`` for subagent runs; one row per Codex rollout
    carrying the LAST cumulative ``token_count``; ``message.id`` for opencode
    (a message without ``time.completed`` is re-read on later runs).
  - Ledger: the line number. Each line is one call — two lines with the same
    second, voice and repo are two calls. The file is small, its whole-file hash
    is checked every run, and on any change the table is rebuilt from scratch.
  - Markers: ``(date, id)``, rebuilt the same way as the ledger.
* **Incremental.** Per file: size, mtime, offset of the end of the last complete
  line, a head fingerprint (sha256 of the first ``min(256, size)`` bytes) and a
  tail fingerprint (the 256 bytes in front of the offset). Size and mtime
  unchanged → not opened at all. Shrunk below the offset, head or tail changed
  → read from the start (natural keys absorb it). A line without its newline is
  not consumed; the offset stays in front of it until it is complete. A line
  that fails in any way is skipped and counted by exception type; a file that
  fails is reported and leaves every other file's offset alone.
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
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import config

_IMPORT_MONO = time.monotonic()  # POSIX start reference for ``index_runs.startup_sec``

SCHEMA_VERSION = 4  # 2: claude_msg keyed by message.id alone, no folder names, ledger by line;
                    # 3: index_runs.peak_private_mb (Windows) next to peak_rss_mb (POSIX);
                    # 4: index_runs.startup_sec (no migration: the schema is not shipped yet)
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
    session_id TEXT PRIMARY KEY, entrypoint TEXT, origin TEXT, cwd_hash TEXT, file_key TEXT);
CREATE TABLE IF NOT EXISTS claude_msg (
    msg_id TEXT PRIMARY KEY, file_key TEXT, session_id TEXT,
    kind TEXT, agent_id TEXT, origin TEXT, model TEXT, family TEXT,
    ts TEXT, day TEXT,
    input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER,
    cwd_hash TEXT);
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
    model TEXT, cwd_hash TEXT, first_ts TEXT, day TEXT,
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
    t_cache_read INTEGER, t_cache_write INTEGER, finish TEXT, completed INTEGER);
CREATE TABLE IF NOT EXISTS ledger (
    line_no INTEGER PRIMARY KEY, ts_local TEXT, voice TEXT, repo_hash TEXT,
    status TEXT, blocked_until TEXT, tokens INTEGER, day TEXT);
CREATE TABLE IF NOT EXISTS marker (
    date TEXT NOT NULL, id TEXT NOT NULL, scope TEXT, change TEXT, expect TEXT,
    PRIMARY KEY (date, id));
CREATE TABLE IF NOT EXISTS index_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT,
    duration_sec REAL, files_read INTEGER, bytes_read INTEGER, lines_read INTEGER,
    lines_skipped INTEGER, errors TEXT, per_source TEXT,
    peak_private_mb REAL, peak_rss_mb REAL, status TEXT, startup_sec REAL);
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
    """Error text for the INDEX: type name, plus errno for an OSError — never the
    message, which can carry a path (OSError, Path.relative_to, …). The full text
    goes to stderr only (``_note_error``), which the scheduler logs locally."""
    if isinstance(exc, OSError):
        return f"{type(exc).__name__} errno={exc.errno}"
    return type(exc).__name__


def _note_error(stats: SourceStats, where: str, exc: BaseException) -> None:
    """Record an error: sanitized into the run log, in full on stderr."""
    stats.error(f"{where}: {_err_text(exc)}")
    with contextlib.suppress(Exception):
        print(f"harness_index {where}: {type(exc).__name__}: {exc}", file=sys.stderr)


_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


class _OutOfRangeError(ValueError):
    """A number SQLite cannot store as INTEGER — the line is skipped, not truncated."""


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = int(value)
    if isinstance(value, int):
        if not _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX:
            raise _OutOfRangeError("integer outside the SQLite range")
        return value
    return None


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _cwd_hash(cwd: object) -> str | None:
    """The working directory only as a hash — the folder name itself is not stored
    (it is never read, and folder names can name customers)."""
    if not isinstance(cwd, str) or not cwd:
        return None
    return _hash(cwd)


_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)


def _local_day_from_iso(ts: object) -> str | None:
    """Local calendar day of an ISO timestamp (``Z``/offset → converted; naive = local).

    None for anything unusable — unparseable, or before 1970 (Windows'
    ``astimezone()`` raises OSError there, so the rule is the same everywhere).
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            if dt < _EPOCH_UTC:
                return None
            dt = dt.astimezone()
        elif dt.year < 1970:
            return None
        return dt.date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


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


# The run log's memory figure, by platform: one column per meaning, never one
# column with two meanings.
PEAK_LABELS = {"peak_private_mb": "Spitze privater Speicher", "peak_rss_mb": "Spitze RSS"}


def _peak_memory_mb() -> tuple[str, float | None]:
    """``(column, MB)`` of this process's memory peak, best effort (MB None if unknown).

    Windows: peak PRIVATE memory (``peak_private_mb``, see ``_peak_private_mb``).
    POSIX: peak resident set (``peak_rss_mb``, ``getrusage``).
    """
    if os.name == "nt":
        try:
            return "peak_private_mb", _peak_private_mb_windows()
        except Exception:
            return "peak_private_mb", None
    try:
        resource = importlib.import_module("resource")  # POSIX only
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB, macOS bytes.
        return "peak_rss_mb", round(float(peak) / (1_048_576 if sys.platform == "darwin" else 1024), 1)
    except Exception:
        return "peak_rss_mb", None


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    )


def _kernel32() -> Any:
    """kernel32 with explicit argtypes/restype — Windows only.

    Without them ctypes passes and returns plain C ints: the pseudo-handle of
    ``GetCurrentProcess()`` (−1) and real 64-bit handles got truncated, so
    ``SetPriorityClass`` failed with ERROR_INVALID_PARAMETER (87) and
    ``GetProcessMemoryInfo`` with ERROR_INVALID_HANDLE (6) — measured twice
    on Windows by the Auftraggeber, hidden by a blanket ``suppress``. ``use_last_error`` makes
    ``ctypes.get_last_error()`` reliable. The ``sys.platform`` check keeps mypy
    on Linux from type-checking Windows-only names (no ``type: ignore`` that
    would turn into ``unused-ignore`` on Windows).
    """
    if sys.platform == "win32":
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.argtypes = ()
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k.SetPriorityClass.restype = wintypes.BOOL
        k.GetPriorityClass.argtypes = (wintypes.HANDLE,)
        k.GetPriorityClass.restype = wintypes.DWORD
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.CloseHandle.restype = wintypes.BOOL
        k.K32GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wintypes.DWORD)
        k.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        return k
    raise OSError("kernel32 is Windows-only")


class _FileTime(ctypes.Structure):
    _fields_ = (("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD))


_FILETIME_TO_EPOCH_SEC = 11_644_473_600  # 1601-01-01 -> 1970-01-01


def _startup_sec(now: float | None = None) -> float | None:
    """Seconds from process creation to ``now``: what ran BEFORE ``started``
    (interpreter start, imports, a cold cache). Windows: ``GetProcessTimes``
    creation time; POSIX: ``time.monotonic()`` taken at module import (the
    interpreter start itself is not included). ``None`` when it cannot be read."""
    now = time.time() if now is None else now
    if sys.platform != "win32":
        return round(max(0.0, time.monotonic() - _IMPORT_MONO), 3)
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(_FileTime),) * 4)
        k.GetProcessTimes.restype = wintypes.BOOL
        created, exited, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
        if not k.GetProcessTimes(k.GetCurrentProcess(), *(ctypes.byref(t) for t in (created, exited, kernel, user))):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return float(round(max(0.0, now - (ticks / 10_000_000 - _FILETIME_TO_EPOCH_SEC)), 3))
    except Exception:
        return None


def _last_error() -> int:
    if sys.platform == "win32":
        return int(ctypes.get_last_error())
    return 0


def _peak_private_mb(counters: _ProcessMemoryCounters) -> float:
    """Peak private memory in MB: ``PeakPagefileUsage``, the peak commit charge.

    Not ``PeakWorkingSetSize``: background mode (``_lower_priority``) caps the
    working set at about 32 MB, so that figure read 32.0 MB whatever the run
    used. Measured on Windows (Korrekturrunde 2): a probe allocating 200 MB
    read peak working set 32.0 MB and peak private 318.3 MB; the real first run
    read 32.0 MB, where 75 MB had been measured from outside on the earlier head."""
    return round(float(counters.PeakPagefileUsage) / 1_048_576, 1)


def _peak_private_mb_windows() -> float | None:
    """Peak private memory of this process via K32GetProcessMemoryInfo, or None
    (with one line on stderr naming GetLastError)."""
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    k = _kernel32()
    if k.K32GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return _peak_private_mb(counters)
    with contextlib.suppress(Exception):
        print(f"harness_index: GetProcessMemoryInfo failed, GetLastError={_last_error()}", file=sys.stderr)
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
    skipped_by_type: dict[str, int] = field(default_factory=dict)
    # Ledger and markers only: broken lines of the file as it is NOW (a stock,
    # replaced on every rebuild — K18), never added to lines_skipped, whose sum
    # over all runs would otherwise count the same broken line on every rebuild.
    broken_lines: int | None = None
    broken_by_type: dict[str, int] = field(default_factory=dict)

    def error(self, message: str) -> None:
        if len(self.errors) < 20:
            self.errors.append(message[:300])

    def skip(self, exc: BaseException) -> None:
        """A line that raised: counted as skipped, by exception TYPE (no text)."""
        self.lines_skipped += 1
        name = type(exc).__name__
        self.skipped_by_type[name] = self.skipped_by_type.get(name, 0) + 1

    def as_dict(self) -> dict:
        return {
            "files_seen": self.files_seen, "files_read": self.files_read,
            "files_skipped_window": self.files_skipped_window,
            "bytes_read": self.bytes_read, "lines_read": self.lines_read,
            "lines_skipped": self.lines_skipped, "skipped_by_type": dict(self.skipped_by_type),
            "broken_lines": self.broken_lines, "broken_by_type": dict(self.broken_by_type),
            "errors": list(self.errors),
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
        k = _kernel32()
        handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return _last_error() == 5  # ERROR_ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(handle)
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
        raise SchemaMismatchError(
            f"schema_version {row[0]} in {db_path}, expected {SCHEMA_VERSION} — delete the file, "
            "the next run rebuilds it (rows of transcripts deleted since are then gone)")
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
    A trailing segment without newline waits (the offset stays in front of it)
    while the file is younger than ``TAIL_QUIET_SEC``. Once the file is quiet the
    segment is settled once: yielded if it parses as JSON, else counted as
    skipped, and the offset moves to the end of the file either way.
    """
    st = path.stat()
    size, mtime = st.st_size, st.st_mtime
    with path.open("rb") as f:
        reset = False
        offset = 0
        if prev is not None:
            same_head = size >= prev.head_len and _head_hash(f, prev.head_len) == prev.head_hash
            if same_head and size == prev.size and mtime == prev.mtime and prev.offset >= prev.size:
                return  # unchanged and nothing left behind the offset: not read at all
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
                # incomplete last line: while the file is young the offset stays in
                # front of it (the writer may be mid-line)
                stats.bytes_read += len(raw)
                if now - mtime >= TAIL_QUIET_SEC:
                    # quiet file: the rest will not grow into a line any more. Either
                    # way the offset moves to the end — a rest that is valid JSON is
                    # evaluated once, one that is not is counted as skipped once; the
                    # file is not reopened for it on every run.
                    new_state.offset += len(raw)
                    try:
                        json.loads(raw)
                    except Exception:  # not ValueError only: deep nesting raises RecursionError
                        if raw.strip():
                            stats.lines_skipped += 1
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
    """True when size and mtime are where the last run left them — the file is
    then not even opened (≈ 3000 files per run; every open is a virus-scanner
    hit on Windows). A rewrite that keeps both size and mtime is not detected.

    Not while a rest sits behind the offset (``offset < size``, an unfinished last
    line): it ages without the file changing, so it is looked at again until it
    is quiet and settled (see ``_iter_new_lines``)."""
    if prev is None or prev.offset < prev.size:
        return False
    st = path.stat()
    return st.st_size == prev.size and st.st_mtime == prev.mtime


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


def _undo_line(conn: sqlite3.Connection, info: dict, before: dict) -> None:
    """Undo a line whose handler raised: its rows back to ``SAVEPOINT line``,
    the file's in-memory ``info`` back to its copy from before the line."""
    with contextlib.suppress(sqlite3.Error):
        conn.execute("ROLLBACK TO line")
        conn.execute("RELEASE line")
    info.clear()
    info.update(before)


def _run_file(
    conn: sqlite3.Connection,
    source: str,
    file_key: str,
    path: Path,
    stats: SourceStats,
    *,
    handle: Callable[[dict, FileState, bool], None],
    needles: Callable[[FileState], tuple[bytes, ...]],
    on_begin: Callable[[], None] | None = None,
    on_reset: Callable[[], None] | None = None,
    on_done: Callable[[FileState], None] | None = None,
) -> None:
    """Read the new lines of one file inside ONE transaction (rows + state).

    The state read before the transaction only decides whether the file needs
    opening at all (a stale "unchanged" is harmless: the next run looks again).
    What the run builds on — the file's state and, via ``on_begin``, whatever
    the handler carries over from the database — is read after ``BEGIN
    IMMEDIATE``: a second run (after a lock takeover) that committed in between
    would otherwise be overwritten by this one with older numbers."""
    if _unchanged(path, _load_state(conn, source, file_key)):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        prev = _load_state(conn, source, file_key)
        if on_begin is not None:
            on_begin()
        state: FileState | None = None
        now = time.time()
        for raw, state, reset in _iter_new_lines(path, prev, stats, now):
            if reset and on_reset is not None:
                on_reset()
            if not raw:
                continue
            try:
                # inside the try: json.loads raises RecursionError (not a
                # ValueError) on a deeply nested line — that line is skipped,
                # not the file rolled back on every run
                obj = _parse_line(raw, needles(state), stats)
                if obj is None:
                    continue
                # One line = one unit: a line that raises leaves no partial
                # write behind (e.g. the agent call of a line whose usage then
                # held a number out of range), neither in the rows nor in `info`.
                before = dict(state.info)
                conn.execute("SAVEPOINT line")
                try:
                    handle(obj, state, reset)
                except BaseException:
                    _undo_line(conn, state.info, before)
                    raise
                conn.execute("RELEASE line")
            except Exception as e:
                # Database-level failures end this file (rolled back, retried
                # next run); anything else is a property of THIS line: skipped
                # and counted by type, the file goes on.
                if isinstance(e, sqlite3.OperationalError) or type(e) is sqlite3.DatabaseError:
                    raise
                stats.skip(e)
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
        _note_error(stats, "stat", e)
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
    """Run one file's work; ANY failure is recorded and the run goes on with the
    next file. Every file has its own transaction, so a failing file never moves
    (or resets) another file's offset."""
    try:
        work(*args)
    except Exception as e:
        _note_error(stats, f"file #{_hash(str(path), 8)}", e)


def _claude_assistant(
    conn: sqlite3.Connection, obj: dict, stats: SourceStats, *, file_key: str, kind: str,
    agent_id: str | None, session_id: str | None, origin: str, cwd_hash: str | None,
) -> None:
    msg = obj.get("message")
    if not isinstance(msg, dict):
        stats.lines_skipped += 1
        return
    ts = _str(obj.get("timestamp"))
    day = _local_day_from_iso(ts)
    if day is None:
        # no usable timestamp (missing, unparseable, before 1970): the line cannot
        # be placed on a day, so it is skipped instead of counted invisibly
        stats.skip(ValueError("timestamp"))
        return
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
    counters = (_int(usage.get("input_tokens")), _int(usage.get("output_tokens")),
                _int(usage.get("cache_read_input_tokens")), _int(usage.get("cache_creation_input_tokens")))
    # One row per message.id across ALL files: the API issues it once, but it
    # recurs in up to four lines of one transcript (one answer split per content
    # block) and in several files (measured by the Auftraggeber: subagent
    # transcripts of the same parent session, never a main file). Counters: max()
    # per field — the lines of one id do NOT carry identical usage (measured on
    # real data: 57 % of ids differ, streaming), the
    # last/largest value is the answer's. Never a sum. The attribution (file,
    # kind, origin, …) is upgraded once, from a subagent row to a main-file row;
    # otherwise the first file read keeps it (files are read in sorted order).
    conn.execute(
        "INSERT INTO claude_msg(msg_id, file_key, session_id, kind, agent_id, origin, model, family, "
        "ts, day, input, output, cache_read, cache_write, cwd_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(msg_id) DO UPDATE SET "
        "input=max(coalesce(input, 0), coalesce(excluded.input, 0)), "
        "output=max(coalesce(output, 0), coalesce(excluded.output, 0)), "
        "cache_read=max(coalesce(cache_read, 0), coalesce(excluded.cache_read, 0)), "
        "cache_write=max(coalesce(cache_write, 0), coalesce(excluded.cache_write, 0)), "
        "file_key=CASE WHEN kind!='main' AND excluded.kind='main' THEN excluded.file_key ELSE file_key END, "
        "session_id=CASE WHEN kind!='main' AND excluded.kind='main' THEN excluded.session_id ELSE session_id END, "
        "agent_id=CASE WHEN kind!='main' AND excluded.kind='main' THEN excluded.agent_id ELSE agent_id END, "
        "origin=CASE WHEN kind!='main' AND excluded.kind='main' THEN excluded.origin ELSE origin END, "
        "cwd_hash=CASE WHEN kind!='main' AND excluded.kind='main' THEN excluded.cwd_hash ELSE cwd_hash END, "
        "kind=CASE WHEN kind!='main' AND excluded.kind='main' THEN excluded.kind ELSE kind END",
        (msg_id[:NAME_MAX], file_key, session_id, kind, agent_id, origin, _name(model), family,
         ts, day, *counters, cwd_hash),
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
            info["cwd_hash"] = _cwd_hash(obj.get("cwd"))
        session_id = _str(obj.get("sessionId")) or stem_session
        origin = origin_of(info.get("entrypoint"))
        kind = obj.get("type")
        if kind == "assistant":
            _claude_assistant(conn, obj, stats, file_key=file_key, kind="main", agent_id=None,
                              session_id=session_id[:NAME_MAX], origin=origin,
                              cwd_hash=info.get("cwd_hash"))
        elif kind == "cost-state":
            cost = _float(obj.get("totalCostUSD"))
            start_ms = _int(obj.get("startTime"))
            # cumulative per session: the LARGEST value counts, not the last one
            # read (files are read in sorted order, which says nothing about time)
            conn.execute(
                "INSERT INTO claude_cost(session_id, file_key, cost_usd, start_ms, day, origin) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                "file_key=CASE WHEN coalesce(excluded.cost_usd,0) >= coalesce(cost_usd,0) "
                "THEN excluded.file_key ELSE file_key END, "
                "start_ms=CASE WHEN coalesce(excluded.cost_usd,0) >= coalesce(cost_usd,0) "
                "THEN excluded.start_ms ELSE start_ms END, "
                "day=CASE WHEN coalesce(excluded.cost_usd,0) >= coalesce(cost_usd,0) "
                "THEN excluded.day ELSE day END, "
                "origin=CASE WHEN coalesce(excluded.cost_usd,0) >= coalesce(cost_usd,0) "
                "THEN excluded.origin ELSE origin END, "
                "cost_usd=max(coalesce(cost_usd,0), coalesce(excluded.cost_usd,0))",
                (session_id[:NAME_MAX], file_key, cost, start_ms, _local_day_from_ms(start_ms), origin),
            )

    def on_done(state: FileState) -> None:
        info = state.info
        conn.execute(
            "INSERT OR REPLACE INTO claude_session(session_id, entrypoint, origin, cwd_hash, file_key) "
            "VALUES (?,?,?,?,?)",
            (stem_session[:NAME_MAX], info.get("entrypoint"), origin_of(info.get("entrypoint")),
             info.get("cwd_hash"), file_key),
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
            info["cwd_hash"] = _cwd_hash(obj.get("cwd"))
        if obj.get("type") != "assistant":
            return
        # The subagent's share stays "subagent", attributed to the PARENT's
        # entrypoint; the file's own field only when the parent is unknown.
        origin = parent or origin_of(obj.get("entrypoint") or info.get("entrypoint"))
        _claude_assistant(conn, obj, stats, file_key=file_key, kind="subagent", agent_id=agent_id,
                          session_id=parent_session, origin=origin, cwd_hash=info.get("cwd_hash"))

    _run_file(conn, "claude", file_key, path, stats, handle=handle, needles=needles)


_UNPARSABLE = object()


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
    except Exception as e:  # not ValueError only: deep nesting raises RecursionError
        stats.skip(e)  # counted here, so the not-an-object branch below must not count it twice
        data = _UNPARSABLE
    session_id = path.parent.parent.name[:NAME_MAX]
    agent_id = path.name.removeprefix("agent-").removesuffix(".meta.json")[:NAME_MAX]
    conn.execute("BEGIN IMMEDIATE")
    try:
        if isinstance(data, dict):
            try:
                values = (session_id, agent_id, file_key, _name(data.get("toolUseId")),
                          _name(data.get("agentType")), _name(data.get("model")) or "Standard",
                          _int(data.get("spawnDepth")), _name(data.get("requestShape")),
                          1 if data.get("parentAgentId") else 0,
                          datetime.fromtimestamp(st.st_mtime).date().isoformat(),
                          _parent_origin(conn, session_id) or ORIGIN_UNKNOWN)
            except Exception as e:  # a bad field skips this file's row, not the run
                stats.skip(e)
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO claude_agent_meta(session_id, agent_id, file_key, tool_use_id, "
                    "agent_type, model, spawn_depth, request_shape, nested, day, origin) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", values,
                )
        elif data is not _UNPARSABLE:  # valid JSON (``null`` too), but not an object
            stats.lines_skipped += 1
        _save_state(conn, "claude-meta", file_key, FileState(st.st_size, st.st_mtime, st.st_size))
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


# ── source 2: Codex rollouts ────────────────────────────────────────────────

_CODEX_FIELDS = (
    "session_id", "thread_source", "originator", "model", "cwd_hash", "first_ts", "day",
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
    row: dict = {}

    def on_begin() -> None:
        # inside the transaction, with the file state: both come from the same commit
        existing = conn.execute(
            f"SELECT {', '.join(_CODEX_FIELDS)} FROM codex_rollout WHERE file_key=?", (file_key,),
        ).fetchone()
        row.clear()
        if existing:
            row.update(zip(_CODEX_FIELDS, existing, strict=True))

    def on_reset() -> None:
        row.clear()  # the file's content is the truth: start the one row afresh

    def needles(_state: FileState) -> tuple[bytes, ...]:
        return (b'"type":"session_meta"', b'"type":"turn_context"', b'"type":"token_count"')

    def handle(obj: dict, _state: FileState, _reset: bool) -> None:
        # The line's values go into `upd` and reach `row` only once the whole
        # line succeeded: a line that raises halfway (a number out of range)
        # must not leave the row mixed from two token_count lines.
        upd: dict = {}
        kind = obj.get("type")
        payload = _dict(obj.get("payload"))
        ts = _str(obj.get("timestamp"))
        if kind == "session_meta":
            upd["session_id"] = _name(payload.get("id"))
            upd["thread_source"] = _name(payload.get("thread_source"))
            upd["originator"] = _name(payload.get("originator"))
            upd["cwd_hash"] = _cwd_hash(payload.get("cwd"))
            first = _str(payload.get("timestamp")) or ts
            upd["first_ts"] = first
            upd["day"] = _local_day_from_iso(first)
        elif kind == "turn_context":
            if payload.get("model") is not None:
                upd["model"] = _name(payload.get("model"))
        elif kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                # LAST cumulative value wins — never a sum over events.
                total = info["total_token_usage"]
                upd["tc_ts"] = ts
                upd["input"] = _int(total.get("input_tokens"))
                upd["cached_input"] = _int(total.get("cached_input_tokens"))
                upd["cache_write"] = _int(total.get("cache_write_input_tokens"))
                upd["output"] = _int(total.get("output_tokens"))
                upd["reasoning"] = _int(total.get("reasoning_output_tokens"))
                upd["total"] = _int(total.get("total_tokens"))
            limits = payload.get("rate_limits")
            if isinstance(limits, dict):
                upd["rl_ts"] = ts
                for key in ("primary", "secondary"):
                    win = _dict(limits.get(key))
                    upd[f"{key}_used"] = _float(win.get("used_percent"))
                    upd[f"{key}_window"] = _int(win.get("window_minutes"))
                    upd[f"{key}_resets"] = _int(win.get("resets_at"))
                upd["plan_type"] = _name(limits.get("plan_type"))
        row.update(upd)

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
              on_begin=on_begin, on_reset=on_reset, on_done=on_done)


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
            sid, updated = row[0], row[5]
            if isinstance(updated, int) and (newest is None or updated > newest):
                newest = updated
            known = conn.execute("SELECT time_updated FROM oc_session WHERE id=?", (sid,)).fetchone()
            if known is not None and known[0] == updated:
                continue
            stats.lines_read += 1
            try:
                _opencode_session_row(conn, row, stats)
            except Exception as e:  # one bad row is skipped, never the whole source
                if isinstance(e, sqlite3.OperationalError) or type(e) is sqlite3.DatabaseError:
                    raise
                stats.skip(e)
        if newest is not None:
            _meta_set(conn, "oc_session_watermark", str(newest))
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def _opencode_session_row(conn: sqlite3.Connection, row: tuple, stats: SourceStats) -> None:
    sid, parent, agent, model_raw, created, updated = row[:6]
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


def _opencode_message_values(mid: object, sid: object, created: object, updated: object,
                             data: object) -> tuple | None:
    """The oc_message row for one message, or None when ``data`` is not a JSON object."""
    try:
        obj = json.loads(data) if isinstance(data, (str, bytes)) else None
    except ValueError:
        obj = None
    if not isinstance(obj, dict):
        return None
    tokens = _dict(obj.get("tokens"))
    cache = _dict(tokens.get("cache"))
    times = _dict(obj.get("time"))
    created_ms = _int(times.get("created")) or _int(created)
    return (_name(mid), _name(sid), _name(obj.get("role")), _name(obj.get("agent") or obj.get("mode")),
            _name(obj.get("modelID")), _name(obj.get("providerID")), created_ms, _int(updated),
            _local_day_from_ms(created_ms), _float(obj.get("cost")),
            _int(tokens.get("input")), _int(tokens.get("output")), _int(tokens.get("reasoning")),
            _int(cache.get("read")), _int(cache.get("write")), _name(obj.get("finish")),
            1 if times.get("completed") is not None else 0)


_OC_MESSAGE_COLUMNS = (
    "id, session_id, role, agent, model_id, provider_id, created_ms, time_updated, day, cost, "
    "t_input, t_output, t_reasoning, t_cache_read, t_cache_write, finish, completed"
)
# An assistant message without time.completed is re-read on every run for this
# long after it was created, whatever its time_updated says: cost and tokens
# are written when the answer completes, and it is not verified that opencode
# bumps time_updated then. Aborted messages never complete — hence the bound.
OPENCODE_INCOMPLETE_RECHECK_MS = 2 * 24 * 3600 * 1000


def _store_opencode_message(conn: sqlite3.Connection, row: tuple, stats: SourceStats,
                            *, only_if_changed: bool) -> None:
    mid, sid, created, updated, data = row
    try:
        values = _opencode_message_values(mid, sid, created, updated, data)
    except Exception as e:  # one bad row is skipped, never the whole source
        if not only_if_changed:
            stats.lines_read += 1
        stats.skip(e)
        return
    if only_if_changed:
        stored = conn.execute(f"SELECT {_OC_MESSAGE_COLUMNS} FROM oc_message WHERE id=?", (mid,)).fetchone()
        if stored is not None and values is not None and tuple(stored) == values:
            return  # re-checked, nothing new
    stats.lines_read += 1
    stats.bytes_read += len(data) if isinstance(data, (str, bytes)) else 0
    if values is None:
        # remembered as seen (role NULL), so it is not re-read every run
        stats.lines_skipped += 1
        conn.execute("INSERT OR REPLACE INTO oc_message(id, session_id, time_updated) VALUES (?,?,?)",
                     (_name(mid), _name(sid), _int(updated) if isinstance(updated, int) else None))
        return
    conn.execute(
        f"INSERT OR REPLACE INTO oc_message({_OC_MESSAGE_COLUMNS}) "
        f"VALUES ({', '.join('?' for _ in range(17))})", values,
    )


def _opencode_messages(conn: sqlite3.Connection, src: sqlite3.Connection, floor_ms: int, stats: SourceStats) -> None:
    since = _watermark(conn, "oc_message_watermark", floor_ms)
    recheck_floor = int(time.time() * 1000) - OPENCODE_INCOMPLETE_RECHECK_MS
    incomplete = {r[0] for r in conn.execute(
        "SELECT id FROM oc_message WHERE role='assistant' AND completed=0 AND created_ms >= ?",
        (recheck_floor,))}
    cur = src.execute(
        "SELECT id, session_id, time_created, time_updated, data FROM message "
        "WHERE time_updated >= ? ORDER BY time_updated", (since,),
    )
    newest = None
    seen: set[str] = set()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in cur:
            mid, updated = row[0], row[3]
            seen.add(mid)
            if isinstance(updated, int) and (newest is None or updated > newest):
                newest = updated
            known = conn.execute("SELECT time_updated FROM oc_message WHERE id=?", (mid,)).fetchone()
            if known is not None and known[0] == updated and mid not in incomplete:
                continue
            _store_opencode_message(conn, row, stats, only_if_changed=known is not None and known[0] == updated)
        recheck = sorted(incomplete - seen)
        for start in range(0, len(recheck), 500):
            chunk = recheck[start:start + 500]
            for row in src.execute(
                "SELECT id, session_id, time_created, time_updated, data FROM message "
                f"WHERE id IN ({', '.join('?' for _ in chunk)})", chunk,
            ):
                _store_opencode_message(conn, row, stats, only_if_changed=True)
        if newest is not None:
            _meta_set(conn, "oc_message_watermark", str(newest))
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


# ── sources 4 + 5: ledger and markers ───────────────────────────────────────


def _read_small_file(conn: sqlite3.Connection, source: str, path: Path,
                     stats: SourceStats) -> tuple[bytes, os.stat_result] | None:
    """Content of a small, user-written file if it changed since the last run,
    else None. Decided by the hash of the WHOLE file, read every run (two files
    of a few KB) — not by size/mtime, so an edit of the same length with an
    unchanged mtime is seen too. Such files are re-read and their table rebuilt
    as a whole: a line edited in the middle neither survives as a ghost nor goes
    unnoticed."""
    st = path.stat()
    prev = _load_state(conn, source, source)
    raw = path.read_bytes()
    if prev is not None and prev.head_hash == hashlib.sha256(raw).hexdigest():
        return None
    stats.files_read += 1
    stats.bytes_read += len(raw)
    return raw, st


def _broken_stock(stats: SourceStats, broken: dict[str, int]) -> dict:
    """The broken lines of a rebuilt small file as a STOCK: into this run's
    stats (``broken_lines``, not ``lines_skipped``) and, returned, into the
    file's state ``info`` — where the next rebuild overwrites it."""
    stats.broken_lines = sum(broken.values())
    stats.broken_by_type = dict(broken)
    return {"broken": stats.broken_lines, "broken_by_type": dict(broken)}


def _index_ledger(conn: sqlite3.Connection, path: Path, stats: SourceStats) -> None:
    """The extern-voice ledger: a few hundred lines, appended by a script. Each
    LINE is one call — two lines with the same second, voice and repo are two
    calls (measured: 4 such pairs, 2 of them Codex calls with different tokens),
    so the key is the line number and the table is rebuilt whenever the file's
    hash changes. A deleted ledger keeps its rows."""
    if not path.is_file():
        return
    stats.files_seen += 1
    changed = _read_small_file(conn, "ledger", path, stats)
    if changed is None:
        return
    raw, st = changed
    broken: dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM ledger")
        for line_no, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            stats.lines_read += 1
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError("not an object")
                ts = obj.get("ts_local")
                voice = _name(obj.get("voice"))
                if not isinstance(ts, str) or not voice:
                    raise ValueError("ts_local/voice missing")
                values = (line_no, ts[:40], voice, _hash(_str(obj.get("repo")) or ""),
                          _name(obj.get("status")), _name(obj.get("blocked_until")),
                          _int(obj.get("tokens")), _local_day_from_iso(ts))
            except Exception as e:
                broken[type(e).__name__] = broken.get(type(e).__name__, 0) + 1
                continue
            conn.execute(
                "INSERT INTO ledger(line_no, ts_local, voice, repo_hash, status, blocked_until, tokens, day) "
                "VALUES (?,?,?,?,?,?,?,?)", values,
            )
        _save_state(conn, "ledger", "ledger",
                    FileState(st.st_size, st.st_mtime, st.st_size, 0, hashlib.sha256(raw).hexdigest(),
                              _broken_stock(stats, broken)))
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def _index_markers(conn: sqlite3.Connection, path: Path, stats: SourceStats) -> None:
    """Markers are small and user-edited: a changed file replaces the table
    (a corrected date must not leave a ghost line). A deleted file keeps it."""
    if not path.is_file():
        return
    stats.files_seen += 1
    changed = _read_small_file(conn, "markers", path, stats)
    if changed is None:
        return
    raw, st = changed
    head = hashlib.sha256(raw).hexdigest()
    broken: dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM marker")
        for line in raw.splitlines():
            if not line.strip():
                continue
            stats.lines_read += 1
            try:  # not ValueError only: deep nesting raises RecursionError
                obj = json.loads(line)
                if not isinstance(obj, dict) or not isinstance(obj.get("date"), str) or not obj.get("id"):
                    raise ValueError("date/id missing")
                values = (datetime.strptime(obj["date"][:10], "%Y-%m-%d").date().isoformat(),
                          _name(obj.get("id")), _name(obj.get("scope")),
                          str(obj.get("change") or "")[:300], str(obj.get("expect") or "")[:300])
            except Exception as e:
                broken[type(e).__name__] = broken.get(type(e).__name__, 0) + 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO marker(date, id, scope, change, expect) VALUES (?,?,?,?,?)", values,
            )
        _save_state(conn, "markers", "markers",
                    FileState(st.st_size, st.st_mtime, st.st_size, 0, head, _broken_stock(stats, broken)))
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
                totals: dict, errors: dict, per_source: dict, peak: dict, status: str,
                startup_sec: float | None = None) -> None:
    conn.execute(
        "INSERT INTO index_runs(started, finished, duration_sec, files_read, bytes_read, lines_read, "
        "lines_skipped, errors, per_source, peak_private_mb, peak_rss_mb, status, startup_sec) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.fromtimestamp(started).isoformat(timespec="seconds"),
         datetime.fromtimestamp(finished).isoformat(timespec="seconds"),
         round(finished - started, 3), totals["files_read"], totals["bytes_read"],
         totals["lines_read"], totals["lines_skipped"], json.dumps(errors),
         json.dumps(per_source), peak.get("peak_private_mb"), peak.get("peak_rss_mb"), status, startup_sec),
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
            _note_error(per_source[name], name, e)
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
        startup_sec = _startup_sec(started)
        try:
            conn = open_index(sources.db)
        except (sqlite3.Error, SchemaMismatchError, OSError) as e:
            # stdout/stderr only (the index itself is not usable): full text
            say(f"Harness-Index nicht nutzbar: {type(e).__name__}: {e}")
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}
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
            peak_column, peak_mb = _peak_memory_mb()
            peak = {"peak_private_mb": None, "peak_rss_mb": None, peak_column: peak_mb}
            detail = {k: v.as_dict() for k, v in per_source.items()}
            try:
                _record_run(conn, started=started, finished=finished, totals=totals, errors=errors,
                            per_source=detail, peak=peak, status=status, startup_sec=startup_sec)
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error as e:  # the data is committed; only the run log is missing
                say(f"index_runs nicht geschrieben: {_err_text(e)}")
        finally:
            conn.close()
        summary = {
            "status": status, "duration_sec": round(finished - started, 3), **totals,
            "errors": errors, **peak, "startup_sec": startup_sec, "per_source": detail,
        }
        say(
            f"Harness-Index {status}: {totals['files_read']} Dateien, {totals['lines_read']} Zeilen, "
            f"{totals['bytes_read'] / 1_048_576:.1f} MB gelesen, {totals['lines_skipped']} übersprungen, "
            f"{summary['duration_sec']:.1f} s (Start davor {startup_sec if startup_sec is not None else '?'} s), {PEAK_LABELS[peak_column]} "
            f"{peak_mb if peak_mb is not None else '?'} MB"
        )
        for name in ("ledger", "markers"):
            broken = per_source[name].broken_lines
            if broken:
                say(f"  {'Ledger' if name == 'ledger' else 'Marker'}: {broken} kaputte Zeilen")
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
        # full error texts (paths included) — for the local orchestrator log only
        "stderr": err.decode("utf-8", "replace").strip()[-2000:],
    }
    if proc.returncode != 0:
        result["error"] = f"Exit {proc.returncode}: {err.decode('utf-8', 'replace').strip()[-300:]}"
    return result


# ── read side: the dashboard's /api/harness ────────────────────────────────

# Harness-change marker → KPI. `expect` is free text, so the mapping is a FIXED
# keyword table, visible here. Each entry lists alternatives; an alternative
# matches when ALL its words occur in `expect` (case-insensitive). A marker
# without any match gets only its vertical line, never a guessed metric. All
# series are per DAY — the index has no loop/pass identifier (excluded phase 2).
MARKER_KPIS: tuple[tuple[tuple[tuple[str, ...], ...], str, str], ...] = (
    ((("externe aufrufe",),), "ledger_calls", "Externe Aufrufe je Tag (Ledger)"),
    ((("fehlversuche",),), "ledger_failures", "Externe Fehlversuche je Tag (Ledger, Status ≠ ok)"),
    ((("opencode-kosten",), ("kosten je pass", "opencode")), "opencode_cost",
     "opencode-Kosten je Tag (Katalogpreis, USD)"),
    ((("sessions je pass",),), "opencode_sessions", "opencode-Sitzungen je Tag (ohne Unter-Sitzungen)"),
)
# Which source a KPI's series comes from — its first day with data bounds the
# "before" window (days before it are MISSING, not 0).
_KPI_SOURCE = {
    "ledger_calls": "ledger", "ledger_failures": "ledger",
    "opencode_cost": "opencode", "opencode_sessions": "opencode",
}
MARKER_WINDOWS = (3, 7, 14)
# sqlite busy timeout of the read connection. 0.2 s, not 0.5: on Windows SQLite
# sleeps in timer-granular steps and a 0.5 s timeout measured 1.08–1.15 s.
HARNESS_READ_TIMEOUT_SEC = 0.2
HARNESS_QUERY_BUDGET_SEC = 3.0     # hard cap for all queries of one request
CATEGORIES = ("interaktiv", "orchestrator", "subagent", "unbekannt")
_COUNTERS = ("input", "output", "cache_read", "cache_write")


def marker_kpis(expect: object) -> list[tuple[str, str]]:
    """The (kpi key, label) pairs a marker's ``expect`` text names, in table order."""
    text = str(expect or "").lower()
    return [
        (key, label) for alternatives, key, label in MARKER_KPIS
        if any(all(w in text for w in words) for words in alternatives)
    ]


def marker_window(series: dict[str, float], marker_day: str, n: int, today: str, *,
                  covered_from: str | None = None, covered_to: str | None = None) -> dict:
    """Before/after comparison around a marker: n days each, the marker day in neither.

    ``before`` = [D-n, D-1], ``after`` = [D+1, D+n]. A day outside the source's
    coverage is MISSING, not 0: earlier than ``covered_from`` (the source's
    first day with data, or the index window's lower bound), or later than
    ``covered_to`` (the last day the index holds completely —
    ``_source_covered_to``; None = no bound) or not completed yet (``today``
    or later). Averages run over the days counted. A percentage change
    is given ONLY when both windows are fully covered — a half-covered window
    flipped the sign with the window length on real data (+176 % / −81 %);
    otherwise ``notes`` say "vorher/nachher unvollständig (n von N Tagen)". A
    marker dated today or later gets no calculation at all.
    """
    d = datetime.strptime(marker_day, "%Y-%m-%d").date()
    today_d = datetime.strptime(today, "%Y-%m-%d").date()
    last_full = today_d - timedelta(days=1)
    if covered_to is not None:
        last_full = min(last_full, datetime.strptime(covered_to, "%Y-%m-%d").date())
    before_all = [(d - timedelta(days=i)).isoformat() for i in range(n, 0, -1)]
    result: dict = {
        "n": n, "before_days": [], "after_days": [], "before_complete": False,
        "after_complete": False, "complete": False, "before_sum": None, "after_sum": None,
        "before_avg": None, "after_avg": None, "delta_pct": None, "notes": [],
    }
    if d >= today_d:
        result["notes"] = ["noch keine Nachher-Tage"]
        return result
    last = last_full.isoformat()
    before = [x for x in before_all if (covered_from is None or x >= covered_from) and x <= last]
    after_all = [(d + timedelta(days=i)).isoformat() for i in range(1, n + 1)]
    after = [x for x in after_all if (covered_from is None or x >= covered_from) and x <= last]
    result["before_days"], result["after_days"] = before, after
    result["before_complete"], result["after_complete"] = len(before) == n, len(after) == n
    result["complete"] = result["before_complete"] and result["after_complete"]
    if before:
        result["before_sum"] = round(sum(series.get(x, 0) for x in before), 6)
        result["before_avg"] = round(result["before_sum"] / len(before), 6)
    if after:
        result["after_sum"] = round(sum(series.get(x, 0) for x in after), 6)
        result["after_avg"] = round(result["after_sum"] / len(after), 6)
    if not result["before_complete"]:
        result["notes"].append(f"vorher unvollständig ({len(before)} von {n} Tagen)")
    if not result["after_complete"]:
        result["notes"].append(f"nachher unvollständig ({len(after)} von {n} Tagen)")
    if last_full < today_d - timedelta(days=1) and any(x > last for x in (*before_all, *after_all)):
        result["notes"].append("Quelle in keinem Indexlauf vollständig" if last_full == date.min
                               else f"Index vollständig nur bis {last}")
    if result["complete"] and result["before_avg"]:
        result["delta_pct"] = round((result["after_avg"] - result["before_avg"]) / result["before_avg"] * 100, 1)
    return result


def _unavailable(reason: str) -> dict:
    return {"harness": {"available": False, "reason": reason}}


def _read_reason(exc: BaseException) -> str:
    """Reason shown on the page (never stored): SQLite's own message ("database
    is locked", "interrupted", "file is not a database") carries no path; any
    other exception is reported by type only."""
    if isinstance(exc, sqlite3.Error):
        return f"{type(exc).__name__}: {exc}"
    return _err_text(exc)


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
        return _unavailable(f"Index nicht lesbar: {_read_reason(e)}")
    try:
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1_000)
        return {"harness": _read_payload(conn, days=days, window=window, now=now)}
    except _NotReadyError as e:
        return _unavailable(str(e))
    except sqlite3.Error as e:
        return _unavailable(f"Index nicht lesbar: {_read_reason(e)}")
    except Exception as e:
        return _unavailable(f"Index-Auswertung fehlgeschlagen: {_read_reason(e)}")
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
            "Datei löschen, der nächste Indexlauf baut sie neu auf")
    run = conn.execute(
        "SELECT finished, duration_sec, lines_read, lines_skipped, status, errors, files_read, bytes_read "
        "FROM index_runs ORDER BY id DESC LIMIT 1").fetchone()
    if run is None:
        raise _NotReadyError("Index noch nicht gelaufen (kein abgeschlossener Lauf)")
    skipped_total, runs_total = conn.execute(
        "SELECT coalesce(sum(lines_skipped), 0), count(*) FROM index_runs").fetchone()
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
                     "files_read": run[6], "bytes_read": run[7],
                     "skipped_total": skipped_total, "runs_total": runs_total,
                     "broken_lines": _read_broken_lines(conn)},
    }
    payload["usage"] = _read_usage(conn, first)
    payload["agent_calls"] = _read_agent_calls(conn, first)
    payload["cost"] = _read_costs(conn, first)
    payload["quota"] = _read_codex_quota(conn, now)
    payload["extern"] = _read_extern(conn, first)
    payload["markers"] = _read_markers(conn, window=window, today=today)
    return payload


def _read_broken_lines(conn: sqlite3.Connection) -> dict[str, int]:
    """Broken lines of the ledger and the marker file as they are NOW — the
    stock their last rebuild left in ``file_state.info`` (not a sum over runs)."""
    out = {"ledger": 0, "markers": 0}
    for source, info in conn.execute(
        "SELECT source, info FROM file_state WHERE source IN ('ledger', 'markers')",
    ):
        # Any failure reading the stock (out-of-range number, RecursionError of a
        # deeply nested value, a non-text cell) leaves 0: the endpoint never fails on it.
        with contextlib.suppress(Exception):
            out[source] = _int(_dict(json.loads(info or "{}")).get("broken")) or 0
    return out


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
    # Day of a run = the day of its calling tool_use line (via toolUseId); the
    # meta file's mtime only where no call is indexed — so both counts use the
    # same day boundaries.
    meta = conn.execute(
        "SELECT count(*) FROM claude_agent_meta m LEFT JOIN claude_agent_call c "
        "ON c.tool_use_id = m.tool_use_id WHERE coalesce(c.day, m.day) >= ?", (first,)).fetchone()[0]
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
        "opencode_sessions": "SELECT day, count(*) FROM oc_session WHERE parent_id IS NULL AND day >= ? "
                             "GROUP BY day",
    }
    return {d: float(v or 0) for d, v in conn.execute(queries[key], (since,))}


def _source_first_day(conn: sqlite3.Connection, source: str, today: str) -> str:
    """First day the source has data — earlier days are missing, not zero.

    opencode is additionally bounded by the index window (rows older than
    ``HARNESS_WINDOW_DAYS`` are never read); the ledger is read whole."""
    if source == "ledger":
        first = conn.execute("SELECT min(day) FROM ledger").fetchone()[0]
        return first or today  # no data at all: every "before" day is missing
    first = conn.execute(
        "SELECT min(d) FROM (SELECT min(day) AS d FROM oc_message WHERE role='assistant' "
        "UNION ALL SELECT min(day) FROM oc_session)").fetchone()[0]
    if not first:
        return today
    floor = (datetime.strptime(today, "%Y-%m-%d").date()
             - timedelta(days=int(config.HARNESS_WINDOW_DAYS))).isoformat()
    return str(max(first, floor))


# Newest runs `_source_covered_to` looks at (≈ 10 days at one run per 30 min).
COVERAGE_RUNS = 500


def _source_covered_to(conn: sqlite3.Connection, source: str) -> str:
    """Last day the index holds COMPLETELY for ``source``: the day before the
    START of the newest run in which the source reported no error.

    The start, not the end: a run across midnight read some files before it.
    A day after this is MISSING in a marker window, not 0 — the index may not
    have run since (``python dashboard.py`` without the orchestrator), or the
    source failed in every run since. No such run among the newest
    ``COVERAGE_RUNS`` → ``date.min`` (no day counts as covered).
    """
    for started, per_source in conn.execute(
        "SELECT started, per_source FROM index_runs ORDER BY id DESC LIMIT ?", (COVERAGE_RUNS,),
    ):
        try:
            if _dict(_dict(json.loads(per_source or "{}")).get(source)).get("errors"):
                continue
            return (datetime.fromisoformat(started).date() - timedelta(days=1)).isoformat()
        except (TypeError, ValueError):
            continue
    return date.min.isoformat()


def _read_markers(conn: sqlite3.Connection, *, window: int, today: str) -> list[dict]:
    markers = conn.execute("SELECT date, id, scope, change, expect FROM marker ORDER BY date, id").fetchall()
    if not markers:
        return []
    since = (datetime.strptime(markers[0][0], "%Y-%m-%d").date() - timedelta(days=max(MARKER_WINDOWS))).isoformat()
    series_cache: dict[str, dict[str, float]] = {}
    first_day: dict[str, str] = {}
    last_day: dict[str, str] = {}
    out = []
    for day, mid, scope, change, expect in markers:
        rows = []
        for key, label in marker_kpis(expect):
            if key not in series_cache:
                series_cache[key] = _kpi_series(conn, key, since)
            source = _KPI_SOURCE[key]
            if source not in first_day:
                first_day[source] = _source_first_day(conn, source, today)
                last_day[source] = _source_covered_to(conn, source)
            rows.append({"kpi": key, "label": label, "covered_from": first_day[source],
                         "covered_to": last_day[source],
                         **marker_window(series_cache[key], day, window, today,
                                         covered_from=first_day[source], covered_to=last_day[source])})
        out.append({"date": day, "id": mid, "scope": scope, "change": change, "expect": expect,
                    "kpis": rows})
    return out


_IDLE_PRIORITY_CLASS = 0x0040
_BELOW_NORMAL_PRIORITY_CLASS = 0x4000
_PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000


def _lower_priority() -> bool:
    """Lower our own CPU (and on Windows I/O and memory) priority.

    POSIX: ``os.nice(10)`` from inside the child — the parent could only do it
    through ``preexec_fn``, which is not thread-safe. Windows: see
    ``_lower_priority_windows``.

    Returns whether it worked; a failure is one line on stderr (with
    GetLastError on Windows), never an exception.
    """
    if os.name == "nt":
        return _lower_priority_windows()
    if hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError as e:
            with contextlib.suppress(Exception):
                print(f"harness_index: os.nice failed ({e})", file=sys.stderr)
            return False
        return True
    return False


def _lower_priority_windows() -> bool:
    """Background mode, and a CPU priority class never above BELOW_NORMAL.

    The parent starts us with BELOW_NORMAL_PRIORITY_CLASS, which leaves the
    I/O priority normal; PROCESS_MODE_BACKGROUND_BEGIN (settable only by a
    process on itself) lowers I/O and memory priority as well, so a first run
    over gigabytes does not compete with a running task's disk access. Two
    side effects, both measured on Windows in Korrekturrunde 2:

    * The mode caps the WORKING SET at about 32 MB. Microsoft does not document
      it; B. Dawson, "32 MiB Working Sets on a 64 GiB machine" (randomascii,
      2023-10-01) measured the same cap, with passes over a 64 MiB buffer
      about 250 times slower. Beyond the cap pages are trimmed and come back as
      soft page faults, so a run takes longer; the index streams its files, and
      the measured first run took 51.7 s in this mode. It is also why the run
      log reports peak PRIVATE memory, not the working set (``_peak_private_mb``).
    * ``GetPriorityClass`` read NORMAL (32) during the run, although the parent
      had started the child BELOW_NORMAL (16384). Microsoft's documentation of
      SetPriorityClass says nothing about the class in background mode. So the
      class is read back and set to BELOW_NORMAL again whenever it reads higher
      than that. That background mode survives the second call is pinned on
      Windows by ``test_windows_background_mode_keeps_below_normal_and_private_peak``
      (a second PROCESS_MODE_BACKGROUND_BEGIN must fail with
      ERROR_PROCESS_MODE_ALREADY_BACKGROUND, 402).
    """
    step = "background mode"
    try:
        k = _kernel32()
        me = k.GetCurrentProcess()
        ok = bool(k.SetPriorityClass(me, _PROCESS_MODE_BACKGROUND_BEGIN))
        if ok and k.GetPriorityClass(me) not in (_IDLE_PRIORITY_CLASS, _BELOW_NORMAL_PRIORITY_CLASS):
            step = "BELOW_NORMAL after background mode"
            ok = bool(k.SetPriorityClass(me, _BELOW_NORMAL_PRIORITY_CLASS))
        detail = "" if ok else f"GetLastError={_last_error()}"
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    if not ok:
        with contextlib.suppress(Exception):
            print(f"harness_index: {step} not set ({detail})", file=sys.stderr)
    return ok


def _print_startup_warnings() -> None:
    """``config.STARTUP_WARNINGS`` to stderr, each entry once (it leaves the list)."""
    pending = config.STARTUP_WARNINGS
    while pending:
        with contextlib.suppress(Exception):
            print(pending[0], file=sys.stderr)
        pending.pop(0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harness index (SQLite) over local transcripts and logs")
    parser.add_argument("--update", action="store_true", help="run one incremental update")
    args = parser.parse_args(argv)
    if not args.update:
        parser.print_help()
        return 0
    _print_startup_warnings()
    _lower_priority()
    summary = run_update()
    return 1 if summary.get("status") == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
