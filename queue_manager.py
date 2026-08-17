"""
Reads and updates the agent-queue.md file.
Parses open tasks, marks them done, appends results and log entries.
Supports: file context injection, cwd extraction, file locking, encoding fallback.
"""

from dataclasses import dataclass
import logging
import os
import re
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from config import (
    ALLOWED_CWD_ROOTS,
    CLAUDE_EFFORT_LEVELS,
    MAX_CONTEXT_FILE_SIZE,
    QUEUE_DONE_DELETE_DAYS,
    QUEUE_DONE_MOVE_HOURS,
    QUEUE_EVENTS_LOG_FILE,
    QUEUE_EVENTS_LOG_RETENTION_DAYS,
    QUEUE_FILE,
    RESULTS_SECTION,
    LOG_SECTION,
    VAULT_PATH,
)
# dispatcher._TAG_MAP is the maßgebliche list of every routing tag (provider-only
# AND model-specific). Imported at module level, not deferred like the local
# `from dispatcher import _TAG_MAP` in parallel_runner._parse_subtask() — dispatcher's
# own module-level imports (config, limits, providers.*) never reach back into
# queue_manager, so there is no cycle to defer here.
from dispatcher import _TAG_MAP as _PROVIDER_TAG_MAP

logger = logging.getLogger(__name__)


# Matches:  - [ ] Task text  (optionally with <!-- retry: ... --> comment)
OPEN_TASK_RE = re.compile(r"^- \[ \] (.+?)(?:\s*<!--.*?-->)?\s*$", re.MULTILINE)

# Matches Obsidian wikilinks: [[Note Name]] or [[path/to/Note]]
WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")

# Matches explicit file paths ending in .md (including Windows drive paths like C:\...).
# Two variants:
#   - Quoted:   "My File.md" or 'My File.md'  (allows spaces, use for multi-word names)
#   - Unquoted: simple-path.md                (no spaces, word-boundary safe)
FILEPATH_RE = re.compile(
    r"""(["'])((?:[A-Za-z]:)?[\w/\\ .-]+?\.md)\1"""   # quoted path (group 2), backreference ensures matching quotes
    r"""|(?:^|\s)((?:[A-Za-z]:)?[\w/\\.-]+\.md)"""    # unquoted path (group 3)
)

# Matches cwd: tag in task text, including paths with spaces until the next hashtag token or EOL.
# To reduce false positives in normal prose, valid cwd metadata requires the path to start
# immediately after "cwd:" (quoted or unquoted). Use quotes for paths containing hashtags.
# Examples:
#   cwd:C:\proj
#   cwd:C:\Program Files\My App #tool:test-loop
#   cwd:"C:\Program Files\My App" #timeout:10m
CWD_RE = re.compile(
    r'(?i)(?:^|\s)cwd:\s*(?:"([^"]+)"|(\S(?:.*?\S)?))(?=(?:\s+#\S+)|\s*$)',
)

# Matches #timeout: tag in task text
TIMEOUT_RE = re.compile(r"(?i)(?<!\S)#timeout:(\d+)([smh])(?=\s|$)")

# Matches #tool:name metadata tag
TOOL_TAG_RE = re.compile(r"#tool:[\w-]+")

# Matches #tool_providers:p1,p2 metadata tag
TOOL_PROVIDERS_TAG_RE = re.compile(r"(?i)#tool_providers:([\w,]+)")

# Matches provider selection tags
PROVIDER_TAG_RE = re.compile(r"#(?:claude|gemini|codex)\b", re.IGNORECASE)

# Matches retry comment (legacy HH:MM or absolute local timestamp)
RETRY_TAG_RE = re.compile(r"<!-- retry: ([^>]+?) -->")

# Matches the persistent hang-retry counter comment (idle-kill bookkeeping).
HANG_COUNT_RE = re.compile(r"<!-- hang: (\d+) -->")

# Matches #agent:<name> profile tag
PROFILE_TAG_RE = re.compile(r"(?i)#agent:([\w-]+)")

# Matches #approve:<categories> pre-approval tag  (e.g. #approve:push,publish)
PREAPPROVE_TAG_RE = re.compile(r"(?i)#approve:([\w,:-]+)")

# Matches #shutdown tag
SHUTDOWN_TAG_RE = re.compile(r"(?i)(?<!\S)#shutdown(?=\s|$)")

# Matches #verify:<script> — a post-task check run AFTER the provider reports success.
# The script decides whether the task actually achieved anything; see
# orchestrator._run_verify_script.
#
# The unquoted branch stops at whitespace AND at '#'. Both bounds are load-bearing:
#   - no whitespace → a following `cwd:D:\Pfad mit Leerzeichen` stays intact (unlike
#     CWD_RE this tag has no "until the next hashtag" lookahead, which would swallow it)
#   - no '#' → `#verify:c.ps1#every:24h` yields "c.ps1", not "c.ps1#every:24h". Without
#     that bound the tag text lands INSIDE the script path, every lookup fails with
#     "Skript nicht gefunden", and fail-closed turns it into a permanent alarm on every
#     successful run. (The adjoining `#every:` is unusable either way — every tag regex
#     here carries a `(?<!\S)` lookbehind, so a tag glued to the previous token is never
#     matched. Writing tags without a separating space is simply invalid input; this
#     bound just keeps the damage out of the path.)
# Quote paths containing spaces or '#': #verify:"C:\My Scripts\check.ps1".
#
# The path part is optional (`*`, not `+`) so a pathless `#verify:` still MATCHES and is
# therefore stripped from the prompt instead of being handed to the model as literal tag
# text. extract_verify_tag() returns None for it — a typo silently disables the check,
# which is why `--lint-queue` flags it as verify_without_path.
VERIFY_TAG_RE = re.compile(r'(?i)(?<!\S)#verify:(?:"([^"]*)"|([^\s#]*))')

# Matches #parallel tag
PARALLEL_TAG_RE = re.compile(r"(?i)(?<!\S)#parallel(?=\s|$)")

# Matches #worktree tag — opt-in isolation of parallel subtasks via `git worktree`.
# Parent task only; subtasks inherit it implicitly via their CWD group.
WORKTREE_TAG_RE = re.compile(r"(?i)(?<!\S)#worktree(?=\s|$)")

# Matches #keep-worktree tag — disables auto-cleanup of the worktree directory
# after a successful subtask run, so the user can inspect the working copy.
KEEP_WORKTREE_TAG_RE = re.compile(r"(?i)(?<!\S)#keep-worktree(?=\s|$)")

# Matches model selection tags across ALL providers (claude/gemini/codex/vibe/openrouter).
# Derived from dispatcher._TAG_MAP instead of hand-copied, so this cannot silently fall
# behind again — that drift is exactly how this regex used to cover only 6 of the 20
# aliases (claude_{haiku,sonnet,opus}, gemini_{pro,flash}, codex_mini) for months, while
# gemini_flash_lite, codex_5/_5_4, vibe_medium/_small and all nine or_* aliases routed to
# the right *provider* but never forced a *model* (model_id_for_provider() returned None,
# so the provider's default model ran instead).
#
# Excludes the 5 bare provider-selection tags (#claude, #gemini, #codex, #vibe,
# #openrouter — where the tag text equals the provider name) since those route without
# forcing any specific model; PROVIDER_TAG_RE already covers #claude/#gemini/#codex.
#
# Sorted longest-first before joining: with two aliases where one is a literal prefix of
# the other (codex_5 / codex_5_4), a naive alternation risks the shorter one winning the
# match for input meant for the longer one. The `(?![\w-])` boundary below makes the
# backtracking engine recover via the next alternative either way, but sorting removes
# the reliance on that backtrack — the match is right on the first try, order-independent.
_MODEL_ALIAS_TAGS = sorted(
    (tag[1:] for tag, provider in _PROVIDER_TAG_MAP.items() if tag[1:] != provider),
    key=len,
    reverse=True,
)
MODEL_TAG_RE = re.compile(
    r"(?i)(?<!\S)#(" + "|".join(re.escape(t) for t in _MODEL_ALIAS_TAGS) + r")(?![\w-])"
)

