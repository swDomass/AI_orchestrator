"""
AI Orchestrator — Persistent Memory System

Three-layer memory architecture:

1. **Curated MEMORY.md** — long-term patterns, decisions, conventions.
   Always loaded into prompt (small, high-value). User-editable.
   Path: VAULT_PATH/99_System/AI/memory/MEMORY.md

2. **Daily logs** — append-only session log per day.
   Read today + yesterday at task start for cheap temporal locality.
   Path: VAULT_PATH/99_System/AI/memory/daily/Memory YYYY-MM-DD.md

3. **TF-IDF search** — keyword-similarity + temporal decay over all
   past task results for deep relevant context from weeks/months ago.
   Path: VAULT_PATH/99_System/AI/memory/task_results/*.md

Storage layout:
    VAULT_PATH/99_System/AI/memory/
        MEMORY.md       ← curated long-term memory (layer 1)
        daily/          ← daily append-only logs (layer 2)
        task_results/   ← one .md per completed task (layer 3)
        error_patterns/ ← reserved
        preferences/    ← reserved
        archive/        ← memories older than MEMORY_MAX_AGE_DAYS

Task result file format:
    ---
    task: "Review und fixe Bugs"
    provider: claude+review-loop
    cwd: /d/programmieren/projekt
    duration_sec: 45.2
    timestamp: 2026-02-26T14:23:00
    success: true
    ---

    Fixed 3 P1 bugs in auth module. All tests pass.
"""

import logging
import math
import re
import shutil
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from config import (
    MEMORY_ARCHIVE_DELETE_DAYS,
    MEMORY_DAILY_LOG_RETENTION_DAYS,
    MEMORY_HALF_LIFE_DAYS,
    MEMORY_LESSONS_RETENTION_DAYS,
    MEMORY_MAX_AGE_DAYS,
    MEMORY_MIN_SCORE,
    MEMORY_SUMMARY_MAX_CHARS,
    MEMORY_TOP_K,
    PROMPT_CURATED_MEMORY_TOKENS,
    PROMPT_DAILY_LOG_TOKENS,
    VAULT_PATH,
)
from queue_manager import _write_bytes_atomic

logger = logging.getLogger(__name__)

# Root memory directory inside vault
_MEMORY_ROOT = VAULT_PATH / "99_System" / "AI" / "memory"
_TASK_RESULTS_DIR = _MEMORY_ROOT / "task_results"
_ARCHIVE_DIR = _MEMORY_ROOT / "archive"
_DAILY_DIR = _MEMORY_ROOT / "daily"
_CURATED_MEMORY_FILE = _MEMORY_ROOT / "MEMORY.md"
_daily_log_lock = threading.Lock()
_lessons_lock = threading.Lock()

# Pre-compiled tokenizer patterns (avoids recompilation on every search call)
_RE_CAMEL_SPLIT = re.compile(r"([a-z])([A-Z])")
_RE_DELIMITERS  = re.compile(r"[_\-/\\.]")
_RE_WORDS       = re.compile(r"[a-zA-ZäöüÄÖÜß0-9]{3,}")

# Throttle: archive_old_memories() läuft maximal 1× pro Kalendertag
_archive_last_run_date: Optional[date] = None

# Simple stopwords for tokenization
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "have", "has",
    "been", "was", "are", "not", "aber", "und", "die", "der", "das",
    "ein", "eine", "ist", "des", "dem", "den", "auf", "mit", "von",
    "sie", "auch", "sich", "bei", "wie", "als", "aus", "wird",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure_dirs() -> None:
    """Create memory directory tree if missing."""
    for d in (_TASK_RESULTS_DIR, _ARCHIVE_DIR, _DAILY_DIR,
              _MEMORY_ROOT / "error_patterns",
              _MEMORY_ROOT / "preferences"):
        d.mkdir(parents=True, exist_ok=True)


def _slugify(text: str, max_chars: int = 40) -> str:
    """Convert text to a safe filename slug."""
    slug = text.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:max_chars]


