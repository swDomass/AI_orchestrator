"""
Queue linter — validates agent-queue.md without executing anything.

Catches bad queue entries before they reach a provider:
  - Invalid / missing cwd
  - Unknown #tool:<name>
  - Unknown model alias (#claude_*, #gemini_*, #codex_*, #vibe_*, #or_*) — the shape
    check derives its provider prefixes from queue_manager._MODEL_ALIAS_PREFIXES
    (itself generated from dispatcher._TAG_MAP), not a hand-copied list
  - Cross-provider model leakage (e.g. #claude_opus on a task tagged #gemini)
  - #effort: — unknown level, malformed form the strict regex cannot see
    (#effort: low / #effort=low / trailing punctuation), duplicate tags, and a level on
    a task routed to a non-Claude provider (warning: --effort is Claude-only). Checked
    on subtasks too, since SubTask.effort honours the tag.
  - Duplicate #id: values in the open queue
  - #needs: references that will never resolve
  - #or_* tag without OPENROUTER_API_KEY configured
  - #vibe / #vibe_* tag without the `vibe` CLI on PATH (the task is parked, not
    handed to a fallback executor — see dispatcher._REVIEWER_ONLY)
  - #parallel with no/single subtask, or subtasks sharing CWD
  - HTML comments inside the task body (silently truncate the task text), or at the
    line end without being a valid retry/hang marker (silently dropped on rewrite)

CLI: ``python orchestrator.py --lint-queue``
Exit codes: 0 = clean, 1 = warnings, 2 = errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import sys

from config import (
    CLAUDE_EFFORT_LEVELS,
    OPENROUTER_API_KEY,
    QUEUE_FILE,
    _MODEL_ALIASES_BY_PROVIDER,
    is_known_model_tag,
)
from queue_manager import (
    AT_TAG_RE,
    CWD_RE,
    EFFORT_ATTEMPT_RE,
    EFFORT_TAG_RE,
    EVERY_TAG_RE,
    FRESHONLY_TAG_RE,
    GRACE_TAG_RE,
    HANG_COUNT_RE,
    NEEDS_TAG_RE,
    PARALLEL_TAG_RE,
    PROVIDER_TAG_RE,
    RETRY_TAG_RE,
    VERIFY_TAG_RE,
    _collect_completed_ids,
    _decode_queue_bytes,
    _MODEL_ALIAS_PREFIXES,
    _parse_subtask_line,
    extract_cwd,
    extract_every_tag,
    extract_id_tag,
    extract_needs_tags,
    extract_model_tag,
    extract_pass_providers,
    extract_verify_tag,
    has_cwd_tag,
    _is_whole_day_interval,
)

# Regex for any open task line (subset of OPEN_TASK_RE — without retry-stripping)
_OPEN_TASK_LINE_RE = re.compile(r"^- \[ \] (.+?)(?:\s*<!--.*?-->)?\s*$")

# The only HTML comments a task line may legitimately carry: the schedule markers
# queue_manager appends at the very end of the line. Composed from the queue parser's
# own patterns rather than hand-copied, so a marker format change cannot drift the two
# apart — and a marker the parser would NOT recognise (e.g. missing spaces) is correctly
# treated as a stray comment here. Inherits the two capture groups of the source
# patterns; only ever used with .sub(), so the groups are inert — a future .search()
# would get the timestamp or None depending on which alternative matched.
_TRAILING_MARKER_RE = re.compile(
    rf"\s*(?:{RETRY_TAG_RE.pattern}|{HANG_COUNT_RE.pattern})\s*$"
)

# The LAST comment on the line, if the line ends in one. The tempered dot
# `(?:(?!-->).)*` is load-bearing: a plain lazy `.*?` is still anchored by `$` and would
# span from the first `<!--` to the last `-->` — the exact defect this module reports —
# which would misclassify a body comment as a harmless trailing one.
_TRAILING_COMMENT_RE = re.compile(r"\s*<!--(?:(?!-->).)*-->\s*$")

# Detect any `#or_*` or bare `#openrouter` tag (case-insensitive).
_OPENROUTER_TAG_RE = re.compile(r"(?i)(?<!\S)#(openrouter|or_[A-Za-z0-9_]+)(?=\s|$)")

# Detect any `#vibe` or `#vibe_*` tag (case-insensitive).
_VIBE_TAG_RE = re.compile(r"(?i)(?<!\S)#(vibe(?:_[A-Za-z0-9_]+)?)(?=\s|$)")

# Shape of "looks like a model-alias tag but might not be one" — provider_word plus a
# suffix, e.g. #claude_giga, #vibe_giant. Prefixes come from queue_manager's
# _MODEL_ALIAS_PREFIXES (itself derived from dispatcher._TAG_MAP), not hand-copied —
# that hand-copied list is exactly how this check missed #vibe_* for months: the
# alternation enumerated claude|gemini|codex|or only, predating the vibe aliases.
# `openrouter` is matched as a second, standalone alternative because its own model
# aliases use the `or_` prefix, not the provider name itself.
_UNKNOWN_MODEL_SHAPE_RE = re.compile(
    r"(?i)(?<!\S)#((?:"
    + "|".join(re.escape(p) for p in _MODEL_ALIAS_PREFIXES)
    + r")_[A-Za-z0-9_]+|openrouter)(?=\s|$)"
)

# The permissive counterpart to queue_manager.EFFORT_TAG_RE lives in queue_manager as
# EFFORT_ATTEMPT_RE — shared with strip_metadata_tags() and the parallel_runner
# inheritance rule, which need the identical answer to "was a tag attempted here?".
# Keeping a private copy here is what let the three drift apart. Its boundaries are
# deliberately tight, because a false positive here turns a legitimate line into a lint
# ERROR (exit code 2) — noise in the one report that is supposed to be trustworthy, and
# worse than the silent-tag hole it closes. It does not stop the queue from running:
# `--lint-queue` is an opt-in offline command wired to no CI and no hook in this repo, so
# the exit code only matters to whoever runs it.
_EFFORT_PROBE_RE = EFFORT_ATTEMPT_RE


def _routed_providers(task_text: str) -> set[str]:
    """Every provider this task explicitly routes to — bare ``#<provider>`` tag or any
    model alias.

    Derived from ``_MODEL_ALIASES_BY_PROVIDER`` instead of ``PROVIDER_TAG_RE``, which
    (since it derives its bare provider names from ``dispatcher._TAG_MAP.values()``)
    now matches ``#vibe`` and ``#openrouter`` too, but still only the bare provider
    names — it therefore still misses every *model-alias* tag (``#or_*``, ``#codex_5``,
    ``#gemini_flash_lite``, ...), which route to a provider without naming it. Completeness
    matters here: the caller uses the result to decide whether a Claude-only tag is a
    silent no-op.
    """
    found: set[str] = set()
    for provider, aliases in _MODEL_ALIASES_BY_PROVIDER.items():
        for tag in (provider, *aliases):
            if re.search(rf"(?i)(?<!\S)#{re.escape(tag)}(?![\w-])", task_text):
                found.add(provider)
                break
    return found

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
    for line_no, task_text, _subs, _raw in open_tasks:
        tid = extract_id_tag(task_text)
        if tid:
            open_ids.setdefault(tid, []).append(line_no)

    # Per-task checks
    valid_tool_names = _load_tool_names()
    for line_no, task_text, subtasks, raw_line in open_tasks:
        findings.extend(_check_task(
            line_no=line_no,
            task_text=task_text,
            subtasks=subtasks,
            raw_line=raw_line,
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
    """Yield (line_no, task_text, subtasks_tuple, raw_line) per open task in '## Queue'.

    ``raw_line`` is handed through unparsed because ``task_text`` comes from
    ``_OPEN_TASK_LINE_RE`` and is therefore already truncated when the line carries an
    HTML comment inside the body — checks for that defect must see the raw line.
    """
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
        yield line_no, task_text, tuple(subs), raw


def _check_task(
    *,
    line_no: int,
    task_text: str,
    subtasks: tuple[str, ...],
    raw_line: str,
    open_ids: dict[str, list[int]],
    completed_ids: set[str],
    valid_tool_names: set[str],
) -> list[LintFinding]:
    out: list[LintFinding] = []

    # Runs before the empty-text guard: a line whose text is empty *because* a comment
    # ate it needs the cause reported, not just the symptom.
    out.extend(_check_html_comment(line_no, task_text, raw_line))

    if not task_text:
        out.append(LintFinding(LEVEL_ERROR, line_no, task_text,
                               "leerer Task-Text", code="empty_task"))
        return out

    out.extend(_check_cwd(line_no, task_text))
    out.extend(_check_tool_tag(line_no, task_text, valid_tool_names))
    out.extend(_check_model_tag(line_no, task_text))
    out.extend(_check_openrouter(line_no, task_text))
    out.extend(_check_vibe(line_no, task_text))
    out.extend(_check_duplicate_id(line_no, task_text, open_ids))
    out.extend(_check_needs(line_no, task_text, open_ids, completed_ids))
    out.extend(_check_parallel(line_no, task_text, subtasks))
    out.extend(_check_at_tag(line_no, task_text))
    out.extend(_check_every_tag(line_no, task_text))
    out.extend(_check_anchor_interval(line_no, task_text))
    out.extend(_check_grace_tag(line_no, task_text))
    out.extend(_check_freshonly_tag(line_no, task_text))
    out.extend(_check_verify_tag(line_no, task_text))
    out.extend(_check_effort_tag(line_no, task_text))
    # #effort: is also honoured on subtasks (parallel_runner.SubTask.effort), so the
    # check has to see them too — otherwise `  - [ ] Teil A #effort:ultra` runs at the
    # session default with no lint error and no warning, which is exactly the failure
    # mode the loose regex exists to prevent.
    # NOTE: the same blind spot pre-exists for model tags on subtasks. Not widened here
    # on purpose — fixing it would add findings to existing queues, which is a separate
    # change, not part of introducing #effort:.
    # KNOWN LIMITATION: the parent's line_no is reported for every subtask, because
    # _iter_open_tasks() yields subtasks as plain texts without their own line numbers.
    # With several subtasks the finding therefore points at the parent line. Carrying
    # per-subtask line numbers means changing _iter_open_tasks and every consumer
    # (_check_parallel among them) — a separate change, not part of adding #effort:.
    for subtask_text in subtasks:
        out.extend(_check_effort_tag(line_no, subtask_text))
    return out


def _check_effort_tag(line_no: int, task_text: str) -> list[LintFinding]:
    """Flag a malformed or ineffective ``#effort:`` tag.

    Fail-OPEN like ``#verify:``: a bad level parses away to None and the task simply
    runs at the session default rather than failing the run.

    How visible the mistake is depends on where the tag sits. On a **parent** task
    ``orchestrator.run_once()`` also logs a warning (unknown level *and* malformed shape),
    so a scheduled run leaves a trace. On a **subtask** it does not — ``_parse_subtask``
    calls ``extract_effort_tag`` and warns about nothing — and a bare ``#effort`` without a
    value is warned about nowhere. For those two, this check is the only place the mistake
    becomes visible at all.

    The strict regex alone is not enough — it cannot see forms it does not match. So the
    permissive `_EFFORT_PROBE_RE` runs over **every** occurrence, mirroring _check_at_tag's
    two-regex approach. Checking "is there any strict match?" first would let a malformed
    token hide behind a well-formed one (`#effort:low #effort=high` reported nothing, and
    `strip_metadata_tags()` cannot remove the malformed half either, so it leaked into the
    prompt).
    """
    if "#effort" not in task_text.lower():
        return []

    out: list[LintFinding] = []
    strict = list(EFFORT_TAG_RE.finditer(task_text))
    strict_starts = {m.start() for m in strict}

    # Every #effort-shaped token that is NOT a well-formed tag at that exact position.
    # Position-based, not text-based: `(#effort:low)` yields a probe token that would
    # pass a standalone strict match, yet the strict regex finds nothing in the real
    # string because of the leading paren.
    for probe in _EFFORT_PROBE_RE.finditer(task_text):
        if probe.start() in strict_starts:
            continue
        out.append(LintFinding(
            LEVEL_ERROR, line_no, task_text,
            f"unbrauchbares Effort-Tag '{probe.group(0).strip()}' — "
            f"erwartet '#effort:<level>' ohne Leerzeichen und ohne Satzzeichen, "
            f"Level: {', '.join(CLAUDE_EFFORT_LEVELS)}",
            code="malformed_effort",
        ))

    if not strict:
        return out

    # More than one tag: only the first is applied, the rest look active and are not.
    if len(strict) > 1:
        out.append(LintFinding(
            LEVEL_ERROR, line_no, task_text,
            f"{len(strict)}× #effort: in einer Zeile — nur das erste Tag wirkt, "
            f"die übrigen sehen aktiv aus und sind es nicht",
            code="effort_duplicate_tag",
        ))

    raw_level = strict[0].group(1)
    if raw_level.lower() not in CLAUDE_EFFORT_LEVELS:
        out.append(LintFinding(
            LEVEL_ERROR, line_no, task_text,
            f"unbekanntes Effort-Level '#effort:{raw_level}' — erlaubt: "
            f"{', '.join(CLAUDE_EFFORT_LEVELS)}",
            code="unknown_effort",
        ))

    # --effort exists only on the Claude CLI. On a task explicitly routed elsewhere the
    # tag is a silent no-op: the user asked for an effort level and gets none. Warning,
    # not error — the task itself still runs correctly (cf. the OpenRouter missing-key
    # warning), unlike the cross-provider MODEL leak which is an error.
    routed = _routed_providers(task_text)
    if routed and "claude" not in routed:
        out.append(LintFinding(
            LEVEL_WARN, line_no, task_text,
            f"'#effort:' wirkt nur auf Claude, Task ist aber auf {sorted(routed)} geroutet — "
            f"das Tag bleibt ohne Wirkung",
            code="effort_non_claude",
        ))

    return out


def _check_verify_tag(line_no: int, task_text: str) -> list[LintFinding]:
    """Flag a ``#verify:`` that carries no script path.

    Fail-OPEN by nature and therefore worth an error: the tag parses, the task runs, and
    the outcome check simply never happens — a typo silently removes the very safety net
    that exists because silent failures are hard to notice. The task itself keeps working,
    so nothing else in the system will ever complain.
    """
    if "#verify:" not in task_text.lower():
        return []

    out: list[LintFinding] = []

    # More than one tag: strip_metadata_tags removes them all, but only the FIRST is
    # ever executed. The others look like active checks and silently are not.
    matches = VERIFY_TAG_RE.findall(task_text)
    if len(matches) > 1:
        out.append(LintFinding(
            LEVEL_ERROR, line_no, task_text,
            f"{len(matches)}× #verify: in einer Zeile — nur das erste Tag wird "
            f"ausgeführt, die übrigen sehen aktiv aus und sind es nicht",
            code="verify_duplicate_tag",
        ))

    if not extract_verify_tag(task_text):
        out.append(LintFinding(
            LEVEL_ERROR, line_no, task_text,
            "#verify: ohne erkennbaren Skript-Pfad — der Post-Task-Check wird still "
            "übersprungen (fail-open). Entweder fehlt der Pfad, oder das Tag klebt am "
            "vorigen Wort (Tags brauchen ein Leerzeichen davor, sonst greift das "
            "Lookbehind nicht). Pfade mit Leerzeichen oder '#' in Anführungszeichen setzen",
            code="verify_without_path",
        ))

    # Odd number of quotes after the tag → the quoted branch cannot match and the path
    # is silently read as an unquoted one, truncated at the first space.
    tail = task_text[task_text.lower().index("#verify:"):]
    if tail.count('"') % 2 == 1:
        out.append(LintFinding(
            LEVEL_ERROR, line_no, task_text,
            "#verify: mit unbalancierten Anführungszeichen — der Pfad wird dann als "
            "unquoted gelesen und am ersten Leerzeichen abgeschnitten",
            code="verify_unbalanced_quotes",
        ))

    return out


def _check_html_comment(line_no: int, task_text: str, raw_line: str) -> list[LintFinding]:
    """Flag HTML comments a task line must not carry.

    ``OPEN_TASK_RE`` only tolerates comments at the very end of the line, and
    ``queue_manager`` only ever writes ``retry``/``hang`` markers there. Everything else
    fails in one of two distinct ways, so they get distinct findings:

    * **In the body** — as soon as the line *ends* in a comment, the parser ends the task
      text at the first ``<!--`` and treats everything up to the last ``-->`` on the line
      as the marker. Rewriting the completed line then deletes that swallowed range —
      typically including the ``#every:``/``#at:``/model tags — from the file for good,
      and the recurring task silently stops firing. Flagged even while the line does not
      yet end in ``-->`` and the text still parses in full: on a ``#every:`` task the
      first successful completion appends a retry marker and arms the defect.
    * **At the end, but not a marker the parser recognises** — the task text survives,
      but the comment is dropped on the next rewrite. If it was *meant* as a schedule
      marker, the framing must be exact (``RETRY_TAG_RE`` requires the spaces after
      ``<!--``, after ``retry:`` and before ``-->``; extra spaces are tolerated, missing
      ones are not), and a near-miss means the schedule silently never applies.

    Known blind spot: a body comment that happens to be marker-shaped *and* sits at the
    line end (``- [ ] Setze auf <!-- hang: 3 -->``) is indistinguishable from a real
    marker and passes. Truncation still occurs there — but the same string is what the
    orchestrator legitimately writes, so it cannot be separated by inspection.
    """
    body = raw_line.rstrip()
    while True:
        stripped = _TRAILING_MARKER_RE.sub("", body)
        if stripped == body:
            break
        body = stripped

    if "<!--" not in body:
        return []

    without_trailing = _TRAILING_COMMENT_RE.sub("", body)
    if "<!--" not in without_trailing:
        return [LintFinding(
            LEVEL_ERROR, line_no, task_text,
            "HTML-Kommentar am Zeilenende, den der Parser nicht als retry-/hang-Marker "
            "erkennt — er wird beim nächsten Zurückschreiben der Zeile kommentarlos "
            "gelöscht. War er als Schedule-Marker gemeint: die Leerzeichen nach '<!--', "
            "nach 'retry:' und vor '-->' sind Pflicht ('<!-- retry: YYYY-MM-DD HH:MM -->'), "
            "sonst greift der Zeitplan nie",
            code="html_comment_trailing",
        )]

    return [LintFinding(
        LEVEL_ERROR, line_no, task_text,
        "HTML-Kommentar im Task-Text — sobald die Zeile in einem Kommentar endet (bei "
        "#every: spätestens nach dem ersten angehängten retry-Marker), schneidet der "
        "Parser den Task ab dem ersten '<!--' ab; beim Zurückschreiben der erledigten "
        "Zeile gehen die dahinter stehenden Tags (#every:, #at:, Modell) dauerhaft "
        "verloren. Kommentar entfernen oder den Inhalt in eine Skill-Datei auslagern",
        code="html_comment_in_body",
    )]


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

    # Detect unknown alias *shape* (#claude_unknown, #or_xxx, #vibe_xxx, ...) —
    # anything that looks like a model tag but isn't in our alias tables.
    for m in _UNKNOWN_MODEL_SHAPE_RE.finditer(task_text):
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

    # PASS_PROVIDER_TAG_RE only ever matches a registered provider name (it is
    # generated from dispatcher._TAG_MAP since b947b76), so there is no unknown
    # provider to check for there. But we DO want to flag a model alias whose
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


def _check_vibe(line_no: int, task_text: str) -> list[LintFinding]:
    """Tasks tagged #vibe or #vibe_* require the `vibe` binary on PATH.

    Unlike a missing OPENROUTER_API_KEY, this does NOT fall back to the default
    chain: dispatcher._REVIEWER_ONLY parks the task instead, because degrading a
    reviewer-only tag to a file-writing executor would be a wider blast radius
    than the task asked for. Still a warning, not an error — the queue line
    itself is well-formed and starts working the moment `vibe` is installed.
    """
    if not _VIBE_TAG_RE.search(task_text):
        return []
    if shutil.which("vibe"):
        return []
    return [LintFinding(
        LEVEL_WARN, line_no, task_text,
        "#vibe / #vibe_* gesetzt, aber die 'vibe'-CLI ist nicht im PATH — anders als "
        "bei OpenRouter fällt der Task NICHT auf die default-Chain zurück (Vibe ist "
        "reviewer-only), sondern wird geparkt",
        code="vibe_missing_cli",
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


def _check_at_tag(line_no: int, task_text: str) -> list[LintFinding]:
    """`#at:` only accepts forms _retry_is_due() understands. Catch malformed
    values that look like they're trying to be a schedule but aren't parseable."""
    # Detect any `#at:<something>` even if it doesn't match the strict regex,
    # so we can flag the malformed case.
    permissive = re.search(r"(?i)(?<!\S)#at:(\S+)", task_text)
    if not permissive:
        return []
    strict = AT_TAG_RE.search(task_text)
    if strict:
        return []  # well-formed
    return [LintFinding(
        LEVEL_ERROR, line_no, task_text,
        f"#at: erwartet 'YYYY-MM-DDTHH:MM', 'YYYY-MM-DD HH:MM' oder 'HH:MM' "
        f"(bekam: {permissive.group(1)!r})",
        code="invalid_at",
    )]


def _check_every_tag(line_no: int, task_text: str) -> list[LintFinding]:
    """`#every:` must be `<number><s|m|h|d>`."""
    permissive = re.search(r"(?i)(?<!\S)#every:(\S+)", task_text)
    if not permissive:
        return []
    strict = EVERY_TAG_RE.search(task_text)
    if strict:
        return []
    return [LintFinding(
        LEVEL_ERROR, line_no, task_text,
        f"#every: erwartet '<zahl><s|m|h|d>', z.B. '#every:24h' "
        f"(bekam: {permissive.group(1)!r})",
        code="invalid_every",
    )]


def _check_grace_tag(line_no: int, task_text: str) -> list[LintFinding]:
    """`#grace:` must be `<number><s|m|h|d>` and is only used by `#freshonly` tasks."""
    permissive = re.search(r"(?i)(?<!\S)#grace:(\S+)", task_text)
    if not permissive:
        return []
    out: list[LintFinding] = []
    if not GRACE_TAG_RE.search(task_text):
        out.append(LintFinding(
            LEVEL_ERROR, line_no, task_text,
            f"#grace: erwartet '<zahl><s|m|h|d>', z.B. '#grace:4h' "
            f"(bekam: {permissive.group(1)!r})",
            code="invalid_grace",
        ))
        return out
    if not FRESHONLY_TAG_RE.search(task_text):
        out.append(LintFinding(
            LEVEL_WARN, line_no, task_text,
            "#grace: ohne #freshonly hat keine Wirkung (Grace gilt nur für #freshonly-Tasks)",
            code="grace_without_freshonly",
        ))
    return out


def _check_anchor_interval(line_no: int, task_text: str) -> list[LintFinding]:
    """A `#at:` time-of-day anchor only drives recurrence for whole-day `#every:`
    intervals (24h, 48h, 7d, ...). With a sub-day or non-whole-day interval the anchor
    is ignored for rescheduling (the task falls back to now+interval) — flag the silent
    mismatch so the user isn't surprised the slot still drifts."""
    if not AT_TAG_RE.search(task_text):
        return []
    every_sec = extract_every_tag(task_text)
    if every_sec is None or _is_whole_day_interval(every_sec):
        return []
    return [LintFinding(
        LEVEL_WARN, line_no, task_text,
        "#at:-Anker wirkt nur bei ganztägigem #every: (24h, 7d, …) als Tageszeit-Anker; "
        "bei diesem Intervall wird er fürs Reschedule ignoriert (now+Intervall, Slot driftet)",
        code="anchor_subday_interval",
    )]


def _check_freshonly_tag(line_no: int, task_text: str) -> list[LintFinding]:
    """`#freshonly` is a bare flag, only meaningful on recurring (`#every:`) tasks."""
    # A value-bearing form (#freshonly:false) is a mistake — the flag takes no value
    # and would otherwise be silently ignored.
    if re.search(r"(?i)(?<!\S)#freshonly:\S*", task_text):
        return [LintFinding(
            LEVEL_WARN, line_no, task_text,
            "#freshonly ist ein Flag ohne Wert — '#freshonly:...' wird ignoriert, "
            "nutze nur '#freshonly'",
            code="freshonly_with_value",
        )]
    if not FRESHONLY_TAG_RE.search(task_text):
        return []
    if EVERY_TAG_RE.search(task_text):
        return []
    return [LintFinding(
        LEVEL_WARN, line_no, task_text,
        "#freshonly ohne #every hat keine Wirkung (gilt nur für wiederkehrende Tasks)",
        code="freshonly_without_every",
    )]