# Matches #effort:<level> — reasoning effort for the run (Claude only; see
# config.CLAUDE_EFFORT_LEVELS). Deliberately LOOSE: it matches any word, so an
# invalid level (#effort:ultra) still produces a match and can be reported as a
# lint error instead of vanishing silently. Validation happens in
# extract_effort_tag(); a strict alternation here would make a typo indistinguishable
# from "no tag at all".
EFFORT_TAG_RE = re.compile(r"(?i)(?<!\S)#effort:([A-Za-z0-9_-]+)(?=\s|$)")

# Matches an *attempted* #effort tag — the canonical answer to "did the author try to
# write an effort tag here?", as opposed to EFFORT_TAG_RE's "is this a usable one?".
# Group 1 holds the ":value" / "=value" part and is None for a bare "#effort".
#
# Three callers need that same answer and used to disagree, which was the bug:
#   * strip_metadata_tags() — a malformed half ("#effort=high") survived stripping and
#     reached the provider prompt as literal text.
#   * queue_linter — reported it, but linting is not an execution gate.
#   * parallel_runner inheritance — read a malformed child tag as "no tag" and handed
#     the child the parent's level instead of the session default.
#
# The "(?<!\]\()" guard keeps a Markdown fragment link — "[effort docs](#effort:low)" —
# from being read as a tag; without it an ordinary link produced a spurious lint error AND
# got mangled by stripping. A leading "(" otherwise still counts, so "(#effort:high)" is
# caught as malformed.
EFFORT_ATTEMPT_RE = re.compile(
    r"(?i)(?<!\]\()(?:^|(?<=[\s(]))#effort(?![\w-])(\s*[:=]\s*[^\s)]*)?"
)

# Matches #pass1:<provider> and #pass2:<provider> for cross-provider tool support
PASS_PROVIDER_TAG_RE = re.compile(r"(?i)(?<!\S)#pass([12]):(claude|gemini|codex)(?=\s|$)")

# Matches #second_opinion:<alias> — opt-in second-opinion provider for review-loop.
# Value is a model alias (e.g. or_glm, or_minimax_free, claude_opus) or a bare
# provider name (openrouter, claude, gemini, codex). Resolution happens in the tool.
SECOND_OPINION_TAG_RE = re.compile(r"(?i)(?<!\S)#second_opinion:([A-Za-z0-9_]+)(?=\s|$)")

# Matches #id:name — gives a task a unique ID for dependency tracking
ID_TAG_RE = re.compile(r"(?i)(?<!\S)#id:([\w-]+)(?=\s|$)")

# Matches #needs:name1,name2 or #need:name1,name2 — declares task dependencies (comma-separated)
NEEDS_TAG_RE = re.compile(r"(?i)(?<!\S)#needs?:([\w,\-]+)(?=\s|$)")

# Matches #at:<timestamp> — one-time future start. Reuses the retry-due primitive.
# Accepts the same forms _retry_is_due() understands: full ISO (YYYY-MM-DDTHH:MM
# or YYYY-MM-DD HH:MM) and legacy HH:MM (closest-day interpretation).
AT_TAG_RE = re.compile(
    r"(?i)(?<!\S)#at:(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\d{2}:\d{2})(?=\s|$)"
)

# Matches #every:<duration> — recurring schedule. Duration units: s, m, h, d.
# Examples: #every:30m, #every:24h, #every:7d.
EVERY_TAG_RE = re.compile(r"(?i)(?<!\S)#every:(\d+)([smhd])(?=\s|$)")

# Matches #freshonly — bare flag (no value). Marks a recurring task whose run is only
# meaningful close to its anchored slot: a missed slot is realigned to the next
# occurrence instead of being caught up late. See realign_stale_freshonly().
# Requires whitespace/EOL after, so a stray value like `#freshonly:false` does NOT
# silently count as the flag being set (the linter flags such values separately).
FRESHONLY_TAG_RE = re.compile(r"(?i)(?<!\S)#freshonly(?=\s|$)")

# Matches #grace:<duration> — how late after the anchored slot a #freshonly task
# may still run before it counts as stale. Same units as #every. Default 2h.
GRACE_TAG_RE = re.compile(r"(?i)(?<!\S)#grace:(\d+)([smhd])(?=\s|$)")

# Default grace window for #freshonly tasks without an explicit #grace: tag.
DEFAULT_GRACE_SEC = 2 * 3600

# Whole-day threshold: anchored recurrence only applies to day-multiple intervals.
_ONE_DAY_SEC = 86400

# Extract only the markdown body under "## Queue" (until the next H2 heading)
QUEUE_SECTION_RE = re.compile(r"^## Queue\s*$\n?(.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class QueueTask:
    task_text: str
    line_no: int
    subtasks: tuple[str, ...] = ()   # populated for #parallel tasks
    blocked_reason: str = ""         # non-empty = task is blocked by unmet #needs: deps
    raw_line: str = ""               # full source line incl. comments (hang-counter marker)


def _find_heading_line(content: str, heading: str, prefer_last: bool = False):
    """Find an exact H2 heading line in the queue file content."""
    pattern = re.compile(rf"^{re.escape(heading)}\s*$", re.MULTILINE)
    matches = list(pattern.finditer(content))
    if not matches:
        return None
    return matches[-1] if prefer_last else matches[0]


def _insert_after_heading(
    content: str,
    heading: str,
    insert_text: str,
    *,
    prefer_last: bool = False,
) -> str | None:
    """Insert text immediately after an exact heading line."""
    match = _find_heading_line(content, heading, prefer_last=prefer_last)
    if not match:
        return None
    return content[: match.end()] + insert_text + content[match.end():]


def _insert_before_heading(
    content: str,
    heading: str,
    insert_text: str,
    *,
    prefer_last: bool = False,
) -> str | None:
    """Insert text immediately before an exact heading line."""
    match = _find_heading_line(content, heading, prefer_last=prefer_last)
    if not match:
        return None
    return content[: match.start()] + insert_text + content[match.start():]


# --- File locking ---

# On Windows, msvcrt.locking requires lock and unlock to cover the same byte range.
# We always lock exactly 1 byte at position 0 — this is sufficient for advisory locking.
_LOCK_SIZE = 1
_QUEUE_UPDATE_LOCK_RETRIES = 5
_QUEUE_UPDATE_LOCK_RETRY_DELAY_SEC = 0.05


def _lock_file(f):
    """Acquire exclusive lock on file (platform-specific)."""
    if sys.platform == "win32":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, _LOCK_SIZE)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)


def _unlock_file(f):
    """Release lock on file (platform-specific)."""
    if sys.platform == "win32":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, _LOCK_SIZE)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# --- Safe file I/O with encoding fallback ---

def _read_file_safe(path: Path) -> str:
    """Read file with UTF-8, fallback to cp1252 on Windows."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp1252")


def _queue_lock_path() -> Path:
    """Path of the sidecar lock file used to serialize atomic queue updates."""
    return QUEUE_FILE.with_name(f"{QUEUE_FILE.name}.lock")


def _open_queue_lock():
    """Open the sidecar lock file (created on demand)."""
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    return open(_queue_lock_path(), "a+b")


def _decode_queue_bytes(raw: bytes) -> str:
    """Decode queue file bytes with UTF-8 fallback to cp1252."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write bytes atomically via temp file + replace (same directory)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())

        os.replace(tmp_path, path)

        # Best-effort directory sync so the rename is durable after crashes.
        dir_fd = None
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            os.fsync(dir_fd)
        except (AttributeError, OSError):
            pass
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _read_queue_content() -> str:
    """Read queue file with locking and encoding fallback."""
    if not QUEUE_FILE.exists():
        return ""
    try:
        with _open_queue_lock() as lock_f:
            _lock_file(lock_f)
            try:
                if not QUEUE_FILE.exists():
                    return ""
                raw = QUEUE_FILE.read_bytes()
            finally:
                _unlock_file(lock_f)
        return _decode_queue_bytes(raw)
    except (OSError, BlockingIOError):
        # Locking failed (another instance?) - read without lock
        return _read_file_safe(QUEUE_FILE)


def _write_queue_content(content: str) -> None:
    """Write queue file with locking."""
    try:
        with _open_queue_lock() as lock_f:
            _lock_file(lock_f)
            try:
                _write_bytes_atomic(QUEUE_FILE, content.encode("utf-8"))
            finally:
                _unlock_file(lock_f)
    except (OSError, BlockingIOError):
        # Locking failed - write without lock (better than losing data)
        _write_bytes_atomic(QUEUE_FILE, content.encode("utf-8"))


