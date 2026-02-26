"""
Reads and updates the agent-queue.md file.
Parses open tasks, marks them done, appends results and log entries.
Supports: file context injection, cwd extraction, file locking, encoding fallback.
"""

from dataclasses import dataclass
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from config import QUEUE_FILE, RESULTS_SECTION, LOG_SECTION, VAULT_PATH, MAX_CONTEXT_FILE_SIZE, ALLOWED_CWD_ROOTS


# Matches:  - [ ] Task text  (optionally with <!-- retry: ... --> comment)
OPEN_TASK_RE = re.compile(r"^- \[ \] (.+?)(?:\s*<!--.*?-->)?\s*$", re.MULTILINE)

# Matches Obsidian wikilinks: [[Note Name]] or [[path/to/Note]]
WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")

# Matches explicit file paths ending in .md (including Windows drive paths like C:\...)
FILEPATH_RE = re.compile(r"(?:^|\s)((?:[A-Za-z]:)?[\w/\\.-]+\.md)")

# Matches cwd: tag in task text, including paths with spaces until the next hashtag token or EOL.
# To reduce false positives in normal prose, valid cwd metadata requires the path to start
# immediately after "cwd:" (quoted or unquoted). Use quotes for paths containing hashtags.
# Examples:
#   cwd:C:\proj
#   cwd:C:\Program Files\My App #tool:test-loop
#   cwd:"C:\Program Files\My App" #timeout:10m
CWD_RE = re.compile(
    r'(?i)(?:^|\s)cwd:(?:"([^"]+)"|(\S(?:.*?\S)?))(?=(?:\s+#\S+)|\s*$)',
)

# Matches #timeout: tag in task text
TIMEOUT_RE = re.compile(r"(?i)(?<!\S)#timeout:(\d+)([smh])(?=\s|$)")

# Matches #tool:name metadata tag
TOOL_TAG_RE = re.compile(r"#tool:[\w-]+")

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

