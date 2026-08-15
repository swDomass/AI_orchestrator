from providers.base import RunResult
from tools.review_loop import (
    ReviewLoopTool,
    _FIX_PROMPT_STABLE,
    _is_no_findings_output,
    _merge_findings,
    _parse_drift_check,
    _resolve_second_opinion,
    _should_drift_check,
)


def test_no_findings_sentinel_accepts_slash_format():
    assert _is_no_findings_output("No P1/P2/P3 findings.") is True


def test_no_findings_sentinel_accepts_comma_format():
    assert _is_no_findings_output("No P1, P2, P3 findings.") is True


def test_no_findings_sentinel_accepts_bold_wrapped_and_no_period():
    assert _is_no_findings_output("**No P1/P2/P3 findings**") is True


def test_no_findings_sentinel_accepts_found_suffix_and_or_separator():
    assert _is_no_findings_output("No P1, P2 or P3 findings found.") is True


class _ScriptedProvider:
    name = "codex"

    def __init__(self, outputs: list[str], *, name: str = "codex"):
        self._outputs = list(outputs)
        self.prompts: list[str] = []
        self.name = name
        self._forced_model: str | None = None

    def run(self, task: str, cwd: str | None = None, timeout: int = 0, read_only: bool = False) -> RunResult:
        self.prompts.append(task)
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))


def test_review_loop_reviews_uncommitted_changes_prompt_and_finishes_on_clean(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)

    provider = _ScriptedProvider(outputs=[
        "No P1/P2/P3 findings.",  # review
        "VERIFIED",               # verification phase
    ])
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 1
    assert len(provider.prompts) == 2
    assert "UNCOMMITTED changes" in provider.prompts[0]


def test_review_loop_does_not_fix_p3_and_reports_it_as_offer(monkeypatch, tmp_path):
    """P3 is non-blocking (changed 2026-07-30): a P3-only round ends the loop instead of
    triggering a fix. Cosmetics on working code widen the diff for no functional gain,
    and since each round re-reads the fresh diff, a P3 fix can surface new P3 — the loop
    would feed itself. The deferred P3 is surfaced as an offer instead.

    Replaces the former test_review_loop_fixes_p3_findings_too, which asserted the old
    fix-everything contract.
    """
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)

    provider = _ScriptedProvider(
        outputs=[
            "- [P3] docs typo 1",   # review iter 1 — P3 only
            "VERIFIED",             # verification phase runs straight away
        ]
    )
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 1, "a P3-only round must not start a second iteration"
    # Review + verification only — no fix prompt.
    assert len(provider.prompts) == 2
    assert "--- Fix 1 ---" not in result.output
    # ...but the finding is not swallowed: it is reported for the user to decide.
    assert "P3 offen" in result.output
    assert "docs typo 1" in result.output


def test_review_loop_fixes_blocking_findings_and_defers_p3(monkeypatch, tmp_path):
    """Mixed round: P1/P2 are fixed, the P3 rides along to the final report untouched
    and never reaches the fix prompt."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)

    provider = _ScriptedProvider(
        outputs=[
            "- [P2] real problem\n- [P3] naming nit",  # review iter 1
            "Fixed the P2",                            # fix iter 1
            "- [P3] naming nit",                       # review iter 2 — P3 only → done
            "VERIFIED",                                # verification phase
            "Pattern: x\nTool-Hint: y",                # summarizer (iterations > 1)
        ]
    )
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 2
    # The fix prompt (prompts[1]) must carry the P2 and NOT the P3.
    assert "real problem" in provider.prompts[1]
    assert "naming nit" not in provider.prompts[1], "P3 must not reach the fix prompt"
    assert "P3 offen" in result.output


def test_deferred_p3_survives_a_later_clean_round(monkeypatch, tmp_path):
    """The P3 from round 1 must still appear in the closing offer when round 2 comes back
    completely clean.

    Regression: `deferred_p3` was rebuilt from the *current* round's findings, so the
    round-1 P3 vanished the moment the reviewer stopped repeating it — which is the normal
    case, since the P2 fix changes the diff. The earlier test masked it by scripting the
    same P3 in both rounds.
    """
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)

    provider = _ScriptedProvider(
        outputs=[
            "- [P2] real problem\n- [P3] round one nit",  # review iter 1
            "Fixed the P2",                               # fix iter 1
            "No P1/P2/P3 findings.",                      # review iter 2 — fully clean
            "VERIFIED",                                   # verification phase
            "Pattern: x\nTool-Hint: y",                   # summarizer
        ]
    )
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 2
    assert "P3 offen" in result.output
    assert "round one nit" in result.output, (
        "a P3 reported in an earlier round must not be lost when a later round is clean"
    )


def test_deferred_p3_is_deduplicated_across_rounds(monkeypatch, tmp_path):
    """The same P3 repeated in every round is offered once, not once per iteration."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)

    provider = _ScriptedProvider(
        outputs=[
            "- [P2] real problem\n- [P3] naming nit",  # review iter 1
            "Fixed the P2",                            # fix iter 1
            "- [P3] naming nit",                       # review iter 2 — same P3
            "VERIFIED",
            "Pattern: x\nTool-Hint: y",
        ]
    )
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=str(tmp_path))

    assert result.success is True
    offer = result.output.split("--- P3 offen")[1]
    assert offer.count("naming nit") == 1


