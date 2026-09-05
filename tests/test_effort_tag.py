"""Tests for the #effort:<level> queue tag → `claude --effort <level>`.

Covers the four things that can silently break this feature:
  1. extraction (including an invalid level, which must NOT masquerade as "no tag")
  2. flag pass-through on Claude, and its ABSENCE without a tag
  3. graceful ignore on providers that have no --effort flag
  4. the lint guard that makes a misspelled level visible at all
"""

import threading
from types import SimpleNamespace

import pytest

from unittest.mock import patch

import queue_manager
from config import CLAUDE_EFFORT_LEVELS
from providers.claude import ClaudeProvider
from providers.codex import CodexProvider

with patch("config._load_dotenv"):
    from queue_linter import LEVEL_ERROR, LEVEL_WARN, lint_queue


# ── Extraction ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("level", sorted(CLAUDE_EFFORT_LEVELS))
def test_extract_effort_tag_accepts_every_valid_level(level):
    assert queue_manager.extract_effort_tag(f"Refactor module #effort:{level}") == level


def test_extract_effort_tag_is_case_insensitive():
    assert queue_manager.extract_effort_tag("Task #EFFORT:XHIGH") == "xhigh"


def test_extract_effort_tag_without_tag_returns_none():
    assert queue_manager.extract_effort_tag("Plain task with no tags") is None


def test_extract_effort_tag_rejects_unknown_level():
    """An unknown level degrades to None (session default) — the loud part is the
    lint finding, not an exception that would kill a scheduled run."""
    assert queue_manager.extract_effort_tag("Task #effort:ultra") is None


def test_extract_effort_tag_matches_mid_text_and_at_line_end():
    assert queue_manager.extract_effort_tag("Fix #effort:low then ship") == "low"
    assert queue_manager.extract_effort_tag("Fix then ship #effort:max") == "max"


def test_extract_effort_tag_ignores_substring_in_word():
    """`(?<!\\S)` guards against matching inside another token."""
    assert queue_manager.extract_effort_tag("see no#effort:low here") is None


def test_extract_effort_tag_raw_keeps_invalid_value():
    """The raw variant is what lets callers tell "no tag" from "bad value" — the
    validating extractor collapses both to None."""
    assert queue_manager.extract_effort_tag_raw("Task #effort:ultra") == "ultra"
    assert queue_manager.extract_effort_tag_raw("Task #effort:LOW") == "low"
    assert queue_manager.extract_effort_tag_raw("Task without tag") is None


def test_strip_metadata_tags_removes_effort_tag():
    """Routing metadata must not reach the prompt. Two failure modes if it does: the
    model reads a literal `#effort:low` as task text, and the "line was nothing but
    metadata" guard in run_once() stops firing."""
    assert queue_manager.strip_metadata_tags(
        "Klassifiziere Inbox #claude_opus #effort:low"
    ) == "Klassifiziere Inbox"
    # A line of pure metadata must strip to empty, so the empty-task guard still trips.
    assert queue_manager.strip_metadata_tags("#tool:review-loop #effort:low") == ""
    # Malformed levels are stripped too — the regex is loose on purpose.
    assert queue_manager.strip_metadata_tags("Task #effort:ultra") == "Task"


@pytest.mark.parametrize("line, expected", [
    # The regression: EFFORT_TAG_RE removed only the well-formed tag, so the broken half
    # survived and went to the provider as literal task text.
    ("Task #effort:low #effort=high", "Task"),
    ("Task #effort=high", "Task"),
    ("Task #effort: high", "Task"),
    ("Task #effort:low.", "Task"),
])
def test_strip_metadata_tags_removes_malformed_effort_attempts(line, expected):
    """Stripping has to follow the *attempt*, not only the usable tag. Linting reports a
    malformed tag but is not an execution gate, so a scheduled run would otherwise ship
    the broken token to the model."""
    assert queue_manager.strip_metadata_tags(line) == expected


def test_strip_metadata_tags_leaves_prose_and_markdown_links_alone():
    """Only attempts carrying a value are removed. A bare "#effort" in prose stays (the
    linter still reports it), and a Markdown fragment link must survive untouched —
    mangling it would silently rewrite the task text."""
    assert queue_manager.strip_metadata_tags(
        "Increase the #effort here"
    ) == "Increase the #effort here"
    assert queue_manager.strip_metadata_tags(
        "Review [effort docs](#effort:low)"
    ) == "Review [effort docs](#effort:low)"


