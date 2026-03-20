"""Tests for the multi-pass adversarial critical-review tool."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Mock config._load_dotenv before importing modules that depend on config
with patch("config._load_dotenv"):
    from providers.base import RunResult
    from queue_manager import extract_pass_providers, strip_metadata_tags
    from tools.base_tool import ToolResult
    from tools.critical_review import (
        CriticalReviewTool,
        _plan_v2_path,
        _resolve_pass2_provider,
        _resolve_plan_file,
    )


# ── Helpers ────────────────────────────────────────────────────────────


class _ScriptedProvider:
    """Provider that returns pre-scripted outputs in FIFO order."""

    def __init__(self, name: str, outputs: list[str]):
        self.name = name
        self._outputs = list(outputs)
        self.calls: list[dict] = []

    def run(self, task: str, cwd: str | None = None, timeout: int = 0,
            read_only: bool = False) -> RunResult:
        self.calls.append({"task": task, "cwd": cwd, "timeout": timeout, "read_only": read_only})
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))


class _ErrorProvider:
    """Provider that always returns an error."""

    def __init__(self, name: str, error: str = "provider_error"):
        self.name = name
        self._error = error
        self.calls: list[dict] = []

    def run(self, task: str, cwd: str | None = None, timeout: int = 0,
            read_only: bool = False) -> RunResult:
        self.calls.append({"task": task, "cwd": cwd, "timeout": timeout, "read_only": read_only})
        return RunResult(success=False, output="", error=self._error)


def _noop(*_args, **_kwargs):
    pass


@pytest.fixture
def _patch(monkeypatch):
    """Suppress notifications and external calls."""
    monkeypatch.setattr("tools.critical_review.notify_tool_done", _noop)
    monkeypatch.setattr("tools.critical_review.notify_tool_progress", _noop)
    monkeypatch.setattr("tools.critical_review.is_cached_provider_available", lambda _name: True)


# ── Tag Parsing Tests ─────────────────────────────────────────────────


class TestExtractPassProviders:

    def test_both_tags(self):
        task = "Review auth #tool:critical-review #pass1:claude #pass2:gemini cwd:/d/proj"
        result = extract_pass_providers(task)
        assert result == {1: "claude", 2: "gemini"}

    def test_only_pass2(self):
        task = "Audit #tool:critical-review #pass2:gemini"
        result = extract_pass_providers(task)
        assert result == {2: "gemini"}

    def test_only_pass1(self):
        task = "Audit #tool:critical-review #pass1:codex"
        result = extract_pass_providers(task)
        assert result == {1: "codex"}

    def test_no_tags(self):
        task = "Review auth #tool:critical-review cwd:/d/proj"
        result = extract_pass_providers(task)
        assert result == {}

    def test_case_insensitive(self):
        task = "#Pass1:Claude #Pass2:Gemini"
        result = extract_pass_providers(task)
        assert result == {1: "claude", 2: "gemini"}

    def test_same_provider_both_passes(self):
        task = "#pass1:claude #pass2:claude"
        result = extract_pass_providers(task)
        assert result == {1: "claude", 2: "claude"}


class TestStripPassTags:

    def test_removes_pass_tags(self):
        task = "Review auth #pass1:claude #pass2:gemini #tool:critical-review"
        stripped = strip_metadata_tags(task)
        assert "#pass1:" not in stripped
        assert "#pass2:" not in stripped
        assert "Review auth" in stripped

    def test_preserves_other_content(self):
        task = "Review auth #pass1:claude #pass2:gemini"
        stripped = strip_metadata_tags(task)
        assert "Review auth" in stripped


# ── Tool Class Attributes ─────────────────────────────────────────────


class TestToolAttributes:

    def test_name(self):
        tool = CriticalReviewTool()
        assert tool.name == "critical-review"

    def test_read_only(self):
        tool = CriticalReviewTool()
        assert tool.read_only is True

    def test_description_mentions_adversarial(self):
        tool = CriticalReviewTool()
        assert "adversarial" in tool.description.lower() or "Adversarial" in tool.description


# ── Resolve Pass 2 Provider ──────────────────────────────────────────


class TestResolvePass2Provider:

    def test_no_pass2_tag_returns_default(self):
        default = SimpleNamespace(name="claude")
        result = _resolve_pass2_provider({}, default)
        assert result is default

    def test_pass2_tag_resolves(self, monkeypatch):
        gemini = SimpleNamespace(name="gemini")
        monkeypatch.setattr(
            "dispatcher.get_provider_by_name",
            lambda name: gemini if name == "gemini" else None,
        )
        default = SimpleNamespace(name="claude")
        result = _resolve_pass2_provider({2: "gemini"}, default)
        assert result is gemini

    def test_unknown_pass2_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            "dispatcher.get_provider_by_name",
            lambda _name: None,
        )
        default = SimpleNamespace(name="claude")
        result = _resolve_pass2_provider({2: "nonexistent"}, default)
        assert result is default


# ── Plan File Resolution ─────────────────────────────────────────────


class TestResolvePlanFile:

    def test_finds_md_in_cwd(self, tmp_path):
        (tmp_path / "plan.md").write_text("# My Plan", encoding="utf-8")
        path, ref = _resolve_plan_file("Prüfe plan.md", tmp_path)
        assert path is not None
        assert path.name == "plan.md"
        assert ref == "plan.md"

    def test_finds_nested_md(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "design.md").write_text("# Design", encoding="utf-8")
        path, ref = _resolve_plan_file("Prüfe docs/design.md", tmp_path)
        assert path is not None
        assert path.name == "design.md"

    def test_no_file_ref_returns_none(self, tmp_path):
        path, ref = _resolve_plan_file("Review the auth module", tmp_path)
        assert path is None
        assert ref == ""

    def test_nonexistent_file_returns_none(self, tmp_path):
        path, ref = _resolve_plan_file("Prüfe nonexistent.md", tmp_path)
        assert path is None

    def test_wikilink_in_cwd(self, tmp_path):
        (tmp_path / "MyPlan.md").write_text("# Plan", encoding="utf-8")
        path, ref = _resolve_plan_file("Prüfe [[MyPlan]]", tmp_path)
        assert path is not None
        assert path.name == "MyPlan.md"


class TestPlanV2Path:

    def test_simple(self):
        p = _plan_v2_path(Path("/foo/plan.md"))
        assert p == Path("/foo/plan-v2.md")

    def test_nested(self):
        p = _plan_v2_path(Path("/proj/docs/design.md"))
        assert p == Path("/proj/docs/design-v2.md")

    def test_hyphenated_name(self):
        p = _plan_v2_path(Path("/proj/my-plan.md"))
        assert p == Path("/proj/my-plan-v2.md")


# ── 2-Pass (no plan) ─────────────────────────────────────────────────


class TestTwoPassNoPlan:

    def test_two_pass_same_provider(self, tmp_path, _patch):
        provider = _ScriptedProvider("claude", [
            "## Pass 1 Analysis\nFindings here.",
            "## Pass 2 Adversarial\nChallenges here.",
        ])
        tool = CriticalReviewTool()
        result = tool.run(
            "Review the auth module",
            provider,
            cwd=str(tmp_path),
        )
        assert result.success is True
        assert result.iterations == 2
        assert "Part 1" in result.output
        assert "Part 2" in result.output
        assert len(provider.calls) == 2  # no Pass 3

    def test_two_pass_cross_provider(self, tmp_path, _patch, monkeypatch):
        pass1_provider = _ScriptedProvider("claude", ["Pass 1 output."])
        pass2_provider = _ScriptedProvider("gemini", ["Pass 2 output."])

        monkeypatch.setattr(
            "dispatcher.get_provider_by_name",
            lambda name: pass2_provider if name == "gemini" else None,
        )

        tool = CriticalReviewTool()
        result = tool.run(
            "Review auth",
            pass1_provider,
            cwd=str(tmp_path),
            pass_providers={2: "gemini"},
        )

        assert result.success is True
        assert result.iterations == 2
        assert len(pass1_provider.calls) == 1
        assert len(pass2_provider.calls) == 1
        assert "claude" in result.output
        assert "gemini" in result.output

    def test_output_file_written(self, tmp_path, _patch):
        provider = _ScriptedProvider("claude", ["analysis", "challenge"])
        tool = CriticalReviewTool()
        tool.run("Review", provider, cwd=str(tmp_path))

        docs = tmp_path / "docs"
        assert docs.exists()
        files = list(docs.glob("critical-review-*.md"))
        assert len(files) >= 2
        combined = [f for f in files if "-pass1" not in f.name]
        pass1_files = [f for f in files if "-pass1" in f.name]
        assert len(combined) == 1
        assert len(pass1_files) == 1

    def test_combined_report_contains_both_parts(self, tmp_path, _patch):
        provider = _ScriptedProvider("claude", [
            "ANALYSIS_CONTENT_HERE",
            "ADVERSARIAL_CONTENT_HERE",
        ])
        tool = CriticalReviewTool()
        tool.run("Review", provider, cwd=str(tmp_path))

        docs = tmp_path / "docs"
        combined = [f for f in docs.glob("critical-review-*.md") if "-pass1" not in f.name][0]
        content = combined.read_text(encoding="utf-8")
        assert "ANALYSIS_CONTENT_HERE" in content
        assert "ADVERSARIAL_CONTENT_HERE" in content
        assert "Part 1" in content
        assert "Part 2" in content

    def test_pass1_standalone_file_written(self, tmp_path, _patch):
        provider = _ScriptedProvider("claude", ["analysis", "challenge"])
        tool = CriticalReviewTool()
        tool.run("Review", provider, cwd=str(tmp_path))

        docs = tmp_path / "docs"
        pass1_files = list(docs.glob("critical-review-*-pass1.md"))
        assert len(pass1_files) == 1
        content = pass1_files[0].read_text(encoding="utf-8")
        assert "analysis" in content


# ── 3-Pass (with plan) ───────────────────────────────────────────────


class TestThreePassWithPlan:

    def test_plan_triggers_pass3(self, tmp_path, _patch):
        """When a plan file is referenced, Pass 3 runs and writes -v2.md."""
        (tmp_path / "plan.md").write_text("# Original Plan\nDo things.", encoding="utf-8")
        provider = _ScriptedProvider("claude", [
            "Pass 1 analysis",
            "Pass 2 challenge",
            "# Improved Plan\nDo better things.",
        ])
        tool = CriticalReviewTool()
        result = tool.run(
            "Prüfe plan.md",
            provider,
            cwd=str(tmp_path),
        )

        assert result.success is True
        assert result.iterations == 3
        assert len(provider.calls) == 3

        # -v2 file written
        v2 = tmp_path / "plan-v2.md"
        assert v2.exists()
        assert "Improved Plan" in v2.read_text(encoding="utf-8")

    def test_plan_content_in_pass1_prompt(self, tmp_path, _patch):
        (tmp_path / "plan.md").write_text("UNIQUE_PLAN_CONTENT_ABC", encoding="utf-8")
        provider = _ScriptedProvider("claude", ["p1", "p2", "p3"])
        tool = CriticalReviewTool()
        tool.run("Prüfe plan.md", provider, cwd=str(tmp_path))

        # Pass 1 prompt should contain plan content
        assert "UNIQUE_PLAN_CONTENT_ABC" in provider.calls[0]["task"]

    def test_plan_content_in_pass2_prompt(self, tmp_path, _patch):
        (tmp_path / "plan.md").write_text("UNIQUE_PLAN_CONTENT_XYZ", encoding="utf-8")
        provider = _ScriptedProvider("claude", ["p1", "p2", "p3"])
        tool = CriticalReviewTool()
        tool.run("Prüfe plan.md", provider, cwd=str(tmp_path))

        # Pass 2 prompt should also contain plan content
        assert "UNIQUE_PLAN_CONTENT_XYZ" in provider.calls[1]["task"]

    def test_plan_content_in_pass3_prompt(self, tmp_path, _patch):
        (tmp_path / "plan.md").write_text("ORIGINAL_PLAN_TEXT", encoding="utf-8")
        provider = _ScriptedProvider("claude", ["p1", "p2", "p3"])
        tool = CriticalReviewTool()
        tool.run("Prüfe plan.md", provider, cwd=str(tmp_path))

        # Pass 3 prompt should contain original plan + both reviews
        p3_prompt = provider.calls[2]["task"]
        assert "ORIGINAL_PLAN_TEXT" in p3_prompt
        assert "p1" in p3_prompt  # Pass 1 output
        assert "p2" in p3_prompt  # Pass 2 output

    def test_v2_path_next_to_original(self, tmp_path, _patch):
        sub = tmp_path / "docs"
        sub.mkdir()
        (sub / "design.md").write_text("# Design", encoding="utf-8")
        provider = _ScriptedProvider("claude", ["p1", "p2", "improved"])
        tool = CriticalReviewTool()
        tool.run("Prüfe docs/design.md", provider, cwd=str(tmp_path))

        v2 = sub / "design-v2.md"
        assert v2.exists()

    def test_no_plan_means_no_pass3(self, tmp_path, _patch):
        """Without a plan file reference, only 2 passes run."""
        provider = _ScriptedProvider("claude", ["p1", "p2", "should not be called"])
        tool = CriticalReviewTool()
        result = tool.run("Review auth module", provider, cwd=str(tmp_path))

        assert result.success is True
        assert result.iterations == 2
        assert len(provider.calls) == 2

    def test_pass3_error_still_saves_review(self, tmp_path, _patch):
        """Pass 3 fails but review report is still saved."""
        (tmp_path / "plan.md").write_text("# Plan", encoding="utf-8")

        call_count = [0]
        def mock_run(task, cwd=None, timeout=0, read_only=False):
            call_count[0] += 1
            if call_count[0] <= 2:
                return RunResult(success=True, output=f"pass {call_count[0]}")
            return RunResult(success=False, error="timeout")

        provider = SimpleNamespace(name="claude", run=mock_run)
        tool = CriticalReviewTool()
        result = tool.run("Prüfe plan.md", provider, cwd=str(tmp_path))

        # Should still succeed (review saved, just no v2)
        assert result.success is True
        assert result.iterations == 2  # Pass 3 failed → 2 successful

        # No -v2 file
        assert not (tmp_path / "plan-v2.md").exists()

        # But review report exists
        docs = tmp_path / "docs"
        combined = [f for f in docs.glob("critical-review-*.md") if "-pass1" not in f.name]
        assert len(combined) == 1

    def test_metadata_includes_plan_info(self, tmp_path, _patch):
        (tmp_path / "plan.md").write_text("# Plan", encoding="utf-8")
        provider = _ScriptedProvider("claude", ["p1", "p2", "p3"])
        tool = CriticalReviewTool()
        tool.run("Prüfe plan.md", provider, cwd=str(tmp_path))

        docs = tmp_path / "docs"
        combined = [f for f in docs.glob("critical-review-*.md") if "-pass1" not in f.name][0]
        content = combined.read_text(encoding="utf-8")
        assert "Pass 3 (Synthesis)" in content
        assert "plan.md" in content

    def test_wikilink_plan(self, tmp_path, _patch):
        """Wikilink-style reference to a plan file in CWD."""
        (tmp_path / "MyPlan.md").write_text("# My Plan", encoding="utf-8")
        provider = _ScriptedProvider("claude", ["p1", "p2", "improved plan"])
        tool = CriticalReviewTool()
        result = tool.run("Prüfe [[MyPlan]]", provider, cwd=str(tmp_path))

        assert result.success is True
        assert result.iterations == 3
        assert (tmp_path / "MyPlan-v2.md").exists()


# ── Token Aggregation ────────────────────────────────────────────────


class TestTokenAggregation:

    def test_tokens_aggregated_2pass(self, tmp_path, _patch):
        call_count = [0]
        def mock_run(task, cwd=None, timeout=0, read_only=False):
            call_count[0] += 1
            return RunResult(
                success=True, output=f"output {call_count[0]}",
                input_tokens=100 * call_count[0],
                output_tokens=50 * call_count[0],
            )

        provider = SimpleNamespace(name="claude", run=mock_run)
        tool = CriticalReviewTool()
        result = tool.run("Review", provider, cwd=str(tmp_path))

        assert result.input_tokens == 300  # 100 + 200
        assert result.output_tokens == 150  # 50 + 100

    def test_tokens_aggregated_3pass(self, tmp_path, _patch):
        (tmp_path / "plan.md").write_text("# Plan", encoding="utf-8")

        call_count = [0]
        def mock_run(task, cwd=None, timeout=0, read_only=False):
            call_count[0] += 1
            return RunResult(
                success=True, output=f"output {call_count[0]}",
                input_tokens=100 * call_count[0],
                output_tokens=50 * call_count[0],
            )

        provider = SimpleNamespace(name="claude", run=mock_run)
        tool = CriticalReviewTool()
        result = tool.run("Prüfe plan.md", provider, cwd=str(tmp_path))

        assert result.input_tokens == 600  # 100 + 200 + 300
        assert result.output_tokens == 300  # 50 + 100 + 150


# ── Pass 2 Read-Only ─────────────────────────────────────────────────


class TestPass2ReadOnly:

    def test_pass1_sends_read_only(self, tmp_path, _patch):
        provider = _ScriptedProvider("claude", ["analysis", "challenge"])
        tool = CriticalReviewTool()
        tool.run("Review", provider, cwd=str(tmp_path))

        assert provider.calls[0]["read_only"] is True

    def test_pass2_sends_read_only(self, tmp_path, _patch):
        provider = _ScriptedProvider("claude", ["analysis", "challenge"])
        tool = CriticalReviewTool()
        tool.run("Review", provider, cwd=str(tmp_path))

        assert len(provider.calls) == 2
        assert provider.calls[1]["read_only"] is True


class TestPass1InPass2Prompt:

    def test_pass1_output_injected_in_pass2(self, tmp_path, _patch):
        provider = _ScriptedProvider("claude", [
            "UNIQUE_PASS1_FINDING_XYZ",
            "Pass 2 response",
        ])
        tool = CriticalReviewTool()
        tool.run("Review", provider, cwd=str(tmp_path))

        pass2_prompt = provider.calls[1]["task"]
        assert "UNIQUE_PASS1_FINDING_XYZ" in pass2_prompt

    def test_pass1_output_truncated_if_too_long(self, tmp_path, _patch, monkeypatch):
        monkeypatch.setattr("tools.critical_review.TOOL_CR_PASS1_MAX_INJECT_CHARS", 100)
        long_output = "A" * 200
        provider = _ScriptedProvider("claude", [long_output, "Pass 2"])
        tool = CriticalReviewTool()
        tool.run("Review", provider, cwd=str(tmp_path))

        pass2_prompt = provider.calls[1]["task"]
        assert "...[Pass 1 output truncated]" in pass2_prompt
        assert "A" * 200 not in pass2_prompt


# ── Error Handling ────────────────────────────────────────────────────


class TestPass1Failure:

    def test_pass1_error_returns_failure(self, tmp_path, _patch):
        provider = _ErrorProvider("claude", "timeout")
        tool = CriticalReviewTool()
        result = tool.run("Review", provider, cwd=str(tmp_path))

        assert result.success is False
        assert result.error == "timeout"
        assert result.retryable is True

    def test_pass1_error_no_files_written(self, tmp_path, _patch):
        provider = _ErrorProvider("claude", "timeout")
        tool = CriticalReviewTool()
        tool.run("Review", provider, cwd=str(tmp_path))

        docs = tmp_path / "docs"
        if docs.exists():
            assert len(list(docs.glob("*.md"))) == 0


class TestPass2Failure:

    def test_pass2_error_saves_pass1(self, tmp_path, _patch):
        call_count = [0]
        def mock_run(task, cwd=None, timeout=0, read_only=False):
            call_count[0] += 1
            if call_count[0] == 1:
                return RunResult(success=True, output="Pass 1 OK")
            return RunResult(success=False, error="provider_crash")

        provider = SimpleNamespace(name="claude", run=mock_run)
        tool = CriticalReviewTool()
        result = tool.run("Review", provider, cwd=str(tmp_path))

        assert result.success is False
        assert "Pass 1 gespeichert" in result.output

        docs = tmp_path / "docs"
        pass1_files = list(docs.glob("critical-review-*-pass1.md"))
        assert len(pass1_files) == 1

    def test_pass2_capacity_exhausted(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tools.critical_review.notify_tool_done", _noop)
        monkeypatch.setattr("tools.critical_review.notify_tool_progress", _noop)

        call_count = [0]
        def mock_available(name):
            call_count[0] += 1
            return call_count[0] <= 1

        monkeypatch.setattr("tools.critical_review.is_cached_provider_available", mock_available)

        provider = _ScriptedProvider("claude", ["Pass 1 OK"])
        tool = CriticalReviewTool()
        result = tool.run("Review", provider, cwd=str(tmp_path))

        assert result.success is False
        assert result.error_code == "capacity_exhausted"
        assert result.retryable is True


class TestCapacityExhaustedBeforeStart:

    def test_not_available_returns_capacity_exhausted(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tools.critical_review.notify_tool_done", _noop)
        monkeypatch.setattr("tools.critical_review.notify_tool_progress", _noop)
        monkeypatch.setattr("tools.critical_review.is_cached_provider_available", lambda _: False)

        provider = _ScriptedProvider("claude", ["should not be called"])
        tool = CriticalReviewTool()
        result = tool.run("Review", provider, cwd=str(tmp_path))

        assert result.success is False
        assert result.error_code == "capacity_exhausted"
        assert len(provider.calls) == 0


# ── Report Metadata ──────────────────────────────────────────────────


class TestReportMetadata:

    def test_metadata_section_present(self, tmp_path, _patch):
        provider = _ScriptedProvider("claude", ["analysis", "challenge"])
        tool = CriticalReviewTool()
        tool.run("Review", provider, cwd=str(tmp_path))

        docs = tmp_path / "docs"
        combined = [f for f in docs.glob("critical-review-*.md") if "-pass1" not in f.name][0]
        content = combined.read_text(encoding="utf-8")
        assert "Review-Metadaten" in content
        assert "Pass 1 (Analysis)" in content
        assert "Pass 2 (Adversarial)" in content

    def test_cross_provider_label_in_header(self, tmp_path, _patch, monkeypatch):
        pass1_provider = _ScriptedProvider("claude", ["analysis"])
        pass2_provider = _ScriptedProvider("gemini", ["challenge"])
        monkeypatch.setattr(
            "dispatcher.get_provider_by_name",
            lambda name: pass2_provider if name == "gemini" else None,
        )

        tool = CriticalReviewTool()
        tool.run("Review", pass1_provider, cwd=str(tmp_path), pass_providers={2: "gemini"})

        docs = tmp_path / "docs"
        combined = [f for f in docs.glob("critical-review-*.md") if "-pass1" not in f.name][0]
        content = combined.read_text(encoding="utf-8")
        assert "claude / gemini" in content


# ── Edge Cases ────────────────────────────────────────────────────────


class TestEdgeCases:

    def test_kwargs_accepted(self, tmp_path, _patch):
        provider = _ScriptedProvider("claude", ["a", "b"])
        tool = CriticalReviewTool()
        result = tool.run(
            "Review", provider, cwd=str(tmp_path),
            pass_providers={}, some_future_kwarg="ignored",
        )
        assert result.success is True

    def test_no_cwd_defaults_to_dot(self, _patch):
        provider = _ScriptedProvider("claude", ["a", "b"])
        tool = CriticalReviewTool()
        result = tool.run("Review", provider)
        assert result.success is True