def _make_filename(task: str, provider: str) -> str:
    """Build filename: YYYY-MM-DD_{slug}_{provider}.md"""
    date = datetime.now().strftime("%Y-%m-%d")
    slug = _slugify(task)
    safe_provider = re.sub(r"[^\w+.-]", "-", provider)[:30]
    return f"{date}_{slug}_{safe_provider}.md"


def _truncate_summary(text: str) -> str:
    """Truncate to MEMORY_SUMMARY_MAX_CHARS (first N + last 200)."""
    if len(text) <= MEMORY_SUMMARY_MAX_CHARS:
        return text
    tail = min(200, MEMORY_SUMMARY_MAX_CHARS // 3)
    first = max(0, MEMORY_SUMMARY_MAX_CHARS - tail)
    return text[:first] + "\n...\n" + text[-tail:]


def _tokenize(text: str) -> set[str]:
    """Lowercase, split on delimiters and camelCase, keep words ≥3 chars, remove stopwords."""
    text = _RE_CAMEL_SPLIT.sub(r"\1 \2", text)
    text = _RE_DELIMITERS.sub(" ", text)
    words = _RE_WORDS.findall(text.lower())
    return {w for w in words if w not in _STOPWORDS}


def _tfidf_sim(query_tokens: set[str], doc_tokens: set[str]) -> float:
    """Jaccard-like cosine approximation: |intersection| / sqrt(|q| * |d|)."""
    if not query_tokens or not doc_tokens:
        return 0.0
    intersection = len(query_tokens & doc_tokens)
    return intersection / math.sqrt(len(query_tokens) * len(doc_tokens))


def _temporal_score(sim: float, age_days: float, half_life: float = MEMORY_HALF_LIFE_DAYS) -> float:
    """Apply temporal decay: sim * (0.5 ** (age_days / half_life))."""
    return sim * (0.5 ** (age_days / half_life))


# ── Frontmatter parsing ───────────────────────────────────────────────────────

def _parse_memory_file(path: Path) -> Optional[dict]:
    """Parse a memory .md file. Returns dict with keys: task, provider, cwd,
    duration_sec, timestamp, success, summary, path."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    if not content.startswith("---"):
        return None

    # Find the closing --- delimiter on its own line (not just any --- in body)
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        # Fallback: try split-based approach
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        frontmatter_raw = parts[1].strip()
        body = parts[2].strip()
    else:
        offset = end_match.start()
        frontmatter_raw = content[3:3 + offset].strip()
        body = content[3 + end_match.end():].strip()

    # Minimal YAML-style parser (key: value, no nested structures)
    meta: dict = {}
    for line in frontmatter_raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            meta[k] = v

    try:
        ts = datetime.fromisoformat(meta.get("timestamp", ""))
    except ValueError:
        ts = datetime.fromtimestamp(path.stat().st_mtime)

    return {
        "task": meta.get("task", ""),
        "provider": meta.get("provider", ""),
        "cwd": meta.get("cwd", ""),
        "duration_sec": float(meta.get("duration_sec", 0) or 0),
        "timestamp": ts,
        "success": meta.get("success", "true").lower() not in ("false", "0"),
        "summary": body,
        "path": path,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def store_result(
    task: str,
    result: str,
    provider: str,
    duration_sec: float,
    cwd: Optional[str] = None,
    *,
    success: bool = True,
) -> Optional[Path]:
    """Write a task result to memory/task_results/.

    Returns the path written, or None on error.
    """
    try:
        _ensure_dirs()
        summary = _truncate_summary(result)
        ts = datetime.now().isoformat(timespec="seconds")

        frontmatter = (
            f"---\n"
            f'task: "{task[:200].replace(chr(34), chr(39))}"\n'
            f"provider: {provider}\n"
            f"cwd: {cwd or ''}\n"
            f"duration_sec: {duration_sec:.1f}\n"
            f"timestamp: {ts}\n"
            f"success: {str(success).lower()}\n"
            f"---\n\n"
        )

        content = frontmatter + summary
        filename = _make_filename(task, provider)
        dest = _TASK_RESULTS_DIR / filename

        # Avoid clobbering same-second duplicates
        counter = 1
        original_stem = dest.stem
        while dest.exists():
            dest = _TASK_RESULTS_DIR / f"{original_stem}_{counter}.md"
            counter += 1

        dest.write_text(content, encoding="utf-8")
        logger.debug("Memory stored: %s", dest.name)

        # Also append to today's daily log
        append_daily_log(task, result, provider, duration_sec, cwd=cwd, success=success)

        return dest
    except Exception as e:
        logger.warning("Memory store failed: %s", e)
        return None


def search_memory(
    query: str,
    cwd: Optional[str] = None,
    top_k: int = MEMORY_TOP_K,
) -> list[dict]:
    """Search memory files for relevant past context.

    Uses TF-IDF keyword similarity + temporal decay.
    CWD bonus: same-cwd memories get 1.2× multiplier.
    Returns up to top_k dicts with keys: task, summary, score, timestamp, cwd.
    """
    if not _TASK_RESULTS_DIR.exists():
        return []

    query_tokens = _tokenize(query)
    now = datetime.now()
    scored: list[tuple[float, dict]] = []

    for path in _TASK_RESULTS_DIR.glob("*.md"):
        mem = _parse_memory_file(path)
        if not mem:
            continue

        doc_text = mem["task"] + " " + mem["summary"]
        doc_tokens = _tokenize(doc_text)
        sim = _tfidf_sim(query_tokens, doc_tokens)

        age_days = max(0.0, (now - mem["timestamp"]).total_seconds() / 86400)
        score = _temporal_score(sim, age_days)

        # CWD bonus
        if cwd and mem["cwd"] and _paths_match(cwd, mem["cwd"]):
            score *= 1.2

        if score > 0:
            scored.append((score, {
                "task": mem["task"],
                "summary": mem["summary"],
                "score": score,
                "timestamp": mem["timestamp"],
                "cwd": mem["cwd"],
                "provider": mem["provider"],
                "success": mem["success"],
            }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:top_k]]


def get_context_for_task(task_text: str, cwd: Optional[str] = None) -> str:
    """Build an injectable memory context block for a task.

    - Searches by keyword similarity + temporal decay.
    - Discards results below MEMORY_MIN_SCORE to avoid polluting the prompt.
    - Generic tasks with no keyword match above threshold: use N most recent
      from same CWD, or N most recent overall.
    - Returns "" if no memories found.
    """
    all_results = search_memory(task_text, cwd=cwd)

    # Apply minimum score threshold — only use similarity results if they're meaningful
    results = [r for r in all_results if r["score"] >= MEMORY_MIN_SCORE]

    if results:
        log_preview = results[:3]
        logger.info(
            "[memory] %d relevant match(es) found (threshold %.2f):%s",
            len(results),
            MEMORY_MIN_SCORE,
            "".join(
                f"\n  #{i} score={m['score']:.3f} [{m['timestamp'].strftime('%Y-%m-%d')}] {m['task'][:60]}"
                for i, m in enumerate(log_preview, 1)
            ),
        )
    else:
        if all_results:
            logger.info(
                "[memory] %d match(es) below threshold %.2f (best=%.3f) — using recent fallback",
                len(all_results),
                MEMORY_MIN_SCORE,
                all_results[0]["score"],
            )
        # Fallback: most recent memories
        results = _get_recent_memories(cwd=cwd, n=MEMORY_TOP_K)

    if not results:
        return ""

    lines: list[str] = []
    for i, mem in enumerate(results, 1):
        ts = mem["timestamp"].strftime("%Y-%m-%d")
        status = "✅" if mem["success"] else "❌"
        lines.append(
            f"{i}. [{ts}] {status} {mem['task'][:80]}\n"
            f"   Provider: {mem['provider']}\n"
            f"   {mem['summary'][:200]}"
        )

    return "\n\n".join(lines)


def _get_recent_memories(cwd: Optional[str] = None, n: int = MEMORY_TOP_K) -> list[dict]:
    """Return the N most recent memory files, filtered by cwd if provided."""
    if not _TASK_RESULTS_DIR.exists():
        return []

    mems = []
    for path in _TASK_RESULTS_DIR.glob("*.md"):
        mem = _parse_memory_file(path)
        if mem:
            mems.append(mem)

    # Filter by cwd if we have enough matches
    if cwd:
        cwd_mems = [
            m for m in mems
            if m["cwd"] and _paths_match(cwd, m["cwd"])
        ]
        if cwd_mems:
            mems = cwd_mems

    mems.sort(key=lambda m: m["timestamp"], reverse=True)
    return [
        {
            "task": m["task"],
            "summary": m["summary"],
            "score": 0.0,
            "timestamp": m["timestamp"],
            "cwd": m["cwd"],
            "provider": m["provider"],
            "success": m["success"],
        }
        for m in mems[:n]
    ]


def _paths_match(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except Exception:
        return a == b


# ── Layer 1: Curated MEMORY.md ───────────────────────────────────────────────

def get_curated_memory(max_chars: int = 0) -> str:
    """Read the curated MEMORY.md file.

    Returns the file content (truncated to max_chars if set), or "" if missing.
    This file is user-maintained — the orchestrator never writes to it automatically.
    """
    if not _CURATED_MEMORY_FILE.exists():
        return ""
    try:
        content = _CURATED_MEMORY_FILE.read_text(encoding="utf-8").strip()
        if not max_chars:
            max_chars = PROMPT_CURATED_MEMORY_TOKENS * 5  # ~5 chars/token
        if len(content) > max_chars:
            content = content[:max_chars] + "\n..."
        return content
    except Exception as e:
        logger.warning("Failed to read curated memory: %s", e)
        return ""


# ── Layer 2: Daily Logs ──────────────────────────────────────────────────────

def _daily_log_path(d: date) -> Path:
    """Return path for a given day's log: daily/Memory YYYY-MM-DD.md"""
    return _DAILY_DIR / f"Memory {d.isoformat()}.md"


def append_daily_log(
    task: str,
    result: str,
    provider: str,
    duration_sec: float,
    cwd: Optional[str] = None,
    *,
    success: bool = True,
) -> bool:
    """Append a task entry to today's daily log.

    Format is Obsidian-friendly Markdown. Returns True on success.
    """
    try:
        _ensure_dirs()
        today = date.today()
        path = _daily_log_path(today)
        now = datetime.now()
        ts = now.strftime("%H:%M")
        status = "success" if success else "failed"

        # Truncate result for daily log (shorter than full task_results)
        summary = result[:300].replace("\n", " ").strip()
        if len(result) > 300:
            summary += "..."

        entry = (
            f"\n## {ts} — {task[:120]}\n"
            f"- **Provider:** {provider}\n"
        )
        if cwd:
            entry += f"- **CWD:** {cwd}\n"
        entry += (
            f"- **Duration:** {duration_sec:.0f}s\n"
            f"- **Status:** {status}\n"
            f"- {summary}\n"
        )

        # Parallel subtasks run in threads within the same orchestrator process.
        # Guard creation so only one writer emits the daily header.
        with _daily_log_lock:
            if not path.exists():
                path.write_text(f"# Memory {today.isoformat()}\n{entry}", encoding="utf-8")
            else:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(entry)

        logger.debug("Daily log appended: %s", path.name)
        return True
    except Exception as e:
        logger.warning("Daily log append failed: %s", e)
        return False


def get_daily_context(max_chars: int = 0) -> str:
    """Read today's and yesterday's daily logs.

    Returns combined content (today first, then yesterday), truncated to
    max_chars. Returns "" if no daily logs exist for either day.
    """
    today = date.today()
    yesterday = today - timedelta(days=1)

    if not max_chars:
        max_chars = PROMPT_DAILY_LOG_TOKENS * 5  # ~5 chars/token

    def _read_daily_log(d: date) -> str:
        path = _daily_log_path(d)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.warning("Failed to read daily log %s: %s", path.name, e)
            return ""

    def _tail_truncate(text: str, budget: int) -> str:
        if len(text) <= budget:
            return text
        if budget <= 4:
            return text[-budget:]
        return "...\n" + text[-(budget - 4):]

    today_content = _read_daily_log(today)
    yesterday_content = _read_daily_log(yesterday)

    if not today_content and not yesterday_content:
        return ""

    separator = "\n\n---\n\n"

    if today_content and len(today_content) >= max_chars:
        return _tail_truncate(today_content, max_chars)

    if today_content:
        parts = [today_content]
        used = len(today_content)
    else:
        parts = []
        used = 0

    if yesterday_content:
        budget = max_chars - used
        if parts:
            budget -= len(separator)
        if budget > 0:
            parts.append(_tail_truncate(yesterday_content, budget))

    combined = separator.join(parts) if parts else _tail_truncate(yesterday_content, max_chars)
    if len(combined) > max_chars:
        return _tail_truncate(combined, max_chars)
    return combined


# ── Layer 3: TF-IDF search (existing) — see search_memory / get_context_for_task


# ── Layer 4: Lessons Learned ─────────────────────────────────────────────────

_LESSONS_FILE = _MEMORY_ROOT / "lessons.md"


def get_lessons_context(tool_name: str | None = None, max_chars: int = 2000) -> str:
    """Read lessons.md and return entries, optionally filtered by tool name.

    Returns relevant lesson entries as a string block for prompt injection.
    Each entry has Pattern + Tool-Hint fields (no Fix field).
    """
    if not _LESSONS_FILE.exists():
        return ""
    try:
        content = _LESSONS_FILE.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("Failed to read lessons.md: %s", e)
        return ""

    if not content:
        return ""

    # Strip HTML comments (used for format templates)
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL).strip()

    # Parse sections (## headings)
    sections = re.split(r"^(## .+)$", content, flags=re.MULTILINE)
    entries: list[str] = []

    for i in range(1, len(sections), 2):
        heading = sections[i].strip()
        body = sections[i + 1].strip() if i + 1 < len(sections) else ""
        if not body:
            continue
        # Filter by tool name if specified
        if tool_name and f"| {tool_name} |" not in heading.lower() and tool_name not in heading.lower():
            continue
        entries.append(f"{heading}\n{body}")

    if not entries:
        return ""

    result = "\n\n".join(entries)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n..."
    return result


