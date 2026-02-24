"""
Reads and updates the agent-queue.md file.
Parses open tasks, marks them done, appends results and log entries.
Supports: file context injection, cwd extraction, file locking, encoding fallback.
"""

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from config import QUEUE_FILE, RESULTS_SECTION, LOG_SECTION, VAULT_PATH, MAX_CONTEXT_FILE_SIZE, ALLOWED_CWD_ROOTS


# Matches:  - [ ] Task text  (optionally with <!-- retry: ... --> comment)
OPEN_TASK_RE = re.compile(r"^- \[ \] (.+?)(?:\s*<!--.*?-->)?\s*$", re.MULTILINE)

# Matches Obsidian wikilinks: [[Note Name]] or [[path/to/Note]]
WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")

# Matches explicit file paths ending in .md
FILEPATH_RE = re.compile(r"(?:^|\s)([\w/\\. -]+\.md)")

# Matches cwd: tag in task text
CWD_RE = re.compile(r"cwd:(\S+)")

# Matches #timeout: tag in task text
TIMEOUT_RE = re.compile(r"#timeout:(\d+)([smh])")

# Matches #tool:name metadata tag
TOOL_TAG_RE = re.compile(r"#tool:[\w-]+")

# Matches provider selection tags
PROVIDER_TAG_RE = re.compile(r"#(?:claude|gemini|codex)\b", re.IGNORECASE)

# Matches retry comment
RETRY_TAG_RE = re.compile(r"<!-- retry: (\d{2}:\d{2}) -->")

# Extract only the markdown body under "## Queue" (until the next H2 heading)
QUEUE_SECTION_RE = re.compile(r"^## Queue\s*$\n?(.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)


# --- File locking ---

# On Windows, msvcrt.locking requires lock and unlock to cover the same byte range.
# We always lock exactly 1 byte at position 0 — this is sufficient for advisory locking.
_LOCK_SIZE = 1


def _lock_file(f):
    """Acquire exclusive lock on file (platform-specific)."""
    if sys.platform == "win32":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, _LOCK_SIZE)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


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


def _read_queue_content() -> str:
    """Read queue file with locking and encoding fallback."""
    if not QUEUE_FILE.exists():
        return ""
    try:
        with open(QUEUE_FILE, "rb") as f:
            _lock_file(f)
            try:
                raw = f.read()
            finally:
                _unlock_file(f)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp1252", errors="replace")
    except (OSError, BlockingIOError):
        # Locking failed (another instance?) - read without lock
        return _read_file_safe(QUEUE_FILE)


def _write_queue_content(content: str) -> None:
    """Write queue file with locking."""
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        mode = "r+b" if QUEUE_FILE.exists() else "w+b"
        with open(QUEUE_FILE, mode) as f:
            _lock_file(f)
            try:
                f.seek(0)
                f.truncate()
                f.write(content.encode("utf-8"))
                f.flush()
            finally:
                _unlock_file(f)
    except (OSError, BlockingIOError):
        # Locking failed - write without lock (better than losing data)
        QUEUE_FILE.write_text(content, encoding="utf-8")


def _apply_update(transform: Callable[[str], str | None]) -> None:
    """
    Atomically update the queue file by applying a transformation function.
    Handles locking and encoding fallback (reads as UTF-8/cp1252, always writes UTF-8).
    """
    if not QUEUE_FILE.exists():
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        QUEUE_FILE.touch()

    try:
        # Open in binary mode to handle decoding manually
        with open(QUEUE_FILE, "r+b") as f:
            _lock_file(f)
            try:
                raw = f.read()
                content = ""
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    content = raw.decode("cp1252", errors="replace")

                new_content = transform(content)

                if new_content is None or new_content == content:
                    return

                # Rewind and overwrite with UTF-8
                f.seek(0)
                f.truncate()
                f.write(new_content.encode("utf-8"))
                f.flush()
            finally:
                _unlock_file(f)
    except Exception as e:
        print(f"Fehler beim Update der Queue-Datei: {e}")


def _extract_queue_section(content: str) -> str:
    """Return the body of the '## Queue' section, or the full content as fallback."""
    match = QUEUE_SECTION_RE.search(content)
    if not match:
        return content
    return match.group(1)