@pytest.mark.parametrize("line, expected", [
    ("Task #effort:low", True),
    ("Task #effort:ultra", True),      # unknown level is still an attempt
    ("Task #effort=high", True),       # malformed separator, still an attempt
    ("Task #effort: high", True),
    ("Task (#effort:high)", True),
    ("Task #effort", False),           # bare word, no value → not an attempt
    ("Increase the #efforts here", False),
    ("Review [effort docs](#effort:low)", False),   # Markdown fragment link
    ("Plain task", False),
])
def test_has_effort_tag_attempt(line, expected):
    """The canonical "did someone try to set an effort level here?" predicate. It exists
    because extract_effort_tag() AND extract_effort_tag_raw() both collapse every
    malformed shape to None, making "bad tag" indistinguishable from "no tag" — which is
    what let a malformed child tag inherit the parent's level."""
    assert queue_manager.has_effort_tag_attempt(line) is expected


def test_effort_tag_re_still_matches_invalid_level():
    """The regex must stay LOOSE. If it only matched valid levels, a typo would be
    indistinguishable from 'no tag' and the linter could never report it."""
    m = queue_manager.EFFORT_TAG_RE.search("Task #effort:ultra")
    assert m is not None and m.group(1) == "ultra"


# ── Claude: flag pass-through ─────────────────────────────────────────────────

def _capture_claude_cmd(monkeypatch, effort):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="empty output")

    monkeypatch.setattr("providers.claude.run_with_watchdog", fake_run)
    provider = ClaudeProvider()
    provider._forced_effort = effort
    try:
        provider.run("task")
    finally:
        provider._forced_effort = None
    return calls[0]


@pytest.mark.parametrize("level", sorted(CLAUDE_EFFORT_LEVELS))
def test_claude_forced_effort_appends_effort_flag(monkeypatch, level):
    cmd = _capture_claude_cmd(monkeypatch, level)
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == level


def test_claude_without_forced_effort_omits_effort_flag(monkeypatch):
    """Negative test — without a tag the CLI must keep its own session default.
    Without this, an accidental default injection here would silently override
    the user's `effortLevel` setting on every headless run."""
    cmd = _capture_claude_cmd(monkeypatch, None)
    assert "--effort" not in cmd


# ── Graceful ignore on providers without the flag ─────────────────────────────

