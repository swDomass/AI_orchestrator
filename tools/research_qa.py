"""
Research-QA Tool: Deep research on an implementation task, then generate questions.

Three-phase workflow:
  1. Discovery:  Explore codebase, find relevant files, understand architecture.
  2. Analysis:   Deep-dive into the task — dependencies, edge cases, design decisions.
  3. Questions:  Produce structured document with findings and open questions for the user.

Read-only — no code changes. Output written to {cwd}/.research-qa/ for review.

Usage in queue:
    - [ ] Add OAuth2 login flow #tool:research-qa cwd:/d/programmieren/projekt
    - [ ] Migrate DB from SQLite to Postgres #tool:research-qa cwd:/d/programmieren/projekt
"""

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from config import (
    TOOL_INTER_STEP_SLEEP_SEC,
    TOOL_RQA_DISCOVERY_TIMEOUT_SEC,
    TOOL_RQA_ANALYSIS_TIMEOUT_SEC,
    TOOL_RQA_QUESTIONS_TIMEOUT_SEC,
)
from notifier import notify_tool_done, notify_tool_progress
from providers.base import BaseProvider, RunResult
from tools.base_tool import BaseTool, ToolResult, _build_system_prompt, _write_tool_file

RQA_DIR = ".research-qa"
_SNAPSHOT_IGNORED_DIRS = {
    ".git",
}
_SANDBOX_COPY_EXCLUDED_DIRS = {
    RQA_DIR,
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
}
_SNAPSHOT_INLINE_BACKUP_SUFFIXES = {
    ".bat",
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".ps1",
    ".py",
    ".rst",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_SNAPSHOT_INLINE_BACKUP_FILE_MAX_BYTES = 256 * 1024
_SNAPSHOT_INLINE_BACKUP_TOTAL_MAX_BYTES = 8 * 1024 * 1024
_SANDBOX_DIRNAME = "_workspace_sandboxes"


@dataclass(frozen=True)
class _WorkspaceFileState:
    mtime: float
    size: int
    mode: int
    content: bytes | None = None


@dataclass(frozen=True)
class _WorkspaceState:
    files: dict[str, _WorkspaceFileState]
    dirs: set[str]


# -- Prompts ------------------------------------------------------------------

_DISCOVERY_PROMPT = """\
You are a Research Agent. Explore the codebase to build a comprehensive \
understanding for the following implementation task.

TASK: {task}

Steps:
1. Read the project README, CLAUDE.md, or similar documentation files.
2. Explore the directory structure with available read-only tools.
3. Identify the key files, modules, and abstractions related to this task.
4. Read the most relevant source files in detail.
5. If read-only shell or git inspection is available, check recent related changes; otherwise note the limitation explicitly.
6. Look for existing tests, configs, CI files that would be affected.

IMPORTANT: Do NOT modify any files, do not request broader permissions, and stay within read-only tooling.

Output format (all sections required):

## Project Overview
[Brief summary of the project's purpose, tech stack, and architecture]

## Relevant Code Areas
[For each relevant file/module: path, purpose, key classes/functions, \
and why it matters for this task]

## Existing Patterns & Conventions
[Coding patterns, naming conventions, test patterns, dependency management \
approach observed in the codebase]

## External Dependencies
[Libraries, APIs, services that are relevant to this task — both existing \
and potentially needed]
"""

_ANALYSIS_PROMPT = """\
You are a Senior Software Architect. Based on the discovery findings below, \
perform a thorough analysis of what it would take to implement this task.

TASK: {task}

DISCOVERY FINDINGS:
{discovery}

Think deeply about:
1. Multiple possible implementation approaches (at least 2-3 alternatives).
2. For each approach: pros, cons, effort estimate (S/M/L/XL), risk level.
3. Which existing code needs to change vs. new code to write.
4. Data model changes (schema, migrations, backward compatibility).
5. API/interface changes (breaking changes, versioning).
6. Security implications (auth, input validation, secrets, OWASP).
7. Performance implications (scaling, caching, concurrency).
8. Testing strategy (unit, integration, e2e — what's testable, what's hard to test).
9. Dependencies on external systems or teams.
10. Migration/rollback plan if this goes wrong.
11. Edge cases and failure modes.
12. What you are uncertain about or cannot determine from the code alone.

IMPORTANT: Do NOT modify any files. This is analysis only.

Output format (all sections required):

## Implementation Approaches
[At least 2-3 alternatives with pros/cons/effort/risk for each]

## Recommended Approach
[Which approach and why — be specific about the reasoning]

## Required Changes
[Concrete list of files to create/modify/delete, with what changes each needs]

## Data & API Impact
[Schema changes, migrations, breaking API changes, backward compatibility]

## Security & Performance
[Security considerations and performance implications]

## Testing Strategy
[What tests to write, what's hard to test, test data needs]

## Risks & Edge Cases
[Things that could go wrong, edge cases, failure modes]

## Unknowns & Uncertainties
[What cannot be determined from the codebase alone — things that need \
clarification from the developer/team]
"""

_QUESTIONS_PROMPT = """\
You are a meticulous Technical Lead preparing a pre-implementation review. \
Based on the research and analysis below, generate a comprehensive list of \
questions that the developer must answer before starting implementation.

TASK: {task}

DISCOVERY FINDINGS:
{discovery}

ANALYSIS:
{analysis}

Generate questions in these categories:

1. **Requirements Clarification** — Ambiguities in the task description, \
   unclear acceptance criteria, missing requirements.

2. **Architecture Decisions** — Which approach to take, trade-offs that need \
   a human decision, integration points.

3. **Scope Boundaries** — What's in scope vs. out of scope, MVP vs. full \
   implementation, phasing.

4. **Technical Unknowns** — Things the code analysis couldn't resolve, \
   external system behaviors, undocumented assumptions.

5. **Risk Mitigation** — How to handle failure modes, rollback strategies, \
   feature flags, gradual rollout.

6. **Testing & Validation** — How to verify correctness, test data needs, \
   environments, manual QA.

Rules:
- Each question must be specific and actionable (not generic).
- Reference concrete files, functions, or code patterns where relevant.
- Suggest possible answers or options where appropriate (e.g. "Option A: ... \
  Option B: ...") so the developer can simply pick one.
- Prioritize: mark critical questions that BLOCK implementation with [BLOCKING].
- Skip obvious or trivially answerable questions.
- Aim for 8-20 questions total. Quality over quantity.

IMPORTANT: Do NOT modify any files.

Output format:

## Blocking Questions
[Questions marked [BLOCKING] that must be answered before any work begins]

## Requirements Clarification
[Questions about what exactly needs to be built]

## Architecture Decisions
[Questions about how to build it]

## Scope & Phasing
[Questions about boundaries and ordering]

## Technical Unknowns
[Questions the code analysis couldn't answer]

## Risk & Rollback
[Questions about failure handling]

## Testing & Validation
[Questions about verification]
"""


def _capture_workspace_state(
    cwd: str | None,
    *,
    excluded_dirs: set[str] | None = None,
) -> _WorkspaceState:
    """Capture workspace metadata with bounded inline content snapshots."""
    root = Path(cwd or ".")
    ignored_dirs = excluded_dirs or _SNAPSHOT_IGNORED_DIRS
    files: dict[str, _WorkspaceFileState] = {}
    dirs = {""}
    backup_budget_left = _SNAPSHOT_INLINE_BACKUP_TOTAL_MAX_BYTES
    if not root.exists():
        return _WorkspaceState(files=files, dirs=dirs)
    try:
        for current_root, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                dirname for dirname in dirnames
                if dirname not in ignored_dirs
            ]
            current_path = Path(current_root)
            try:
                rel_dir = current_path.relative_to(root)
                rel_dir_str = "" if rel_dir == Path(".") else rel_dir.as_posix()
                dirs.add(rel_dir_str)
            except ValueError:
                continue
            for filename in filenames:
                path = current_path / filename
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    continue
                if any(part in ignored_dirs for part in rel.parts):
                    continue
                try:
                    stat = path.stat()
                    content = None
                    suffix = path.suffix.lower()
                    if (
                        stat.st_size <= _SNAPSHOT_INLINE_BACKUP_FILE_MAX_BYTES
                        and stat.st_size <= backup_budget_left
                        and suffix in _SNAPSHOT_INLINE_BACKUP_SUFFIXES
                    ):
                        content = path.read_bytes()
                        backup_budget_left -= len(content)
                    files[rel.as_posix()] = _WorkspaceFileState(
                        mtime=stat.st_mtime,
                        size=stat.st_size,
                        mode=stat.st_mode,
                        content=content,
                    )
                except OSError:
                    pass
    except OSError:
        pass
    return _WorkspaceState(files=files, dirs=dirs)