def test_contradictory_review_output_does_not_pass_the_success_gate(monkeypatch, tmp_path):
    """A reviewer that prints a blocking finding AND the clean sentinel is contradicting
    itself. The sentinel must not win.

    Regression: the gate is `no_findings or not blocking_findings`, and
    `_is_no_findings_output()` matched any line, so this output satisfied the left side
    while a real P2 sat unfixed — the loop reported success on a blocking finding.
    """
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)

    provider = _ScriptedProvider(
        outputs=[
            "- [P2] real bug\nNo P1/P2/P3 findings.",  # review iter 1 — contradictory
            "Fixed the P2",                            # fix iter 1 must happen
            "No P1/P2/P3 findings.",                   # review iter 2 — genuinely clean
            "VERIFIED",
            "Pattern: x\nTool-Hint: y",
        ]
    )
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 2, "the contradictory round must not count as clean"
    assert "real bug" in provider.prompts[1], "the blocking finding must reach the fix prompt"


def test_is_clean_output_requires_both_sentinel_and_no_findings():
    from tools.review_loop import _is_clean_output

    assert _is_clean_output("No P1/P2/P3 findings.", []) is True
    assert _is_clean_output("- [P2] bug\nNo P1/P2/P3 findings.", ["- [P2] bug"]) is False
    assert _is_clean_output("- [P3] nit\nNo P1/P2/P3 findings.", ["- [P3] nit"]) is False
    assert _is_clean_output("some prose", []) is False

# ─── Second-Opinion Phase ──────────────────────────────────────────────

def test_merge_findings_dedups_exact_strings():
    primary = ["- [P1] Bug A", "- [P2] Bug B"]
    extra = ["- [P2] Bug B", "- [P3] Bug C"]  # B duplicate, C new
    merged = _merge_findings(primary, extra)
    assert merged == ["- [P1] Bug A", "- [P2] Bug B", "- [P3] Bug C"]


def test_merge_findings_empty_extra_returns_primary():
    primary = ["- [P1] Bug A"]
    assert _merge_findings(primary, []) == primary


def test_resolve_second_opinion_returns_none_for_unknown_alias():
    assert _resolve_second_opinion("does_not_exist_xyz") is None


def test_resolve_second_opinion_returns_none_for_falsy():
    assert _resolve_second_opinion(None) is None
    assert _resolve_second_opinion("") is None


def test_resolve_second_opinion_accepts_vibe_bare_and_aliases(monkeypatch):
    """Vibe is the second non-Claude voice: `#second_opinion:vibe` (CLI default
    model) and the two model aliases must all resolve to the vibe provider."""
    import dispatcher
    from providers.vibe import VibeProvider

    monkeypatch.setitem(dispatcher._providers, "vibe", VibeProvider())

    provider, model_id = _resolve_second_opinion("vibe")
    assert provider.name == "vibe"
    assert model_id is None  # bare provider → vibe's own configured model

    for alias in ("vibe_medium", "vibe_small"):
        provider, model_id = _resolve_second_opinion(alias)
        assert provider.name == "vibe"
        assert model_id == alias