def _apply_update(transform: Callable[[str], str | None]) -> bool:
    """
    Atomically update the queue file by applying a transformation function.
    Handles locking and encoding fallback (reads as UTF-8/cp1252, always writes UTF-8).
    """
    for attempt in range(1, _QUEUE_UPDATE_LOCK_RETRIES + 1):
        try:
            with _open_queue_lock() as lock_f:
                _lock_file(lock_f)
                try:
                    raw = QUEUE_FILE.read_bytes() if QUEUE_FILE.exists() else b""
                    content = _decode_queue_bytes(raw)

                    new_content = transform(content)

                    if new_content is None or new_content == content:
                        return False

                    _write_bytes_atomic(QUEUE_FILE, new_content.encode("utf-8"))
                    return True
                finally:
                    _unlock_file(lock_f)
        except (BlockingIOError, PermissionError, OSError) as e:
            if attempt >= _QUEUE_UPDATE_LOCK_RETRIES:
                print(f"Fehler beim Update der Queue-Datei (Lock): {e}")
                return False
            time.sleep(_QUEUE_UPDATE_LOCK_RETRY_DELAY_SEC)
        except Exception as e:
            print(f"Fehler beim Update der Queue-Datei: {e}")
            return False
    return False


def _extract_queue_section(content: str) -> str:
    """Return the body of the '## Queue' section, or the full content as fallback."""
    match = QUEUE_SECTION_RE.search(content)
    if not match:
        return content
    return match.group(1)


def _resolve_scheduled_dt(raw: str, now: datetime | None = None) -> datetime | None:
    """Resolve a retry/at timestamp string to a concrete datetime, or None if unparseable.

    Absolute forms (YYYY-MM-DD HH:MM / YYYY-MM-DDTHH:MM) are returned directly.

    Bare HH:MM is ambiguous across midnight; we pick the interpretation (today /
    ±1 day) closest to *now*:
      - "14:00", now=15:00 → today 14:00 (1h ago)
      - "00:15", now=23:50 → tomorrow 00:15 (25m ahead)
      - "23:50", now=00:10 → yesterday 23:50 (20m ago)
    """
    now = now or datetime.now()
    raw = raw.strip()

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass

    try:
        hour, minute = map(int, raw.split(":", 1))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None

    candidates = [
        candidate - timedelta(days=1),
        candidate,
        candidate + timedelta(days=1),
    ]
    return min(candidates, key=lambda c: abs((c - now).total_seconds()))


def _retry_is_due(retry_at: str, now: datetime | None = None) -> bool:
    """Return True when a retry marker is due (resolved time has passed).

    Delegates parsing to _resolve_scheduled_dt. Unparseable markers fail open
    (return True) so tasks are never stuck forever.
    """
    now = now or datetime.now()
    dt = _resolve_scheduled_dt(retry_at, now)
    if dt is None:
        return True
    return dt <= now


def _anchor_time_of_day(task_text: str) -> tuple[int, int] | None:
    """Return (hour, minute) of the task's #at: anchor, or None if absent/unparseable.

    Works for both bare HH:MM and full-ISO #at: forms — only the time-of-day matters
    for recurring anchoring.
    """
    raw = extract_at_tag(task_text)
    if not raw:
        return None
    dt = _resolve_scheduled_dt(raw)
    if dt is None:
        return None
    return (dt.hour, dt.minute)


def _is_whole_day_interval(every_sec: int) -> bool:
    """True when the interval is a positive whole-day multiple (24h, 48h, 7d, ...)."""
    return every_sec >= _ONE_DAY_SEC and every_sec % _ONE_DAY_SEC == 0


