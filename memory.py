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
    MEMORY_NOOP_MAX_OUTPUT_TOKENS,
    MEMORY_SUMMARY_MAX_CHARS,
    MEMORY_TASK_LABEL_CHARS,
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


# A provider answering "there is no task in this message" produced no work and carries no
# information. Re-injecting such an answer as "relevant past context" turns the memory block
# into a few-shot prompt for the same non-answer — measured 2026-08-19, when 3 of the 5
# injected examples were exactly that and the run did nothing for the third time in a row.
#
# The shape that matters is the REFUSAL, not the words "no task": a legitimate short result
# like "Keine Aufgaben überfällig" (daily-task-status) or its English counterpart "I see no
# tasks overdue today" must survive. So the absence phrase has to sit next to an ADDRESSEE
# marker ("von dir", "in this message") — that combination only occurs when the model is
# telling the user it found nothing to execute.
#
# ADR-002 rejected "heuristic fingerprint detection on the answer text" as a way to judge a
# RUN, on the grounds that it misfires on legitimate follow-up questions. That objection is
# correct and still stands. What happens here is a different thing: the fingerprint decides
# only whether a stored result is worth RE-INJECTING as context. The run verdict stays with
# `#verify:`. That distinction is only worth anything while the pattern is actually precise,
# which is why the anchors below are mandatory on both languages — an unanchored English
# branch discarded 4 of 6 legitimate results in review, i.e. exactly ADR-002's objection.
# Only markers that place the absence IN THE MESSAGE. "von dir" / "from you" were in this
# list and had to go: they mean "concerning you", not "in what you sent me", and every one
# of seven realistic results was discarded because of them — including "Es gab keine
# Aufgabe von dir, die gegen die Leitplanken verstößt". Removing them changed nothing on
# real data: 8 hits across the 206 stored results before and after.
#
# It does NOT fully close the self-deletion case, and saying otherwise would overstate it:
# a SHORT report (≤ MEMORY_NOOP_MAX_OUTPUT_TOKENS) that quotes the complete refusal wording
# including "in dieser Nachricht" is still discarded — measured. Accepted rather than fixed,
# because excluding quoted text would mean parsing intent out of the wording, which is where
# ADR-002's objection to fingerprinting starts. The loss is one memory entry, fail-open.
#
# "in it" only counts at the end of a sentence — as in "…but no concrete task in it." A word
# boundary is not enough, it still fires on "no tasks in it that are overdue", where "in it"
# refers to a file. Missing "no concrete task in it, only background" is the fail-open side.
_ADDRESSEE = (
    r"(?:in (?:dieser|deiner) nachricht|in (?:this|your) message"
    r"|in it(?=\s*[.!?]|\s*$))"
)
_NOUN_DE = r"(?:konkrete[nr]?\s+)?(?:frage\s+oder\s+)?(?:aufgabe|anweisung|auftrag)\w*"
_ABSENCE_DE = (
    # No "aufgabenstellung" alternative: "aufgabe" matches first and the trailing \w*
    # swallows the rest, so the longer spelling is covered and a separate branch is dead.
    # "es fehlt eine …" is a second real phrasing of the same refusal, found by an
    # independent sweep of the store (see _is_noninformative for the recall figure).
    rf"kein(?:e|en)?\s+{_NOUN_DE}"
    rf"|(?:es\s+)?fehlt\s+(?:eine|ein)\s+{_NOUN_DE}"
)
_ABSENCE_EN = r"no\s+(?:concrete|actual|specific|explicit)?\s*(?:task|instruction|request|question)s?\b"
_RE_NO_TASK_ANSWER = re.compile(
    rf"(?:{_ABSENCE_DE}|{_ABSENCE_EN})[^.]{{0,60}}?{_ADDRESSEE}"
    rf"|{_ADDRESSEE}[^.]{{0,60}}?(?:{_ABSENCE_DE}|{_ABSENCE_EN})",
    re.IGNORECASE,
)

