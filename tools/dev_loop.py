"""
Dev-Loop Tool: Research → Execute → Dual-Review (Quality + Resolution) → Iterate.

Three-phase workflow:
  1. Research Agent:   Analyzes codebase, creates implementation plan, web-searches only if needed.
  2. Execution Agent:  Implements the solution (re-runs with review context on each iteration).
  3a. Quality Review:  Checks correctness, security, performance, maintainability, etc. (P1/P2/P3).
  3b. Resolution Review: Checks only "does the code solve the original task 100%?" (RESOLVED/PARTIAL/UNRESOLVED).

Loop continues until BOTH reviews pass. No auto-push.
Output is written to {cwd}/.dev-loop/<task-hash>/ for traceability — one
subdirectory per distinct task text, so two dev-loops in the same repo cannot
overwrite each other's plan, round files, summary or state.

Usage in queue:
    - [ ] Fix login bug in auth.py #tool:dev-loop cwd:/d/programmieren/projekt
    - [ ] Add CSV export to dashboard #tool:dev-loop cwd:/d/programmieren/projekt
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

from config import (
    TOOL_DEV_EXEC_TIMEOUT_SEC,
    TOOL_DEV_PLAN_TIMEOUT_SEC,
    TOOL_DEV_QUALITY_REVIEW_TIMEOUT_SEC,
    TOOL_DEV_RESEARCH_TIMEOUT_SEC,
    TOOL_DEV_RESOLUTION_REVIEW_TIMEOUT_SEC,
    TOOL_INTER_STEP_SLEEP_SEC,
    TOOL_LANDING_MIN_PHASE_SEC,
    TOOL_LANDING_RESERVE_SEC,
    TOOL_MAX_ITERATIONS,
)
from limits import is_cached_provider_available
from notifier import notify_tool_done, notify_tool_progress
from providers.base import BaseProvider, error_code_of, is_transient
from tools.base_tool import BaseTool, SessionContext, TokenCounter, ToolResult, ToolTracer, _build_system_prompt, _make_capacity_exhausted_result, _write_tool_file
from tools.review_loop import (
    ROUND_REFLECTION_INSTRUCTION,
    KNOWN_LIMITS_REVIEW_BLOCK,
    KNOWN_LIMITS_RESOLUTION_BLOCK,
    _is_clean_output,
    _parse_findings,
    format_known_limits,
    is_deferred_known_limit,
    parse_known_limits,
    release_escalated_known_limits,
    strip_p3_lines,
    validate_known_limits,
)

# The PARENT directory, deliberately kept as-is. Per-task output goes one level
# deeper (_run_dir below) rather than into a sibling `.dev-loop-<hash>`, because
# ToolTracer.create() derives its own path from the TOOL NAME — `{cwd}/.dev-loop/
# traces/` — and analytics._parse_tool_traces() globs `**/.*/traces/*.jsonl` to
# attribute traces to a tool. Renaming the parent would move every trace file and
# silently change that attribution; nesting leaves it untouched.
DEV_LOOP_DIR = ".dev-loop"
_STATE_FILE = "state.json"

# state.json schema version.
#   1 (implicit, no key) — the original four-key research cache: tool, task_hash,
#     phase, research_and_plan. Written once, after Research+Plan.
#   2 — additionally carries the iteration checkpoint that lets a run parked by an
#     exhausted quota CONTINUE instead of restarting from iteration 1.
# Version 1 files stay readable and are treated as "research cache only": a run
# that finds one skips Research+Plan exactly as before and starts at iteration 1.
# That is what makes this change safe for a state file written by the currently
# running orchestrator before the upgrade.
_STATE_VERSION = 2

# The ONLY park reason that makes a checkpoint resumable. Stored in state.json so
# "may this run continue?" is a recorded fact and not an inference from file
# contents. Without it every progress checkpoint would also license a resume, which
# would silently turn this into the general retry/continuation mechanism for hangs,
# format errors and process crashes that the Auftrag excludes.
_PARK_CAPACITY = "capacity_exhausted"

# How often a single task may be parked for capacity AND still have its recorded
# budget capped so the next run can act (see `_park_budget`). Past this the cap is
# dropped and the budget is recorded truthfully, so the run reaches its deadline and
# ends terminally instead of cycling.
#
# The bound is needed because `next_iteration` does NOT advance on a park — a park in
# iteration N records N so that iteration is repeated, so a task whose quota dies in
# the same iteration every time is a fixed point that TOOL_MAX_ITERATIONS never
# reaches. Five is a budget: each cycle costs one real quota exhaustion plus one
# minimal landing round, so this caps the pathological case at ~20 extra minutes
# rather than leaving it open-ended.
_MAX_CAPACITY_PARKS = 5

# Wall-clock `memory.create_lesson_from_loop` spends on its own provider call
# (memory.py runs it with timeout=120). Mirrored here because that call sits in
# another module and therefore outside `_landing_cap`; the loop skips it when less
# than this is left, so it cannot silently extend the documented overrun bound.
_LESSON_CALL_SEC = 120


def _task_hash(task: str) -> str:
    return hashlib.sha256(task.encode()).hexdigest()[:8]


def _run_dir(cwd: str | None, task_hash: str) -> Path:
    """Output directory for ONE dev-loop run: ``{cwd}/.dev-loop/<task-hash>/``.

    The hash was computed and stored INSIDE state.json since the beginning but
    never reached the path, so the directory was keyed by cwd alone: two
    dev-loops in one repo overwrote each other's research-and-plan.md,
    round-00N.md, summary.md and state.json. Keying by task identity is what the
    hash was always for.

    Collision behaviour is unchanged by this: 8 hex chars, and _load_state()
    already gated the cached plan on the same value. Identical task text
    resuming into the same directory is the intended behaviour, not an accident.
    """
    return Path(cwd or ".") / DEV_LOOP_DIR / task_hash


def _legacy_state_path(cwd: str) -> str:
    """Pre-2026-09-09 state location: ``{cwd}/.dev-loop/state.json``.

    Read-only fallback so a run interrupted BY this upgrade still resumes
    instead of silently redoing its research phase. Nothing writes here any more.
    """
    return os.path.join(cwd, DEV_LOOP_DIR, _STATE_FILE)


def _read_state_file(path: str, task_hash: str) -> "dict | None":
    """Parse one state file, returning it only if it belongs to this task+tool.

    Every way a state file can be unusable is treated the same as a missing
    file, because both call sites are load-bearing: _load_state() runs before
    any work starts, and _clear_state() runs immediately before dev-loop returns
    success — an uncaught exception there turns a completed run (plan written,
    reviews passed, work done) into a task reported as failed.

    Three failure shapes, all reachable, none of them decoration:

    * **Not an object.** VALID json that is not a dict (``null``, ``[]``,
      ``"x"``, ``5``) parses fine and then raises AttributeError on ``.get`` —
      past a JSONDecodeError-only guard. Hence the ``isinstance`` check.
    * **Cut inside a multi-byte character.** ``_save_state()`` writes with
      ``ensure_ascii=False`` and truncate-then-write, so a run killed mid-write
      (the watchdog's ``taskkill /F /T``, a crash, a full disk) leaves a file
      ending part-way through a ``—`` or an umlaut — routine characters in plan
      text. That raises **UnicodeDecodeError**, which is NOT a
      ``json.JSONDecodeError``; measured, a state file cut 3 bytes short used to
      escape both call sites uncaught.
    * **Malformed json / unreadable file.** JSONDecodeError, OSError.

    ``ValueError`` is the common base of JSONDecodeError and UnicodeDecodeError,
    so the two-tuple below covers all three shapes without naming a decoder.
    """
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return None
        if state.get("task_hash") == task_hash and state.get("tool") == "dev-loop":
            return state
    except (OSError, ValueError):
        pass
    return None


def _load_state(cwd: str, task_hash: str) -> "dict | None":
    """Load persisted research state if it matches the current task.

    Looks in the per-task directory first, then at the legacy shared location.
    Both paths validate ``task_hash`` and ``tool``, so a legacy file left behind
    by a DIFFERENT task is never consumed.
    """
    state = _read_state_file(str(_run_dir(cwd, task_hash) / _STATE_FILE), task_hash)
    if state is not None:
        return state
    return _read_state_file(_legacy_state_path(cwd), task_hash)


def _save_state(cwd: str, task_hash: str, state: dict) -> None:
    """Persist state to .dev-loop/<task-hash>/state.json (creates dir if needed).

    Writes to a temp file in the same directory and then ``os.replace``s it, which
    is atomic on both Windows and POSIX. Until 2026-09-10 this truncated the real
    file and wrote in place — tolerable while it happened exactly ONCE per run,
    right after Research+Plan. Now that every iteration checkpoints here, that
    truncation window would be hit N times per run instead of once, and a crash
    inside it leaves a half-written file. `_read_state_file` already treats a
    truncated file as missing (that is what its UnicodeDecodeError handling is
    for), so the failure mode was "silently lose the resume point" — survivable
    but exactly what this state exists to prevent. Same pattern as
    tools/scientific_investigation.py:178-189.
    """
    dir_path = _run_dir(cwd, task_hash)
    dir_path.mkdir(parents=True, exist_ok=True)
    target = dir_path / _STATE_FILE
    tmp = dir_path / f"{_STATE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, target)


def _clear_state(cwd: str, task_hash: str) -> None:
    """Remove persisted state after successful completion.

    Also removes the legacy shared file, but ONLY when it belongs to this task —
    deleting another task's resume point would be a regression, and the legacy
    location is by definition shared.
    """
    try:
        os.remove(_run_dir(cwd, task_hash) / _STATE_FILE)
    except OSError:
        pass

    legacy = _legacy_state_path(cwd)
    if _read_state_file(legacy, task_hash) is not None:
        try:
            os.remove(legacy)
        except OSError:
            pass


def _resume_checkpoint(cwd: str | None, task_hash: str) -> "dict | None":
    """Normalised iteration checkpoint from state.json, or None.

    None means "start at iteration 1", and that is the answer for every shape this
    does not fully recognise: no file, a version-1 research cache, a version-2 file
    without a usable `next_iteration`, or any field of the wrong type. Falling back
    to a fresh start is always SAFE (it repeats work); trusting a half-understood
    checkpoint is not (it could skip work that never happened). Every field is
    therefore coerced defensively rather than trusted — the file is on disk in a
    repo a provider has write access to.
    """
    if not cwd:
        return None
    state = _load_state(cwd, task_hash)
    if not isinstance(state, dict) or state.get("state_version") != _STATE_VERSION:
        return None
    # Only a run that PARKED for capacity may be continued. A plain progress record
    # must not hand a later run the reduced budget, the start iteration and the
    # dirty-tree waiver — that would turn this into the general resume mechanism for
    # hangs, format errors, process crashes and manual retries, which the Auftrag
    # rules out under KÜR. The reason is stored rather than inferred because "which
    # write produced this file" is not recoverable from its contents.
    if state.get("park_reason") != _PARK_CAPACITY:
        return None
    try:
        nxt = int(state.get("next_iteration") or 0)
    except (TypeError, ValueError):
        return None
    if nxt < 1 or nxt > TOOL_MAX_ITERATIONS:
        # > max would resume past the loop and return an empty result.
        #
        # `nxt == 1` is explicitly VALID and the guard used to reject it, which was a
        # real defect: a capacity park during iteration 1 writes next_iteration=1 (the
        # iteration must be repeated, not skipped), and rejecting that threw away the
        # whole record — `dirty_paths` (so the worktree gate refused the resume
        # terminally), `elapsed_budget_sec` (so the next run got a fresh full budget,
        # exactly what AI-3 exists to prevent), `deferred_p3` and both loop-detector
        # sets. The iteration number is only one of six things this file carries;
        # "nothing to skip" was never a reason to discard the other five.
        return None
    try:
        elapsed = float(state.get("elapsed_budget_sec") or 0.0)
    except (TypeError, ValueError):
        elapsed = 0.0

    def _strs(key: str) -> list[str]:
        val = state.get(key)
        return [v for v in val if isinstance(v, str)] if isinstance(val, list) else []

    def _str_dict(key: str) -> dict[str, str]:
        val = state.get(key)
        if not isinstance(val, dict):
            return {}
        return {k: v for k, v in val.items() if isinstance(k, str) and isinstance(v, str)}

    def _str_tuple(val: object) -> tuple[str, ...]:
        """A flat tuple of strings, whatever the file actually contained.

        Non-strings are dropped rather than coerced: these tuples go into SETS, and a
        nested list survives `tuple()` as an unhashable element, so `set.update()`
        raises TypeError out of the middle of run(). Measured, not theorised — this
        file sits in a directory the provider under review can write.
        """
        if not isinstance(val, (list, tuple)):
            return ()
        return tuple(v for v in val if isinstance(v, str))

    def _sig_list(key: str) -> list[tuple[str, ...]]:
        val = state.get(key)
        if not isinstance(val, list):
            return []
        return [_str_tuple(v) for v in val]

    def _review_sigs() -> list[tuple[tuple[str, ...], str, str]]:
        val = state.get("seen_review_signatures")
        if not isinstance(val, list):
            return []
        out: list[tuple[tuple[str, ...], str, str]] = []
        for entry in val:
            if not isinstance(entry, (list, tuple)) or len(entry) != 3:
                continue
            findings, verdict, text = entry
            if not isinstance(verdict, str) or not isinstance(text, str):
                continue
            out.append((_str_tuple(findings), verdict, text))
        return out

    prev_res = state.get("previous_resolution_output")

    return {
        "next_iteration": nxt,
        "elapsed_budget_sec": max(0.0, elapsed),
        "previous_quality_findings": _strs("previous_quality_findings"),
        # `or ""` is NOT enough: a truthy non-string (a dict, a list) passes it
        # unchanged and then blows up in the f-string that builds review_context.
        "previous_resolution_output": prev_res if isinstance(prev_res, str) else "",
        "deferred_p3": _strs("deferred_p3"),
        # {finding: reason} accepted BEKANNTE GRENZE deferrals — absent in a file
        # written before this feature, which _str_dict reads as {} like any other
        # unrecognised shape (defensive coercion, same reasoning as every field above).
        "known_limits": _str_dict("known_limits"),
        # Nested lists round-trip as lists; the in-memory sets hold tuples of strings.
        "seen_quality_signatures": _sig_list("seen_quality_signatures"),
        "seen_review_signatures": _review_sigs(),
        "tokens": state.get("tokens") if isinstance(state.get("tokens"), dict) else {},
        "dirty_paths": _strs("dirty_paths"),
        # False means the git snapshot at park time FAILED, so the recorded path set
        # proves nothing. Distinct from "the snapshot succeeded and was empty", which
        # is a legitimate state the worktree gate must accept — conflating the two
        # turned a park that touched no business files into a terminal refusal.
        "dirty_snapshot_ok": bool(state.get("dirty_snapshot_ok")),
        # How many times this task has already been parked for capacity. Bounds the
        # otherwise open fixed point of "quota dies in the same iteration forever".
        "park_count": max(0, int(state.get("park_count") or 0))
        if isinstance(state.get("park_count"), int) else 0,
    }


def _write_checkpoint(
    cwd: str | None,
    task_hash: str,
    *,
    cache_phase: str,
    research_and_plan: str,
    next_iteration: int,
    elapsed_budget_sec: float,
    previous_quality_findings: list[str],
    previous_resolution_output: str,
    deferred_p3: dict,
    seen_quality_signatures: set,
    seen_review_signatures: set,
    tokens: "TokenCounter",
    park_reason: str | None = None,
    park_count: int = 0,
    known_limits: dict | None = None,
) -> bool:
    """Write the version-2 state. Returns True only if it is durably on disk.

    `known_limits` defaults to None/{} rather than being required like
    `deferred_p3` — it round-trips identically once passed, the default just
    keeps every pre-existing direct caller (tests included) working unchanged.

    `park_reason` decides whether the record may ever be RESUMED from:
    `_PARK_CAPACITY` for a run parked by an exhausted quota, None for a plain
    research cache. Anything else stays readable but is not a continuation licence.

    Never raises — but it no longer LIES either. The return value exists because a
    capacity park that cannot persist its checkpoint is not the state it claims to
    be: the task gets parked, the next run finds no continuation point, and the
    worktree gate then refuses it terminally for the dirt the parked run left. The
    caller has to be able to say so.

    `all_outputs` is deliberately absent — see the resume banner in run() for why.
    """
    if not cwd:
        return False
    dirty, dirty_ok = _dirty_snapshot(cwd)
    try:
        _save_state(cwd, task_hash, {
            "tool": "dev-loop",
            "task_hash": task_hash,
            "state_version": _STATE_VERSION,
            "park_reason": park_reason,
            "park_count": park_count,
            "phase": cache_phase,
            "research_and_plan": research_and_plan,
            "next_iteration": next_iteration,
            "elapsed_budget_sec": round(elapsed_budget_sec, 1),
            "previous_quality_findings": list(previous_quality_findings),
            "previous_resolution_output": previous_resolution_output,
            "deferred_p3": list(deferred_p3.keys()),
            "known_limits": dict(known_limits or {}),
            "seen_quality_signatures": [list(t) for t in seen_quality_signatures],
            "seen_review_signatures": [
                [list(sig), verdict, text] for sig, verdict, text in seen_review_signatures
            ],
            "tokens": tokens.as_kwargs(),
            "dirty_paths": dirty,
            "dirty_snapshot_ok": dirty_ok,
        })
        return True
    except Exception as exc:
        print(f"  [dev-loop] ⚠️ Checkpoint konnte nicht geschrieben werden: {exc}")
        return False


def _consume_park_licence(cwd: str | None, task_hash: str) -> None:
    """Clear `park_reason` in place, leaving everything else in the file untouched.

    A checkpoint is a single-use permission to continue, not a standing property of
    the task. Only `_clear_state` (success) and `_terminal_runtime` ever removed the
    file, so a licence written weeks ago survived every OTHER ending — hang,
    format_error, both loop detectors, max iterations, a rejected plan — none of
    which touches state.json. It kept licensing the reduced budget and the
    dirty-tree waiver indefinitely, while the wall-clock those runs burned was never
    counted against the task.

    Flips one key rather than rewriting the record, so the research cache and the
    carry-overs survive for a run that parks again (which writes a fresh licence).
    Never raises: failing to consume costs one extra resume, not the run.
    """
    if not cwd:
        return
    try:
        state = _load_state(cwd, task_hash)
        if isinstance(state, dict) and state.get("park_reason") is not None:
            state["park_reason"] = None
            _save_state(cwd, task_hash, state)
    except Exception as exc:
        print(f"  [dev-loop] ⚠️ Fortsetzungs-Lizenz konnte nicht entwertet werden: {exc}")


def _is_capacity_error(error: str) -> bool:
    """True when a provider failure means "the usage budget is gone", not "this call broke".

    One predicate for all four provider call sites, so they cannot drift apart.

    Why this matters more than it looks: `rate_limit` is in TRANSIENT_ERRORS, so
    without this the generic path returns retryable=True with error_code
    "rate_limit" — and orchestrator.py then sets a cooldown and ROTATES TO THE NEXT
    PROVIDER. A half-finished dev-loop would be handed to Codex, which starts at
    iteration 1 with a fresh deadline and no knowledge of the reviews so far. The
    capacity path parks the task instead and keeps it on its own provider.
    """
    return error_code_of(error) == "rate_limit"


def _dirty_paths(cwd: str | None) -> list[str]:
    """Just the paths — see `_dirty_snapshot` for the version that reports failure."""
    return _dirty_snapshot(cwd)[0]


def _dirty_snapshot(cwd: str | None) -> tuple[list[str], bool]:
    """(paths, ok). Paths `git status --porcelain` reports as dirty, sorted,
    EXCLUDING `.dev-loop/`; `ok` is False when git could not be asked at all.

    The flag is why this is not just a list. An EMPTY result has two very different
    meanings — "the tree is clean apart from our own run directory" (legitimate: a
    park that touched no business files) and "git failed, we know nothing". Folding
    both into `[]` made the worktree gate refuse the first case terminally, because
    it could not tell an honest empty snapshot from an absent one.

    The exclusion is load-bearing, not tidiness. `.dev-loop/` is gitignored in this
    repo (.gitignore:42) but need not be in every target repo, and the tool writes
    state.json, round-*.md and traces/ there WHILE running. Counting them would
    break the ownership check in both directions: the checkpoint records the dirty
    set just BEFORE writing itself, so state.json would always look like a path that
    appeared afterwards — i.e. like somebody else's work — and every resume would be
    refused. The tool's own bookkeeping directory is not work under review.

    Recorded when a run parks itself, and compared against on the next attempt, so
    the worktree gate can tell "the tree is dirty with MY work" from "somebody else
    worked here". Deliberately the same command `parallel_runner._is_clean_git_repo`
    uses, so the two cannot disagree about what dirty means (untracked counts,
    staged counts, gitignored does not).

    Never raises: this feeds an exemption, and a git hiccup must degrade to "no
    proof of ownership" (the normal gate then refuses), never to a crash.
    """
    if not cwd:
        return ([], False)
    try:
        import subprocess

        from parallel_runner import _GIT_CHECK_TIMEOUT_SEC
        res = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True,
            encoding="utf-8", errors="replace", timeout=_GIT_CHECK_TIMEOUT_SEC,
            check=False,
        )
        if res.returncode != 0:
            return ([], False)
        # Porcelain v1: 2 status chars, a space, then the path. A rename reads
        # "R  old -> new"; the NEW path is what is on disk, so that is what we keep.
        out: set[str] = set()
        for line in res.stdout.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            path = path.strip('"')
            # git reports POSIX separators even on Windows.
            if path == DEV_LOOP_DIR or path.startswith(DEV_LOOP_DIR + "/"):
                continue
            out.add(path)
        return (sorted(out), True)
    except Exception:
        return ([], False)


# Appended to the execution prompt of the LAST iteration a run will get. The point
# is not to hurry the model but to change what "good work" means for one round:
# with no further iteration coming, starting something new produces a half-built
# thing nobody will finish, while writing the remainder down produces a usable
# handover. Introduced 2026-09-10 — before it, the deadline simply cut the loop off
# between iterations and the task was stamped failed.
_FINAL_ROUND_NOTE = """

## LETZTE ITERATION — kontrolliert abschliessen

Das Laufzeitbudget dieses Laufs ist fast aufgebraucht. Dies ist die LETZTE
Iteration; danach folgt nur noch der Review, keine weitere Runde.

Deshalb gilt fuer diese Runde ausschliesslich:
- Bringe den vorhandenen Stand in einen STABILEN, in sich schluessigen Zustand.
- Fange KEINE neue Baustelle an. Kein Refactoring, keine zusaetzlichen Features,
  keine Umbauten, die du nicht in dieser Runde fertig bekommst.
- Halbfertiges aus frueheren Runden: entweder jetzt fertigstellen oder sauber
  zuruecknehmen — nichts halb Gebautes stehen lassen.
- Was offen bleibt, wird NICHT begonnen, sondern am Ende unter der Ueberschrift
  "## Offen geblieben" als Liste ausgegeben, ein Punkt je Zeile.
"""


# Resolution review output patterns.
# `_VERDICT_PREFIX`/`_VERDICT_SUFFIX` tolerate a leading bullet/numbered-list
# marker ("- RESOLVED:", "1. UNRESOLVED:") and markdown emphasis wrapping the
# verdict word (bold `**`, italic `*`/`_`, code `` ` ``) — measured 2026-09-04:
# a reviewer wrote `**UNRESOLVED: Nothing was implemented.**`, and the plain
# `^\s*UNRESOLVED\s*:` anchor broke on the two leading asterisks alone, with a
# prose preamble on earlier lines on top (already tolerated: `.search()` +
# MULTILINE finds the verdict line regardless of what precedes it on other
# lines — see test_parse_resolution_prose_preamble_on_earlier_lines).
# `\b` after the keyword is load-bearing: without it, a line starting
# "Partially, the fix..." would match `_PARTIAL_RE` (both sides of the "L" in
# "PARTIAL"/"Partially" are word characters, so only a boundary check tells
# them apart).
_VERDICT_PREFIX = r"^\s*(?:[-*•]|\d+[.)])?\s*(?:[*_`]{1,2})?"
_VERDICT_SUFFIX = r"\b(?:[*_`]{1,2})?\s*:"
_RESOLVED_RE = re.compile(rf"{_VERDICT_PREFIX}RESOLVED{_VERDICT_SUFFIX}", re.IGNORECASE | re.MULTILINE)
_PARTIAL_RE = re.compile(rf"{_VERDICT_PREFIX}PARTIAL{_VERDICT_SUFFIX}", re.IGNORECASE | re.MULTILINE)
_UNRESOLVED_RE = re.compile(rf"{_VERDICT_PREFIX}UNRESOLVED{_VERDICT_SUFFIX}", re.IGNORECASE | re.MULTILINE)


def _parse_resolution(text: str) -> str:
    """Return 'RESOLVED', 'PARTIAL', 'UNRESOLVED', or 'UNKNOWN'.

    Uses earliest-match logic so that e.g. a 'PARTIAL:' on line 1
    wins over a 'RESOLVED:' mentioned on a later line.
    """
    best_label, best_pos = "UNKNOWN", len(text) + 1
    for label, regex in [
        ("RESOLVED", _RESOLVED_RE),
        ("PARTIAL", _PARTIAL_RE),
        ("UNRESOLVED", _UNRESOLVED_RE),
    ]:
        m = regex.search(text)
        if m and m.start() < best_pos:
            best_label, best_pos = label, m.start()
    return best_label



# ── Prompts ──────────────────────────────────────────────────────────────────

_RESEARCH_AND_PLAN_PROMPT = """\
You are a Research+Planning Agent. In a single response, analyze the codebase \
AND produce a concrete implementation plan.

TASK: {task}

Steps:
1. Explore relevant files (git status, directory listing, read key files).
2. Understand the current code structure and the root cause of the issue or \
the requirements for the new feature.
3. Search the web ONLY if you cannot determine required library APIs, \
error meanings, or documentation from the local codebase alone.
4. Identify all files that need to be changed or created.
5. Then immediately produce a concrete plan based on your findings.

Output format (REQUIRED — all sections must be present):

## Problem Analysis
[Clear description of the issue / feature to build]

## Relevant Files
[Files that need to be changed or created, with brief reason for each]

## Dependencies & Edge Cases
[External libs, API changes, error handling, edge cases discovered]

## Implementation Plan
1. Files to create or modify (exact paths)
2. Changes per file (brief description of what to add/modify/remove)
3. Order of operations (which changes first)

## Test Strategy
- How to verify the changes work
- Which tests to run or create

## Risks
- What could go wrong with this implementation
"""

_RESEARCH_ONLY_PROMPT = """\
You are a Research Agent. Analyze the codebase to understand what needs to be \
done for the following task. Do NOT produce an implementation plan — only the \
research findings.

TASK: {task}

Steps:
1. Explore relevant files (git status, directory listing, read key files).
2. Understand the current code structure and root cause / requirements.
3. Search the web ONLY if you cannot determine required library APIs from the \
local codebase.
4. Identify all files that need to be changed or created.

Output format (required sections):

## Problem Analysis
[Clear description of the issue / feature to build]

## Relevant Files
[Files that need to be changed or created, with brief reason for each]

## Dependencies & Edge Cases
[External libs, API changes, error handling concerns, edge cases]
"""

_EXECUTION_PROMPT = """\
You are an Execution Agent. Implement the following task based on the research \
findings and implementation plan below.

ORIGINAL TASK: {task}

RESEARCH AND PLAN:
{research_and_plan}
{review_context}
Instructions:
- Implement the solution exactly as laid out in the Implementation Plan section above.
- Fix every finding listed above (if any). Only blocking P1/P2 findings are listed —
  P3 is deliberately withheld, so there is nothing optional in that list.
- Apply changes directly to the files.
- Run existing tests if feasible.
- Do NOT commit, push, or deploy.
- Summarize what you changed at the end.
"""

_QUALITY_REVIEW_PROMPT = """\
You are a Code Quality Review Agent. Review ONLY the uncommitted changes in \
the current git working tree.

ORIGINAL TASK: {task}

CRITICAL — Read the working tree FRESH:
- Files have been modified by an Execute-phase agent BEFORE you. Any cwd or
  git-status info pinned earlier in this conversation is STALE.
- BEFORE you analyze, run `git diff --no-ext-diff`, `git status --porcelain`,
  and `git ls-files --others --exclude-standard` to see the CURRENT state.
- Re-Read every file you analyze — do not rely on prior tool-call results.

Review these aspects:
- Correctness: Does the code do what it's supposed to?
- Clean: Readable, well-structured, no dead code or commented-out blocks?
- Secure: No injection vulnerabilities, hardcoded secrets, unsafe operations?
- Performant: No obvious bottlenecks or unnecessary operations?
- Maintainable: Good abstractions, clear naming, no magic values?
- Testable: Tests included or updated where appropriate?
- Robust: Handles edge cases, errors, and unexpected inputs?
- Documented: Public APIs have docstrings where appropriate?
- Compliant: Follows existing project conventions and style?

Do NOT modify any files.
Ignore the `.dev-loop/` directory — it contains tool metadata.

Output format (strict):
- One bullet per finding: `- [P1] ...`, `- [P2] ...`, `- [P3] ...`
- P1 = critical / crash / security, P2 = significant issue, P3 = minor / style
- If no findings at all: output exactly: `No P1/P2/P3 findings.`
"""

_RESOLUTION_REVIEW_PROMPT = """\
You are an Issue Resolution Review Agent.
Your ONLY job: determine whether the uncommitted code changes fully solve the \
original task.

ORIGINAL TASK: {task}

CRITICAL — Read the working tree FRESH:
- Files were just modified by the Execute-phase agent. Any cwd or git-status
  info pinned earlier in this conversation is STALE.
- BEFORE you decide, run `git diff --no-ext-diff` and `git status --porcelain`
  to see the CURRENT state. Re-Read changed files.

Instructions:
- Focus ONLY on whether the task requirements are fully met.
- Do NOT evaluate code quality — that is handled by a separate agent.
- Do NOT modify any files.
- Ignore the `.dev-loop/` directory — it contains tool metadata.

Output format (strict — begin your response with exactly one of these):

RESOLVED: [brief explanation of how the task is fully solved]
PARTIAL: [what is done and what is still missing]
UNRESOLVED: [what was attempted and why it does not solve the task]
"""


class DevLoopTool(BaseTool):
    name = "dev-loop"
    description = (
        "Research → Execute → Dual-Review Loop "
        "(Code Quality + Issue Resolution) bis beide Reviews grünes Licht geben"
    )
    # Execute writes the diff, Quality + Resolution review exactly that diff.
    # Foreign uncommitted changes make the reviewers judge someone else's work —
    # measured 2026-09-04, where the Quality reviewer refused its output format
    # over a working tree left behind by the previous task, which tipped the run
    # into format_error and burned the whole retry budget.
    requires_clean_worktree = True

    def resume_permits_dirty(self, cwd: str | None, task: str) -> tuple[bool, str]:
        """Allow a dirty tree only when the dirt is provably this task's own.

        Without this, the capacity park built above is a trap: the run leaves the
        tree dirty with its own half-finished work, and the worktree gate then
        refuses the resumed task TERMINALLY (orchestrator.py:2202 stamps ❌ rather
        than parking). The task would be worse off for having been parked.

        The proof is a SUBSET check against the dirty path set recorded when the run
        parked (`dirty_paths` in state.json):

        * fewer or equal paths → someone committed or cleaned up, still our tree → allow
        * ANY path that was clean at park time → somebody else worked here → refuse

        The asymmetry is deliberate and errs toward refusing. A false allow lets
        reviewers judge a diff containing foreign work — exactly the corruption the
        gate exists to prevent. A false refuse costs one terminal failure that says
        precisely what happened, which is visible and fixable.

        Why a path SET and not a boolean "I parked here" flag: the flag would answer
        "did this task park?", which is the wrong question — after a park the tree
        keeps changing, and what matters is whether it changed *by someone else*. The
        set answers that and names the offending path in the refusal.

        The tool's own run directory is a SEPARATE mechanism and not part of this
        comparison at all: `_dirty_paths` filters `.dev-loop/` out on both sides, so
        the checkpoint's own state.json — written a moment after the path set was
        recorded — cannot masquerade as work that appeared later. Without that
        filter every resume would be refused in a repo where `.dev-loop/` is not
        gitignored (it is here, .gitignore:42, but that is a property of this repo,
        not of every target repo).

        Two limits, named rather than discovered later:

        * The comparison is by PATH, not by content. A foreign edit to a file that
          was ALREADY dirty when the run parked is invisible to it. In a repo where
          most files are dirty that is most of the tree. Recording a hash per path
          would close it and is deliberately not done — it would have to be read and
          rewritten on every checkpoint, and the failure it prevents (someone editing
          a file the parked run had already touched) ends in a review of a mixed diff,
          which is the state the gate merely narrows rather than eliminates.
        * The proof lives in a file inside the repo, which the provider whose diff
          this gate protects can write. That is not a hardened trust boundary and is
          not meant to be one: a provider with write access to the working tree can
          already change anything the reviewers will look at.
        """
        if not cwd:
            return (False, "")
        # The gate is handed the RAW queue line ("Fix bug #tool:dev-loop cwd:..."),
        # while _execute_tool_task calls the tool with strip_metadata_tags(task) —
        # so the tool checkpoints under a DIFFERENT hash than a naive lookup here
        # would use. Every real dev-loop line carries at least #tool: and cwd:, so
        # the two never matched and this exemption could never fire in production;
        # it only looked correct because the first tests passed the same raw string
        # to both sides. strip_metadata_tags is idempotent, so stripping here is
        # also correct if a caller ever passes an already-clean task.
        from queue_manager import strip_metadata_tags
        resume = _resume_checkpoint(cwd, _task_hash(strip_metadata_tags(task)))
        if resume is None:
            return (False, "")
        if not resume["dirty_snapshot_ok"]:
            # git could not be asked when the run parked, so the recorded set proves
            # nothing about ownership. Fall through to the normal refusal.
            #
            # Keyed on the FLAG, not on `not recorded`: an empty set is a legitimate
            # and reachable state — a park that changed no business files leaves only
            # `.dev-loop/`, which `_dirty_snapshot` filters out. Treating that as "no
            # proof" made the resume terminally refused in exactly the repos the
            # filter exists for (those where `.dev-loop/` is not gitignored, so the
            # outer `_is_clean_git_repo` still sees the tree as dirty).
            return (False, "")
        recorded = set(resume["dirty_paths"])
        current_paths, current_ok = _dirty_snapshot(cwd)
        if not current_ok:
            return (False, "")
        current = set(current_paths)
        foreign = current - recorded
        if foreign:
            return (False, f"fremde Änderungen seit der Unterbrechung: "
                           f"{', '.join(sorted(foreign)[:3])}")
        return (True, f"Fortsetzung ab Iteration {resume['next_iteration']}, "
                      f"{len(current)} eigene Pfade unverändert")

    def _get_plan_approval_mode(self) -> str:
        """Check policy.yaml for plan approval mode: auto | approve | skip."""
        try:
            from policy import load_policy
            policy = load_policy()
            phases = policy.get("tool_phases", {}).get("dev-loop", {})
            return phases.get("plan_approval", "auto")
        except (ImportError, OSError, ValueError):
            return "auto"

    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
        **kwargs,
    ) -> ToolResult:
        print(f"  [dev-loop] Starte Dev-Loop (max {TOOL_MAX_ITERATIONS} Iterationen)")

        tracer = ToolTracer.create(self.name, cwd)
        tracer.emit("run_start", task=task[:200], provider=provider.name,
                    max_iterations=TOOL_MAX_ITERATIONS)

        # Hash first: the output directory is keyed by task identity, not by cwd
        # alone (see _run_dir) — otherwise two dev-loops in one repo overwrite
        # each other's round files and plan.
        t_hash = _task_hash(task)
        dev_loop_dir = _run_dir(cwd, t_hash)
        system_prompt = _build_system_prompt(provider.name, memory_context, tool_name=self.name, cwd=cwd)
        all_outputs: list[str] = []
        # Ordered set of every P3 seen in any iteration, emitted once as an offer on
        # success — same contract as review_loop. Outlives a single round on purpose:
        # a P3 from round 1 must not vanish because round 2 came back clean.
        deferred_p3: dict[str, None] = {}
        # {finding: reason} accepted BEKANNTE GRENZE deferrals. Same accumulate-and-
        # survive contract as deferred_p3 — round-tripped through the checkpoint below.
        known_limits: dict[str, str] = {}
        seen_quality_signatures: set[tuple[str, ...]] = set()
        last_quality_tuple: tuple[str, ...] = ()
        seen_review_signatures: set[tuple[tuple[str, ...], str, str]] = set()

        # ── Resume: pick up an iteration checkpoint left by an earlier, parked run ──
        # Loaded here (before any provider call) because it decides both where the
        # loop starts and how much wall-clock budget is left. `_load_state` already
        # validates tool + task_hash, so another task's file is never consumed; a
        # version-1 file (research cache only) yields resume=None and the run starts
        # at iteration 1 exactly as before.
        resume = _resume_checkpoint(cwd, t_hash)
        start_iteration = 1
        consumed_budget = 0.0
        park_count = 0
        # Seeded here rather than at the loop, because the post-Research+Plan
        # checkpoint below already has to write them and runs earlier.
        previous_quality_findings_seed: list[str] = []
        previous_resolution_output_seed: str = ""
        if resume is not None:
            start_iteration = max(1, resume["next_iteration"])
            consumed_budget = resume["elapsed_budget_sec"]
            park_count = resume["park_count"]
            previous_quality_findings_seed = list(resume["previous_quality_findings"])
            previous_resolution_output_seed = resume["previous_resolution_output"]
            deferred_p3.update(dict.fromkeys(resume["deferred_p3"]))
            known_limits.update(resume["known_limits"])
            seen_quality_signatures.update(resume["seen_quality_signatures"])
            seen_review_signatures.update(resume["seen_review_signatures"])
            print(f"  [dev-loop] ▶ Fortsetzung ab Iteration {start_iteration} "
                  f"({consumed_budget:.0f}s Budget bereits verbraucht)")
            # all_outputs is deliberately NOT restored — it is unbounded text and
            # would push state.json into the megabytes. The durable copy of every
            # finished round is round-NNN.md in the run directory, so a pointer is
            # both smaller and more useful than a partial replay.
            _done_before = (f"Iterationen 1-{start_iteration - 1} liegen als round-*.md in "
                            f"{dev_loop_dir}." if start_iteration > 1 else
                            f"Iteration 1 war unterbrochen und wird wiederholt; "
                            f"Zwischenstaende in {dev_loop_dir}.")
            # CONSUME the resume licence immediately. A checkpoint is a single-use
            # permission to continue, not a standing property of the task: without
            # this, a `park_reason` written weeks ago survives every LATER
            # non-capacity ending (hang, format_error, loop detector, max iterations,
            # a rejected plan) — none of which touches state.json — and keeps
            # licensing both the reduced budget and the dirty-tree waiver, while the
            # wall-clock those runs burned is never counted against the task. Parking
            # again writes a fresh licence; ending any other way leaves none.
            _consume_park_licence(cwd, t_hash)
            all_outputs.append(
                f"--- Fortsetzung ---\nDieser Lauf setzt eine wegen erschoepftem "
                f"Kontingent geparkte Ausfuehrung fort. {_done_before}"
            )

        # Research+Plan are merged into a single subprocess call (saves ~42 k
        # cache_creation tokens vs. the prior two-call pattern). Skip-mode
        # still runs research-only because there is no plan to produce.
        # Per-phase caps: task #timeout: hard backstop is an upper deckel only.
        research_plan_timeout = self._phase_cap(
            timeout, TOOL_DEV_RESEARCH_TIMEOUT_SEC + TOOL_DEV_PLAN_TIMEOUT_SEC)
        exec_timeout = self._phase_cap(timeout, TOOL_DEV_EXEC_TIMEOUT_SEC)
        quality_timeout = self._phase_cap(timeout, TOOL_DEV_QUALITY_REVIEW_TIMEOUT_SEC)
        resolution_timeout = self._phase_cap(timeout, TOOL_DEV_RESOLUTION_REVIEW_TIMEOUT_SEC)

        # Total-runtime deadline bounds the SUM of all iterations/phases — and,
        # since 2026-09-10, across RESUMES too: `consumed_budget` is the wall-clock
        # earlier parked runs of this same task already spent. Without subtracting
        # it, every capacity park would hand out a fresh full budget and the bound
        # would mean nothing (the failure orchestrator.py:2451-2457 describes).
        run_started_at = time.monotonic()
        deadline = self._runtime_deadline(consumed_budget)

        # The reserve is a constant, the budget comes from policy.yaml, and nothing
        # relates the two. A contract whose `max_runtime_sec` is at or below the
        # reserve makes every run a landing round from iteration 1 — which is the
        # CORRECT outcome (a budget that small cannot carry a second round), but it
        # used to happen silently, and "dev-loop suddenly only ever does one
        # iteration" is not something anyone would trace back to a policy edit.
        # Named, not capped: capping it would mean a small budget could never land
        # at all, which is the worse failure.
        landing_reserve = TOOL_LANDING_RESERVE_SEC
        if self._max_runtime_sec() <= landing_reserve:
            print(f"  [dev-loop] ⚠️ max_runtime_sec ({self._max_runtime_sec()}s) ≤ "
                  f"Landereserve ({landing_reserve}s) → JEDER Lauf ist eine einzelne "
                  f"Abschlussrunde. Das ist bei diesem Budget richtig, aber selten "
                  f"beabsichtigt — policy.yaml prüfen.")

        def _spent_budget() -> float:
            """Total wall-clock this TASK has consumed, earlier runs included."""
            return consumed_budget + (time.monotonic() - run_started_at)

        def _landing_cap(phase_timeout: int) -> int:
            """Phase timeout clamped to the wall-clock left AT THIS MOMENT.

            Called immediately before each provider call, so a long execution phase
            shrinks what quality and resolution may still take.

            Per phase and not once per round: the first version computed the
            remaining budget once and spread that same value across all three
            sequential phases, so a 600 s budget produced timeouts of [599, 599, 599]
            — up to 3x what was actually left.

            Applied to EVERY round, not only the landing round. Restricting it to the
            landing round left a far bigger hole than the one it closed: a normal
            iteration starting with 2401 s left (one second above the reserve) was
            still granted 7200 + 3600 + 1800 = 12600 s, i.e. up to ~10200 s past the
            deadline, and the documentation claimed the overrun was capped at 120 s.
            Clamping everywhere is also what makes that 120 s claim true, because the
            floor below is then the only way past the deadline.
            """
            left = int(deadline - time.monotonic())
            return min(phase_timeout, max(TOOL_LANDING_MIN_PHASE_SEC, left))

        def _terminal_runtime(msg: str, iterations: int) -> ToolResult:
            """Budget is gone for good: report it AND drop the resume point.

            Dropping it is the whole reason this helper exists. `_clear_state` used to
            run only in the success branch, so a run that ended on `tool_runtime_exceeded`
            left a checkpoint carrying `elapsed_budget_sec ≈ max_runtime` behind. Any
            later attempt at the SAME queue line — `/retry` via queue_healing, an
            `#every:` occurrence, a hand-reopened task — then started with zero budget
            left and returned immediately without a single provider call. The
            documented recovery path was silently dead, and the only way out was
            deleting .dev-loop/<hash>/state.json by hand or editing the task text.

            A terminal end is not a park: the next attempt is a NEW attempt and gets a
            full budget and a fresh plan.
            """
            if cwd:
                _clear_state(cwd, t_hash)
            print(f"  [dev-loop] ⏱ {msg}")
            notify_tool_done(self.name, iterations, False, msg)
            return ToolResult(
                success=False,
                output="\n\n".join(all_outputs),
                iterations=iterations,
                error=msg,
                error_code="tool_runtime_exceeded",
                retryable=True,
                **tokens.as_kwargs(),
            )

        def _park_budget() -> float:
            """Consumed budget to record when PARKING, capped so a resume can act.

            A park promises the run continues when capacity returns. That promise is
            empty if the recorded budget leaves the next run with nothing: it would
            hit `remaining <= 0` and stamp the task terminally without a single
            provider call — a worse outcome than never parking, and measured as
            reachable when the quota dies late in a landing round.

            The cap is therefore the priority rule between AI-3 ("the budget bounds
            the task, not one process") and K3b ("a quota end must be resumable"):
            a park always leaves exactly enough for one minimal landing round.

            **The slack is FOUR floors, not three, and that one floor is the whole
            point.** The go/no-go check downstream is
            `int(deadline - now) < TOOL_LANDING_MIN_PHASE_SEC * 3`. Leaving exactly
            `3 x floor` made the two numbers identical, and everything between
            computing the deadline and reaching that check — policy load, state read,
            SessionContext, tracer, prints — costs wall-clock. `int()` truncates, so
            **one millisecond** turned 180.0 into 179 and the resumed run went
            terminal without a single provider call. Measured, deterministic, and it
            hit exactly the scenario this cap was written for. The extra floor is the
            startup budget.

            **Bounded across parks by `_MAX_CAPACITY_PARKS`.** `next_iteration` does
            not advance on a park (a park records the CURRENT iteration so it is
            repeated), so a task whose quota dies in the same iteration every time is
            a fixed point that TOOL_MAX_ITERATIONS never reaches. Past the limit the
            cap is dropped: the true spent budget is recorded, the next run finds
            nothing left and ends terminally. That is the deliberate trade — a task
            that could not finish in five capacity windows is not going to.
            """
            if park_count >= _MAX_CAPACITY_PARKS:
                return _spent_budget()
            slack = TOOL_LANDING_MIN_PHASE_SEC * 4
            return min(_spent_budget(), max(0.0, self._max_runtime_sec() - slack))

        tokens = TokenCounter()
        if resume is not None:
            # Token accounting is cumulative over the task, not over the process:
            # memory.store_result and analytics read these, and a resumed run that
            # restarted at zero would under-report what the task actually cost.
            # Only the four real counter fields, by name — never "whatever keys the
            # file happens to carry", which would let a hand-edited state.json set
            # arbitrary attributes on the counter.
            for _field in ("input_tokens", "output_tokens",
                           "cache_creation_input_tokens", "cache_read_input_tokens"):
                _value = resume["tokens"].get(_field)
                if isinstance(_value, int) and _value >= 0:
                    setattr(tokens, _field, _value)

        # Phase B: optional shared session across phases (Anthropic prompt cache).
        # When enabled, all subprocess calls within this run share conversation
        # history. cap=5 triggers a fresh session every 5 iterations to keep the
        # Conversation-history bounded; explicit findings re-injection in the
        # exec prompt (review_context) makes the rollover continuation seamless.
        sess = SessionContext.create(provider, tool_name=self.name, cwd=cwd, cap=5)
        first_call = True  # tracks whether next provider.run() should use session_id (start) or resume

        def _session_kwargs() -> dict:
            """Return session kwargs for the next provider.run() call and flip
            ``first_call`` so subsequent calls resume the same session."""
            nonlocal first_call
            if not sess.enabled:
                return {}
            kw = sess.first_call_kwargs() if first_call else sess.resume_kwargs()
            first_call = False
            return kw

        # Load memory module for lessons
        try:
            import memory as memory_module
        except (ImportError, OSError):
            memory_module = None

        # ── Phase 1: Research+Plan (merged) ──────────────────────────────────
        plan_approval_mode = self._get_plan_approval_mode()
        merged_phase = plan_approval_mode != "skip"

        cached_state = _load_state(cwd, t_hash) if cwd else None
        cache_phase = "research_and_plan_done" if merged_phase else "research_done"

        # `.get(...)` and not `[...]`: a state file carrying the right tool, task_hash
        # and phase but no plan used to raise KeyError straight out of the tool. That
        # was tolerable while the file was written exactly once; since 2026-09-10 every
        # iteration checkpoints here, so the shape is worth not trusting blindly. An
        # empty plan falls through to the fresh Research+Plan below, which is the same
        # safe direction the resume checkpoint takes: redo work rather than skip it.
        if cached_state and cached_state.get("phase") == cache_phase \
                and cached_state.get("research_and_plan"):
            print(f"  [dev-loop] Research+Plan aus Cache geladen (task_hash={t_hash})")
            research_and_plan = cached_state["research_and_plan"]
            all_outputs.append(f"--- Research+Plan (cached) ---\n{research_and_plan}")
        else:
            if not is_cached_provider_available(provider.name):
                msg = "Provider nicht verfügbar — Suspend vor Research+Plan-Phase"
                print(f"  [dev-loop] ⏸ {msg}")
                return _make_capacity_exhausted_result(
                    msg, "", 0, **tokens.as_kwargs(),
                )

            phase_label = "RESEARCH+PLAN" if merged_phase else "RESEARCH"
            print(f"  [dev-loop] === Phase 1: {phase_label} ===")
            template = _RESEARCH_AND_PLAN_PROMPT if merged_phase else _RESEARCH_ONLY_PROMPT
            rp_prompt = system_prompt + "\n\n" + template.format(task=task)
            rp_result = provider.run(
                rp_prompt, cwd=cwd, timeout=_landing_cap(research_plan_timeout),
                **_session_kwargs(),
            )
            if not rp_result.success and rp_result.error == "session_missing":
                print("  [dev-loop] ⚠️ Session missing in Research+Plan — Fallback")
                sess.rollover(self.name, cwd)
                first_call = True
                rp_result = provider.run(
                    rp_prompt, cwd=cwd, timeout=_landing_cap(research_plan_timeout),
                    **_session_kwargs(),
                )
            tokens.add(rp_result)

            if not rp_result.success:
                if _is_capacity_error(rp_result.error):
                    # Park instead of rotating to another provider. There is no plan
                    # to resume from, so `next_iteration` is 1 and Research+Plan is
                    # simply redone — but the checkpoint is still written, for the two
                    # fields that do NOT depend on having produced anything:
                    # `elapsed_budget_sec` and `dirty_paths`. Without them repeated
                    # capacity parks at this earliest point never converge (every
                    # retry gets a fresh full budget) and the worktree gate has no
                    # ownership proof for a tree the run may already have touched.
                    _write_checkpoint(
                        cwd, t_hash,
                        cache_phase=cache_phase, research_and_plan="",
                        next_iteration=1, elapsed_budget_sec=_park_budget(),
                        previous_quality_findings=[], previous_resolution_output="",
                        deferred_p3={}, seen_quality_signatures=set(),
                        seen_review_signatures=set(), tokens=tokens,
                        park_reason=_PARK_CAPACITY, park_count=park_count + 1,
                        known_limits={},
                    )
                    msg = f"Kontingent erschöpft in Research+Plan → Suspend: {rp_result.error}"
                    print(f"  [dev-loop] ⏸ {msg}")
                    return _make_capacity_exhausted_result(
                        msg, "", 0, **tokens.as_kwargs(),
                    )
                msg = f"Research+Plan-Phase fehlgeschlagen: {rp_result.error}"
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, 0, False, msg)
                return ToolResult(
                    success=False,
                    output="",
                    iterations=0,
                    error=msg,
                    error_code=error_code_of(rp_result.error),
                    retryable=is_transient(rp_result.error),
                    **tokens.as_kwargs(),
                )

            research_and_plan = rp_result.output.strip()
            all_outputs.append(f"--- Research+Plan ---\n{research_and_plan}")
            _write_tool_file(
                dev_loop_dir,
                "research-and-plan.md",
                f"# Dev-Loop Research+Plan\n\nTask: {task}\n\n{research_and_plan}\n",
            )
            print(f"  [dev-loop] Research+Plan abgeschlossen → {dev_loop_dir / 'research-and-plan.md'}")

            # Persist so a capacity-exhausted retry can skip this phase.
            # Written through _write_checkpoint (version 2) rather than as the old
            # four-key dict: a run that was parked DURING Research+Plan already left
            # a v2 record carrying `elapsed_budget_sec` and `dirty_paths`, and
            # overwriting it with a v1 dict here would silently hand the next attempt
            # a fresh full budget — the very leak AI-3 exists to close. Iteration
            # state is still empty at this point, which is exactly what the values
            # below say.
            _write_checkpoint(
                cwd, t_hash,
                cache_phase=cache_phase,
                research_and_plan=research_and_plan,
                next_iteration=start_iteration,
                elapsed_budget_sec=_spent_budget(),
                previous_quality_findings=previous_quality_findings_seed,
                previous_resolution_output=previous_resolution_output_seed,
                deferred_p3=deferred_p3,
                known_limits=known_limits,
                seen_quality_signatures=seen_quality_signatures,
                seen_review_signatures=seen_review_signatures,
                tokens=tokens,
                park_count=park_count,
            )

            # Telegram approval if configured (only if plan is part of output)
            if merged_phase and plan_approval_mode == "approve":
                try:
                    from notifier import request_approval
                    approved = request_approval(
                        f"Dev-Loop Plan fuer: {task[:100]}\n\n{research_and_plan[:500]}",
                        timeout=600,
                    )
                    if not approved:
                        msg = "Plan wurde via Telegram abgelehnt."
                        print(f"  [dev-loop] {msg}")
                        notify_tool_done(self.name, 0, False, msg)
                        return ToolResult(
                            success=False,
                            output="\n\n".join(all_outputs),
                            iterations=0,
                            error=msg,
                            **tokens.as_kwargs(),
                        )
                except (ImportError, OSError, ValueError) as exc:
                    print(f"  [dev-loop] Plan-Approval uebersprungen (Fehler: {exc})")

            time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

        # ── Phase 2+3: Execute → Dual-Review → Iterate ───────────────────────
        previous_quality_findings: list[str] = list(previous_quality_findings_seed)
        previous_resolution_output: str = previous_resolution_output_seed

        # Set once, when the remaining wall-clock drops below the landing reserve.
        # From then on this iteration is the last one the run will get, and the
        # executor is told so (_FINAL_ROUND_NOTE) instead of being cut off between
        # iterations the way it was until 2026-09-10.
        final_round = False

        def _park_for_capacity(
            phase_label: str,
            iteration: int,
            error: str,
            prev_quality: list[str],
            prev_resolution: str,
        ) -> ToolResult:
            """Checkpoint and hand back `capacity_exhausted` so the task is PARKED.

            The alternative — letting `rate_limit` through — makes orchestrator.py
            set a cooldown and rotate to the next provider, which restarts the loop
            at iteration 1 with a fresh deadline and no review context. That is how
            a finished two-hour run was thrown away on 2026-09-09.

            `next_iteration` is the CURRENT one, not the next: this iteration did not
            complete, so the resume repeats it. Repeating work is the safe direction;
            skipping an iteration that never produced a review is not.
            """
            saved = _write_checkpoint(
                cwd, t_hash,
                cache_phase=cache_phase,
                research_and_plan=research_and_plan,
                next_iteration=iteration,
                elapsed_budget_sec=_park_budget(),
                previous_quality_findings=prev_quality,
                previous_resolution_output=prev_resolution,
                deferred_p3=deferred_p3,
                known_limits=known_limits,
                seen_quality_signatures=seen_quality_signatures,
                seen_review_signatures=seen_review_signatures,
                tokens=tokens,
                park_reason=_PARK_CAPACITY,
                park_count=park_count + 1,
            )
            if saved:
                msg = (f"Kontingent erschöpft in {phase_label} (Iteration {iteration}) "
                       f"→ Suspend, Fortsetzung ab Iteration {iteration}: {error}")
            else:
                # Do not claim a continuation that does not exist on disk. The task is
                # still parked (better than a terminal stamp), but it will restart from
                # the beginning and the worktree gate may refuse it — say so here
                # rather than letting somebody discover it at 03:00.
                msg = (f"Kontingent erschöpft in {phase_label} (Iteration {iteration}) "
                       f"→ Suspend OHNE Fortsetzungspunkt (Checkpoint nicht schreibbar; "
                       f"der Folgelauf beginnt von vorn und kann am Arbeitsbaum-Check "
                       f"scheitern): {error}")
            print(f"  [dev-loop] ⏸ {msg}")
            return _make_capacity_exhausted_result(
                msg, "\n\n".join(all_outputs), iteration - 1, **tokens.as_kwargs(),
            )

        for iteration in range(start_iteration, TOOL_MAX_ITERATIONS + 1):
            remaining = deadline - time.monotonic()

            # Budget fully gone: same terminal outcome as before. Reached either
            # because the landing round below already ran, or because a single
            # phase overran the reserve.
            if remaining <= 0:
                return _terminal_runtime(
                    f"Gesamt-Laufzeit-Limit erreicht nach Iteration {iteration - 1}",
                    iteration - 1)

            # Soft deadline: not enough left for a normal iteration, but enough to
            # land. Do not start a full round — run ONE final round that is told to
            # stabilise, then stop regardless of the verdict.
            if remaining <= landing_reserve and not final_round:
                final_round = True
                print(f"  [dev-loop] ⏱ Restlaufzeit {remaining:.0f}s ≤ Reserve "
                      f"{landing_reserve}s → Iteration {iteration} ist die "
                      f"ABSCHLUSSRUNDE")

            if final_round:
                # Enforcement, not just scheduling — a single exec phase may ask for
                # TOOL_DEV_EXEC_TIMEOUT_SEC (7200 s) and blow straight through the
                # total budget. The clamp itself is applied per phase via
                # `_landing_cap()` below, NOT once here: the first version computed
                # `budget_left` once and spread the same value over all three
                # sequential phases, so a 600 s budget yielded timeouts of
                # [599, 599, 599] — up to 3x the remaining wall-clock, measured. This
                # value is only the go/no-go check for starting the round at all.
                budget_left = int(deadline - time.monotonic())
                if budget_left < TOOL_LANDING_MIN_PHASE_SEC * 3:
                    # Not even a minimal round fits. Starting one would spend real
                    # quota on three calls that cannot finish; say so instead.
                    return _terminal_runtime(
                        f"Gesamt-Laufzeit-Limit erreicht nach Iteration {iteration - 1} "
                        f"— Restzeit {budget_left}s reicht nicht für eine Abschlussrunde",
                        iteration - 1)

            print(f"\n  [dev-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: EXECUTION ===")
            tracer.emit("iteration_start", iteration=iteration,
                        max_iterations=TOOL_MAX_ITERATIONS, phase="execution")

            # Capacity guard: abort loop if provider is below threshold (RAM-cache, no API call).
            # Routed through the same park helper as a mid-phase quota end, so this
            # exit also leaves a resume point. It used to return directly, which was
            # harmless only while every completed iteration checkpointed anyway —
            # once that was removed (see the note further down), a park here would
            # have left nothing to continue from.
            if not is_cached_provider_available(provider.name):
                return _park_for_capacity(
                    "Kapazitäts-Guard", iteration, "Provider nicht verfügbar",
                    previous_quality_findings, previous_resolution_output,
                )

            # Build review context for execution prompt (empty on first iteration)
            review_context = ""
            if previous_quality_findings or previous_resolution_output:
                parts: list[str] = []
                if previous_quality_findings:
                    parts.append(
                        f"QUALITY REVIEW (Iteration {iteration - 1}):\n"
                        + "\n".join(previous_quality_findings)
                    )
                if previous_resolution_output:
                    parts.append(
                        f"RESOLUTION REVIEW (Iteration {iteration - 1}):\n"
                        + previous_resolution_output
                    )
                review_context = (
                    "\nPREVIOUS REVIEWS — fix all issues listed here, unless you "
                    "defer a P2 finding as BEKANNTE GRENZE in your "
                    "Rundenreflexion:\n\n"
                    + "\n\n".join(parts)
                    + "\n"
                    + ROUND_REFLECTION_INSTRUCTION
                )

            # Search lessons for hints related to current review findings
            if previous_quality_findings and memory_module is not None:
                try:
                    findings_text = "\n".join(previous_quality_findings)
                    hint = memory_module.search_lessons(findings_text)
                    if hint:
                        review_context += (
                            f"\nLESSONS FROM PREVIOUS SIMILAR ISSUES:\n{hint}\n"
                        )
                except (ImportError, OSError, ValueError):
                    pass

            exec_prompt = system_prompt + "\n\n" + _EXECUTION_PROMPT.format(
                task=task,
                research_and_plan=research_and_plan,
                review_context=review_context,
            )
            if final_round:
                # Appended LAST so it is the final instruction the model reads —
                # same reasoning as the task-at-the-end rule in _build_prompt.
                exec_prompt += _FINAL_ROUND_NOTE
            notify_tool_progress(
                self.name, iteration, TOOL_MAX_ITERATIONS, "Implementierung läuft..."
            )
            exec_result = provider.run(
                exec_prompt, cwd=cwd, timeout=_landing_cap(exec_timeout),
                **_session_kwargs(),
            )
            # Fallback: session was lost (e.g. cleanup deleted JSONL between calls).
            # Start a fresh session and retry once with the same prompt — the
            # explicit review_context inject keeps the model in sync.
            if not exec_result.success and exec_result.error == "session_missing":
                print("  [dev-loop] ⚠️ Session missing — Fallback auf fresh session")
                sess.rollover(self.name, cwd)
                first_call = True
                exec_result = provider.run(
                    exec_prompt, cwd=cwd, timeout=_landing_cap(exec_timeout),
                    **_session_kwargs(),
                )
            tokens.add(exec_result)

            if not exec_result.success:
                if _is_capacity_error(exec_result.error):
                    return _park_for_capacity(
                        "Execution", iteration, exec_result.error,
                        previous_quality_findings, previous_resolution_output,
                    )
                msg = f"Execution fehlgeschlagen in Iteration {iteration}: {exec_result.error}"
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    error_code=error_code_of(exec_result.error),
                    retryable=is_transient(exec_result.error),
                    **tokens.as_kwargs(),
                )

            exec_output = exec_result.output.strip()
            all_outputs.append(f"--- Execution {iteration} ---\n{exec_output}")
            # Deferrals the executor marked BEKANNTE GRENZE, validated fail-closed
            # against the findings it was actually shown in review_context above.
            candidates = parse_known_limits(exec_output)
            if candidates:
                known_limits.update(
                    validate_known_limits(candidates, previous_quality_findings, tool_name=self.name)
                )
            time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

            # ── Phase 3a: Code Quality Review ────────────────────────────────
            print(f"  [dev-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: QUALITY REVIEW ===")
            quality_prompt = system_prompt + "\n\n" + _QUALITY_REVIEW_PROMPT.format(task=task)
            if known_limits:
                quality_prompt += "\n" + KNOWN_LIMITS_REVIEW_BLOCK.format(
                    known_limits=format_known_limits(known_limits)
                )
            quality_result = provider.run(
                quality_prompt, cwd=cwd, timeout=_landing_cap(quality_timeout),
                read_only=True,  # safe-by-CLI: review must not edit files
                **_session_kwargs(),
            )
            if not quality_result.success and quality_result.error == "session_missing":
                print("  [dev-loop] ⚠️ Session missing in Quality-Review — Fallback")
                sess.rollover(self.name, cwd)
                first_call = True
                quality_result = provider.run(
                    quality_prompt, cwd=cwd, timeout=_landing_cap(quality_timeout),
                    read_only=True,
                    **_session_kwargs(),
                )
            tokens.add(quality_result)

            if not quality_result.success:
                if _is_capacity_error(quality_result.error):
                    return _park_for_capacity(
                        "Quality-Review", iteration, quality_result.error,
                        previous_quality_findings, previous_resolution_output,
                    )
                msg = f"Quality-Review fehlgeschlagen in Iteration {iteration}: {quality_result.error}"
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    error_code=error_code_of(quality_result.error),
                    retryable=is_transient(quality_result.error),
                    **tokens.as_kwargs(),
                )

            quality_output = quality_result.output.strip()
            all_outputs.append(f"--- Quality Review {iteration} ---\n{quality_output}")
            quality_findings = _parse_findings(quality_output)
            no_quality_findings = _is_clean_output(quality_output, quality_findings)
            if not quality_findings and not no_quality_findings:
                msg = (
                    "Quality-Review-Output entspricht nicht dem erwarteten Format "
                    "(keine P1/P2/P3 Findings und kein 'No P1/P2/P3 findings.')."
                )
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    # A malformed review is a one-off model hiccup, not a permanent
                    # failure — without a code the orchestrator finalized the queue
                    # item as if the work were done. "format_error" is retryable but
                    # capped: it shares the persistent hang counter (MAX_HANG_RETRIES)
                    # so a model that keeps breaking format gets blocked, not looped.
                    error_code="format_error",
                    retryable=True,
                    **tokens.as_kwargs(),
                )
            # A known limit's lifecycle ends here if the reviewer has since escalated
            # the same underlying issue back to P1: the deferral is retracted (not
            # merely overridden for this round), so later prompts stop calling it
            # "deliberately deferred" and a future re-report as P2 blocks normally.
            release_escalated_known_limits(quality_findings, known_limits, tool_name=self.name)
            # P3-only findings are non-blocking; only P1/P2 block progress. A finding
            # matching an accepted known limit is excluded the same way — unless the
            # reviewer re-tagged it P1, which always blocks regardless of an earlier
            # deferral (is_deferred_known_limit() enforces that).
            blocking_findings = [
                f for f in quality_findings
                if not f.startswith("- [P3]") and not is_deferred_known_limit(f, known_limits)
            ]
            for p3 in (f for f in quality_findings if f.startswith("- [P3]")):
                deferred_p3.setdefault(p3, None)
            quality_ok = no_quality_findings or not blocking_findings
            time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

            # ── Phase 3b: Resolution Review ───────────────────────────────────
            print(f"  [dev-loop] === Iteration {iteration}/{TOOL_MAX_ITERATIONS}: RESOLUTION REVIEW ===")
            resolution_prompt = system_prompt + "\n\n" + _RESOLUTION_REVIEW_PROMPT.format(task=task)
            if known_limits:
                resolution_prompt += "\n" + KNOWN_LIMITS_RESOLUTION_BLOCK.format(
                    known_limits=format_known_limits(known_limits)
                )
            resolution_result = provider.run(
                resolution_prompt, cwd=cwd, timeout=_landing_cap(resolution_timeout),
                read_only=True,  # safe-by-CLI: review must not edit files
                **_session_kwargs(),
            )
            if not resolution_result.success and resolution_result.error == "session_missing":
                print("  [dev-loop] ⚠️ Session missing in Resolution-Review — Fallback")
                sess.rollover(self.name, cwd)
                first_call = True
                resolution_result = provider.run(
                    resolution_prompt, cwd=cwd, timeout=_landing_cap(resolution_timeout),
                    read_only=True,
                    **_session_kwargs(),
                )
            tokens.add(resolution_result)

            if not resolution_result.success:
                if _is_capacity_error(resolution_result.error):
                    # THE path from 2026-09-09: this is where #id:orchtrio died with
                    # a finished piece of work in the tree and a terminal ❌ stamp.
                    return _park_for_capacity(
                        "Resolution-Review", iteration, resolution_result.error,
                        previous_quality_findings, previous_resolution_output,
                    )
                msg = f"Resolution-Review fehlgeschlagen in Iteration {iteration}: {resolution_result.error}"
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    error_code=error_code_of(resolution_result.error),
                    retryable=is_transient(resolution_result.error),
                    **tokens.as_kwargs(),
                )

            resolution_output = resolution_result.output.strip()
            all_outputs.append(f"--- Resolution Review {iteration} ---\n{resolution_output}")
            resolution_status = _parse_resolution(resolution_output)
            if resolution_status == "UNKNOWN":
                msg = (
                    "Resolution-Review-Output entspricht nicht dem erwarteten Format "
                    "(erwartet: RESOLVED/PARTIAL/UNRESOLVED)."
                )
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    error_code="format_error",  # retryable, capped — see quality-review above
                    retryable=True,
                    **tokens.as_kwargs(),
                )
            resolution_ok = resolution_status == "RESOLVED"

            # ── Write round file ──────────────────────────────────────────────
            _write_tool_file(
                dev_loop_dir,
                f"round-{iteration:03d}.md",
                (
                    f"# Dev-Loop Round {iteration}\n\n"
                    f"## Task\n{task}\n\n"
                    f"## Execution Summary\n{exec_output}\n\n"
                    f"## Quality Review\n{quality_output}\n\n"
                    f"## Resolution Review\n{resolution_output}\n"
                ),
            )

            quality_label = "OK" if quality_ok else f"{len(blocking_findings)} blocking findings"
            print(
                f"  [dev-loop] Quality: {quality_label} | Resolution: {resolution_status}"
            )
            for f in blocking_findings[:3]:
                print(f"    {f}")

            # ── Both pass → done ──────────────────────────────────────────────
            if quality_ok and resolution_ok:
                msg = (
                    f"Beide Reviews bestanden nach {iteration} Iteration(en). "
                    "Bereit fuer deinen Review + Push."
                )
                if final_round:
                    # A landing round passed its reviews, so this IS a success and
                    # gets the normal stamp — but its executor was told to stop
                    # starting things and list the rest under "## Offen geblieben".
                    # Without saying so, the ✅ recipient has no reason to look for
                    # that list and reads a clean finish where work was deliberately
                    # left on the table.
                    msg += (" ACHTUNG: Abschlussrunde am Laufzeitlimit — der Executor "
                            "war angewiesen, Offenes NICHT mehr zu beginnen. Siehe "
                            "'## Offen geblieben' im Output.")
                if deferred_p3:
                    msg += f" {len(deferred_p3)} P3 offen (nicht gefixt)."
                    # The P3 never reached the executor — surface it as an offer so it is
                    # visible rather than silently dropped. The user decides.
                    all_outputs.append(
                        "--- P3 offen (nicht-blockierend, Angebot) ---\n"
                        + "\n".join(deferred_p3)
                    )
                if known_limits:
                    msg += f" {len(known_limits)} bekannte Grenze(n) zurückgestellt."
                    # Separate from the P3 offer — these were once blocking (P2) and
                    # deliberately deferred with a reason, not merely non-blocking.
                    all_outputs.append(
                        "--- Bekannte Grenzen (zurückgestellt, mit Begründung) ---\n"
                        + format_known_limits(known_limits)
                    )
                print(f"  [dev-loop] {msg}")
                _write_tool_file(
                    dev_loop_dir,
                    "summary.md",
                    (
                        f"# Dev-Loop Abgeschlossen\n\n"
                        f"Task: {task}\n\n"
                        f"Iterationen: {iteration}\n\n"
                        f"Status: DONE — bereit fuer Review + Git Push\n"
                        + ("\nHINWEIS: Abschlussrunde am Laufzeitlimit. Offene Punkte stehen unter '## Offen geblieben' im Lauf-Output.\n"
                           if final_round else "")
                    ),
                )

                # Auto-lesson: generate LLM summary if it took more than 1 iteration.
                # It is a convenience, not the deliverable, and it is an extra
                # provider call that `memory.py` runs with a fixed 120 s timeout —
                # outside `_landing_cap`, because it lives in another module. Skipped
                # whenever less than that is left, not merely in a landing round: an
                # ORDINARY round that started one second above the reserve consumes
                # the rest of the budget through the clamp and would then still make
                # this call, which is exactly the unaccounted consumer the documented
                # overrun bound must not have.
                _lesson_left = deadline - time.monotonic()
                if (iteration > 1 and memory_module is not None
                        and not final_round and _lesson_left >= _LESSON_CALL_SEC):
                    print("  [dev-loop] Generiere Lesson Learned...")
                    memory_module.create_lesson_from_loop(
                        self.name, task, all_outputs, provider, cwd=cwd
                    )

                tracer.emit("run_end", success=True, iterations=iteration)
                notify_tool_done(self.name, iteration, True, msg)

                if cwd:
                    _clear_state(cwd, t_hash)
                return ToolResult(
                    success=True,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    **tokens.as_kwargs(),
                )

            # ── Infinite loop detection (same quality findings twice) ──────────
            if blocking_findings:
                sig = tuple(sorted(blocking_findings))
                if sig in seen_quality_signatures:
                    msg = (
                        f"Quality-Findings wiederholen sich nach {iteration} Iterationen. "
                        "Loop abgebrochen."
                    )
                    if known_limits:
                        msg += f" {len(known_limits)} bekannte Grenze(n) zurückgestellt."
                        all_outputs.append(
                            "--- Bekannte Grenzen (zurückgestellt, mit Begründung) ---\n"
                            + format_known_limits(known_limits)
                        )
                    print(f"  [dev-loop] {msg}")
                    notify_tool_done(self.name, iteration, False, msg)
                    return ToolResult(
                        success=False,
                        output="\n\n".join(all_outputs),
                        iterations=iteration,
                        error=msg,
                        **tokens.as_kwargs(),
                    )
                seen_quality_signatures.add(sig)
                last_quality_tuple = sig

            review_sig = (tuple(sorted(blocking_findings)), resolution_status, resolution_output)
            if review_sig in seen_review_signatures:
                msg = (
                    f"Review-Ergebnis wiederholt sich nach {iteration} Iterationen. "
                    "Loop abgebrochen."
                )
                if known_limits:
                    msg += f" {len(known_limits)} bekannte Grenze(n) zurückgestellt."
                    all_outputs.append(
                        "--- Bekannte Grenzen (zurückgestellt, mit Begründung) ---\n"
                        + format_known_limits(known_limits)
                    )
                print(f"  [dev-loop] {msg}")
                notify_tool_done(self.name, iteration, False, msg)
                return ToolResult(
                    success=False,
                    output="\n\n".join(all_outputs),
                    iterations=iteration,
                    error=msg,
                    **tokens.as_kwargs(),
                )
            seen_review_signatures.add(review_sig)

            # Store context for next execution. The resolution output is re-injected
            # verbatim under "fix every finding listed above", so a P3 bullet the
            # resolution reviewer happened to list would be *requested* — which would make
            # the whole "no P3 reaches the executor" contract false through a second door,
            # independent of session history. Strip them; the RESOLVED/PARTIAL verdict and
            # every non-P3 line survive untouched.
            previous_quality_findings = blocking_findings
            previous_resolution_output = (
                strip_p3_lines(resolution_output) if not resolution_ok else ""
            )
            # Filtered out of the prompt, but not dropped: a P3 the resolution reviewer
            # raised joins the closing offer like any other. Otherwise the filter above
            # would just move the silent loss from one place to another.
            for p3 in (f for f in _parse_findings(resolution_output)
                       if f.startswith("- [P3]")):
                deferred_p3.setdefault(p3, None)

            # NO checkpoint is written here, and that is deliberate. An earlier
            # version wrote one at the end of every completed iteration, which looked
            # like cheap insurance and was in fact a scope violation: `state.json`
            # then licensed a resume after ANY interruption — hang, format error,
            # process crash, a hand-reopened task — i.e. exactly the general
            # retry/continuation mechanism the Auftrag rules out under KÜR, complete
            # with the dirty-tree waiver and a reduced budget. Only a capacity park
            # writes a resumable record (`_park_for_capacity`), and it captures every
            # carry-over at park time, so nothing is lost by not checkpointing here.

            # ── Landing round is over ─────────────────────────────────────────
            # The clean case never reaches here: passing reviews return success=True
            # from the branch above, so a run that lands cleanly under the wire is
            # stamped ✅ like any other — that is the whole point of the landing
            # round. Reaching this line means the reviews did NOT pass, and no
            # further iteration is coming, so the outcome is the same terminal
            # tool_runtime_exceeded as before. orchestrator.py is untouched.
            if final_round:
                tracer.emit("run_end", success=False, reason="landing_round_unresolved",
                            iterations=iteration)
                return _terminal_runtime(
                    f"Laufzeitbudget aufgebraucht — Abschlussrunde nach Iteration "
                    f"{iteration} beendet, Reviews noch nicht bestanden",
                    iteration)

            # Phase B: rollover session every cap iterations to bound conversation
            # length. The next iteration's exec prompt will inject prev findings
            # via review_context, so the model resumes seamlessly even though
            # the new session has no Anthropic-side history.
            sess.bump()
            if sess.needs_rollover():
                print(
                    f"  [dev-loop] Session-Rollover nach {sess.iteration_count} "
                    f"Iterationen (cap={sess.cap})"
                )
                sess.rollover(self.name, cwd)
                first_call = True

            time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

        # Max iterations reached
        msg = f"Max Iterationen ({TOOL_MAX_ITERATIONS}) erreicht. Reviews noch nicht vollstaendig bestanden."
        if known_limits:
            msg += f" {len(known_limits)} bekannte Grenze(n) zurückgestellt."
            all_outputs.append(
                "--- Bekannte Grenzen (zurückgestellt, mit Begründung) ---\n"
                + format_known_limits(known_limits)
            )
        print(f"  [dev-loop] {msg}")
        tracer.emit("run_end", success=False, reason="max_iterations", iterations=TOOL_MAX_ITERATIONS)
        notify_tool_done(self.name, TOOL_MAX_ITERATIONS, False, msg)
        return ToolResult(
            success=False,
            output="\n\n".join(all_outputs),
            iterations=TOOL_MAX_ITERATIONS,
            error=msg,
            **tokens.as_kwargs(),
        )