def search_lessons(query: str, max_results: int = 3) -> str:
    """Search lessons.md for entries matching a query (keyword-based).

    Returns matching lesson entries as a string for prompt injection.
    """
    if not _LESSONS_FILE.exists():
        return ""

    try:
        content = _LESSONS_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""

    # Strip HTML comments
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL).strip()

    query_tokens = _tokenize(query)
    if not query_tokens:
        return ""

    sections = re.split(r"^(## .+)$", content, flags=re.MULTILINE)
    scored: list[tuple[float, str]] = []

    for i in range(1, len(sections), 2):
        heading = sections[i].strip()
        body = sections[i + 1].strip() if i + 1 < len(sections) else ""
        if not body:
            continue
        entry_text = f"{heading}\n{body}"
        entry_tokens = _tokenize(entry_text)
        sim = _tfidf_sim(query_tokens, entry_tokens)
        if sim > 0:
            scored.append((sim, entry_text))

    scored.sort(key=lambda x: x[0], reverse=True)
    matches = [text for _, text in scored[:max_results]]
    return "\n\n".join(matches) if matches else ""


def append_lesson(
    tool_name: str,
    cwd: str,
    pattern: str,
    tool_hint: str,
) -> bool:
    """Append a new lesson entry to lessons.md.

    Called by create_lesson_from_loop() when a tool loop takes >1 iteration.
    Skips entries without a real project name (empty cwd) and deduplicates
    by pattern fingerprint to prevent spam from repeated identical loops.
    Returns True on success.
    """
    try:
        _ensure_dirs()
        today = datetime.now().strftime("%Y-%m-%d")
        project = Path(cwd).name if cwd else ""

        # Skip entries without a real project name — cwd="." yields name=""
        if not project or project in (".", "unknown"):
            logger.debug("Lesson skipped: no real project name for cwd=%r", cwd)
            return False

        entry = (
            f"\n## {today} | {tool_name} | {project}\n"
            f"- **Pattern:** {pattern}\n"
            f"- **Tool-Hint:** {tool_hint}\n"
        )

        with _lessons_lock:
            # Dedup: skip if the same pattern fingerprint already exists in the file
            pattern_fp = pattern[:120]
            if _LESSONS_FILE.exists():
                try:
                    content = _LESSONS_FILE.read_text(encoding="utf-8")
                    if pattern_fp in content:
                        logger.debug("Lesson skipped (duplicate pattern): %s | %s", tool_name, project)
                        return False
                except OSError as e:
                    logger.debug("Lesson dedup read failed (writing anyway): %s", e)
                with open(_LESSONS_FILE, "a", encoding="utf-8") as f:
                    f.write(entry)
            else:
                _LESSONS_FILE.write_text(
                    f"# Lessons Learned\n{entry}",
                    encoding="utf-8",
                )

        logger.info("Lesson appended: %s | %s | %s", today, tool_name, project)
        return True
    except Exception as e:
        logger.warning("Failed to append lesson: %s", e)
        return False