# Layer 2 (the daily log) stores only the first 80 characters of a result, and the real
# no-op line gets cut mid-phrase: "…, aber keine konkrete …" — the addressee marker that
# _RE_NO_TASK_ANSWER needs is gone, so that pattern alone cannot see it (measured). This
# one recognises the truncated stump instead, and only the stump: the ellipsis anchor at
# the end is what keeps it from matching prose that merely says "keine konkrete Aussage".
_RE_NO_TASK_TRUNCATED = re.compile(
    r"(?:kein(?:e|en)?\s+konkrete[nr]?|no\s+(?:concrete|specific))\s*…\s*$",
    re.IGNORECASE,
)

# Only the opening of a result is searched. A long report may legitimately quote such a
# sentence somewhere in the middle; the refusal, if it is one, is the first thing said.
_NO_TASK_SCAN_CHARS = 400

# Written into a daily-log entry's status line when the run was recognised as a no-op, and
# the sole signal the read side filters on. Deliberately human-readable: the daily note is
# what a person scrolls to see what ran overnight, and for queue lines without a `#verify:`
# tag it is the only place this failure class is visible at all.
_NOOP_MARKER = "Leerlauf (nicht injiziert)"


def _looks_like_no_task_answer(text: str) -> bool:
    """True when `text` opens with a "there is no task in this message" refusal."""
    if not text:
        return False
    # Collapse whitespace so a line break inside the phrase does not defeat the proximity
    # window between the absence phrase and the addressee marker.
    opening = " ".join(text[:_NO_TASK_SCAN_CHARS].split())
    return bool(_RE_NO_TASK_ANSWER.search(opening))


# A #parallel aggregate (parallel_runner.format_parallel_result) is a series of
# "**Subtask N** (provider): STATUS — …" blocks. It has to be judged per block, not as one
# text: with two subtasks the second one's refusal still falls inside the 400-char scan
# window, so one lazy subtask would discard the other's real work from memory.
_RE_SUBTASK_BLOCK = re.compile(r"(?m)^\*\*Subtask \d+\*\*")


def _is_noninformative(summary: str, output_tokens: int = 0) -> bool:
    """True when a stored task result is a "I found no task" non-answer (layer 3).

    Requires BOTH the refusal wording AND a small output. Either alone is not enough:
    substantive reports discuss this very failure mode (this repo's own do), and plenty of
    real results are short.

    ``output_tokens == 0`` means "unknown", not "short", and returns False — declining to
    judge is the fail-open direction. That case is NOT exotic: `codex` and `vibe` report no
    token counts at all (vibe says so in its own docstring), so on layer 3 this filter is
    effectively Claude-only, and a fallback run under Claude quota pressure lands exactly
    there. The daily-log layer covers those through its own, token-free rule.

    A character-length fallback cannot stand in for the missing count: `store_result` caps
    every body at MEMORY_SUMMARY_MAX_CHARS (700, measured maximum across the live store:
    705), so any threshold above that is unconditionally true and adds no second condition.

    RECALL, honestly stated: **9 of 13**. The figure once quoted here — "all eight known
    no-ops" — was circular: that set had been collected with this very pattern. An
    independent sweep (runtime under 30 s plus a closing question back to the user) turns
    up five more runs of the same class, one of them in `task_results/`, i.e. in the
    active injection pool. One of the five was recoverable and is now covered:

      • "Es fehlt eine konkrete Aufgabe in deiner Nachricht" — covered, phrasing added
        to _ABSENCE_DE.
      • "Guten Morgen! … Was möchtest Du als nächstes tun?"    ┐ no absence phrase at all;
      • "Ich bin bereit … Gib mir eine konkrete Aufgabe"        │ a different fingerprint,
      • "Kontext geladen. Ich sehe: …"                          │ deliberately NOT chased
      • "Deine Nachricht endet mitten im Satz … ohne Frage"    ┘ with a wider regex.

    Widening the pattern to catch a cheerful "I'm ready, what would you like?" would mean
    matching on tone rather than on a stated absence, which is where ADR-002's objection
    starts to bite. Those runs are the job of `#verify:`, which checks the artefact instead
    of the wording — that division of labour is the point, not a gap in it.
    """
    if not (0 < output_tokens <= MEMORY_NOOP_MAX_OUTPUT_TOKENS):
        return False

    blocks = _RE_SUBTASK_BLOCK.split(summary)
    if len(blocks) > 1:  # at least one subtask marker → an aggregate, not a plain answer
        # Only the part after the block's own header line: that header carries a 60-char
        # preview of the SUBTASK TEXT (parallel_runner.format_parallel_result), and in this
        # repo a queue line may well quote the refusal wording itself.
        bodies = [b.partition("\n")[2].strip() for b in blocks[1:]]
        bodies = [b for b in bodies if b]
        # `all()` over an empty sequence is True — an aggregate of nothing but empty blocks
        # would otherwise count as a no-op.
        return bool(bodies) and all(_looks_like_no_task_answer(b) for b in bodies)
    return _looks_like_no_task_answer(summary)


