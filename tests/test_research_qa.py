from pathlib import Path

import pytest

from providers.base import RunResult
from tools.research_qa import ResearchQATool


# -- Helpers -------------------------------------------------------------------

class _ScriptedProvider:
    """Returns pre-scripted outputs in order."""
    name = "claude"

    def __init__(self, outputs: list[str]):
        self._outputs = list(outputs)
        self.prompts: list[str] = []
        self.read_only_flags: list[bool] = []

    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = 0,
        read_only: bool = False,
    ) -> RunResult:
        self.prompts.append(task)
        self.read_only_flags.append(read_only)
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))


class _MutatingProvider(_ScriptedProvider):
    def __init__(self, outputs: list[str], mutate_on_call: int, filename: str = "mutated.txt"):
        super().__init__(outputs)
        self._mutate_on_call = mutate_on_call
        self._filename = filename

    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = 0,
        read_only: bool = False,
    ) -> RunResult:
        result = super().run(task, cwd=cwd, timeout=timeout, read_only=read_only)
        if len(self.prompts) == self._mutate_on_call and cwd:
            target = Path(cwd, self._filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("mutation", encoding="utf-8")
        return result


class _DeletingProvider(_ScriptedProvider):
    def __init__(self, outputs: list[str], delete_on_call: int, filename: str):
        super().__init__(outputs)
        self._delete_on_call = delete_on_call
        self._filename = filename

    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = 0,
        read_only: bool = False,
    ) -> RunResult:
        result = super().run(task, cwd=cwd, timeout=timeout, read_only=read_only)
        if len(self.prompts) == self._delete_on_call and cwd:
            Path(cwd, self._filename).unlink()
        return result


class _OverwritingProvider(_ScriptedProvider):
    def __init__(self, outputs: list[str], overwrite_on_call: int, filename: str, content: bytes):
        super().__init__(outputs)
        self._overwrite_on_call = overwrite_on_call
        self._filename = filename
        self._content = content

    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = 0,
        read_only: bool = False,
    ) -> RunResult:
        result = super().run(task, cwd=cwd, timeout=timeout, read_only=read_only)
        if len(self.prompts) == self._overwrite_on_call and cwd:
            Path(cwd, self._filename).write_bytes(self._content)
        return result


class _ConcurrentMutationProvider(_ScriptedProvider):
    def __init__(
        self,
        outputs: list[str],
        mutate_on_call: int,
        external_file: Path,
        filename: str = "mutated.txt",
    ):
        super().__init__(outputs)
        self._mutate_on_call = mutate_on_call
        self._external_file = external_file
        self._filename = filename

    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = 0,
        read_only: bool = False,
    ) -> RunResult:
        result = super().run(task, cwd=cwd, timeout=timeout, read_only=read_only)
        if len(self.prompts) == self._mutate_on_call and cwd:
            target = Path(cwd, self._filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("mutation", encoding="utf-8")
            self._external_file.write_text("parallel", encoding="utf-8")
        return result


def _patch(monkeypatch):
    monkeypatch.setattr("tools.research_qa.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.research_qa.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.research_qa.time.sleep", lambda _: None)


DISCOVERY_OUTPUT = """\
## Project Overview
Python web app with Flask.

## Relevant Code Areas
- app.py: main entry
- models.py: DB models

## Existing Patterns & Conventions
Uses pytest, Black formatting.

## External Dependencies
Flask, SQLAlchemy"""

ANALYSIS_OUTPUT = """\
## Implementation Approaches
1. Approach A: Add OAuth2 via authlib (M effort, low risk)
2. Approach B: Roll custom JWT (L effort, medium risk)

## Recommended Approach
Approach A — authlib is well-maintained.

## Required Changes
- app.py: add OAuth routes
- models.py: add User.oauth_token field

## Data & API Impact
New migration for oauth_token column.

## Security & Performance
Token storage must be encrypted at rest.

## Testing Strategy
Mock OAuth provider in integration tests.

## Risks & Edge Cases
Token refresh race conditions.

## Unknowns & Uncertainties
Which OAuth providers to support?"""

QUESTIONS_OUTPUT = """\
## Blocking Questions
- [BLOCKING] Which OAuth providers should be supported (Google, GitHub, both)?
- [BLOCKING] Should existing password users be migrated or kept separate?

## Requirements Clarification
- Should users be able to link multiple OAuth providers to one account?

## Architecture Decisions
- Use authlib or python-social-auth?

## Scope & Phasing
- MVP with one provider first, then add more later?

## Technical Unknowns
- How does the current session management work with Flask-Login?

## Risk & Rollback
- How to handle token refresh failures?

## Testing & Validation
- Do we need a test OAuth server or mock the provider?"""


# -- Happy path ----------------------------------------------------------------

def test_research_qa_succeeds(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT])
    tool = ResearchQATool()
    result = tool.run("Add OAuth2 login", provider, cwd=str(tmp_path))

    assert result.success is True
    assert result.iterations == 3
    assert len(provider.prompts) == 3
    assert provider.read_only_flags == [True, True, True]