_LESSON_SUMMARIZER_PROMPT = """
Analyze the following tool execution history and extract a generalized 'Lesson Learned'.
Identify the root cause of issues encountered (repeated failures, loops) and the key patterns
that eventually led to success.

Avoid project-specific details like specific filenames, line numbers, or variable names
unless they represent a generic pattern (e.g. 'unquoted paths in shell').

ORIGINAL TASK:
{task}

EXECUTION HISTORY:
{history}

Output exactly in this format:
Pattern: [Generalized description of the problem/pattern encountered]
Tool-Hint: [Concise advice for an AI agent to handle this better next time]
"""


def create_lesson_from_loop(
    tool_name: str,
    task: str,
    all_outputs: list[str],
    provider,
    cwd: str | None = None,
) -> bool:
    """Use an LLM to summarize tool execution history into a lesson.
    
    If the summarization is successful, appends it to lessons.md.
    Returns True if a lesson was created and stored.
    """
    if not all_outputs:
        return False
    
    # Combine history, focusing on first and last iterations if too long
    # (Total budget ~4000 chars for history)
    if len(all_outputs) > 4:
        truncated_history = (
            all_outputs[0] + 
            "\n\n[... middle iterations omitted ...]\n\n" + 
            "\n\n".join(all_outputs[-2:])
        )
    else:
        truncated_history = "\n\n".join(all_outputs)
        
    if len(truncated_history) > 6000:
        truncated_history = truncated_history[:3000] + "\n\n[...]\n\n" + truncated_history[-3000:]

    prompt = _LESSON_SUMMARIZER_PROMPT.format(
        task=task,
        history=truncated_history
    )

    try:
        # Best effort: use the provided provider to summarize.
        # Use a shorter timeout as this is a background housekeeping task.
        result = provider.run(prompt, timeout=120, read_only=True)
        if not result.success:
            logger.debug("Lesson summarization failed: %s", result.error)
            return False
        
        output = result.output.strip()
        pattern_match = re.search(r"^Pattern:\s*(.+)$", output, re.MULTILINE | re.IGNORECASE)
        hint_match = re.search(r"^Tool-Hint:\s*(.+)$", output, re.MULTILINE | re.IGNORECASE)
        
        if not pattern_match or not hint_match:
            logger.debug("Lesson summarizer output format mismatch")
            return False
            
        pattern = pattern_match.group(1).strip()
        tool_hint = hint_match.group(1).strip()
        
        return append_lesson(tool_name, cwd or ".", pattern, tool_hint)
    except Exception as e:
        logger.warning("create_lesson_from_loop failed: %s", e)
        return False