def test_resolve_second_opinion_vibe_none_when_cli_missing(monkeypatch):
    """No binary → not registered → phase is skipped, never a crash."""
    import dispatcher

    monkeypatch.delitem(dispatcher._providers, "vibe", raising=False)
    assert _resolve_second_opinion("vibe") is None
    assert _resolve_second_opinion("vibe_medium") is None


def test_second_opinion_adds_findings_to_fix_prompt(monkeypatch, tmp_path):
    """Primary finds 1, second-opinion finds 1 extra → both must end up in fix prompt."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)
    monkeypatch.setattr(
        "tools.review_loop.is_cached_provider_available", lambda _name: True
    )
    monkeypatch.setattr(
        "tools.review_loop._load_git_diff", lambda _cwd, _max: "fake diff"
    )

    primary = _ScriptedProvider(outputs=[
        "- [P2] Primary bug",      # review iter 1
        "Fixed both bugs",          # fix iter 1
        "No P1/P2/P3 findings.",    # review iter 2 (clean)
        "VERIFIED",                 # verification
        "Pattern: x\nTool-Hint: y", # summarizer
    ])
    so_provider = _ScriptedProvider(
        outputs=["- [P2] Missed edge case"], name="openrouter",
    )
    tool = ReviewLoopTool()

    result = tool.run(
        "Review now", primary, cwd=str(tmp_path),
        second_opinion=(so_provider, "or_glm"),
    )

    assert result.success is True
    # Fix prompt is primary.prompts[1] (review→fix→review→verify→summary)
    fix_prompt = primary.prompts[1]
    assert "Primary bug" in fix_prompt
    assert "Missed edge case" in fix_prompt
    # Second-opinion was called exactly once (iteration 1 only)
    assert len(so_provider.prompts) == 1
    assert "fake diff" in so_provider.prompts[0]
    assert "Primary bug" in so_provider.prompts[0]  # primary findings injected
    # Forced model was applied and restored
    assert so_provider._forced_model is None


def test_second_opinion_p3_only_does_not_reopen_the_loop(monkeypatch, tmp_path):
    """Interaction of the two features: a second opinion that finds ONLY P3 sets
    `no_findings = False`, but must not restart the fix loop — the success gate is
    "no blocking findings", not "no findings at all". Otherwise a P3-only second
    opinion would spin iterations on cosmetics that never get fixed.
    """
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)
    monkeypatch.setattr(
        "tools.review_loop.is_cached_provider_available", lambda _name: True
    )
    monkeypatch.setattr(
        "tools.review_loop._load_git_diff", lambda _cwd, _max: "fake diff"
    )

    primary = _ScriptedProvider(outputs=[
        "No P1/P2/P3 findings.",  # review iter 1 — primary sees nothing
        "VERIFIED",               # verification
    ])
    so_provider = _ScriptedProvider(
        outputs=["- [P3] cosmetic nit"], name="openrouter",
    )
    tool = ReviewLoopTool()

    result = tool.run(
        "Review now", primary, cwd=str(tmp_path),
        second_opinion=(so_provider, "or_glm"),
    )

    assert result.success is True
    assert result.iterations == 1, "a P3-only second opinion must not start iteration 2"
    # Review + verification only — no fix prompt.
    assert len(primary.prompts) == 2
    assert "--- Fix 1 ---" not in result.output
    # The P3 is not swallowed either: it surfaces as an offer.
    assert "P3 offen" in result.output
    assert "cosmetic nit" in result.output


def test_second_opinion_skipped_when_provider_unavailable(monkeypatch, tmp_path):
    """If is_cached_provider_available returns False for the SO provider, skip it."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)
    monkeypatch.setattr(
        "tools.review_loop.is_cached_provider_available",
        lambda name: name != "openrouter",
    )

    primary = _ScriptedProvider(outputs=[
        "No P1/P2/P3 findings.",
        "VERIFIED",
    ])
    so_provider = _ScriptedProvider(outputs=["should not be called"], name="openrouter")
    tool = ReviewLoopTool()

    result = tool.run(
        "Review now", primary, cwd=str(tmp_path),
        second_opinion=(so_provider, None),
    )

    assert result.success is True
    assert len(so_provider.prompts) == 0  # never called


