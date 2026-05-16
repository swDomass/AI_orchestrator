"""
Queue linter — validates agent-queue.md without executing anything.

Catches bad queue entries before they reach a provider:
  - Invalid / missing cwd
  - Unknown #tool:<name>
  - Unknown model alias (#claude_*, #gemini_*, #codex_*, #or_*)
  - Cross-provider model leakage (e.g. #claude_opus on a task tagged #gemini)
  - Duplicate #id: values in the open queue
  - #needs: references that will never resolve
  - #or_* tag without OPENROUTER_API_KEY configured
  - #parallel with no/single subtask, or subtasks sharing CWD

CLI: ``python orchestrator.py --lint-queue``
Exit codes: 0 = clean, 1 = warnings, 2 = errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sys

from config import (
    OPENROUTER_API_KEY,
    QUEUE_FILE,
    _MODEL_ALIASES_BY_PROVIDER,
    is_known_model_tag,
)
from queue_manager import (
    CWD_RE,
    MODEL_TAG_RE,
    NEEDS_TAG_RE,
    PARALLEL_TAG_RE,
    PROVIDER_TAG_RE,
    _collect_completed_ids,
    _decode_queue_bytes,
    _parse_subtask_line,
    extract_cwd,
    extract_id_tag,
    extract_needs_tags,
    extract_model_tag,
    extract_pass_providers,
    has_cwd_tag,
)

# Regex for any open task line (subset of OPEN_TASK_RE — without retry-stripping)
_OPEN_TASK_LINE_RE = re.compile(r"^- \[ \] (.+?)(?:\s*<!--.*?-->)?\s*$")

# Detect any `#or_*` or bare `#openrouter` tag (case-insensitive).
_OPENROUTER_TAG_RE = re.compile(r"(?i)(?<!\S)#(openrouter|or_[A-Za-z0-9_]+)(?=\s|$)")

LEVEL_ERROR = "error"
LEVEL_WARN = "warning"
LEVEL_INFO = "info"

_LEVEL_ICON = {
    LEVEL_ERROR: "ERROR",
    LEVEL_WARN:  "WARN ",
    LEVEL_INFO:  "INFO ",
}


@dataclass(frozen=True)
class LintFinding:
    level: str
    line_no: int | None
    task_text: str
    message: str
    code: str = ""

    def format(self) -> str:
        ln = f":{self.line_no}" if self.line_no is not None else ""
        snippet = self.task_text[:80] + ("…" if len(self.task_text) > 80 else "")
        return f"[{_LEVEL_ICON[self.level]}] line{ln}: {self.message} — {snippet!r}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def lint_queue(content: str | None = None) -> list[LintFinding]:
    """Run all lint checks on the queue.

    Args:
        content: full agent-queue.md content. If None, reads QUEUE_FILE.

    Returns:
        List of findings, ordered by line number.
    """
    if content is None:
        content = _read_queue_file()

    if not content.strip():
        return []

    open_tasks = list(_iter_open_tasks(content))
    if not open_tasks:
        return []

    findings: list[LintFinding] = []

    # Build cross-task indexes once
    completed_ids = _collect_completed_ids(content)
    open_ids: dict[str, list[int]] = {}
    for line_no, task_text, _subs in open_tasks:
        tid = extract_id_tag(task_text)
        if tid:
            open_ids.setdefault(tid, []).append(line_no)

    # Per-task checks
    valid_tool_names = _load_tool_names()
    for line_no, task_text, subtasks in open_tasks:
        findings.extend(_check_task(
            line_no=line_no,
            task_text=task_text,
            subtasks=subtasks,
            open_ids=open_ids,
            completed_ids=completed_ids,
            valid_tool_names=valid_tool_names,
        ))

    findings.sort(key=lambda f: (f.line_no or 0, f.level != LEVEL_ERROR))
    return findings


def format_findings(findings: list[LintFinding]) -> str:
    """Render findings as a printable report."""
    if not findings:
        return "Queue-Lint: keine Probleme gefunden.\n"

    by_level = {LEVEL_ERROR: 0, LEVEL_WARN: 0, LEVEL_INFO: 0}
    for f in findings:
        by_level[f.level] = by_level.get(f.level, 0) + 1

    lines = [f.format() for f in findings]
    summary = (
        f"\n{by_level[LEVEL_ERROR]} error(s), "
        f"{by_level[LEVEL_WARN]} warning(s), "
        f"{by_level[LEVEL_INFO]} info"
    )
    return "\n".join(lines) + summary + "\n"


def exit_code_for(findings: list[LintFinding]) -> int:
    """Return 0 (clean), 1 (warnings only), or 2 (errors)."""
    if any(f.level == LEVEL_ERROR for f in findings):
        return 2
    if any(f.level == LEVEL_WARN for f in findings):
        return 1
    return 0


def run_lint() -> int:
    """CLI entry: print findings, return exit code."""
    findings = lint_queue()
    sys.stdout.write(format_findings(findings))
    return exit_code_for(findings)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _read_queue_file() -> str:
    """Read the queue file with encoding fallback. Returns '' if missing."""
    path = Path(QUEUE_FILE)
    if not path.exists():
        return ""
    try:
        return _decode_queue_bytes(path.read_bytes())
    except OSError:
        return ""


def _load_tool_names() -> set[str]:
    """Return registered #tool: names. Importing tools is heavyweight, but the
    linter only runs on-demand from the CLI, so the cost is acceptable."""
    try:
        from tools import list_tools
        return set(list_tools().keys())
    except Exception:
        # If tool imports fail for any reason, skip the unknown-tool check.
        return set()


def _iter_open_tasks(content: str):
    """Yield (line_no, task_text, subtasks_tuple) for every open task in '## Queue'."""
    in_queue = False
    all_lines = content.splitlines()
    for line_idx, raw in enumerate(all_lines):
        line_no = line_idx + 1
        if raw.startswith("## "):
            in_queue = raw.strip() == "## Queue"
            continue
        if not in_queue:
            continue
        m = _OPEN_TASK_LINE_RE.match(raw)
        if not m:
            continue
        task_text = m.group(1).strip()
        # Collect indented subtasks following a #parallel task
        subs: list[str] = []
        if PARALLEL_TAG_RE.search(task_text):
            j = line_idx + 1
            while j < len(all_lines):
                st = _parse_subtask_line(all_lines[j].rstrip())
                if st is None:
                    break
                subs.append(st)
                j += 1
        yield line_no, task_text, tuple(subs)


def _check_task(
    *,
    line_no: int,
    task_text: str,
    subtasks: tuple[str, ...],
    open_ids: dict[str, list[int]],
    completed_ids: set[str],
    valid_tool_names: set[str],
) -> list[LintFinding]:
    out: list[LintFinding] = []

    if not task_text:
        out.append(LintFinding(LEVEL_ERROR, line_no, task_text,
                               "leerer Task-Text", code="empty_task"))
        return out

    out.extend(_check_cwd(line_no, task_text))
    out.extend(_check_tool_tag(line_no, task_text, valid_tool_names))
    out.extend(_check_model_tag(line_no, task_text))
    out.extend(_check_openrouter(line_no, task_text))
    out.extend(_check_duplicate_id(line_no, task_text, open_ids))
    out.extend(_check_needs(line_no, task_text, open_ids, completed_ids))
    out.extend(_check_parallel(line_no, task_text, subtasks))
    return out


def _check_cwd(line_no: int, task_text: str) -> list[LintFinding]:
    has_tag = has_cwd_tag(task_text)
    if not has_tag:
        return []
    # extract_cwd returns None when the tag exists but is invalid (dir missing
    # or outside ALLOWED_CWD_ROOTS). It prints a warning to stdout — we just
    # check the boolean result here.
    if extract_cwd(task_text) is None:
        return [LintFinding(LEVEL_ERROR, line_no, task_text,
                            "cwd: Pfad existiert nicht oder ist außerhalb der ALLOWED_CWD_ROOTS",
                            code="invalid_cwd")]
    return []


def _check_tool_tag(line_no: int, task_text: str, valid: set[str]) -> list[LintFinding]:
    # Use a permissive regex so we can flag unknown names (not just registered ones)
    m = re.search(r"#tool:([A-Za-z0-9_-]+)", task_text)
    if not m:
        return []
    name = m.group(1).lower()
    if not valid:
        # Tool registry import failed — skip silently
        return []
    if name not in valid:
        known = ", ".join(sorted(valid))
        return [LintFinding(LEVEL_ERROR, line_no, task_text,
                            f"unbekanntes #tool:{name} (bekannt: {known})",
                            code="unknown_tool")]
    return []


def _check_model_tag(line_no: int, task_text: str) -> list[LintFinding]:
    """Model alias must (a) be known and (b) belong to the explicitly tagged provider."""
    out: list[LintFinding] = []

    # Detect unknown alias *shape* (#claude_unknown, #or_xxx, ...) — anything
    # that looks like a model tag but isn't in our alias tables.
    for m in re.finditer(
        r"(?i)(?<!\S)#((?:claude|gemini|codex|or)_[A-Za-z0-9_]+|openrouter)(?=\s|$)",
        task_text,
    ):
        tag = m.group(1).lower()
        if tag == "openrouter":
            continue  # not a model tag — handled by _check_openrouter
        if not is_known_model_tag(tag):
            out.append(LintFinding(LEVEL_ERROR, line_no, task_text,
                                   f"unbekannter Model-Alias '#{tag}'",
                                   code="unknown_model"))

    # Cross-provider model leakage: a #claude_* model on a task that also has
    # an explicit #gemini or #codex provider tag (or vice versa).
    model_tag = extract_model_tag(task_text)  # only returns native CLI aliases
    if model_tag:
        owning = _owning_provider_for_alias(model_tag)
        explicit_providers = {
            p.group(0).lstrip("#").lower()
            for p in PROVIDER_TAG_RE.finditer(task_text)
        }
        if owning and explicit_providers and owning not in explicit_providers:
            out.append(LintFinding(
                LEVEL_ERROR, line_no, task_text,
                f"Model-Alias '#{model_tag}' gehört zu '{owning}', "
                f"Task ist aber explizit auf {sorted(explicit_providers)} geroutet",
                code="model_provider_mismatch",
            ))

    # Pass-provider regex restricts to claude|gemini|codex — nothing to check
    # for unknown providers there. But we DO want to flag a model alias whose
    # owning provider isn't covered by any #pass1:/#pass2: tag when both are set.
    pass_providers = extract_pass_providers(task_text)
    if model_tag and pass_providers and not explicit_providers:
        owning = _owning_provider_for_alias(model_tag)
        if owning and owning not in pass_providers.values():
            out.append(LintFinding(
                LEVEL_WARN, line_no, task_text,
                f"Model '#{model_tag}' ({owning}) ist nicht in #pass1/#pass2 verwendet",
                code="model_unused_in_pass",
            ))

    return out


def _check_openrouter(line_no: int, task_text: str) -> list[LintFinding]:
    """Tasks tagged #openrouter or #or_* require OPENROUTER_API_KEY. Without it,
    the dispatcher silently falls back to the default chain (claude/gemini/codex),
    so this is a warning — the task still runs."""
    if not _OPENROUTER_TAG_RE.search(task_text):
        return []
    if OPENROUTER_API_KEY:
        return []
    return [LintFinding(
        LEVEL_WARN, line_no, task_text,
        "#openrouter / #or_* gesetzt, aber OPENROUTER_API_KEY nicht konfiguriert — "
        "Task fällt auf default-Chain zurück",
        code="openrouter_missing_key",
    )]


def _check_duplicate_id(
    line_no: int, task_text: str, open_ids: dict[str, list[int]]
) -> list[LintFinding]:
    tid = extract_id_tag(task_text)
    if not tid:
        return []
    occurrences = open_ids.get(tid, [])
    if len(occurrences) <= 1:
        return []
    # Only report the duplicate on first encounter (line_no == occurrences[0])
    # but we report on every duplicate line so each gets flagged in CLI output.
    if line_no == occurrences[0]:
        others = ", ".join(str(ln) for ln in occurrences[1:])
        return [LintFinding(
            LEVEL_ERROR, line_no, task_text,
            f"doppelte #id:{tid} (auch auf Zeile(n) {others})",
            code="duplicate_id",
        )]
    others = ", ".join(str(ln) for ln in occurrences if ln != line_no)
    return [LintFinding(
        LEVEL_ERROR, line_no, task_text,
        f"doppelte #id:{tid} (auch auf Zeile(n) {others})",
        code="duplicate_id",
    )]


def _check_needs(
    line_no: int,
    task_text: str,
    open_ids: dict[str, list[int]],
    completed_ids: set[str],
) -> list[LintFinding]:
    needs = extract_needs_tags(task_text)
    if not needs:
        return []
    missing: list[str] = []
    for dep in needs:
        if dep in completed_ids:
            continue
        if dep in open_ids:
            continue
        missing.append(dep)
    if not missing:
        return []
    return [LintFinding(
        LEVEL_WARN, line_no, task_text,
        f"#needs: verweist auf unbekannte ID(s): {', '.join(missing)}",
        code="unknown_needs",
    )]


def _check_parallel(
    line_no: int, task_text: str, subtasks: tuple[str, ...]
) -> list[LintFinding]:
    if not PARALLEL_TAG_RE.search(task_text):
        return []
    if len(subtasks) <= 1:
        return [LintFinding(
            LEVEL_WARN, line_no, task_text,
            f"#parallel ohne mehrere Subtasks ({len(subtasks)} gefunden) — kein Parallelismus",
            code="parallel_no_subtasks",
        )]

    # Check if subtasks share CWD (would be sequentialized by the runner)
    cwds: list[str | None] = []
    for st in subtasks:
        cwd = extract_cwd(st)
        cwds.append(cwd)
    distinct = {c for c in cwds if c}
    if any(c is None for c in cwds) and not distinct:
        return [LintFinding(
            LEVEL_INFO, line_no, task_text,
            "#parallel: kein cwd: in Subtasks — alle erben Parent-CWD und laufen sequentiell",
            code="parallel_shared_cwd",
        )]
    if len(distinct) < sum(1 for c in cwds if c):
        return [LintFinding(
            LEVEL_INFO, line_no, task_text,
            "#parallel: einige Subtasks teilen sich cwd — laufen innerhalb der CWD-Gruppe sequentiell",
            code="parallel_shared_cwd",
        )]
    return []


def _owning_provider_for_alias(alias: str) -> str | None:
    """Return the provider name that owns a given model alias, or None."""
    for provider, aliases in _MODEL_ALIASES_BY_PROVIDER.items():
        if alias in aliases:
            return provider
    return None