# Extract only the markdown body under "## Queue" (until the next H2 heading)
QUEUE_SECTION_RE = re.compile(r"^## Queue\s*$\n?(.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class QueueTask:
    task_text: str
    line_no: int
    subtasks: tuple[str, ...] = ()   # populated for #parallel tasks


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
    candidate = VAULT_PATH / (ref + ".md")
    if candidate.exists() and _is_within_vault(candidate):
        return candidate
    for match in VAULT_PATH.rglob(f"{Path(ref).name}.md"):
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
    cwd_path = Path(cwd)

    if not cwd_path.is_dir():
        print(f"  [cwd] Warnung: Verzeichnis existiert nicht: {cwd}")
        return None

    if ALLOWED_CWD_ROOTS:
        resolved = cwd_path.resolve()
        if not any(
            resolved == root.resolve() or resolved.is_relative_to(root.resolve())
            for root in ALLOWED_CWD_ROOTS
        ):
            print(f"  [cwd] Warnung: Verzeichnis nicht in erlaubten Roots: {cwd}")
            return None

    return cwd


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


def strip_metadata_tags(task: str) -> str:
    """Remove routing/metadata tags before sending the task text to a provider."""
    task = CWD_RE.sub("", task)
    task = TIMEOUT_RE.sub("", task)
    task = TOOL_TAG_RE.sub("", task)
    task = PROVIDER_TAG_RE.sub("", task)
    task = PROFILE_TAG_RE.sub("", task)
    task = PREAPPROVE_TAG_RE.sub("", task)
    task = SHUTDOWN_TAG_RE.sub("", task)
    task = PARALLEL_TAG_RE.sub("", task)
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
    refs += [m.group(1).strip() for m in FILEPATH_RE.finditer(task)]

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

        task_text = m.group(1).strip()

        # Collect indented subtask lines for #parallel tasks
        subtask_lines: tuple[str, ...] = ()
        if PARALLEL_TAG_RE.search(task_text):
            collected: list[str] = []
            j = line_idx + 1
            while j < len(all_lines):
                raw = all_lines[j].rstrip()
                if raw.startswith("  -") or raw.startswith("\t-"):
                    # Strip leading whitespace then leading dash, then whitespace
                    subtask_text = raw.lstrip().lstrip("-").strip()
                    if subtask_text:
                        collected.append(subtask_text)
                    j += 1
                else:
                    break
            subtask_lines = tuple(collected)

        items.append(QueueTask(task_text=task_text, line_no=line_no, subtasks=subtask_lines))

    return items


def read_queue() -> list[str]:
    """Return list of open task texts from queue file (compat wrapper)."""
    return [item.task_text for item in read_queue_items()]


def _replace_open_task_line(
    content: str,
    *,
    line_no: int,
    task_text: str,
    replacement: str,
) -> str | None:
    """Replace an open queue line, tolerating line shifts caused by concurrent inserts."""
    lines = content.splitlines(keepends=True)
    preferred_idx = line_no - 1
    idx = preferred_idx
    line_shifted = False

    def _match_task_at(index: int) -> str | None:
        if index < 0 or index >= len(lines):
            return None
        body = lines[index].rstrip("\r\n")
        m = OPEN_TASK_RE.match(body)
        if not m:
            return None
        return m.group(1).strip()

    current_task = _match_task_at(idx)
    if current_task != task_text:
        # Queue line numbers can shift while a task runs (e.g. Telegram /task prepends a new item).
        # Re-scan for the same still-open task and pick the nearest match, preferring same/later lines.
        matches: list[int] = []
        for i, line in enumerate(lines):
            body = line.rstrip("\r\n")
            m = OPEN_TASK_RE.match(body)
            if m and m.group(1).strip() == task_text:
                matches.append(i)

        if not matches:
            if preferred_idx < 0 or preferred_idx >= len(lines):
                print(f"Warnung: Queue-Zeile {line_no} nicht gefunden.")
            elif current_task is None:
                print(f"Warnung: Zeile {line_no} ist kein offener Queue-Task mehr.")
            else:
                print(
                    f"Warnung: Queue-Zeile {line_no} enthält anderen Task "
                    f"('{current_task}' statt '{task_text}')."
                )
            return None

        later_or_equal = [i for i in matches if i >= preferred_idx]
        pool = later_or_equal or matches
        # NOTE: If the queue contains duplicate task texts, we pick the nearest
        # match by index (preferring same-or-later lines). This means only the
        # first occurrence is marked done; subsequent duplicates are left open.
        idx = min(pool, key=lambda i: abs(i - preferred_idx))
        line_shifted = idx != preferred_idx

    original_line = lines[idx]
    newline = "\r\n" if original_line.endswith("\r\n") else "\n" if original_line.endswith("\n") else ""
    lines[idx] = replacement + newline
    if line_shifted:
        print(f"Hinweis: Queue-Task '{task_text}' von Zeile {line_no} auf Zeile {idx + 1} re-synchronisiert.")
    return "".join(lines)


def mark_done(task_text: str, provider: str, *, line_no: int | None = None) -> bool:
    """Mark a task as completed in the queue file."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    replacement = f"- [x] {task_text} ✅ {now} ({provider})"

    def update(content: str) -> str | None:
        if line_no is not None:
            updated = _replace_open_task_line(
                content,
                line_no=line_no,
                task_text=task_text,
                replacement=replacement,
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


def mark_retry(task_text: str, retry_at: str, *, line_no: int | None = None) -> bool:
    """Add retry annotation to a task (stays open, shows when it will retry)."""
    replacement = f"- [ ] {task_text} <!-- retry: {retry_at} -->"

    def update(content: str) -> str | None:
        if line_no is not None:
            updated = _replace_open_task_line(
                content,
                line_no=line_no,
                task_text=task_text,
                replacement=replacement,
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


def append_result(task_text: str, result: str, provider: str) -> bool:
    """Append task result under the Ergebnisse section. Returns False on queue write failure."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = _build_result_entry(now, task_text, result, provider)

    def update(content: str) -> str | None:
        updated = _insert_after_heading(content, RESULTS_SECTION, entry)
        if updated is not None:
            return updated
        return content + f"\n\n{RESULTS_SECTION}{entry}"

    return _apply_update(update)


def _build_result_entry(now: str, task_text: str, result: str, provider: str) -> str:
    """Format a result entry for the Ergebnisse section."""
    return (
        f"\n### {now} | {provider}\n"
        f"**Task:** {task_text}\n\n"
        f"{result.strip()}\n\n"
        f"---"
    )


def finalize_task_with_result(
    task_text: str,
    result: str,
    provider: str,
    *,
    line_no: int | None = None,
) -> bool:
    """Atomically mark a task done and append its result in one queue update."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    done_replacement = f"- [x] {task_text} ✅ {now} ({provider})"
    result_entry = _build_result_entry(now, task_text, result, provider)

    def update(content: str) -> str | None:
        if line_no is not None:
            updated = _replace_open_task_line(
                content,
                line_no=line_no,
                task_text=task_text,
                replacement=done_replacement,
            )
            if updated is None:
                print(
                    f"Warnung: Task '{task_text}' konnte nicht atomar finalisiert werden "
                    f"(Zeile {line_no})."
                )
                return None
        else:
            pattern = re.compile(
                r"^- \[ \] \s*" + re.escape(task_text) + r"\s*(?:<!--.*?-->)?\s*$",
                re.MULTILINE
            )
            if not pattern.search(content):
                print(f"Warnung: Task '{task_text}' konnte nicht atomar finalisiert werden (nicht gefunden).")
                return None
            updated = pattern.sub(lambda _m: done_replacement, content, count=1)

        with_result = _insert_after_heading(updated, RESULTS_SECTION, result_entry)
        if with_result is not None:
            return with_result
        return updated + f"\n\n{RESULTS_SECTION}{result_entry}"

    return _apply_update(update)


def append_log(message: str) -> None:
    """Append a log entry to the Log section."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n<!-- {now} | {message} -->"

    def update(content: str) -> str | None:
        # Prefer the last exact "## Log" heading to avoid matching user content.
        updated = _insert_after_heading(content, LOG_SECTION, entry, prefer_last=True)
        if updated is not None:
            return updated
        return content + f"\n\n{LOG_SECTION}{entry}"

    _apply_update(update)


def append_task(task_text: str) -> bool:
    """Append a new open task to the Queue section."""
    new_line = f"- [ ] {task_text.strip()}"

    def update(content: str) -> str | None:
        updated = _insert_after_heading(content, "## Queue", "\n" + new_line)
        if updated is not None:
            return updated
        # No Queue section — prepend before Ergebnisse or at end
        updated = _insert_before_heading(content, RESULTS_SECTION, f"## Queue\n{new_line}\n\n")
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
        "<!-- - [ ] Fix bug in main.py cwd:/d/programmieren/projekt #timeout:10m -->\n\n"
        f"{RESULTS_SECTION}\n\n"
        f"{LOG_SECTION}\n",
        encoding="utf-8"
    )
    print(f"Queue-Datei erstellt: {QUEUE_FILE}")
