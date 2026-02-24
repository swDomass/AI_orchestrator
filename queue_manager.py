"""
Reads and updates the agent-queue.md file.
Parses open tasks, marks them done, appends results and log entries.
Supports: file context injection, cwd extraction, file locking, encoding fallback.
"""

import re
import sys
from datetime import datetime
from pathlib import Path

from config import QUEUE_FILE, RESULTS_SECTION, LOG_SECTION, VAULT_PATH, MAX_CONTEXT_FILE_SIZE


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


# --- File locking ---

def _lock_file(f):
    """Acquire exclusive lock on file (platform-specific)."""
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, max(1, f.seek(0, 2)))
        f.seek(0)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(f):
    """Release lock on file (platform-specific)."""
    if sys.platform == "win32":
        import msvcrt
        pos = f.seek(0, 2)
        f.seek(0)
        if pos > 0:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, pos)
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
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            _lock_file(f)
            try:
                return f.read()
            finally:
                _unlock_file(f)
    except UnicodeDecodeError:
        return QUEUE_FILE.read_text(encoding="cp1252")
    except (OSError, BlockingIOError):
        # Locking failed (another instance?) - read without lock
        return _read_file_safe(QUEUE_FILE)


def _write_queue_content(content: str) -> None:
    """Write queue file with locking."""
    try:
        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
            _lock_file(f)
            try:
                f.write(content)
            finally:
                _unlock_file(f)
    except (OSError, BlockingIOError):
        # Locking failed - write without lock (better than losing data)
        QUEUE_FILE.write_text(content, encoding="utf-8")


# --- Note resolution ---

def _resolve_note(ref: str) -> Path | None:
    """Find a vault note by wikilink name or relative path."""
    ref = ref.strip()
    candidate = VAULT_PATH / ref
    if candidate.exists():
        return candidate
    candidate = VAULT_PATH / (ref + ".md")
    if candidate.exists():
        return candidate
    matches = list(VAULT_PATH.rglob(f"{Path(ref).name}.md"))
    if matches:
        return matches[0]
    return None


# --- Task metadata extraction ---

def extract_cwd(task: str) -> str | None:
    """Extract working directory from task text (cwd:/path/to/dir)."""
    match = CWD_RE.search(task)
    return match.group(1) if match else None


def extract_timeout(task: str, default: int = 0) -> int:
    """Extract timeout from task text (#timeout:30s, #timeout:5m, #timeout:1h)."""
    match = TIMEOUT_RE.search(task)
    if not match:
        return default
    val, unit = int(match.group(1)), match.group(2)
    return val * {"s": 1, "m": 60, "h": 3600}[unit]


def strip_metadata_tags(task: str) -> str:
    """Remove metadata tags (cwd:, #timeout:) from task text before sending to provider."""
    task = CWD_RE.sub("", task)
    task = TIMEOUT_RE.sub("", task)
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
    """Return list of open task texts from queue file."""
    content = _read_queue_content()
    if not content:
        return []
    return [m.group(1).strip() for m in OPEN_TASK_RE.finditer(content)]


def mark_done(task_text: str, provider: str) -> None:
    """Mark a task as completed in the queue file. Uses string.find() instead of regex."""
    content = _read_queue_content()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    marker = f"- [ ] {task_text}"
    idx = content.find(marker)
    if idx == -1:
        return

    # Find end of line
    end = content.find("\n", idx)
    if end == -1:
        end = len(content)

    replacement = f"- [x] {task_text} ✅ {now} ({provider})"
    new_content = content[:idx] + replacement + content[end:]
    _write_queue_content(new_content)


def mark_retry(task_text: str, retry_at: str) -> None:
    """Add retry annotation to a task (stays open, shows when it will retry)."""
    content = _read_queue_content()

    marker = f"- [ ] {task_text}"
    idx = content.find(marker)
    if idx == -1:
        return

    end = content.find("\n", idx)
    if end == -1:
        end = len(content)

    replacement = f"- [ ] {task_text} <!-- retry: {retry_at} -->"
    new_content = content[:idx] + replacement + content[end:]
    _write_queue_content(new_content)


def append_result(task_text: str, result: str, provider: str) -> None:
    """Append task result under the Ergebnisse section."""
    content = _read_queue_content()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    entry = (
        f"\n### {now} | {provider}\n"
        f"**Task:** {task_text}\n\n"
        f"{result.strip()}\n\n"
        f"---"
    )

    if RESULTS_SECTION in content:
        new_content = content.replace(RESULTS_SECTION, RESULTS_SECTION + entry, 1)
    else:
        new_content = content + f"\n\n{RESULTS_SECTION}{entry}"

    _write_queue_content(new_content)


def append_log(message: str) -> None:
    """Append a log entry to the Log section."""
    content = _read_queue_content()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n<!-- {now} | {message} -->"

    if LOG_SECTION in content:
        new_content = content.replace(LOG_SECTION, LOG_SECTION + entry, 1)
    else:
        new_content = content + f"\n\n{LOG_SECTION}{entry}"

    _write_queue_content(new_content)


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