def _workspace_metadata(state: _WorkspaceState) -> dict[str, tuple[float, int, int]]:
    return {
        path: (file_state.mtime, file_state.size, file_state.mode)
        for path, file_state in state.files.items()
    }


def _diff_workspace(
    before: _WorkspaceState,
    after: _WorkspaceState,
) -> str:
    before_files = _workspace_metadata(before)
    after_files = _workspace_metadata(after)
    created = sorted(set(after_files) - set(before_files))
    deleted = sorted(set(before_files) - set(after_files))
    modified = sorted(
        name
        for name in set(before_files) & set(after_files)
        if before_files[name] != after_files[name]
    )
    created_dirs = sorted(rel_path for rel_path in after.dirs - before.dirs if rel_path)
    deleted_dirs = sorted(rel_path for rel_path in before.dirs - after.dirs if rel_path)

    if not created and not deleted and not modified and not created_dirs and not deleted_dirs:
        return ""

    lines: list[str] = []
    if created:
        lines.append(f"Created ({len(created)}): {', '.join(created)}")
    if deleted:
        lines.append(f"Deleted ({len(deleted)}): {', '.join(deleted)}")
    if modified:
        lines.append(f"Modified ({len(modified)}): {', '.join(modified)}")
    if created_dirs:
        lines.append(f"Created dirs ({len(created_dirs)}): {', '.join(created_dirs)}")
    if deleted_dirs:
        lines.append(f"Deleted dirs ({len(deleted_dirs)}): {', '.join(deleted_dirs)}")
    return "\n".join(lines)