def test_codex_ignores_forced_effort(monkeypatch):
    """Codex has no --effort flag. A task tagged #effort:low that gets routed to
    Codex (fallback or explicit) must run normally, not crash and not grow a flag."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="empty output")

    monkeypatch.setattr("providers.codex.run_with_watchdog", fake_run)

    provider = CodexProvider()
    provider._forced_effort = "low"
    try:
        result = provider.run("inspect", read_only=True)
    finally:
        provider._forced_effort = None

    assert "--effort" not in calls[0]
    assert result is not None  # ran through, no exception


def test_vibe_ignores_forced_effort(monkeypatch):
    """Same graceful-ignore contract as Codex. The constraint names Codex, Mistral/Vibe
    and OpenRouter explicitly, and only Codex was covered — the other two paths were
    verified by hand once and then left unprotected."""
    from providers.vibe import VibeProvider

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="OK", stderr="", stdin_error=None)

    monkeypatch.setattr("providers.vibe.run_with_watchdog", fake_run)

    provider = VibeProvider()
    provider._forced_effort = "xhigh"
    try:
        result = provider.run("review this", read_only=True)
    finally:
        provider._forced_effort = None

    assert "--effort" not in calls[0]
    assert result is not None


def test_openrouter_ignores_forced_effort(monkeypatch):
    """OpenRouter is an HTTP provider — there is no CLI to grow a flag, but a shared
    `_forced_effort` must not leak into the JSON body or raise."""
    import json

    from providers.openrouter import OpenRouterProvider

    monkeypatch.setattr("config.OPENROUTER_API_KEY", "test-key-12345")
    monkeypatch.setattr("config.OPENROUTER_BASE_URL", "https://or.test/v1")
    monkeypatch.setattr("config.OPENROUTER_DEFAULT_MODEL", "test/default-model")

    sent: list[dict] = []

    class _Resp:
        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8")))
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    provider = OpenRouterProvider()
    provider._forced_effort = "max"
    try:
        result = provider.run("review this")
    finally:
        provider._forced_effort = None

    assert result.success is True
    assert "effort" not in sent[0], "effort must not leak into the OpenRouter request body"


def test_base_provider_effort_defaults_to_none():
    assert ClaudeProvider()._forced_effort is None
    assert CodexProvider()._forced_effort is None


def test_forced_effort_is_thread_local():
    """Providers are shared singletons — a level set in one worker thread must not
    leak into a parallel subtask on another thread."""
    provider = ClaudeProvider()
    provider._forced_effort = "max"
    seen: list[str | None] = []

    def worker():
        seen.append(provider._forced_effort)
        provider._forced_effort = "low"

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert seen == [None]              # worker never saw the main thread's value
    assert provider._forced_effort == "max"  # and could not overwrite it
    provider._forced_effort = None


# ── Lint guard ────────────────────────────────────────────────────────────────

def _lint_codes(task_line):
    content = f"# Queue\n\n## Queue\n\n- [ ] {task_line}\n\n## Ergebnisse\n"
    return [(f.level, f.code) for f in lint_queue(content)]


def test_linter_flags_unknown_effort_level():
    assert (LEVEL_ERROR, "unknown_effort") in _lint_codes("Task #effort:ultra")


def test_linter_accepts_valid_effort_level():
    assert "unknown_effort" not in {code for _lvl, code in _lint_codes("Task #effort:low")}


def test_linter_silent_without_effort_tag():
    assert "unknown_effort" not in {code for _lvl, code in _lint_codes("Task without tags")}


@pytest.mark.parametrize("malformed", [
    "Task #effort: low",     # space after colon
    "Task #effort=low",      # wrong separator
    "Task #effort:low.",     # trailing punctuation
    "Task (#effort:low)",    # wrapped in parens
    "Task #effort",          # no value at all
])
def test_linter_flags_malformed_effort_tag(malformed):
    """These forms do NOT match the strict regex, so `extract_effort_tag` returns None and
    the task runs at the session default. `run_once` warns about a malformed shape on a
    parent task, but not on a subtask and not on a bare `#effort` — for those the permissive
    probe in the linter is the only thing that surfaces the mistake at all."""
    assert (LEVEL_ERROR, "malformed_effort") in _lint_codes(malformed)


def test_linter_flags_malformed_tag_hiding_behind_a_valid_one():
    """A malformed token must not escape just because a well-formed tag is also present.
    The probe runs over every occurrence, not only when there is no strict match — the
    malformed half also survives strip_metadata_tags() and would leak into the prompt.
    """
    assert (LEVEL_ERROR, "malformed_effort") in _lint_codes("Task #effort:low #effort=high")
    assert (LEVEL_ERROR, "malformed_effort") in _lint_codes("Task #effort=high #effort:low")


@pytest.mark.parametrize("routing_tag", [
    "#vibe", "#openrouter", "#or_glm", "#codex_5", "#gemini_flash_lite", "#codex",
])
def test_linter_warns_for_every_non_claude_routing_form(routing_tag):
    """The routing check must be complete: bare provider tags AND version-shaped
    model aliases. Uses queue_linter._routed_providers(), not PROVIDER_TAG_RE
    directly — that regex (even after being derived from dispatcher._TAG_MAP,
    which added #vibe/#openrouter to it) only ever matches bare provider names,
    never #or_*/#codex_5/#gemini_flash_lite-style aliases; see the docstring on
    _routed_providers() for why it derives from _MODEL_ALIASES_BY_PROVIDER instead."""
    assert (LEVEL_WARN, "effort_non_claude") in _lint_codes(f"Task {routing_tag} #effort:low")


@pytest.mark.parametrize("claude_tag", ["#claude", "#claude_opus", "#claude_sonnet"])
def test_linter_quiet_for_claude_routing_forms(claude_tag):
    codes = {code for _lvl, code in _lint_codes(f"Task {claude_tag} #effort:low")}
    assert "effort_non_claude" not in codes


@pytest.mark.parametrize("innocent", [
    "Refactor the #efforts module",         # plural
    "Dokumentiere #effort-handling",        # hyphenated
    "Siehe #effortless im Code",            # longer word
    "Task mit hash#effort:low mitten drin",   # not at a token boundary → prose
    "Document literal `#effort:low` syntax",  # documentation in backticks
    "Review https://example.test/docs/#effort:low",  # URL fragment
    r"Review escaped \#effort:low syntax",    # escaped
    "Review [effort docs](#effort:low)",      # Markdown fragment link
    "See [the effort section](#effort) below",  # Markdown link, no value
])
def test_linter_does_not_flag_words_containing_effort(innocent):
    """The permissive probe must not fire on ordinary words. A false positive here turns a
    legitimate line into a lint ERROR (exit code 2) — noise in the one report that is meant
    to be trustworthy, worse than the silent-tag hole the probe closes. It does not stop the
    queue from running; `--lint-queue` is an opt-in offline command, wired to no CI here.
    Regression guard: earlier versions of the probe flagged several of these.
    """
    assert not [code for _lvl, code in _lint_codes(innocent) if "effort" in code]


def test_linter_flags_duplicate_effort_tags():
    """Only the first tag applies; the others look active and are not."""
    assert (LEVEL_ERROR, "effort_duplicate_tag") in _lint_codes("Task #effort:low #effort:max")


def test_linter_warns_on_effort_with_non_claude_provider():
    """--effort exists only on the Claude CLI. On a task routed elsewhere the tag is a
    silent no-op — the user asked for a level and gets none. Warning, not error: the task
    itself still runs correctly."""
    assert (LEVEL_WARN, "effort_non_claude") in _lint_codes("Task #codex #effort:low")
    assert (LEVEL_WARN, "effort_non_claude") in _lint_codes("Task #gemini_flash #effort:low")


def test_linter_quiet_on_effort_with_claude_provider():
    codes = {code for _lvl, code in _lint_codes("Task #claude_opus #effort:low")}
    assert "effort_non_claude" not in codes


def test_linter_checks_effort_on_subtasks():
    """Subtasks honour #effort: (SubTask.effort), so the linter must inspect them too —
    otherwise a bad level on a subtask is completely silent."""
    content = (
        "# Queue\n\n## Queue\n\n- [ ] Parent #parallel\n"
        "  - [ ] Teil A #effort:ultra\n  - [ ] Teil B #effort:low\n\n## Ergebnisse\n"
    )
    codes = {f.code for f in lint_queue(content)}
    assert "unknown_effort" in codes


def test_effort_levels_are_in_ascending_order():
    """Ordinal scale — lint messages render it in this order, and a sorted set would
    print "high, low, max, medium, xhigh" and mislead whoever picks a level."""
    assert CLAUDE_EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")


# ── Parallel subtasks: parent → child inheritance ─────────────────────────────

def test_subtask_parses_own_effort_tag():
    from parallel_runner import _parse_subtask

    assert _parse_subtask("do a thing #effort:medium").effort == "medium"
    assert _parse_subtask("do a thing").effort is None


def test_run_single_subtask_applies_forced_effort(monkeypatch):
    """Direct test of the parallel setter itself.

    The inheritance test below replaces `_run_single_subtask` — which is exactly where
    `SubTask.effort` is transferred onto the provider — so deleting that setter would
    leave it green. This test closes that gap: it drives the real `_run_single_subtask`
    and observes the provider at the moment `_run_with_retry` is called. Mirrors
    tests/test_parallel_runner.py::test_run_single_subtask_applies_forced_claude_model.
    """
    import dispatcher
    import orchestrator
    import parallel_runner as parallel_runner_module
    from parallel_runner import SubTask
    from limits import AllLimits

    class DummyProvider:
        name = "claude"

        def __init__(self):
            self._forced_model = None
            self._forced_effort = None

    provider = DummyProvider()
    subtask = SubTask(
        text="Do the thing",
        provider_forced="claude",
        cwd=None,
        tool_name=None,
        timeout=30,
        effort="low",
    )

    seen_effort: list[str | None] = []

    monkeypatch.setattr(dispatcher, "select_provider", lambda *_a, **_kw: provider)
    monkeypatch.setattr(queue_manager, "strip_metadata_tags", lambda text: text)
    monkeypatch.setattr(orchestrator, "_build_prompt", lambda *_a, **_kw: "prompt")
    monkeypatch.setattr(
        orchestrator,
        "_run_with_retry",
        lambda provider, *_a, **_kw: (
            seen_effort.append(provider._forced_effort),
            (SimpleNamespace(success=True, output="ok", error=""), 0.0),
        )[1],
    )

    result = parallel_runner_module._run_single_subtask(
        subtask, idx=0, limits=AllLimits(), memory_context="", pause_event=None,
    )

    assert seen_effort == ["low"], "SubTask.effort was not pinned onto the provider"
    assert provider._forced_effort is None, "effort was not restored after the subtask"
    assert result.success is True


@pytest.mark.parametrize("bad_child", [
    "subtask bad #effort:ultra",     # valid shape, unknown level
    "subtask bad #effort=high",      # wrong separator
    "subtask bad #effort: high",     # space after colon
    "subtask bad (#effort:high)",    # wrapped in parens
])
def test_run_parallel_does_not_inherit_over_an_invalid_child_tag(monkeypatch, bad_child):
    """An INVALID child level must fall back to the session default, not to the parent's
    level. `SubTask.effort` is None in every one of these cases *and* in the "no tag at
    all" case, so the inheritance rule has to ask whether a tag was attempted — otherwise
    a typo is silently honoured as the parent's setting.

    The malformed shapes are the regression: an earlier fix consulted
    extract_effort_tag_raw(), which still uses the strict regex, so only the first case
    was covered and the other three kept inheriting the parent level.
    """
    import parallel_runner as parallel_runner_module
    from parallel_runner import SubTask, SubTaskResult, run_parallel
    from limits import AllLimits

    monkeypatch.setattr(queue_manager, "ALLOWED_CWD_ROOTS", [])
    # Real _parse_subtask so the raw-tag lookup sees the actual subtask text.
    seen_effort: list[str | None] = []
    monkeypatch.setattr(
        parallel_runner_module,
        "_run_single_subtask",
        lambda subtask, idx, limits, memory_context, pause_event, profile=None: (
            seen_effort.append(subtask.effort),
            SubTaskResult(text=subtask.text, provider_name="mock", success=True, output="ok"),
        )[1],
    )

    class FakeThread:
        def __init__(self, target, args, daemon, name):
            self._target, self._args, self.name, self._alive = target, args, name, False

        def start(self):
            self._alive = True
            self._target(*self._args)
            self._alive = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(parallel_runner_module.threading, "Thread", FakeThread)

    run_parallel(
        "Parent task #parallel #effort:low",
        ("subtask plain", bad_child),
        AllLimits(),
    )

    assert seen_effort == ["low", None], (
        f"an invalid child tag must NOT inherit the parent level: {bad_child!r}"
    )


def test_run_parallel_inherits_parent_effort_for_subtasks_without_effort(monkeypatch):
    """Same rule as model_tag: a parent-level #effort: applies to subtasks that carry
    no tag of their own. Without this the tag would silently no-op on the whole
    parallel path. Mirrors test_parallel_runner's model_tag inheritance test."""
    import parallel_runner as parallel_runner_module
    from parallel_runner import SubTask, SubTaskResult, run_parallel
    from limits import AllLimits

    monkeypatch.setattr(queue_manager, "ALLOWED_CWD_ROOTS", [])
    monkeypatch.setattr(
        parallel_runner_module,
        "_parse_subtask",
        lambda text: SubTask(
            text=text,
            provider_forced=None,
            cwd=None,
            tool_name=None,
            timeout=5,
            # "b" brings its own level and must NOT be overwritten by the parent
            effort="max" if text == "subtask b" else None,
        ),
    )

    seen_effort: list[str | None] = []
    monkeypatch.setattr(
        parallel_runner_module,
        "_run_single_subtask",
        lambda subtask, idx, limits, memory_context, pause_event, profile=None: (
            seen_effort.append(subtask.effort),
            SubTaskResult(text=subtask.text, provider_name="mock", success=True, output="ok"),
        )[1],
    )

    class FakeThread:
        def __init__(self, target, args, daemon, name):
            self._target = target
            self._args = args
            self.name = name
            self._alive = False

        def start(self):
            self._alive = True
            self._target(*self._args)
            self._alive = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(parallel_runner_module.threading, "Thread", FakeThread)

    run_parallel(
        "Parent task #parallel #effort:low",
        ("subtask a", "subtask b"),
        AllLimits(),
    )

    assert seen_effort == ["low", "max"]