def test_second_opinion_skipped_when_diff_too_large(monkeypatch, tmp_path):
    """If diff fetch returns None (too large / unavailable), skip second-opinion."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)
    monkeypatch.setattr(
        "tools.review_loop.is_cached_provider_available", lambda _name: True
    )
    monkeypatch.setattr("tools.review_loop._load_git_diff", lambda _cwd, _max: None)

    primary = _ScriptedProvider(outputs=[
        "No P1/P2/P3 findings.",
        "VERIFIED",
    ])
    so_provider = _ScriptedProvider(outputs=["should not be called"], name="openrouter")
    tool = ReviewLoopTool()

    result = tool.run(
        "Review now", primary, cwd=str(tmp_path),
        second_opinion=(so_provider, None),
    )

    assert result.success is True
    assert len(so_provider.prompts) == 0  # diff missing → skipped


def test_second_opinion_runs_only_iteration_1(monkeypatch, tmp_path):
    """Second-opinion must not run again in iteration 2+."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)
    monkeypatch.setattr(
        "tools.review_loop.is_cached_provider_available", lambda _name: True
    )
    monkeypatch.setattr("tools.review_loop._load_git_diff", lambda _cwd, _max: "diff")

    primary = _ScriptedProvider(outputs=[
        "- [P2] Bug 1",              # review iter 1
        "Fixed 1",                    # fix iter 1
        "- [P2] Bug 2",               # review iter 2 (still findings)
        "Fixed 2",                    # fix iter 2
        "No P1/P2/P3 findings.",      # review iter 3
        "VERIFIED",                   # verification
        "Pattern: x\nTool-Hint: y",   # summarizer
    ])
    so_provider = _ScriptedProvider(
        outputs=["No P1/P2/P3 findings."],  # only 1 output → fails if called twice
        name="openrouter",
    )
    tool = ReviewLoopTool()

    result = tool.run(
        "Review now", primary, cwd=str(tmp_path),
        second_opinion=(so_provider, None),
    )

    assert result.success is True
    assert len(so_provider.prompts) == 1  # called exactly once


# ─── Drift-Check Phase ────────────────────────────────────────────────


def test_parse_drift_check_extracts_drifted_reason():
    status, reason = _parse_drift_check(
        "DRIFTED: Reviewer is fixing unrelated style issues in utils.py"
    )
    assert status == "DRIFTED"
    assert "utils.py" in reason


def test_parse_drift_check_on_topic_returns_empty_reason():
    status, reason = _parse_drift_check("ON_TOPIC: findings match original task")
    assert status == "ON_TOPIC"
    assert reason == ""


def test_parse_drift_check_unknown_when_neither_marker_present():
    status, reason = _parse_drift_check("Some prose without the required marker")
    assert status == "UNKNOWN"
    assert reason == ""


def test_should_drift_check_skip_mode_never_triggers():
    assert _should_drift_check("skip", 100, 20, 999, 0) is False


def test_should_drift_check_always_mode_always_triggers():
    assert _should_drift_check("always", 1, 20, 0, 0) is True
    assert _should_drift_check("always", 7, 20, 3, 3) is True


def test_should_drift_check_auto_triggers_when_findings_grow_from_iter_3():
    # iter < 3 → no trigger even when growing
    assert _should_drift_check("auto", 2, 20, 5, 1) is False
    # iter >= 3 + growing → trigger
    assert _should_drift_check("auto", 3, 20, 5, 1) is True


def test_should_drift_check_auto_triggers_at_halftime():
    # max=20, half=10, no growth, iter<5 logic wouldn't trigger
    assert _should_drift_check("auto", 10, 20, 2, 3) is True