def _copy_workspace_to_sandbox(root: Path, sandbox_root: Path) -> None:
    """Create an isolated workspace mirror for a single provider phase."""
    sandbox_root.mkdir(parents=True, exist_ok=True)
    try:
        for current_root, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                dirname for dirname in dirnames
                if dirname not in _SANDBOX_COPY_EXCLUDED_DIRS
            ]
            current_path = Path(current_root)
            try:
                rel_dir = current_path.relative_to(root)
            except ValueError:
                continue
            target_dir = sandbox_root / rel_dir if rel_dir != Path(".") else sandbox_root
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            for filename in filenames:
                source = current_path / filename
                try:
                    rel_path = source.relative_to(root)
                except ValueError:
                    continue
                if any(part in _SANDBOX_COPY_EXCLUDED_DIRS for part in rel_path.parts):
                    continue
                destination = sandbox_root / rel_path
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                except OSError:
                    pass
    except OSError:
        pass


def _run_read_only_phase(
    provider: BaseProvider,
    prompt: str,
    cwd: str | None,
    timeout: int,
) -> tuple[RunResult, str]:
    """Run a provider phase in an isolated workspace and verify it stayed read-only."""
    root = Path(cwd or ".")
    sandbox_root = None
    phase_cwd = cwd
    if root.exists():
        sandbox_root = root / RQA_DIR / _SANDBOX_DIRNAME / f"phase-{time.time_ns()}"
        _copy_workspace_to_sandbox(root, sandbox_root)
        phase_cwd = str(sandbox_root)

    before = _capture_workspace_state(
        phase_cwd,
        excluded_dirs=_SNAPSHOT_IGNORED_DIRS,
    )
    try:
        result = provider.run(prompt, cwd=phase_cwd, timeout=timeout, read_only=True)
        after = _capture_workspace_state(
            phase_cwd,
            excluded_dirs=_SNAPSHOT_IGNORED_DIRS,
        )
        change_summary = _diff_workspace(before, after)
        return result, change_summary
    finally:
        if sandbox_root is not None:
            try:
                shutil.rmtree(sandbox_root, ignore_errors=True)
            except OSError:
                pass