# ── Archival & Cleanup ────────────────────────────────────────────────────────

def archive_old_memories() -> int:
    """Move task_results/*.md older than MEMORY_MAX_AGE_DAYS to archive/, then delete
    archive entries older than MEMORY_ARCHIVE_DELETE_DAYS. Also cleans up daily logs
    and lessons.md. Returns count of archived files. Never raises.
    Runs at most once per calendar day.
    """
    global _archive_last_run_date
    today = datetime.now().date()
    if _archive_last_run_date == today:
        return 0

    archived = 0
    try:
        _ensure_dirs()
        archive_cutoff = datetime.now() - timedelta(days=MEMORY_MAX_AGE_DAYS)

        # Move task_results → archive
        for path in list(_TASK_RESULTS_DIR.glob("*.md")):
            try:
                mem = _parse_memory_file(path)
                ts = mem["timestamp"] if mem else datetime.fromtimestamp(path.stat().st_mtime)
                if ts < archive_cutoff:
                    dest = _ARCHIVE_DIR / path.name
                    counter = 1
                    while dest.exists():
                        dest = _ARCHIVE_DIR / f"{path.stem}_{counter}.md"
                        counter += 1
                    shutil.move(str(path), str(dest))
                    archived += 1
                    logger.debug("Archived memory: %s", path.name)
            except Exception as e:
                logger.warning("Archive failed for %s: %s", path.name, e)

        # Delete old archive entries
        _cleanup_archive()

        # Delete old daily logs
        _cleanup_daily_logs()

        # Prune old lessons
        _cleanup_lessons()
    except Exception as e:
        logger.warning("archive_old_memories failed: %s", e)
    finally:
        _archive_last_run_date = today

    return archived