def test_should_drift_check_auto_triggers_from_iter_5_safety_net():
    # iter 5, no growth, not halftime — safety-net still fires
    assert _should_drift_check("auto", 5, 20, 1, 1) is True


def test_drift_check_invoked_when_mode_always(monkeypatch, tmp_path):
    """drift_check_mode=always → extra provider call between review and fix."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "tools.review_loop.is_cached_provider_available", lambda _name: True
    )
    monkeypatch.setattr(ReviewLoopTool, "_drift_check_mode", lambda self: "always")

    provider = _ScriptedProvider(outputs=[
        "- [P2] Minor issue",            # review iter 1
        "ON_TOPIC: looks fine",          # drift check iter 1
        "Fixed it",                      # fix iter 1
        "No P1/P2/P3 findings.",         # review iter 2
        "VERIFIED",                      # verification
        "Pattern: x\nTool-Hint: y",      # summarizer
    ])
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=str(tmp_path))

    assert result.success is True
    # 6 provider calls: review, drift, fix, review, verify, summarizer
    # (no drift check in iter 2 because no findings → early return before drift)
    assert len(provider.prompts) == 6
    assert "Goal-Adherence" in provider.prompts[1]  # drift prompt is call #2


def test_drift_check_skipped_when_mode_skip(monkeypatch, tmp_path):
    """drift_check_mode=skip → no extra provider call."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _: None)
    monkeypatch.setattr(ReviewLoopTool, "_drift_check_mode", lambda self: "skip")

    provider = _ScriptedProvider(outputs=[
        "- [P2] Minor issue",            # review iter 1
        "Fixed it",                      # fix iter 1
        "No P1/P2/P3 findings.",         # review iter 2
        "VERIFIED",                      # verification
        "Pattern: x\nTool-Hint: y",      # summarizer
    ])
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=str(tmp_path))

    assert result.success is True
    assert len(provider.prompts) == 5  # no drift-check prompt


def test_drifted_response_injects_warning_into_next_fix_prompt(monkeypatch, tmp_path):
    """When drift check returns DRIFTED, the fix prompt must include a refocus hint."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "tools.review_loop.is_cached_provider_available", lambda _name: True
    )
    monkeypatch.setattr(ReviewLoopTool, "_drift_check_mode", lambda self: "always")

    provider = _ScriptedProvider(outputs=[
        "- [P2] Style nit in unrelated.py",   # review iter 1
        "DRIFTED: refactoring unrelated.py",  # drift check iter 1
        "Skipped off-topic finding",          # fix iter 1
        "No P1/P2/P3 findings.",              # review iter 2
        "VERIFIED",                           # verification
        "Pattern: x\nTool-Hint: y",           # summarizer
    ])
    tool = ReviewLoopTool()

    result = tool.run("Fix login bug", provider, cwd=str(tmp_path))

    assert result.success is True
    # fix prompt is the 3rd provider call
    fix_prompt = provider.prompts[2]
    assert "Drift detected" in fix_prompt
    assert "Refocus on the original task" in fix_prompt


def test_on_topic_response_does_not_inject_warning(monkeypatch, tmp_path):
    """When drift check returns ON_TOPIC, the fix prompt has no drift warning."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "tools.review_loop.is_cached_provider_available", lambda _name: True
    )
    monkeypatch.setattr(ReviewLoopTool, "_drift_check_mode", lambda self: "always")

    provider = _ScriptedProvider(outputs=[
        "- [P2] Minor in target file",   # review iter 1
        "ON_TOPIC: matches task",         # drift check iter 1
        "Fixed it",                       # fix iter 1
        "No P1/P2/P3 findings.",          # review iter 2
        "VERIFIED",                       # verification
        "Pattern: x\nTool-Hint: y",       # summarizer
    ])
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=str(tmp_path))

    assert result.success is True
    fix_prompt = provider.prompts[2]
    assert "Drift detected" not in fix_prompt


def test_scope_guard_present_in_stable_fix_prompt():
    """Scope-guard bullet must be in the stable (cached) prefix, not volatile."""
    assert "off-topic" in _FIX_PROMPT_STABLE.lower()
    assert "do not refactor unrelated" in _FIX_PROMPT_STABLE.lower()


