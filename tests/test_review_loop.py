from providers.base import RunResult
from tools.review_loop import (
    ReviewLoopTool,
    _is_no_findings_output,
    _merge_findings,
    _resolve_second_opinion,
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


def test_review_loop_reviews_uncommitted_changes_prompt_and_finishes_on_clean(monkeypatch):
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)

    provider = _ScriptedProvider(outputs=[
        "No P1/P2/P3 findings.",  # review
        "VERIFIED",               # verification phase
    ])
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=".")

    assert result.success is True
    assert result.iterations == 1
    assert len(provider.prompts) == 2
    assert "UNCOMMITTED changes" in provider.prompts[0]


def test_review_loop_fixes_p3_findings_too(monkeypatch):
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)

    provider = _ScriptedProvider(
        outputs=[
            "- [P3] docs typo 1",      # review iter 1
            "Fixed typo 1",             # fix iter 1
            "No P1/P2/P3 findings.",    # review iter 2
            "VERIFIED",                 # verification phase
            "Pattern: typo\nTool-Hint: fix it", # summarizer
        ]
    )
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=".")

    assert result.success is True
    assert result.iterations == 2
    assert len(provider.prompts) == 5
    assert "docs typo 1" in provider.prompts[1]
    assert "--- Fix 1 ---" in result.output

def test_review_loop_keeps_fixing_distinct_p3_findings_until_clean(monkeypatch):
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)

    provider = _ScriptedProvider(
        outputs=[
            "- [P3] Minor issue 1",      # review iter 1
            "Fixing 1",                   # fix iter 1
            "- [P3] Minor issue 2",       # review iter 2
            "Fixing 2",                   # fix iter 2
            "- [P3] Minor issue 3",       # review iter 3
            "Fixing 3",                   # fix iter 3
            "No P1/P2/P3 findings.",      # review iter 4
            "VERIFIED",                   # verification phase
            "Pattern: issues\nTool-Hint: fix issues", # summarizer
        ]
    )
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=".")

    assert result.success is True
    assert result.iterations == 4
    assert len(provider.prompts) == 9

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


def test_second_opinion_adds_findings_to_fix_prompt(monkeypatch):
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
        outputs=["- [P3] Missed edge case"], name="openrouter",
    )
    tool = ReviewLoopTool()

    result = tool.run(
        "Review now", primary, cwd=".",
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


def test_second_opinion_skipped_when_provider_unavailable(monkeypatch):
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
        "Review now", primary, cwd=".",
        second_opinion=(so_provider, None),
    )

    assert result.success is True
    assert len(so_provider.prompts) == 0  # never called


def test_second_opinion_skipped_when_diff_too_large(monkeypatch):
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
        "Review now", primary, cwd=".",
        second_opinion=(so_provider, None),
    )

    assert result.success is True
    assert len(so_provider.prompts) == 0  # diff missing → skipped


def test_second_opinion_runs_only_iteration_1(monkeypatch):
    """Second-opinion must not run again in iteration 2+."""
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _sec: None)
    monkeypatch.setattr(
        "tools.review_loop.is_cached_provider_available", lambda _name: True
    )
    monkeypatch.setattr("tools.review_loop._load_git_diff", lambda _cwd, _max: "diff")

    primary = _ScriptedProvider(outputs=[
        "- [P3] Bug 1",              # review iter 1
        "Fixed 1",                    # fix iter 1
        "- [P3] Bug 2",               # review iter 2 (still findings)
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
        "Review now", primary, cwd=".",
        second_opinion=(so_provider, None),
    )

    assert result.success is True
    assert len(so_provider.prompts) == 1  # called exactly once