def _next_anchor_occurrence(
    anchor: tuple[int, int], every_sec: int, now: datetime | None = None
) -> datetime:
    """Next datetime at the anchor time-of-day, strictly after now. Callers gate on
    _is_whole_day_interval (so every_sec is 24h or a whole-day multiple).

    Daily (`#every:24h`): today's slot if still ahead, else tomorrow — every day has a
    slot, so filling today's still-future slot is correct.

    Multi-day (`#every:7d`, ...): the cadence is measured from `now` (this run / this
    realign), not from a fixed weekday phase — only the time-of-day is anchored. This
    avoids collapsing the cadence to the same day when `now` is before today's anchor
    time (a fixed-phase calendar would need a tracked epoch we don't keep).
    """
    now = now or datetime.now()
    hour, minute = anchor

    if every_sec == _ONE_DAY_SEC:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        while candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    step_days = max(2, every_sec // _ONE_DAY_SEC)
    candidate = (now + timedelta(days=step_days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    while candidate <= now:
        candidate += timedelta(days=step_days)
    return candidate


# --- Note resolution ---

def _is_within_vault(path: Path) -> bool:
    """Check that a resolved path stays within the vault directory."""
    try:
        path.resolve().relative_to(VAULT_PATH.resolve())
        return True
    except ValueError:
        return False


def _resolve_note(ref: str) -> Path | None:
    """Find a vault note by wikilink name or relative path.

    Security: resolved paths are validated to stay within VAULT_PATH
    to prevent path traversal via crafted wikilinks like [[../../etc/passwd]].
    """
    ref = ref.strip()
    candidate = VAULT_PATH / ref
    if candidate.exists() and _is_within_vault(candidate):
        return candidate
    _ref_with_md = ref if ref.endswith(".md") else ref + ".md"
    candidate = VAULT_PATH / _ref_with_md
    if candidate.exists() and _is_within_vault(candidate):
        return candidate
    _name = Path(ref).name
    if not _name.endswith(".md"):
        _name += ".md"
    for match in VAULT_PATH.rglob(_name):
        if _is_within_vault(match):
            return match
    return None


# --- Task metadata extraction ---

def extract_cwd(task: str) -> str | None:
    """Extract working directory from task text (cwd:/path/to/dir).

    Validates that:
    - The directory exists
    - It is within ALLOWED_CWD_ROOTS (if configured)
    Returns None (with warning) if validation fails.
    """
    match = CWD_RE.search(task)
    if not match:
        return None

    cwd = (match.group(1) or match.group(2) or "").strip()

    # Convert Git Bash / MSYS paths (/d/foo/bar) to Windows paths (D:\foo\bar)
    if sys.platform == "win32" and re.match(r"^/([a-zA-Z])/", cwd):
        cwd = cwd[1].upper() + ":" + cwd[2:].replace("/", "\\")

    cwd_path = Path(cwd)

    if not cwd_path.is_dir():
        print(f"  [cwd] Warnung: Verzeichnis existiert nicht: {cwd}")
        return None

    try:
        resolved = cwd_path.resolve()
    except Exception:
        print(f"  [cwd] Warnung: Verzeichnis konnte nicht aufgelöst werden: {cwd}")
        return None

    if ALLOWED_CWD_ROOTS:
        if not any(
            resolved == root.resolve() or resolved.is_relative_to(root.resolve())
            for root in ALLOWED_CWD_ROOTS
        ):
            print(f"  [cwd] Warnung: Verzeichnis nicht in erlaubten Roots: {cwd}")
            return None

    return str(resolved)


def has_cwd_tag(task: str) -> bool:
    """Return True when a cwd: metadata tag is present, even if invalid."""
    if CWD_RE.search(task):
        return True
    # Detect malformed metadata-like tags (e.g. "cwd: #codex" or bare "cwd:") without
    # treating arbitrary prose such as "explain cwd: semantics" as metadata.
    return re.search(r"(?i)(?:^|\s)cwd:(?=\s*(?:#\S+|$))", task) is not None


def extract_timeout(task: str, default: int = 0) -> int:
    """Extract timeout from task text (#timeout:30s, #timeout:5m, #timeout:1h).

    Since the liveness-watchdog refactor this sets the HARD backstop (absolute
    upper bound for a progressing run), NOT an aggressive deadline. Hang
    detection is liveness/idle-based (see providers/process_runner.py). For
    iterative tools the value is an upper cap only and never raises per-phase
    caps above the TOOL_*_TIMEOUT_SEC constants; total tool runtime is bounded
    by the ToolContract max_runtime_sec."""
    match = TIMEOUT_RE.search(task)
    if not match:
        return default
    val, unit = int(match.group(1)), match.group(2).lower()
    return val * {"s": 1, "m": 60, "h": 3600}[unit]


def extract_profile_tag(task: str) -> str | None:
    """Extract #agent:<name> profile tag from task text.

    If multiple #agent: tags are present, the first wins and a warning is logged.
    """
    matches = PROFILE_TAG_RE.findall(task)
    if len(matches) > 1:
        import logging
        logging.getLogger(__name__).warning(
            "queue_manager: multiple #agent: tags found ('%s') — using first: '%s'",
            "', '".join(matches),
            matches[0],
        )
    return matches[0] if matches else None


def extract_preapproved_actions(task: str) -> set[str]:
    """Parse '#approve:push,publish' → {'push', 'publish'}."""
    result: set[str] = set()
    for m in PREAPPROVE_TAG_RE.finditer(task):
        for part in m.group(1).split(","):
            part = part.strip(": ").lower()
            if part:
                result.add(part)
    return result


def extract_shutdown_tag(task: str) -> bool:
    """Return True if #shutdown tag is present in the task text."""
    return bool(SHUTDOWN_TAG_RE.search(task))


def extract_verify_tag(task: str) -> str | None:
    """Extract the #verify:<script> post-task check path, or None.

    The returned path is passed to orchestrator._run_verify_script after a task
    reports success. It exists because a provider run can look perfectly clean
    (exit 0, well-formed result event) while achieving nothing — the failure mode
    that made three morning-brief runs vanish silently (20./24./25.07.2026, with a
    healthy run on the 21st in between — they were not consecutive). A verify
    script checks the RESULT (did the file actually change?) instead of trusting
    the run, so it catches such failures regardless of their cause.
    """
    m = VERIFY_TAG_RE.search(task)
    if not m:
        return None
    # NOTE: a present-but-pathless tag returns None here, which is indistinguishable
    # from "no tag" at this call site. Runtime code must therefore ask has_verify_tag()
    # as well — otherwise a typo silently disables the check (fail-OPEN).
    # group(1) is the quoted branch: "" is a legitimate (empty) match there, so test for
    # None rather than falsiness — `or` would fall through to group(2), which is None.
    raw = m.group(1) if m.group(1) is not None else (m.group(2) or "")
    return raw.strip() or None


def has_verify_tag(task: str) -> bool:
    """True when a ``#verify:`` tag is present — even if it carries no usable path.

    Deliberately separate from extract_verify_tag(): that one returns None both for
    "no tag" and for "tag without path", and treating those alike is fail-OPEN — a
    typo would silently switch off the very check that exists because silent failures
    go unnoticed. Runtime callers pair the two to tell the cases apart.
    """
    return bool(VERIFY_TAG_RE.search(task))


def extract_worktree_tag(task: str) -> bool:
    """Return True if #worktree tag is present (opt-in isolation for #parallel)."""
    return bool(WORKTREE_TAG_RE.search(task))


def extract_keep_worktree_tag(task: str) -> bool:
    """Return True if #keep-worktree tag is present (skip auto-cleanup on success)."""
    return bool(KEEP_WORKTREE_TAG_RE.search(task))


def extract_model_tag(task: str) -> str | None:
    """Extract a model alias tag for any provider.

    Supported tags: every model-specific alias in dispatcher._TAG_MAP (all of
    claude_{haiku,sonnet,opus}, gemini_{pro,flash,flash_lite}, codex_{5,5_4,mini},
    vibe_{medium,small}, and the nine or_* OpenRouter aliases) — see MODEL_TAG_RE.
    Returns the lowercased alias key (e.g. 'gemini_flash') or None.
    Resolution to a full model ID happens via config.model_id_for_provider(),
    which enforces that a tag only applies to its owning provider.
    """
    m = MODEL_TAG_RE.search(task)
    return m.group(1).lower() if m else None


def extract_effort_tag_raw(task: str) -> str | None:
    """Return the #effort: value lowercased but UNVALIDATED (may be an unknown level).

    Callers use this to tell "no tag at all" apart from "tag with a bad value" —
    extract_effort_tag() collapses both to None. Keeps the raw regex inside this
    module instead of exporting it, matching how has_cwd_tag/has_verify_tag work.
    """
    m = EFFORT_TAG_RE.search(task)
    return m.group(1).lower() if m else None


def has_effort_tag_attempt(task: str) -> bool:
    """True when the line carries an #effort tag *with a value*, valid or not.

    This is what callers need to tell "the author said nothing about effort" apart
    from "the author said something unusable" — extract_effort_tag() and even
    extract_effort_tag_raw() collapse "#effort=high" and "#effort: high" to None,
    so both look identical to no tag at all. A bare "#effort" (no value) does not
    count; it stays reportable by the linter but is left alone in prose.
    """
    return any(m.group(1) for m in EFFORT_ATTEMPT_RE.finditer(task))


def extract_effort_tag(task: str) -> str | None:
    """Extract the reasoning-effort level from a #effort:<level> tag.

    Returns the lowercased level (always a member of config.CLAUDE_EFFORT_LEVELS)
    or None when the tag is absent OR carries an unknown value.

    An unknown value is a *lint* error — queue_linter reports it loudly via the
    same loose regex. At runtime we degrade to the session default instead of
    failing the task, because a typo in an optional tuning knob must not cost a
    scheduled run. Claude-only: other providers never read the resulting value.
    """
    m = EFFORT_TAG_RE.search(task)
    if not m:
        return None
    level = m.group(1).lower()
    return level if level in CLAUDE_EFFORT_LEVELS else None


def extract_tool_providers(task: str) -> list[str] | None:
    """Extract allowed providers for the task's tool from #tool_providers:p1,p2."""
    match = TOOL_PROVIDERS_TAG_RE.search(task)
    if not match:
        return None
    return [p.strip().lower() for p in match.group(1).split(",") if p.strip()]


def extract_pass_providers(task: str) -> dict[int, str]:
    """Extract #pass1:<provider> and #pass2:<provider> from task text.

    Returns e.g. {1: 'claude', 2: 'gemini'} or {} if none found.
    """
    result: dict[int, str] = {}
    for m in PASS_PROVIDER_TAG_RE.finditer(task):
        pass_num = int(m.group(1))
        provider = m.group(2).lower()
        result[pass_num] = provider
    return result


def extract_second_opinion_alias(task: str) -> str | None:
    """Extract the raw alias value from #second_opinion:<alias>.

    Returns the lowercased alias (e.g. 'or_glm', 'claude_opus', 'openrouter')
    or None. The tool resolves the alias to a (provider, model_id) pair —
    queue_manager stays decoupled from provider/model alias tables.
    """
    m = SECOND_OPINION_TAG_RE.search(task)
    return m.group(1).lower() if m else None


def extract_id_tag(task: str) -> str | None:
    """Extract #id:<name> from task text. Returns lowercased name or None."""
    m = ID_TAG_RE.search(task)
    return m.group(1).lower() if m else None


def extract_needs_tags(task: str) -> list[str]:
    """Extract #needs:<deps> from task text. Returns list of lowercased dep names."""
    m = NEEDS_TAG_RE.search(task)
    if not m:
        return []
    return [dep.strip().lower() for dep in m.group(1).split(",") if dep.strip()]


def extract_at_tag(task: str) -> str | None:
    """Extract #at:<timestamp> from task text. Returns the raw timestamp string or None.

    The timestamp is in the same form _retry_is_due() understands, so the same
    primitive decides when the task becomes due. #at: is purely syntactic sugar
    for a one-time future start.
    """
    m = AT_TAG_RE.search(task)
    return m.group(1) if m else None


def extract_every_tag(task: str) -> int | None:
    """Extract #every:<duration> from task text. Returns duration in seconds, or None.

    Supported units: s (seconds), m (minutes), h (hours), d (days).
    Examples: #every:30m → 1800, #every:24h → 86400, #every:7d → 604800.
    """
    m = EVERY_TAG_RE.search(task)
    if not m:
        return None
    val = int(m.group(1))
    unit_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return val * unit_seconds[m.group(2).lower()]


def has_freshonly_tag(task: str) -> bool:
    """Return True if the task carries the #freshonly flag."""
    return FRESHONLY_TAG_RE.search(task) is not None


def extract_grace_tag(task: str) -> int | None:
    """Extract #grace:<duration> from task text. Returns duration in seconds, or None.

    Supported units: s, m, h, d (same as #every). Example: #grace:4h → 14400.
    """
    m = GRACE_TAG_RE.search(task)
    if not m:
        return None
    val = int(m.group(1))
    unit_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return val * unit_seconds[m.group(2).lower()]


def strip_metadata_tags(task: str) -> str:
    """Remove routing/metadata tags before sending the task text to a provider."""
    task = CWD_RE.sub("", task)
    task = TIMEOUT_RE.sub("", task)
    task = TOOL_TAG_RE.sub("", task)
    task = TOOL_PROVIDERS_TAG_RE.sub("", task)
    task = PROVIDER_TAG_RE.sub("", task)
    task = PROFILE_TAG_RE.sub("", task)
    task = PREAPPROVE_TAG_RE.sub("", task)
    task = SHUTDOWN_TAG_RE.sub("", task)
    task = VERIFY_TAG_RE.sub("", task)
    task = PARALLEL_TAG_RE.sub("", task)
    task = KEEP_WORKTREE_TAG_RE.sub("", task)   # must precede WORKTREE_TAG_RE — shared "worktree" stem
    task = WORKTREE_TAG_RE.sub("", task)
    task = MODEL_TAG_RE.sub("", task)
    # Routing metadata like the model tag — must be stripped, or the level ends up in
    # the prompt as literal text AND the "line was nothing but metadata" guard in
    # orchestrator.run_once() stops firing (a bare `#tool:x #effort:low` would strip
    # to `#effort:low` instead of ""). Strips *attempts*, not just usable tags: with
    # EFFORT_TAG_RE alone, `#effort:low #effort=high` stripped to `#effort=high` and the
    # broken half went to the provider as text. Only matches carrying a value are removed,
    # so the word "#effort" in prose survives.
    task = EFFORT_ATTEMPT_RE.sub(lambda m: "" if m.group(1) else m.group(0), task)
    task = ID_TAG_RE.sub("", task)
    task = NEEDS_TAG_RE.sub("", task)
    task = PASS_PROVIDER_TAG_RE.sub("", task)
    task = SECOND_OPINION_TAG_RE.sub("", task)
    task = AT_TAG_RE.sub("", task)
    task = EVERY_TAG_RE.sub("", task)
    task = FRESHONLY_TAG_RE.sub("", task)
    task = GRACE_TAG_RE.sub("", task)
    task = re.sub(r"\s{2,}", " ", task)
    return task.strip()


# --- Context injection ---

def _extract_relevant_section(content: str, task_keywords: set[str], context_lines: int = 50) -> str:
    """Find the section in content most relevant to task_keywords and return it ± context_lines.

    Falls back to the full content if no keyword match is found.
    """
    if not task_keywords:
        return content

    lines = content.splitlines()
    best_idx = -1
    best_score = 0

    for i, line in enumerate(lines):
        line_tokens = set(re.findall(r"[a-zA-ZäöüÄÖÜß0-9]{3,}", line.lower()))
        score = len(task_keywords & line_tokens)
        if score > best_score:
            best_score = score
            best_idx = i

    if best_idx < 0 or best_score == 0:
        return content

    start = max(0, best_idx - context_lines)
    end = min(len(lines), best_idx + context_lines + 1)
    excerpt = "\n".join(lines[start:end])

    prefix = "...\n" if start > 0 else ""
    suffix = "\n..." if end < len(lines) else ""
    return prefix + excerpt + suffix


def collect_file_context(task: str, max_chars: int = 0) -> str:
    """Return ONLY the context blocks for [[wikilinks]]/file paths found in *task*.

    Separated from the task text on purpose: the prompt builder needs to place the
    file context and the instruction independently, because appending them as one
    unit buries the instruction in the middle of the prompt. That is exactly what
    made morning-brief fail silently three times (20./24./25.07.2026) — the model
    received a prompt ending in ~8.700 characters of foreign config documentation
    and answered "no concrete task in your message". See
    orchestrator._build_prompt, which now appends the task LAST.

    Args:
        task: The task text containing wikilinks/file refs.
        max_chars: Budget cap for total injected content (0 = unlimited).
                   If > 0 and content exceeds budget, smart section extraction
                   is applied first, then hard truncation as fallback.
                   Total injected chars across all wikilinks is capped.

    Returns:
        The joined context blocks, or "" when the task references no readable files.

    Respects MAX_CONTEXT_FILE_SIZE.
    """
    refs: list[str] = []
    refs += [m.group(1) for m in WIKILINK_RE.finditer(task)]
    # FILEPATH_RE: group(2) = quoted path (spaces ok), group(3) = unquoted path
    refs += [(m.group(2) or m.group(3)).strip() for m in FILEPATH_RE.finditer(task)]

    # Preserve order while avoiding duplicated file reads/context blocks.
    refs = list(dict.fromkeys(refs))

    if not refs:
        return ""

    # Compute task keywords for smart section extraction
    task_keywords: set[str] = set()
    if max_chars > 0:
        task_keywords = set(re.findall(r"[a-zA-ZäöüÄÖÜß0-9]{3,}", task.lower()))
        _stopwords = {"the", "and", "for", "with", "from", "that", "this",
                      "und", "die", "der", "das", "ein", "eine", "ist"}
        task_keywords -= _stopwords

    # Per-file budget: split max_chars evenly across refs (if budget set)
    per_file_chars = (max_chars // len(refs)) if (max_chars > 0 and refs) else 0
    total_injected = 0

    context_blocks = []
    for ref in refs:
        # Check overall budget remaining
        if max_chars > 0 and total_injected >= max_chars:
            print(f"  [context] Budget erschöpft, überspringe: {ref}")
            break

        path = _resolve_note(ref)
        if not path:
            print(f"  [context] Datei nicht gefunden: {ref}")
            continue

        try:
            if not path.exists():
                print(f"  [context] Datei nicht gefunden: {ref}")
                continue

            size = path.stat().st_size
            if size > MAX_CONTEXT_FILE_SIZE:
                print(f"  [context] Datei zu groß ({size // 1024}KB), übersprungen: {path.name}")
                continue

            content = _read_file_safe(path)
        except OSError as e:
            print(f"  [context] Datei konnte nicht gelesen werden ({path}): {e}")
            continue

        # Apply budget truncation
        remaining_budget = max_chars - total_injected if max_chars > 0 else 0
        file_budget = min(per_file_chars, remaining_budget) if max_chars > 0 else 0

        if file_budget > 0 and len(content) > file_budget:
            # Smart: find the most relevant section first
            content = _extract_relevant_section(content, task_keywords)
            if len(content) > file_budget:
                content = content[:file_budget] + "\n...[truncated]"
            print(f"  [context] Datei eingelesen (gekürzt): {path.name}")
        else:
            print(f"  [context] Datei eingelesen: {path.name}")

        block = f"--- Inhalt von '{path.name}' ---\n{content}\n--- Ende ---"
        context_blocks.append(block)
        total_injected += len(block)

    if not context_blocks:
        return ""

    return "\n\n".join(context_blocks)


# --- Queue operations ---

_DONE_OR_FAILED_RE = re.compile(r"^- \[[x\-]\] (.+)$", re.MULTILINE)


def _collect_completed_ids(content: str) -> set[str]:
    """Return all #id: values from done ([x]) or failed ([-]) tasks in the full file."""
    completed: set[str] = set()
    for m in _DONE_OR_FAILED_RE.finditer(content):
        task_id = extract_id_tag(m.group(1))
        if task_id:
            completed.add(task_id)
    return completed


def _set_retry_marker(line_body: str, dt: datetime) -> str:
    """Return line_body with its `<!-- retry: ... -->` set to dt (replace or append)."""
    marker = f"<!-- retry: {dt.strftime('%Y-%m-%d %H:%M')} -->"
    if RETRY_TAG_RE.search(line_body):
        return RETRY_TAG_RE.sub(marker, line_body)
    return f"{line_body.rstrip()} {marker}"


def realign_stale_freshonly(now: datetime | None = None) -> int:
    """Realign stale `#freshonly` recurring tasks to their next slot WITHOUT running them.

    A `#freshonly` task is "stale" when its scheduled slot (retry marker, else `#at:`
    anchor) lies more than its grace window (#grace:, default 2h) in the past. For such
    tasks the retry marker is rewritten to the next anchor occurrence so that
    read_queue_items() filters them out this cycle — no provider call, no side effects.

    Tasks without `#freshonly` are never touched (they keep catch-up semantics, e.g.
    weekly/monthly maintenance). Run this once per poll cycle BEFORE read_queue_items().

    Returns the number of tasks realigned.
    """
    now = now or datetime.now()
    count = [0]

    def transform(content: str) -> str | None:
        in_queue = False
        out: list[str] = []
        changed = 0
        for line in content.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            newline = line[len(body):]
            if body.startswith("## "):
                in_queue = body.strip() == "## Queue"
                out.append(line)
                continue
            if not in_queue:
                out.append(line)
                continue
            m = OPEN_TASK_RE.match(body)
            if not m:
                out.append(line)
                continue

            task_text = m.group(1).strip()
            every_sec = extract_every_tag(task_text)
            if every_sec is None or not has_freshonly_tag(task_text):
                out.append(line)
                continue

            retry_m = RETRY_TAG_RE.search(body)
            raw_schedule = retry_m.group(1) if retry_m else extract_at_tag(task_text)
            scheduled = _resolve_scheduled_dt(raw_schedule, now) if raw_schedule else None
            if scheduled is None or scheduled > now:
                out.append(line)  # no timing info, or not due yet → untouched
                continue

            grace = extract_grace_tag(task_text)
            if grace is None:
                grace = DEFAULT_GRACE_SEC
            if (now - scheduled).total_seconds() <= grace:
                out.append(line)  # fresh enough → let read_queue_items run it
                continue

            # Stale → realign to the next slot, do NOT run.
            anchor = _anchor_time_of_day(task_text)
            if anchor is not None and _is_whole_day_interval(every_sec):
                next_dt = _next_anchor_occurrence(anchor, every_sec, now)
            else:
                next_dt = now + timedelta(seconds=every_sec)
            out.append(_set_retry_marker(body, next_dt) + newline)
            changed += 1

        count[0] = changed
        return "".join(out) if changed else None

    applied = _apply_update(transform)
    return count[0] if applied else 0


def read_queue_items() -> list[QueueTask]:
    """Return open queue items with stable line identity, skipping future retry markers."""
    content = _read_queue_content()
    if not content:
        return []

    in_queue = False
    items: list[QueueTask] = []
    all_lines = content.splitlines()

    for line_idx, line in enumerate(all_lines):
        line_no = line_idx + 1  # 1-based

        if line.startswith("## "):
            in_queue = line.strip() == "## Queue"
            continue
        if not in_queue:
            continue

        m = OPEN_TASK_RE.match(line)
        if not m:
            continue

        retry_match = RETRY_TAG_RE.search(line)
        if retry_match and not _retry_is_due(retry_match.group(1)):
            continue

        task_text_raw = m.group(1).strip()

        # `#at:<timestamp>` — one-time future start. Reuses the retry-due primitive.
        # If a retry-annotation is already present, it wins (active timing signal).
        # Otherwise, an unmet #at: filters the task out of this poll.
        if not retry_match:
            at_match = AT_TAG_RE.search(task_text_raw)
            if at_match and not _retry_is_due(at_match.group(1)):
                continue

        task_text = task_text_raw

        # Collect indented subtask lines for #parallel tasks
        subtask_lines: tuple[str, ...] = ()
        if PARALLEL_TAG_RE.search(task_text):
            collected: list[str] = []
            j = line_idx + 1
            while j < len(all_lines):
                st = _parse_subtask_line(all_lines[j].rstrip())
                if st is not None:
                    collected.append(st)
                    j += 1
                else:
                    break
            subtask_lines = tuple(collected)

        items.append(QueueTask(task_text=task_text, line_no=line_no, subtasks=subtask_lines, raw_line=line))

    # Pass 2: Resolve #needs: dependencies
    needs_per_item = [extract_needs_tags(item.task_text) for item in items]
    if any(needs_per_item):
        completed_ids = _collect_completed_ids(content)
        resolved: list[QueueTask] = []
        for item, needs in zip(items, needs_per_item):
            if needs:
                missing = [dep for dep in needs if dep not in completed_ids]
                if missing:
                    resolved.append(QueueTask(
                        task_text=item.task_text,
                        line_no=item.line_no,
                        subtasks=item.subtasks,
                        blocked_reason=f"needs {', '.join(missing)}",
                        raw_line=item.raw_line,
                    ))
                    continue
            resolved.append(item)
        return resolved

    return items


def read_queue() -> list[str]:
    """Return list of open task texts from queue file (compat wrapper)."""
    return [item.task_text for item in read_queue_items()]


def _parse_subtask_line(raw: str) -> str | None:
    """Return subtask text from an indented list line, or None if not a subtask line."""
    if raw.startswith("  -") or raw.startswith("\t-"):
        text = raw.lstrip().lstrip("-").strip()
        return text if text else None
    return None


def _replace_open_task_line(
    content: str,
    *,
    line_no: int,
    task_text: str,
    replacement: str | Callable[[str], str],
    subtasks: tuple[str, ...] | None = None,
) -> str | None:
    """Replace an open queue line, tolerating line shifts caused by concurrent inserts.

    ``replacement`` may be a plain string or a callable that receives the resolved
    line's body (without its newline) and returns the new body. The callable form
    exists so a rewrite can carry state forward that only the OLD line knows —
    today that is the persistent ``<!-- hang: N -->`` counter, see ``mark_retry``.
    Resolving the line and reading it must stay in one place: the line number can
    have shifted, so a caller that re-searched the line itself would risk reading
    the counter off a different line than the one it then overwrites.
    """
    lines = content.splitlines(keepends=True)
    preferred_idx = line_no - 1
    idx = preferred_idx
    line_shifted = False

    def _get_task_at(index: int) -> tuple[str | None, tuple[str, ...]]:
        if index < 0 or index >= len(lines):
            return None, ()
        body = lines[index].rstrip("\r\n")
        m = OPEN_TASK_RE.match(body)
        if not m:
            return None, ()

        found_text = m.group(1).strip()
        found_subtasks: list[str] = []
        # Only scan subtasks if caller provided subtasks for matching
        if subtasks is not None:
            j = index + 1
            while j < len(lines):
                st = _parse_subtask_line(lines[j].rstrip())
                if st is not None:
                    found_subtasks.append(st)
                    j += 1
                else:
                    break
        return found_text, tuple(found_subtasks)

    current_task, current_subtasks = _get_task_at(idx)
    is_exact_match = (current_task == task_text) and (subtasks is None or current_subtasks == subtasks)

    if not is_exact_match:
        # Queue line numbers can shift while a task runs (e.g. Telegram /task prepends a new item).
        # Re-scan for the same still-open task and pick the nearest match, preferring same/later lines.
        # O(N) task scan + O(S) subtask scan on matching lines only (S = subtask count).
        matches: list[int] = []
        for i, line in enumerate(lines):
            body = line.rstrip("\r\n")
            m = OPEN_TASK_RE.match(body)
            if not m or m.group(1).strip() != task_text:
                continue
            if subtasks is None:
                matches.append(i)
            else:
                found: list[str] = []
                j = i + 1
                while j < len(lines):
                    st = _parse_subtask_line(lines[j].rstrip())
                    if st is not None:
                        found.append(st)
                        j += 1
                    else:
                        break
                if tuple(found) == subtasks:
                    matches.append(i)

        if not matches:
            if preferred_idx < 0 or preferred_idx >= len(lines):
                print(f"Warnung: Queue-Zeile {line_no} nicht gefunden.")
            elif current_task is None:
                print(f"Warnung: Zeile {line_no} ist kein offener Queue-Task mehr.")
            else:
                cur_info = f" with {len(current_subtasks)} subtasks" if subtasks is not None else ""
                exp_info = f" with {len(subtasks)} subtasks" if subtasks is not None else ""
                print(
                    f"Warnung: Queue-Zeile {line_no} enthält anderen Task "
                    f"('{current_task}'{cur_info} statt '{task_text}'{exp_info})."
                )
            return None

        later_or_equal = [i for i in matches if i >= preferred_idx]
        pool = later_or_equal or matches
        # NOTE: If the queue contains duplicate task texts (and same subtasks), we pick the nearest
        # match by index (preferring same-or-later lines).
        idx = min(pool, key=lambda i: abs(i - preferred_idx))
        line_shifted = idx != preferred_idx

    original_line = lines[idx]
    newline = "\r\n" if original_line.endswith("\r\n") else "\n" if original_line.endswith("\n") else ""
    new_body = replacement(original_line.rstrip("\r\n")) if callable(replacement) else replacement
    lines[idx] = new_body + newline
    if line_shifted:
        print(f"Hinweis: Queue-Task '{task_text}' von Zeile {line_no} auf Zeile {idx + 1} re-synchronisiert.")
    return "".join(lines)


def _completion_replacement(task_text: str, done_replacement: str) -> str:
    """Return the rewrite for a successfully completed task.

    Normal case: returns `done_replacement` (a `- [x] ...` line).

    `#every:<duration>` case: returns `- [ ] <task> <!-- retry: <next> -->`, so the
    task stays in the queue and fires again on schedule.

    Anchored (`#at:HH:MM #every:Nd`, N>=1 day): the next run is the next occurrence of
    the anchor time-of-day — NOT now+duration — so the daily slot never drifts. The
    `#at:` anchor is preserved (normalized to bare `HH:MM`, dropping any stale date).

    Non-anchored (no `#at:` or sub-day interval): legacy behavior — next run is
    now+duration and a stale one-time `#at:` is stripped.
    """
    every_sec = extract_every_tag(task_text)
    if every_sec is None:
        return done_replacement

    now = datetime.now()
    anchor = _anchor_time_of_day(task_text)
    if anchor is not None and _is_whole_day_interval(every_sec):
        next_retry = _next_anchor_occurrence(anchor, every_sec, now)
        # Preserve the anchor, normalized to bare HH:MM (idempotent; drops stale date).
        cleaned = AT_TAG_RE.sub(f"#at:{anchor[0]:02d}:{anchor[1]:02d}", task_text)
    else:
        next_retry = now + timedelta(seconds=every_sec)
        cleaned = AT_TAG_RE.sub("", task_text)

    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return f"- [ ] {cleaned} <!-- retry: {next_retry.strftime('%Y-%m-%d %H:%M')} -->"


def mark_done(
    task_text: str,
    provider: str,
    *,
    line_no: int | None = None,
    subtasks: tuple[str, ...] | None = None,
) -> bool:
    """Mark a task as completed in the queue file.

    For `#every:` tasks, the line is rewritten as an open task with a new retry
    annotation (= now + duration) instead of being marked `[x]`. This implements
    recurring schedules on top of the existing retry primitive — see
    `_completion_replacement`.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    replacement = _completion_replacement(
        task_text,
        f"- [x] {task_text} ✅ {now} ({provider})",
    )

    def update(content: str) -> str | None:
        if line_no is not None:
            updated = _replace_open_task_line(
                content,
                line_no=line_no,
                task_text=task_text,
                replacement=replacement,
                subtasks=subtasks,
            )
            if updated is None:
                print(
                    f"Warnung: Task '{task_text}' konnte nicht als erledigt markiert werden "
                    f"(Zeile {line_no})."
                )
            return updated

        pattern = re.compile(
            r"^- \[ \] \s*" + re.escape(task_text) + r"\s*(?:<!--.*?-->)?\s*$",
            re.MULTILINE
        )
        if not pattern.search(content):
            print(f"Warnung: Task '{task_text}' konnte nicht als erledigt markiert werden (nicht gefunden).")
            return None
        return pattern.sub(lambda _m: replacement, content, count=1)

    return _apply_update(update)


def extract_hang_count(line_text: str) -> int:
    """Read the persistent hang-retry counter from a raw queue line (0 if absent)."""
    match = HANG_COUNT_RE.search(line_text)
    return int(match.group(1)) if match else 0


def mark_retry(
    task_text: str,
    retry_at: str,
    *,
    line_no: int | None = None,
    subtasks: tuple[str, ...] | None = None,
    hang_count: int | None = None,
) -> bool:
    """Add retry annotation to a task (stays open, shows when it will retry).

    The persistent ``<!-- hang: N -->`` marker is the queue's ONLY per-task counter,
    and this function is the only place that writes it. Because every park rebuilds
    the line from scratch, "not passing the counter" used to mean "erase it":

    * ``hang_count=<int>`` SETS the counter. Only the two paths that judge the task
      itself pass one — hang and format_error, each with previous+1. Those are the
      unsuccessful attempts the cap in MAX_HANG_RETRIES exists to bound.
    * ``hang_count=None`` PRESERVES whatever the line already carries. This is every
      other park — capacity, provider cooldown, timeout, strict-mode, approval
      denied/timeout/skipped, parallel error. None of them say anything about the
      task, so they must neither raise the counter nor reset it.
    * ``hang_count=0`` clears it explicitly. Nothing does today; success resets the
      counter by rewriting the line via finalize_task_with_result() instead.

    Before 2026-08-15 None meant "drop the marker". A task alternating between
    format errors and capacity parks therefore never reached MAX_HANG_RETRIES and
    requeued forever — invisible in an unattended night run, which is the only kind
    of run this counter exists for.
    """

    def _build(previous_line: str) -> str:
        count = extract_hang_count(previous_line) if hang_count is None else hang_count
        hang_suffix = f" <!-- hang: {count} -->" if count else ""
        return f"- [ ] {task_text} <!-- retry: {retry_at} -->{hang_suffix}"

    def update(content: str) -> str | None:
        if line_no is not None:
            updated = _replace_open_task_line(
                content,
                line_no=line_no,
                task_text=task_text,
                replacement=_build,
                subtasks=subtasks,
            )
            if updated is None:
                print(
                    f"Warnung: Task '{task_text}' konnte nicht für Retry markiert werden "
                    f"(Zeile {line_no})."
                )
            return updated

        pattern = re.compile(
            r"^- \[ \] \s*" + re.escape(task_text) + r"\s*(?:<!--.*?-->)?\s*$",
            re.MULTILINE
        )
        if not pattern.search(content):
            print(f"Warnung: Task '{task_text}' konnte nicht für Retry markiert werden (nicht gefunden).")
            return None
        return pattern.sub(lambda m: _build(m.group(0)), content, count=1)

    return _apply_update(update)


def finalize_task_with_result(
    task_text: str,
    result: str,
    provider: str,
    *,
    line_no: int | None = None,
    subtasks: tuple[str, ...] | None = None,
) -> bool:
    """Atomically mark a task done in one queue update.
    Result is stored in memory/task_results/, not in the queue file.

    For `#every:` tasks, the line is rewritten as an open task with a new retry
    annotation instead of `[x]` — see `_completion_replacement`.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    done_replacement = _completion_replacement(
        task_text,
        f"- [x] {task_text} ✅ {now} ({provider})",
    )

    def update(content: str) -> str | None:
        if line_no is not None:
            updated = _replace_open_task_line(
                content,
                line_no=line_no,
                task_text=task_text,
                replacement=done_replacement,
                subtasks=subtasks,
            )
            if updated is None:
                print(
                    f"Warnung: Task '{task_text}' konnte nicht atomar finalisiert werden "
                    f"(Zeile {line_no})."
                )
                return None
            return updated
        else:
            pattern = re.compile(
                r"^- \[ \] \s*" + re.escape(task_text) + r"\s*(?:<!--.*?-->)?\s*$",
                re.MULTILINE
            )
            if not pattern.search(content):
                print(f"Warnung: Task '{task_text}' konnte nicht atomar finalisiert werden (nicht gefunden).")
                return None
            return pattern.sub(lambda _m: done_replacement, content, count=1)

    return _apply_update(update)


# Thread-safe lock and rate-limit state for queue events log
_events_log_lock = threading.Lock()
_events_log_cleanup_last_date: date | None = None
_events_log_dir_ensured: bool = False


def append_log(message: str) -> None:
    """Append a queue event to logs/queue-events.log (plain text, no longer writes to queue MD)."""
    global _events_log_dir_ensured
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"{now} | {message}\n"
    try:
        with _events_log_lock:
            if not _events_log_dir_ensured:
                QUEUE_EVENTS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                _events_log_dir_ensured = True
            with open(QUEUE_EVENTS_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(entry)
            _cleanup_queue_events_log()
    except OSError:
        pass  # best-effort — never block the orchestrator


def _cleanup_queue_events_log() -> None:
    """Prune queue-events.log entries older than QUEUE_EVENTS_LOG_RETENTION_DAYS. Once per day.
    Must be called with _events_log_lock held."""
    global _events_log_cleanup_last_date
    today = date.today()
    if _events_log_cleanup_last_date == today:
        return

    if not QUEUE_EVENTS_LOG_FILE.exists():
        _events_log_cleanup_last_date = today
        return
    try:
        cutoff = datetime.now() - timedelta(days=QUEUE_EVENTS_LOG_RETENTION_DAYS)
        lines = QUEUE_EVENTS_LOG_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = []
        for line in lines:
            m = _EVENTS_LOG_TS_RE.match(line)
            if m:
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
                    if ts < cutoff:
                        continue
                except ValueError:
                    pass
            kept.append(line)
        if len(kept) < len(lines):
            _write_bytes_atomic(QUEUE_EVENTS_LOG_FILE, "".join(kept).encode("utf-8"))
            logger.debug("Pruned %d old queue event(s)", len(lines) - len(kept))
    except OSError as e:
        logger.debug("queue-events.log cleanup failed: %s", e)
    finally:
        _events_log_cleanup_last_date = today


def append_task(task_text: str) -> bool:
    """Append a new open task to the Queue section."""
    new_line = f"- [ ] {task_text.strip()}"

    def update(content: str) -> str | None:
        updated = _insert_after_heading(content, "## Queue", "\n" + new_line)
        if updated is not None:
            return updated
        return content + f"\n\n## Queue\n{new_line}\n"

    return _apply_update(update)


def ensure_queue_file() -> None:
    """Create queue file with template if it doesn't exist."""
    if QUEUE_FILE.exists():
        return
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(
        "# Agent Queue\n\n"
        "## Queue\n"
        "<!-- Trage hier Tasks ein. Beispiel: -->\n"
        "<!-- - [ ] Schreibe Zusammenfassung von [[Projekt X]] -->\n"
        "<!-- - [ ] Analysiere Code in [[EEG Programm]] #codex -->\n"
        "<!-- - [ ] Fix bug in main.py cwd:/d/programmieren/projekt #timeout:10m -->\n",
        encoding="utf-8"
    )
    print(f"Queue-Datei erstellt: {QUEUE_FILE}")


# ── Queue Cleanup (erledigt.md) ───────────────────────────────────────────────

# Erledigt file lives next to the queue file
_ERLEDIGT_FILE = QUEUE_FILE.with_name("agent-queue-erledigt.md")

# Rate limiting: run at most once per calendar day
_done_cleanup_last_run_date: date | None = None

# Matches completed tasks with embedded timestamp: - [x] text ✅ YYYY-MM-DD HH:MM (provider)
_DONE_TASK_TS_RE = re.compile(
    r"^- \[[x\-]\] .+ ✅ (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \([^)]+\)\s*$"
)
# Matches timestamp prefix in queue-events.log lines: "YYYY-MM-DD HH:MM |"
_EVENTS_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \|")
# Matches erledigt.md date-section headings: "## YYYY-MM-DD"
_ERLEDIGT_SECTION_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


def cleanup_done_tasks() -> int:
    """Move completed tasks ≥ QUEUE_DONE_MOVE_HOURS old from queue to agent-queue-erledigt.md.
    Prune erledigt entries older than QUEUE_DONE_DELETE_DAYS.
    Returns count of tasks moved. Never raises. Runs at most once per calendar day.
    """
    global _done_cleanup_last_run_date
    today = date.today()
    if _done_cleanup_last_run_date == today:
        return 0

    moved = 0
    try:
        moved = _move_old_done_tasks()
        _prune_erledigt_file()
        if moved:
            logger.info("Moved %d completed task(s) to erledigt.md", moved)
    except Exception as e:
        logger.warning("cleanup_done_tasks failed: %s", e)
    finally:
        _done_cleanup_last_run_date = today
    return moved


def _move_old_done_tasks() -> int:
    """Under queue lock: extract done tasks ≥ QUEUE_DONE_MOVE_HOURS, append to erledigt.md."""
    cutoff = datetime.now() - timedelta(hours=QUEUE_DONE_MOVE_HOURS)
    tasks_by_date: dict[str, list[str]] = {}
    moved_count = 0

    def transform(content: str) -> str | None:
        nonlocal tasks_by_date, moved_count
        # Reset on every attempt (safe for _apply_update retries)
        tasks_by_date.clear()
        moved_count = 0
        lines = content.splitlines(keepends=True)
        kept: list[str] = []
        local_tasks: dict[str, list[str]] = {}
        local_count = 0

        i = 0
        while i < len(lines):
            line = lines[i]
            m = _DONE_TASK_TS_RE.match(line.rstrip("\n\r"))
            if m:
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
                except ValueError:
                    kept.append(line)
                    i += 1
                    continue
                if ts < cutoff:
                    date_str = ts.strftime("%Y-%m-%d")
                    task_lines = [line]
                    local_count += 1
                    # Collect indented subtask lines belonging to this task
                    while i + 1 < len(lines) and lines[i + 1].startswith("  "):
                        i += 1
                        task_lines.append(lines[i])
                    local_tasks.setdefault(date_str, []).extend(task_lines)
                    i += 1
                    continue
            kept.append(line)
            i += 1

        if not local_tasks:
            return None  # Nothing to move — no queue write needed

        tasks_by_date.update(local_tasks)
        moved_count = local_count
        return "".join(kept)

    updated = _apply_update(transform)

    if updated and tasks_by_date:
        _append_to_erledigt(tasks_by_date)
    return moved_count if updated else 0


def _parse_erledigt_sections(content: str) -> tuple[str, dict[str, str]]:
    """Parse erledigt.md into (header, {date_str: section_body}) dict."""
    parts = _ERLEDIGT_SECTION_RE.split(content)
    header = parts[0]
    date_sections: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        date_str = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        date_sections[date_str] = body
    return header, date_sections


def _build_erledigt_content(header: str, date_sections: dict[str, str]) -> str:
    """Rebuild erledigt.md from header + date sections (newest date first)."""
    parts = [header.rstrip()]
    for ds in sorted(date_sections.keys(), reverse=True):
        body = date_sections[ds].strip()
        if body:
            parts.append(f"\n## {ds}\n\n{body}\n")
    return "\n".join(parts) + "\n"


def _append_to_erledigt(tasks_by_date: dict[str, list[str]]) -> None:
    """Append moved tasks to agent-queue-erledigt.md, grouped by completion date."""
    if _ERLEDIGT_FILE.exists():
        try:
            existing = _ERLEDIGT_FILE.read_text(encoding="utf-8")
        except OSError:
            existing = "# Agent Queue — Erledigt\n"
    else:
        existing = "# Agent Queue — Erledigt\n"

    header, date_sections = _parse_erledigt_sections(existing)

    for date_str, lines in tasks_by_date.items():
        new_block = "".join(lines).strip()
        if date_str in date_sections:
            date_sections[date_str] = date_sections[date_str].rstrip() + "\n" + new_block + "\n"
        else:
            date_sections[date_str] = new_block + "\n"

    _write_bytes_atomic(_ERLEDIGT_FILE, _build_erledigt_content(header, date_sections).encode("utf-8"))


def _prune_erledigt_file() -> int:
    """Remove date sections older than QUEUE_DONE_DELETE_DAYS from erledigt.md. Returns pruned count."""
    if not _ERLEDIGT_FILE.exists():
        return 0
    try:
        content = _ERLEDIGT_FILE.read_text(encoding="utf-8")
    except OSError:
        return 0

    cutoff = (datetime.now() - timedelta(days=QUEUE_DONE_DELETE_DAYS)).date()
    header, date_sections = _parse_erledigt_sections(content)

    kept: dict[str, str] = {}
    pruned = 0
    for date_str, body in date_sections.items():
        try:
            if datetime.strptime(date_str, "%Y-%m-%d").date() < cutoff:
                pruned += 1
                continue
        except ValueError:
            pass
        kept[date_str] = body

    if pruned == 0:
        return 0

    _write_bytes_atomic(_ERLEDIGT_FILE, _build_erledigt_content(header, kept).encode("utf-8"))
    logger.info("Pruned %d old date section(s) from erledigt.md", pruned)
    return pruned