def _task_label(task: str) -> str:
    """Shorten a past task to a LABEL, visibly marked as an excerpt.

    Used by both injection layers. The quoted task used to run to 80 chars in the history
    block and 120 in the daily log, which for a long queue line reproduced the opening of
    the very task being dispatched — and the real "## Aufgabe" section then read as the
    next entry of the same list. The trailing "…" is what says "excerpt" rather than
    "complete instruction", so a hard cut without it recreates half the problem.
    """
    label = task[:MEMORY_TASK_LABEL_CHARS].rstrip()
    return f"{label} …" if len(task) > MEMORY_TASK_LABEL_CHARS else label


def _is_noop_log_entry(summary_line: str) -> bool:
    """True when a daily-log summary line (layer 2) is a no-op refusal.

    Separate from _is_noninformative because layer 2 has neither a token count nor the full
    text — just 80 characters and an ellipsis. The rule is therefore looser, and that is a
    deliberate asymmetry: dropping a daily-log line costs a two-day rolling prompt buffer
    entry, while the authoritative record stays in task_results/. Dropping a task_results
    entry would lose the memory itself, so that side keeps the strict rule.
    """
    if not summary_line:
        return False
    line = " ".join(summary_line.split())
    return bool(_RE_NO_TASK_TRUNCATED.search(line)) or _looks_like_no_task_answer(line)


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
    duration_sec, timestamp, success, summary, output_tokens, noninformative, path."""
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

    try:
        output_tokens = int(meta.get("output_tokens", 0) or 0)
    except ValueError:
        output_tokens = 0

    return {
        "task": meta.get("task", ""),
        "provider": meta.get("provider", ""),
        "cwd": meta.get("cwd", ""),
        "duration_sec": float(meta.get("duration_sec", 0) or 0),
        "timestamp": ts,
        "success": meta.get("success", "true").lower() not in ("false", "0"),
        "summary": body,
        "output_tokens": output_tokens,
        # Judged at read time, not at write time: that way the filter also covers the
        # no-op runs already sitting in the store without a migration or a new frontmatter
        # field, and store_result never has to overrule its callers.
        "noninformative": _is_noninformative(body, output_tokens),
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
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> Optional[Path]:
    """Write a task result to memory/task_results/.

    Token fields are written into the frontmatter for billing analytics
    (analytics.py reads them). Default 0 keeps backward compatibility for
    callers that don't track tokens (e.g. parallel aggregation).

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
            f"input_tokens: {input_tokens}\n"
            f"output_tokens: {output_tokens}\n"
            f"cache_creation_input_tokens: {cache_creation_input_tokens}\n"
            f"cache_read_input_tokens: {cache_read_input_tokens}\n"
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
        append_daily_log(
            task, result, provider, duration_sec, cwd=cwd, success=success,
            output_tokens=output_tokens,
        )

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

    Entries classified as non-informative (`_is_noninformative` — a run that answered it
    found no task) are excluded, so they cannot occupy one of the top_k slots.
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
        # Dropped here, not in get_context_for_task: a no-op that merely loses its slot
        # later would still have consumed one of the top_k, so the prompt would carry four
        # real memories instead of five.
        if mem["noninformative"]:
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

    # CWD preference: if there are same-CWD results, prefer them over cross-project matches.
    # Only fall through to cross-CWD if same-CWD count is < 2.
    if cwd and results:
        same_cwd, cross_cwd = [], []
        for r in results:
            (same_cwd if r["cwd"] and _paths_match(cwd, r["cwd"]) else cross_cwd).append(r)
        if len(same_cwd) >= 2:
            results = same_cwd
        elif same_cwd:
            results = same_cwd + cross_cwd[: MEMORY_TOP_K - len(same_cwd)]

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
        label = _task_label(mem["task"])
        # Tied to the outcome: a fixed "(erledigt)" behind a ❌ read as "failed (done)".
        outcome = "erledigt" if mem["success"] else "fehlgeschlagen"
        lines.append(
            f"{i}. [{ts}] {status} ({outcome}) {label}\n"
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
        # Same exclusion as in search_memory — this is the fallback path taken when nothing
        # scores above the threshold, which is exactly when a recent no-op would otherwise
        # be the freshest thing in the store and win a slot by recency alone.
        if mem and not mem["noninformative"]:
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
    output_tokens: int = 0,
) -> bool:
    """Append a task entry to today's daily log.

    Format is Obsidian-friendly Markdown. Returns True on success.

    A no-op answer is not written at all. This log is a prompt-injection layer by design
    (see the 80-char cap below), and injecting "the last run answered: there is no task"
    teaches the next run to answer the same. Judged here, where the FULL result is still
    available — by the time the entry is read back it has been cut to 80 characters, which
    removes the very phrase the strict pattern keys on. The authoritative record of the run
    stays in task_results/ either way, so nothing is lost, only un-injected.
    """
    try:
        _ensure_dirs()
        today = date.today()
        path = _daily_log_path(today)
        now = datetime.now()
        ts = now.strftime("%H:%M")
        status = "success" if success else "failed"

        # Daily log: first line only, max 80 chars — keeps log compact for prompt injection
        first_line = result.partition("\n")[0].strip()
        summary = (first_line[:80] + "…") if len(first_line) > 80 else first_line

        # Three rules, and each covers a hole the others leave:
        #  - the full-text rule has the most signal (8 of 8 real no-ops vs. 3 of 8 for the
        #    80-char view) but is capped at MEMORY_NOOP_MAX_OUTPUT_TOKENS;
        #  - the truncation stump runs regardless of that cap — without it a no-op just
        #    above the cap passed both layers, which was a regression, not an old state;
        #  - the full loose rule only where there is no token count at all (codex, vibe).
        #    Letting it run unconditionally made it overrule a justified "no": a
        #    40 000-token report whose first line quotes the refusal wording disappeared.
        if _is_noninformative(result, output_tokens) or _RE_NO_TASK_TRUNCATED.search(summary):
            noop = True
        elif not output_tokens and _looks_like_no_task_answer(summary):
            noop = True
        else:
            noop = False

        # A recognised no-op is MARKED, not dropped. Dropping it kept the prompt clean and
        # made the failure class less visible than before this fix: the daily note is where
        # a human looks to see what ran overnight, and for the three recurring queue lines
        # without a `#verify:` tag that entry is the only symptom there is. The read side
        # filters on this marker, so the prompt stays clean either way — and the marker,
        # unlike a token count, is written by the same decision that read side honours.
        if noop:
            status += f" · {_NOOP_MARKER}"
            logger.warning("No-op answer detected, not injected: %s", task[:60])

        # Same label helper as the layer-3 history block. At 120 chars and without the
        # excerpt marker this heading reproduced more of the live queue line than the
        # memory block it was meant to complement, and read as a complete instruction.
        entry = (
            f"\n## {ts} — {_task_label(task)}\n"
            f"- **Provider:** {provider}\n"
        )
        if cwd:
            entry += f"- **CWD:** {cwd}\n"
        entry += f"- **Duration:** {duration_sec:.0f}s\n- **Status:** {status}\n"
        # Two distinct signals, and both are needed:
        #  - the status marker above says "this IS a no-op" (for the human, and for the
        #    read side to drop it);
        #  - this line says "this entry was judged when it was written", which is what lets
        #    the read side leave a KEPT entry alone. Without it an entry the write side
        #    deliberately kept was re-judged on 80 characters and dropped anyway — present
        #    in the file, never in a prompt.
        # Absent for providers that report no counts (codex, vibe); there the read side
        # applies the same loose rule the write side used, so the two still agree.
        if output_tokens:
            entry += f"- **Tokens:** {output_tokens}\n"
        entry += f"- {summary}\n"

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


