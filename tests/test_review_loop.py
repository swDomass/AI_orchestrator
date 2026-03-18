from providers.base import RunResult
from tools.review_loop import ReviewLoopTool, _is_no_findings_output


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

    def __init__(self, outputs: list[str]):
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    def run(self, task: str, cwd: str | None = None, timeout: int = 0) -> RunResult:
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
        ]
    )
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=".")

    assert result.success is True
    assert result.iterations == 2
    assert len(provider.prompts) == 4
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
        ]
    )
    tool = ReviewLoopTool()

    result = tool.run("Review now", provider, cwd=".")

    assert result.success is True
    assert result.iterations == 4
    assert len(provider.prompts) == 8