def test_drift_check_failure_does_not_abort_loop(monkeypatch, tmp_path):
    """When the drift-check provider call fails, the loop continues without warning."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "tools.review_loop.is_cached_provider_available", lambda _name: True
    )
    monkeypatch.setattr(ReviewLoopTool, "_drift_check_mode", lambda self: "always")

    class _FailingDriftProvider:
        name = "claude"
        _forced_model = None

        def __init__(self):
            self.prompts: list[str] = []
            self._call_idx = 0

        def run(self, task, cwd=None, timeout=0, read_only=False, **_):
            self.prompts.append(task)
            self._call_idx += 1
            # call #1 = review, #2 = drift (fail), #3 = fix, #4 = review clean, #5 = verify, #6 = summary
            if self._call_idx == 1:
                return RunResult(success=True, output="- [P2] Bug")
            if self._call_idx == 2:
                return RunResult(success=False, output="", error="rate_limit")
            if self._call_idx == 3:
                return RunResult(success=True, output="Fixed")
            if self._call_idx == 4:
                return RunResult(success=True, output="No P1/P2/P3 findings.")
            if self._call_idx == 5:
                return RunResult(success=True, output="VERIFIED")
            return RunResult(success=True, output="Pattern: x\nTool-Hint: y")

    provider = _FailingDriftProvider()
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=str(tmp_path))

    assert result.success is True
    # Loop continued past the failed drift check
    assert provider._call_idx >= 5
    # Fix prompt (call #3) has no drift warning because drift call failed
    assert "Drift detected" not in provider.prompts[2]


def test_drift_check_unknown_output_treated_as_no_warning(monkeypatch, tmp_path):
    """Drift check returning neither ON_TOPIC nor DRIFTED → no warning injected."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "tools.review_loop.is_cached_provider_available", lambda _name: True
    )
    monkeypatch.setattr(ReviewLoopTool, "_drift_check_mode", lambda self: "always")

    provider = _ScriptedProvider(outputs=[
        "- [P2] Issue",                       # review iter 1
        "Some prose without sentinel marker", # drift check iter 1 (UNKNOWN)
        "Fixed",                              # fix iter 1
        "No P1/P2/P3 findings.",              # review iter 2
        "VERIFIED",                           # verification
        "Pattern: x\nTool-Hint: y",           # summarizer
    ])
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=str(tmp_path))

    assert result.success is True
    fix_prompt = provider.prompts[2]
    assert "Drift detected" not in fix_prompt



def test_review_loop_aborts_on_runtime_deadline(monkeypatch, tmp_path):
    """Total-runtime deadline already passed -> abort iteration 1 with
    tool_runtime_exceeded instead of running all 20 iterations."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _s: None)
    monkeypatch.setattr("tools.review_loop.is_cached_provider_available", lambda _n: True)

    # Deadline in the past -> loop must bail before any provider.run call.
    monkeypatch.setattr(ReviewLoopTool, "_runtime_deadline", lambda self: 0.0)

    provider = _ScriptedProvider(outputs=["should never be used"])
    result = ReviewLoopTool().run("Review now", provider, cwd=str(tmp_path))

    assert result.success is False
    assert result.error_code == "tool_runtime_exceeded"
    assert result.retryable is True
    assert provider.prompts == []  # no phase executed


def test_phase_cap_does_not_raise_above_constant():
    """A high task #timeout: hard backstop is an upper deckel only."""
    from config import TOOL_REVIEW_TIMEOUT_SEC
    tool = ReviewLoopTool()
    # huge task timeout must be clamped to the phase constant
    assert tool._phase_cap(999999, TOOL_REVIEW_TIMEOUT_SEC) == TOOL_REVIEW_TIMEOUT_SEC
    # a smaller task timeout caps below the constant
    assert tool._phase_cap(60, TOOL_REVIEW_TIMEOUT_SEC) == 60
    # no task timeout -> phase default
    assert tool._phase_cap(None, TOOL_REVIEW_TIMEOUT_SEC) == TOOL_REVIEW_TIMEOUT_SEC