def _strip_noop_entries(log_text: str) -> str:
    """Drop no-op entries from a daily log's text.

    The write path already refuses to add them, but entries written before that existed
    are still inside the two-day window this log feeds into the prompt — the one from
    2026-08-19 08:58 would have been handed to the next morning's briefing as "yesterday".
    Filtering on read covers those without rewriting the file, which stays the honest
    record of what ran.

    An entry is `## HH:MM — task` followed by its `- ` bullets; the last bullet holds the
    result summary. Anything before the first `## ` (the `# Memory <date>` header) is kept.
    """
    # re.split on a line-anchored "## ", not partition("\n## "): the latter needs a
    # preceding newline, so an entry sitting at the very start of the file — a log written
    # without the "# Memory <date>" header, e.g. after hand-editing in Obsidian — ended up
    # in the head and was kept unexamined.
    parts = re.split(r"(?m)^## ", log_text)
    if len(parts) == 1:
        return log_text

    head = parts[0].rstrip("\n")
    kept = [head] if head.strip() else []
    for chunk in parts[1:]:
        bullets = [ln for ln in chunk.splitlines() if ln.startswith("- ")]
        # The summary is the last bullet; the ones before it are Provider/CWD/Duration/
        # Status/Tokens.
        summary_line = bullets[-1][2:] if bullets else ""
        # An entry the write side already recognised says so in its status line. That verdict
        # was reached on the full result — far more signal than 80 characters allow — so it
        # is honoured as-is rather than re-derived here. Entries without the marker are the
        # backlog written before it existed; those are what the loose rule is for.
        if any(_NOOP_MARKER in ln for ln in bullets):
            continue
        # No `Tokens:` line means the entry predates this mechanism (or came from a provider
        # without counts) — only then does the read side form its own opinion. An entry that
        # WAS judged is left alone; re-deriving the verdict from 80 characters would drop
        # results the write side deliberately kept.
        already_judged = any(ln.startswith("- **Tokens:**") for ln in bullets)
        if not already_judged and _is_noop_log_entry(summary_line):
            continue
        # Entries written before _task_label existed still carry up to 120 characters of
        # the queue line in their heading — the very thing the label was introduced against.
        # Shorten on read as well, so the backlog does not keep feeding it into prompts for
        # two more days.
        head_line, _, body = chunk.partition("\n")
        ts_part, sep, task_part = head_line.partition(" — ")
        if sep and len(task_part) > MEMORY_TASK_LABEL_CHARS:
            head_line = f"{ts_part}{sep}{_task_label(task_part)}"
        kept.append("## " + (f"{head_line}\n{body}" if body else head_line).rstrip("\n"))

    # Nothing but the header left: return empty so `if daily:` in _build_prompt stays false
    # and the prompt does not carry an empty "## Heutiger Verlauf" section.
    if not any(part.startswith("## ") for part in kept):
        return ""

    return "\n\n".join(kept).strip()


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
            return _strip_noop_entries(path.read_text(encoding="utf-8").strip())
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


