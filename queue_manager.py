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

# Matches #agent:<name> profile tag
PROFILE_TAG_RE = re.compile(r"(?i)#agent:([\w-]+)")

# Matches #approve:<categories> pre-approval tag  (e.g. #approve:push,publish)
PREAPPROVE_TAG_RE = re.compile(r"(?i)#approve:([\w,:-]+)")

# Matches #shutdown tag
SHUTDOWN_TAG_RE = re.compile(r"(?i)(?<!\S)#shutdown(?=\s|$)")

# Matches #parallel tag
PARALLEL_TAG_RE = re.compile(r"(?i)(?<!\S)#parallel(?=\s|$)")

# Matches model selection tags across all providers:
#   Claude: #claude_haiku, #claude_sonnet, #claude_opus
#   Gemini: #gemini_pro, #gemini_flash
#   Codex:  #codex_mini
MODEL_TAG_RE = re.compile(
    r"(?i)(?<!\S)#(claude_(?:haiku|sonnet|opus)|gemini_(?:pro|flash)|codex_mini)(?![\w-])"
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

# Extract only the markdown body under "## Queue" (until the next H2 heading)
QUEUE_SECTION_RE = re.compile(r"^## Queue\s*$\n?(.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class QueueTask:
    task_text: str
    line_no: int
    subtasks: tuple[str, ...] = ()   # populated for #parallel tasks
    blocked_reason: str = ""         # non-empty = task is blocked by unmet #needs: deps


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


def _retry_is_due(retry_at: str, now: datetime | None = None) -> bool:
    """Return True when a retry marker is due.

    Newer markers may store an absolute local timestamp (YYYY-MM-DD HH:MM
    or YYYY-MM-DDTHH:MM), which is unambiguous and compared directly.

    Legacy markers store only HH:MM. To resolve the ambiguity across midnight,
    we pick the interpretation closest to *now* (within ±12h) and check if
    that time has already passed.

    Examples (assuming retry is always set for the near future):
      - retry_at="14:00", now=15:00 → candidate today 14:00 (1h ago) → due
      - retry_at="14:00", now=13:00 → candidate today 14:00 (1h ahead) → not due
      - retry_at="00:15", now=23:50 → today 00:15 is 23h35m ago, tomorrow 00:15
        is 25m ahead → pick tomorrow → not due
      - retry_at="23:50", now=00:10 → today 23:50 is 23h40m ahead, yesterday
        23:50 is 20m ago → pick yesterday → due
    """
    now = now or datetime.now()
    retry_at = retry_at.strip()

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(retry_at, fmt) <= now
        except ValueError:
            pass

    try:
        hour, minute = map(int, retry_at.split(":", 1))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        # Invalid retry marker: fail open so tasks are not stuck forever.
        return True

    # Consider both today and +/- 1 day, pick the one closest to now
    candidates = [
        candidate - timedelta(days=1),
        candidate,
        candidate + timedelta(days=1),
    ]
    closest = min(candidates, key=lambda c: abs((c - now).total_seconds()))

    return closest <= now


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
    """Extract timeout from task text (#timeout:30s, #timeout:5m, #timeout:1h)."""
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


def extract_model_tag(task: str) -> str | None:
    """Extract a model alias tag for any provider.

    Supported tags: #claude_haiku/_sonnet/_opus, #gemini_pro/_flash, #codex_mini.
    Returns the lowercased alias key (e.g. 'gemini_flash') or None.
    Resolution to a full model ID happens via config.model_id_for_provider(),
    which enforces that a tag only applies to its owning provider.
    """
    m = MODEL_TAG_RE.search(task)
    return m.group(1).lower() if m else None


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
    task = PARALLEL_TAG_RE.sub("", task)
    task = MODEL_TAG_RE.sub("", task)
    task = ID_TAG_RE.sub("", task)
    task = NEEDS_TAG_RE.sub("", task)
    task = PASS_PROVIDER_TAG_RE.sub("", task)
    task = SECOND_OPINION_TAG_RE.sub("", task)
    task = AT_TAG_RE.sub("", task)
    task = EVERY_TAG_RE.sub("", task)
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


def inject_file_context(task: str, max_chars: int = 0) -> str:
    """
    Finds [[wikilinks]] and file paths in the task text,
    reads the referenced vault files, and appends their content to the prompt.

    Args:
        task: The task text containing wikilinks/file refs.
        max_chars: Budget cap for total injected content (0 = unlimited).
                   If > 0 and content exceeds budget, smart section extraction
                   is applied first, then hard truncation as fallback.
                   Total injected chars across all wikilinks is capped.

    Respects MAX_CONTEXT_FILE_SIZE.
    """
    refs: list[str] = []
    refs += [m.group(1) for m in WIKILINK_RE.finditer(task)]
    # FILEPATH_RE: group(2) = quoted path (spaces ok), group(3) = unquoted path
    refs += [(m.group(2) or m.group(3)).strip() for m in FILEPATH_RE.finditer(task)]

    # Preserve order while avoiding duplicated file reads/context blocks.
    refs = list(dict.fromkeys(refs))

    if not refs:
        return task

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
        return task

    return task + "\n\n" + "\n\n".join(context_blocks)


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

        items.append(QueueTask(task_text=task_text, line_no=line_no, subtasks=subtask_lines))

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
    replacement: str,
    subtasks: tuple[str, ...] | None = None,
) -> str | None:
    """Replace an open queue line, tolerating line shifts caused by concurrent inserts."""
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
    lines[idx] = replacement + newline
    if line_shifted:
        print(f"Hinweis: Queue-Task '{task_text}' von Zeile {line_no} auf Zeile {idx + 1} re-synchronisiert.")
    return "".join(lines)


def _completion_replacement(task_text: str, done_replacement: str) -> str:
    """Return the rewrite for a successfully completed task.

    Normal case: returns `done_replacement` (a `- [x] ...` line).
    `#every:<duration>` case: returns `- [ ] <task> <!-- retry: now+duration -->`,
    so the task stays in the queue and fires again on schedule. The `#at:` tag
    (if any) is stripped — it served its purpose on the first fire.
    """
    every_sec = extract_every_tag(task_text)
    if every_sec is None:
        return done_replacement
    next_retry = datetime.now() + timedelta(seconds=every_sec)
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


def mark_retry(
    task_text: str,
    retry_at: str,
    *,
    line_no: int | None = None,
    subtasks: tuple[str, ...] | None = None,
) -> bool:
    """Add retry annotation to a task (stays open, shows when it will retry)."""
    replacement = f"- [ ] {task_text} <!-- retry: {retry_at} -->"

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
        return pattern.sub(lambda _m: replacement, content, count=1)

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