def _cleanup_archive() -> int:
    """Delete archive/*.md files older than MEMORY_ARCHIVE_DELETE_DAYS. Returns count deleted."""
    if not _ARCHIVE_DIR.exists():
        return 0
    delete_cutoff = datetime.now() - timedelta(days=MEMORY_ARCHIVE_DELETE_DAYS)
    deleted = 0
    for path in list(_ARCHIVE_DIR.glob("*.md")):
        try:
            mem = _parse_memory_file(path)
            ts = mem["timestamp"] if mem else datetime.fromtimestamp(path.stat().st_mtime)
            if ts < delete_cutoff:
                path.unlink()
                deleted += 1
                logger.debug("Deleted archive: %s", path.name)
        except Exception as e:
            logger.warning("Archive delete failed for %s: %s", path.name, e)
    return deleted


def _cleanup_daily_logs() -> int:
    """Delete daily log files older than MEMORY_DAILY_LOG_RETENTION_DAYS. Returns count deleted."""
    if not _DAILY_DIR.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=MEMORY_DAILY_LOG_RETENTION_DAYS)
    deleted = 0
    for path in list(_DAILY_DIR.glob("Memory ????-??-??.md")):
        try:
            date_str = path.stem[len("Memory "):]
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff:
                path.unlink()
                deleted += 1
                logger.debug("Deleted daily log: %s", path.name)
        except Exception as e:
            logger.warning("Daily log cleanup failed for %s: %s", path.name, e)
    return deleted