def test_research_qa_writes_all_files(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT])
    tool = ResearchQATool()
    tool.run("Add OAuth2 login", provider, cwd=str(tmp_path))

    rqa_dir = tmp_path / ".research-qa"
    assert (rqa_dir / "01-discovery.md").exists()
    assert (rqa_dir / "02-analysis.md").exists()
    assert (rqa_dir / "03-questions.md").exists()
    assert (rqa_dir / "research-qa-complete.md").exists()


def test_research_qa_combined_doc_contains_all_sections(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT])
    tool = ResearchQATool()
    tool.run("Add OAuth2 login", provider, cwd=str(tmp_path))

    combined = (tmp_path / ".research-qa" / "research-qa-complete.md").read_text(encoding="utf-8")
    assert "Teil 1: Discovery" in combined
    assert "Teil 2: Analyse" in combined
    assert "Teil 3: Fragen" in combined
    assert "[BLOCKING]" in combined
    assert "Add OAuth2 login" in combined


def test_research_qa_output_includes_all_phases(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT])
    tool = ResearchQATool()
    result = tool.run("Add OAuth2 login", provider, cwd=str(tmp_path))

    assert "--- Discovery ---" in result.output
    assert "--- Analysis ---" in result.output
    assert "--- Questions ---" in result.output


# -- Phase failures ------------------------------------------------------------