class ResearchQATool(BaseTool):
    name = "research-qa"
    read_only = True
    description = (
        "Deep Research + Fragen-Dokument: "
        "Recherchiert Codebase, analysiert Implementierung, erstellt Fragen-Katalog"
    )

    def run(
        self,
        task: str,
        provider: BaseProvider,
        cwd: str | None = None,
        timeout: int | None = None,
        memory_context: str = "",
        **kwargs,
    ) -> ToolResult:
        print(f"  [research-qa] Starte Research & Fragen-Analyse")

        rqa_dir = Path(cwd or ".") / RQA_DIR
        system_prompt = _build_system_prompt(provider.name, memory_context, tool_name=self.name, cwd=cwd)
        all_outputs: list[str] = []

        discovery_timeout = timeout or TOOL_RQA_DISCOVERY_TIMEOUT_SEC
        analysis_timeout = timeout or TOOL_RQA_ANALYSIS_TIMEOUT_SEC
        questions_timeout = timeout or TOOL_RQA_QUESTIONS_TIMEOUT_SEC

        total_input_tokens = 0
        total_output_tokens = 0

        # -- Phase 1: Discovery ------------------------------------------------
        print("  [research-qa] === Phase 1: DISCOVERY ===")
        notify_tool_progress(self.name, 1, 3, "Codebase-Erkundung...")
        discovery_prompt = system_prompt + "\n\n" + _DISCOVERY_PROMPT.format(task=task)
        discovery_result, discovery_changes = _run_read_only_phase(
            provider,
            discovery_prompt,
            cwd,
            discovery_timeout,
        )
        total_input_tokens += discovery_result.input_tokens
        total_output_tokens += discovery_result.output_tokens

        if discovery_changes:
            msg = (
                "Read-only-Verstoss in Phase Discovery: "
                f"Workspace wurde veraendert.\n{discovery_changes}"
            )
            print(f"  [research-qa] {msg}")
            notify_tool_done(self.name, 0, False, msg)
            return ToolResult(
                success=False,
                output="",
                iterations=0,
                error=msg,
                error_code="read_only_violation",
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            )

        if not discovery_result.success:
            msg = f"Discovery fehlgeschlagen: {discovery_result.error}"
            print(f"  [research-qa] {msg}")
            notify_tool_done(self.name, 0, False, msg)
            return ToolResult(
                success=False, output="", iterations=0,
                error=msg, error_code=discovery_result.error, retryable=True,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            )

        discovery_output = discovery_result.output.strip()
        all_outputs.append(f"--- Discovery ---\n{discovery_output}")
        _write_tool_file(rqa_dir, "01-discovery.md",
                     f"# Discovery: {task}\n\n{discovery_output}\n")
        print(f"  [research-qa] Discovery abgeschlossen")
        time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

        # -- Phase 2: Analysis -------------------------------------------------
        print("  [research-qa] === Phase 2: ANALYSIS ===")
        notify_tool_progress(self.name, 2, 3, "Tiefenanalyse...")
        analysis_prompt = system_prompt + "\n\n" + _ANALYSIS_PROMPT.format(
            task=task, discovery=discovery_output,
        )
        analysis_result, analysis_changes = _run_read_only_phase(
            provider,
            analysis_prompt,
            cwd,
            analysis_timeout,
        )
        total_input_tokens += analysis_result.input_tokens
        total_output_tokens += analysis_result.output_tokens

        if analysis_changes:
            msg = (
                "Read-only-Verstoss in Phase Analysis: "
                f"Workspace wurde veraendert.\n{analysis_changes}"
            )
            print(f"  [research-qa] {msg}")
            notify_tool_done(self.name, 1, False, msg)
            return ToolResult(
                success=False,
                output="\n\n".join(all_outputs),
                iterations=1,
                error=msg,
                error_code="read_only_violation",
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            )

        if not analysis_result.success:
            msg = f"Analysis fehlgeschlagen: {analysis_result.error}"
            print(f"  [research-qa] {msg}")
            notify_tool_done(self.name, 1, False, msg)
            return ToolResult(
                success=False, output="\n\n".join(all_outputs), iterations=1,
                error=msg, error_code=analysis_result.error, retryable=True,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            )

        analysis_output = analysis_result.output.strip()
        all_outputs.append(f"--- Analysis ---\n{analysis_output}")
        _write_tool_file(rqa_dir, "02-analysis.md",
                     f"# Analysis: {task}\n\n{analysis_output}\n")
        print(f"  [research-qa] Analysis abgeschlossen")
        time.sleep(TOOL_INTER_STEP_SLEEP_SEC)

        # -- Phase 3: Question Generation --------------------------------------
        print("  [research-qa] === Phase 3: QUESTIONS ===")
        notify_tool_progress(self.name, 3, 3, "Fragen-Katalog wird erstellt...")
        questions_prompt = system_prompt + "\n\n" + _QUESTIONS_PROMPT.format(
            task=task, discovery=discovery_output, analysis=analysis_output,
        )
        questions_result, questions_changes = _run_read_only_phase(
            provider,
            questions_prompt,
            cwd,
            questions_timeout,
        )
        total_input_tokens += questions_result.input_tokens
        total_output_tokens += questions_result.output_tokens

        if questions_changes:
            msg = (
                "Read-only-Verstoss in Phase Questions: "
                f"Workspace wurde veraendert.\n{questions_changes}"
            )
            print(f"  [research-qa] {msg}")
            notify_tool_done(self.name, 2, False, msg)
            return ToolResult(
                success=False,
                output="\n\n".join(all_outputs),
                iterations=2,
                error=msg,
                error_code="read_only_violation",
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            )

        if not questions_result.success:
            msg = f"Fragen-Generierung fehlgeschlagen: {questions_result.error}"
            print(f"  [research-qa] {msg}")
            notify_tool_done(self.name, 2, False, msg)
            return ToolResult(
                success=False, output="\n\n".join(all_outputs), iterations=2,
                error=msg, error_code=questions_result.error, retryable=True,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            )

        questions_output = questions_result.output.strip()
        all_outputs.append(f"--- Questions ---\n{questions_output}")
        _write_tool_file(
            rqa_dir,
            "03-questions.md",
            f"# Questions: {task}\n\n{questions_output}\n",
        )

        # -- Write final combined document -------------------------------------
        combined = (
            f"# Research & Fragen: {task}\n\n"
            f"---\n\n"
            f"## Aufgabe\n\n{task}\n\n"
            f"---\n\n"
            f"# Teil 1: Discovery\n\n{discovery_output}\n\n"
            f"---\n\n"
            f"# Teil 2: Analyse\n\n{analysis_output}\n\n"
            f"---\n\n"
            f"# Teil 3: Fragen\n\n{questions_output}\n\n"
            f"---\n\n"
            f"**Bitte beantworte die obigen Fragen (besonders [BLOCKING]) "
            f"bevor mit der Implementierung begonnen wird.**\n"
        )
        _write_tool_file(rqa_dir, "research-qa-complete.md", combined)

        msg = (
            f"Research & Fragen-Dokument erstellt. "
            f"Siehe {rqa_dir / 'research-qa-complete.md'}"
        )
        print(f"  [research-qa] {msg}")
        notify_tool_done(self.name, 3, True, msg)
        return ToolResult(
            success=True,
            output="\n\n".join(all_outputs),
            iterations=3,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
        )