def _cleanup_lessons() -> int:
    """Remove lessons.md entries older than MEMORY_LESSONS_RETENTION_DAYS. Returns count removed."""
    with _lessons_lock:
        if not _LESSONS_FILE.exists():
            return 0
        try:
            content = _LESSONS_FILE.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Failed to read lessons.md for cleanup: %s", e)
            return 0

        cutoff = datetime.now() - timedelta(days=MEMORY_LESSONS_RETENTION_DAYS)

        # Split into header + sections at "## YYYY-MM-DD ..." headings
        section_re = re.compile(r"^(## \d{4}-\d{2}-\d{2}.*)$", re.MULTILINE)
        parts = section_re.split(content)
        header = parts[0]

        kept: list[str] = []
        removed = 0
        for i in range(1, len(parts), 2):
            heading = parts[i]
            body = parts[i + 1] if i + 1 < len(parts) else ""
            # heading is guaranteed to start with "## YYYY-MM-DD" by section_re
            try:
                entry_date = datetime.strptime(heading[3:13], "%Y-%m-%d")
                if entry_date < cutoff:
                    removed += 1
                    continue
            except ValueError:
                pass
            kept.append(heading + body)

        if removed == 0:
            return 0

        try:
            new_content = header + "".join(kept)
            _write_bytes_atomic(_LESSONS_FILE, new_content.encode("utf-8"))
            logger.info("Pruned %d old lesson(s) from lessons.md", removed)
        except OSError as e:
            logger.warning("Failed to write pruned lessons.md: %s", e)
            return 0
        return removed