def get_lessons_context(
    tool_name: str | None = None,
    cwd: str | None = None,
    max_chars: int = 2000,
) -> str:
    """Read lessons.md and return entries, filtered by tool name and CWD.

    Returns relevant lesson entries as a string block for prompt injection.
    Each entry has Pattern + Tool-Hint fields (no Fix field).

    Filtering logic:
    - Entries with ``| * |`` as project match all CWDs (universal lessons).
    - Entries with a specific project name match only when CWD contains that name.
    - tool_name filter applies independently (AND with CWD filter).
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
        # Filter by CWD: "| * |" = universal, otherwise project name must appear in CWD
        if cwd:
            # Extract project field from heading: "## DATE | tool | PROJECT"
            parts = [p.strip() for p in heading.lstrip("#").strip().split("|")]
            if len(parts) >= 3:
                project = parts[2]
                if project != "*" and project.lower() not in cwd.lower():
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


_LESSON_DEDUP_THRESHOLD = 0.45  # TF-IDF similarity above this = duplicate


def append_lesson(
    tool_name: str,
    cwd: str,
    pattern: str,
    tool_hint: str,
    universal: bool = False,
) -> bool:
    """Append a new lesson entry to lessons.md.

    Called by create_lesson_from_loop() when a tool loop takes >1 iteration.
    Deduplicates by TF-IDF similarity against existing patterns.
    Uses ``*`` as project when ``universal=True`` (pattern is not project-specific).
    Returns True on success.
    """
    try:
        _ensure_dirs()
        today = datetime.now().strftime("%Y-%m-%d")

        if universal:
            project = "*"
        else:
            project = Path(cwd).name if cwd else ""
            # Skip entries without a real project name
            if not project or project in (".", "unknown"):
                logger.debug("Lesson skipped: no real project name for cwd=%r", cwd)
                return False

        entry = (
            f"\n## {today} | {tool_name} | {project}\n"
            f"- **Pattern:** {pattern}\n"
            f"- **Tool-Hint:** {tool_hint}\n"
        )

        new_tokens = _tokenize(pattern)

        with _lessons_lock:
            if _LESSONS_FILE.exists():
                try:
                    content = _LESSONS_FILE.read_text(encoding="utf-8")
                except OSError as e:
                    logger.debug("Lesson dedup read failed (writing anyway): %s", e)
                    content = ""

                # Semantic dedup: check TF-IDF similarity against existing patterns
                if content:
                    existing = re.findall(
                        r"^\- \*\*Pattern:\*\*\s*(.+)$", content, re.MULTILINE
                    )
                    for ex_pattern in existing:
                        ex_tokens = _tokenize(ex_pattern)
                        sim = _tfidf_sim(new_tokens, ex_tokens)
                        if sim >= _LESSON_DEDUP_THRESHOLD:
                            logger.debug(
                                "Lesson skipped (similar %.2f): %s",
                                sim, pattern[:80],
                            )
                            return False

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

Rules:
- Avoid project-specific details (filenames, line numbers, variable names).
- Keep Pattern to 1-2 sentences, Tool-Hint to 2-3 sentences. Be concise.
- Set Universal to YES if the pattern applies to any codebase (not just this project).
  Set to NO only if it depends on project-specific tech/domain.

ORIGINAL TASK:
{task}

EXECUTION HISTORY:
{history}

Output exactly in this format (3 lines, no extras):
Pattern: [Generalized description of the problem/pattern encountered]
Tool-Hint: [Concise advice for an AI agent to handle this better next time]
Universal: [YES or NO]
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
        universal_match = re.search(r"^Universal:\s*(YES|NO)\b", output, re.MULTILINE | re.IGNORECASE)

        if not pattern_match or not hint_match:
            logger.debug("Lesson summarizer output format mismatch")
            return False

        pattern = pattern_match.group(1).strip()
        tool_hint = hint_match.group(1).strip()
        universal = bool(universal_match and universal_match.group(1).upper() == "YES")

        return append_lesson(tool_name, cwd or ".", pattern, tool_hint, universal=universal)
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
