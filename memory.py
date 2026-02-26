"""
AI Orchestrator — Persistent Memory System

Stores task results as Markdown files in the vault and retrieves relevant
past context for new tasks using TF-IDF similarity with temporal decay.

Storage layout:
    VAULT_PATH/99_System/AI/memory/
        task_results/   ← one .md per completed task
        error_patterns/ ← reserved
        preferences/    ← reserved
        archive/        ← memories older than MEMORY_MAX_AGE_DAYS

File format:
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config import (
    MEMORY_HALF_LIFE_DAYS,
    MEMORY_MAX_AGE_DAYS,
    MEMORY_SUMMARY_MAX_CHARS,
    MEMORY_TOP_K,
    VAULT_PATH,
)

logger = logging.getLogger(__name__)

# Root memory directory inside vault
_MEMORY_ROOT = VAULT_PATH / "99_System" / "AI" / "memory"
_TASK_RESULTS_DIR = _MEMORY_ROOT / "task_results"
_ARCHIVE_DIR = _MEMORY_ROOT / "archive"

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
    for d in (_TASK_RESULTS_DIR, _ARCHIVE_DIR,
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
    """Truncate to MEMORY_SUMMARY_MAX_CHARS (first 500 + last 200)."""
    first = MEMORY_SUMMARY_MAX_CHARS - 200
    if len(text) <= MEMORY_SUMMARY_MAX_CHARS:
        return text
    return text[:first] + "\n...\n" + text[-200:]


def _tokenize(text: str) -> set[str]:
    """Lowercase, extract alphanumeric words ≥3 chars, remove stopwords."""
    words = re.findall(r"[a-zA-ZäöüÄÖÜß0-9]{3,}", text.lower())
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

    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    frontmatter_raw = parts[1].strip()
    body = parts[2].strip()

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
        if cwd and mem["cwd"] and Path(cwd).resolve() == Path(mem["cwd"]).resolve():
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
    - Generic tasks with no keyword match: use N most recent from same CWD,
      or N most recent overall.
    - Returns "" if no memories found.
    """
    results = search_memory(task_text, cwd=cwd)

    if not results:
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


def archive_old_memories() -> int:
    """Move task_results/*.md files older than MEMORY_MAX_AGE_DAYS to archive/.

    Returns count of archived files. Never raises.
    """
    try:
        _ensure_dirs()
        cutoff = datetime.now() - timedelta(days=MEMORY_MAX_AGE_DAYS)
        archived = 0

        for path in list(_TASK_RESULTS_DIR.glob("*.md")):
            try:
                mem = _parse_memory_file(path)
                ts = mem["timestamp"] if mem else datetime.fromtimestamp(path.stat().st_mtime)
                if ts < cutoff:
                    dest = _ARCHIVE_DIR / path.name
                    if dest.exists():
                        dest = _ARCHIVE_DIR / f"{path.stem}_dup.md"
                    shutil.move(str(path), str(dest))
                    archived += 1
                    logger.debug("Archived memory: %s", path.name)
            except Exception as e:
                logger.warning("Archive failed for %s: %s", path.name, e)

        return archived
    except Exception as e:
        logger.warning("archive_old_memories failed: %s", e)
        return 0