def _retry_is_due(retry_at: str, now: datetime | None = None) -> bool:
    """Return True when a HH:MM retry marker is due.

    Retry markers store only HH:MM. To resolve the ambiguity across midnight,
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
    # rglob is inherently constrained to VAULT_PATH.
    return next(VAULT_PATH.rglob(f"{Path(ref).name}.md"), None)


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

    cwd = match.group(1)
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


def extract_timeout(task: str, default: int = 0) -> int:
    """Extract timeout from task text (#timeout:30s, #timeout:5m, #timeout:1h)."""
    match = TIMEOUT_RE.search(task)
    if not match:
        return default
    val, unit = int(match.group(1)), match.group(2)
    return val * {"s": 1, "m": 60, "h": 3600}[unit]


def strip_metadata_tags(task: str) -> str:
    """Remove routing/metadata tags before sending the task text to a provider."""
    task = CWD_RE.sub("", task)
    task = TIMEOUT_RE.sub("", task)
    task = TOOL_TAG_RE.sub("", task)
    task = PROVIDER_TAG_RE.sub("", task)
    task = re.sub(r"\s{2,}", " ", task)
    return task.strip()


# --- Context injection ---

def inject_file_context(task: str) -> str:
    """
    Finds [[wikilinks]] and file paths in the task text,
    reads the referenced vault files, and appends their content to the prompt.
    Respects MAX_CONTEXT_FILE_SIZE.
    """
    refs: list[str] = []
    refs += [m.group(1) for m in WIKILINK_RE.finditer(task)]
    refs += [m.group(1).strip() for m in FILEPATH_RE.finditer(task)]

    # Preserve order while avoiding duplicated file reads/context blocks.
    refs = list(dict.fromkeys(refs))

    if not refs:
        return task

    context_blocks = []
    for ref in refs:
        path = _resolve_note(ref)
        if path and path.exists():
            size = path.stat().st_size
            if size > MAX_CONTEXT_FILE_SIZE:
                print(f"  [context] Datei zu groß ({size // 1024}KB), übersprungen: {path.name}")
                continue
            content = _read_file_safe(path)
            context_blocks.append(
                f"--- Inhalt von '{path.name}' ---\n{content}\n--- Ende ---"
            )
            print(f"  [context] Datei eingelesen: {path.name}")
        else:
            print(f"  [context] Datei nicht gefunden: {ref}")

    if not context_blocks:
        return task

    return task + "\n\n" + "\n\n".join(context_blocks)


# --- Queue operations ---

def read_queue() -> list[str]:
    """Return list of open task texts from queue file, skipping those with future retry time."""
    content = _read_queue_content()
    if not content:
        return []

    queue_content = _extract_queue_section(content)
    tasks = []

    for m in OPEN_TASK_RE.finditer(queue_content):
        full_line = m.group(0)
        task_text = m.group(1).strip()

        retry_match = RETRY_TAG_RE.search(full_line)
        if retry_match:
            retry_at = retry_match.group(1)
            if not _retry_is_due(retry_at):
                continue

        tasks.append(task_text)

    return tasks


def mark_done(task_text: str, provider: str) -> None:
    """Mark a task as completed in the queue file."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    pattern = re.compile(
        r"^- \[ \] \s*" + re.escape(task_text) + r"\s*(?:<!--.*?-->)?\s*$",
        re.MULTILINE
    )
    replacement = f"- [x] {task_text} ✅ {now} ({provider})"

    def update(content: str) -> str | None:
        if not pattern.search(content):
            print(f"Warnung: Task '{task_text}' konnte nicht als erledigt markiert werden (nicht gefunden).")
            return None
        return pattern.sub(replacement, content, count=1)

    _apply_update(update)


def mark_retry(task_text: str, retry_at: str) -> None:
    """Add retry annotation to a task (stays open, shows when it will retry)."""
    pattern = re.compile(
        r"^- \[ \] \s*" + re.escape(task_text) + r"\s*(?:<!--.*?-->)?\s*$",
        re.MULTILINE
    )
    replacement = f"- [ ] {task_text} <!-- retry: {retry_at} -->"

    def update(content: str) -> str | None:
        if not pattern.search(content):
            print(f"Warnung: Task '{task_text}' konnte nicht für Retry markiert werden (nicht gefunden).")
            return None
        return pattern.sub(replacement, content, count=1)

    _apply_update(update)


def append_result(task_text: str, result: str, provider: str) -> None:
    """Append task result under the Ergebnisse section."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = (
        f"\n### {now} | {provider}\n"
        f"**Task:** {task_text}\n\n"
        f"{result.strip()}\n\n"
        f"---"
    )

    def update(content: str) -> str | None:
        if RESULTS_SECTION in content:
            return content.replace(RESULTS_SECTION, RESULTS_SECTION + entry, 1)
        else:
            return content + f"\n\n{RESULTS_SECTION}{entry}"

    _apply_update(update)


def append_log(message: str) -> None:
    """Append a log entry to the Log section."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n<!-- {now} | {message} -->"

    def update(content: str) -> str | None:
        if LOG_SECTION in content:
            return content.replace(LOG_SECTION, LOG_SECTION + entry, 1)
        else:
            return content + f"\n\n{LOG_SECTION}{entry}"

    _apply_update(update)


def append_task(task_text: str) -> None:
    """Append a new open task to the Queue section."""
    new_line = f"- [ ] {task_text.strip()}"

    def update(content: str) -> str | None:
        if "## Queue" in content:
            return content.replace("## Queue", "## Queue\n" + new_line, 1)
        # No Queue section — prepend before Ergebnisse or at end
        if RESULTS_SECTION in content:
            return content.replace(RESULTS_SECTION, f"## Queue\n{new_line}\n\n{RESULTS_SECTION}", 1)
        return content + f"\n\n## Queue\n{new_line}\n"

    _apply_update(update)


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