def test_discovery_failure_returns_error(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([])  # no outputs → failure
    tool = ResearchQATool()
    result = tool.run("Task X", provider, cwd=str(tmp_path))

    assert result.success is False
    assert result.iterations == 0
    assert "Discovery fehlgeschlagen" in result.error
    assert result.retryable is True


def test_analysis_failure_returns_error(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([DISCOVERY_OUTPUT])  # discovery ok, analysis fails
    tool = ResearchQATool()
    result = tool.run("Task X", provider, cwd=str(tmp_path))

    assert result.success is False
    assert result.iterations == 1
    assert "Analysis fehlgeschlagen" in result.error
    assert result.retryable is True


def test_questions_failure_returns_error(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([DISCOVERY_OUTPUT, ANALYSIS_OUTPUT])  # questions fail
    tool = ResearchQATool()
    result = tool.run("Task X", provider, cwd=str(tmp_path))

    assert result.success is False
    assert result.iterations == 2
    assert "Fragen-Generierung fehlgeschlagen" in result.error
    assert result.retryable is True


def test_research_qa_fails_on_workspace_mutation(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _MutatingProvider(
        [DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT],
        mutate_on_call=2,
    )
    tool = ResearchQATool()

    result = tool.run("Task X", provider, cwd=str(tmp_path))

    assert result.success is False
    assert result.iterations == 1
    assert result.error_code == "read_only_violation"
    assert "Read-only-Verstoss in Phase Analysis" in result.error
    assert "mutated.txt" in result.error
    assert not (tmp_path / "mutated.txt").exists()


def test_research_qa_restores_deleted_files_after_read_only_violation(monkeypatch, tmp_path):
    _patch(monkeypatch)
    original = tmp_path / "keep.txt"
    original.write_text("stable", encoding="utf-8")
    provider = _DeletingProvider(
        [DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT],
        delete_on_call=1,
        filename="keep.txt",
    )
    tool = ResearchQATool()

    result = tool.run("Task X", provider, cwd=str(tmp_path))

    assert result.success is False
    assert result.error_code == "read_only_violation"
    assert "keep.txt" in result.error
    assert original.read_text(encoding="utf-8") == "stable"


def test_research_qa_restores_deleted_unlisted_files_after_read_only_violation(monkeypatch, tmp_path):
    _patch(monkeypatch)
    original = tmp_path / "Main.java"
    original.write_text("class Main {}", encoding="utf-8")
    provider = _DeletingProvider(
        [DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT],
        delete_on_call=1,
        filename="Main.java",
    )
    tool = ResearchQATool()

    result = tool.run("Task X", provider, cwd=str(tmp_path))

    assert result.success is False
    assert result.error_code == "read_only_violation"
    assert "Main.java" in result.error
    assert original.read_text(encoding="utf-8") == "class Main {}"


def test_research_qa_restores_large_files_after_read_only_violation(monkeypatch, tmp_path):
    from tools import research_qa

    _patch(monkeypatch)
    original = tmp_path / "large.bin"
    payload = b"\x00\x01\x02\x03" * ((research_qa._SNAPSHOT_INLINE_BACKUP_FILE_MAX_BYTES // 4) + 1)
    original.write_bytes(payload)
    provider = _OverwritingProvider(
        [DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT],
        overwrite_on_call=1,
        filename="large.bin",
        content=b"mutated",
    )
    tool = ResearchQATool()

    result = tool.run("Task X", provider, cwd=str(tmp_path))

    assert result.success is False
    assert result.error_code == "read_only_violation"
    assert "large.bin" in result.error
    assert original.read_bytes() == payload


def test_research_qa_ignores_git_metadata_mutations(monkeypatch, tmp_path):
    _patch(monkeypatch)
    (tmp_path / ".git").mkdir()
    provider = _MutatingProvider(
        [DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT],
        mutate_on_call=1,
        filename=".git/index",
    )
    tool = ResearchQATool()

    result = tool.run("Task X", provider, cwd=str(tmp_path))

    assert result.success is True
    assert not (tmp_path / ".git" / "index").exists()


def test_research_qa_does_not_rollback_parallel_real_workspace_changes(monkeypatch, tmp_path):
    _patch(monkeypatch)
    external_file = tmp_path / "user-change.txt"
    external_file.write_text("before", encoding="utf-8")
    provider = _ConcurrentMutationProvider(
        [DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT],
        mutate_on_call=1,
        external_file=external_file,
    )
    tool = ResearchQATool()

    result = tool.run("Task X", provider, cwd=str(tmp_path))

    assert result.success is False
    assert result.error_code == "read_only_violation"
    assert external_file.read_text(encoding="utf-8") == "parallel"
    assert not (tmp_path / "mutated.txt").exists()


@pytest.mark.parametrize(
    "filename",
    [
        "build/output.txt",
        "dist/app.js",
        "target/classes/App.class",
        "node_modules/pkg/index.js",
        ".research-qa/provider-note.md",
    ],
)
def test_research_qa_detects_mutations_in_previously_excluded_dirs(monkeypatch, tmp_path, filename):
    _patch(monkeypatch)
    provider = _MutatingProvider(
        [DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT],
        mutate_on_call=1,
        filename=filename,
    )
    tool = ResearchQATool()

    result = tool.run("Task X", provider, cwd=str(tmp_path))

    assert result.success is False
    assert result.error_code == "read_only_violation"
    assert filename in result.error
    assert not (tmp_path / filename).exists()


def test_capture_workspace_state_skips_large_file_backups(tmp_path):
    from tools import research_qa

    large = tmp_path / "large.txt"
    large.write_text(
        "x" * (research_qa._SNAPSHOT_INLINE_BACKUP_FILE_MAX_BYTES + 1),
        encoding="utf-8",
    )

    state = research_qa._capture_workspace_state(str(tmp_path))

    assert state.files["large.txt"].content is None


# -- Discovery output is passed to analysis prompt ----------------------------

def test_discovery_output_in_analysis_prompt(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT])
    tool = ResearchQATool()
    tool.run("Task X", provider, cwd=str(tmp_path))

    analysis_prompt = provider.prompts[1]
    assert "Python web app with Flask" in analysis_prompt  # discovery output passed through


def test_both_outputs_in_questions_prompt(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT])
    tool = ResearchQATool()
    tool.run("Task X", provider, cwd=str(tmp_path))

    questions_prompt = provider.prompts[2]
    assert "Python web app with Flask" in questions_prompt  # discovery
    assert "authlib is well-maintained" in questions_prompt  # analysis


# -- Memory context ------------------------------------------------------------

def test_memory_context_injected(monkeypatch, tmp_path):
    _patch(monkeypatch)
    provider = _ScriptedProvider([DISCOVERY_OUTPUT, ANALYSIS_OUTPUT, QUESTIONS_OUTPUT])
    tool = ResearchQATool()
    tool.run("Task X", provider, cwd=str(tmp_path), memory_context="Previous OAuth attempt failed")

    # Memory context should appear in all three prompts
    for prompt in provider.prompts:
        assert "Previous OAuth attempt failed" in prompt


# -- Tool metadata -------------------------------------------------------------

def test_tool_name_and_description():
    tool = ResearchQATool()
    assert tool.name == "research-qa"
    assert "Fragen" in tool.description


# -- Registry ------------------------------------------------------------------

def test_registry_contains_research_qa():
    from tools.registry import get_tool, list_tools
    assert get_tool("research-qa") is not None
    tools = list_tools()
    assert "research-qa" in tools
