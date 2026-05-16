"""Tests for queue_linter.py — validates queue files without executing tasks."""

from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    import queue_linter
    from queue_linter import (
        LEVEL_ERROR,
        LEVEL_INFO,
        LEVEL_WARN,
        exit_code_for,
        format_findings,
        lint_queue,
    )


def _codes(findings):
    return {f.code for f in findings}


def _levels(findings):
    return {f.level for f in findings}


# ---------------------------------------------------------------------------
# Empty / clean queues
# ---------------------------------------------------------------------------

def test_empty_content_returns_no_findings():
    assert lint_queue("") == []


def test_queue_with_no_open_tasks_returns_no_findings():
    content = "# Queue\n\n## Queue\n\n## Ergebnisse\n"
    assert lint_queue(content) == []


def test_clean_simple_task_passes(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])
    content = f"## Queue\n- [ ] Review code cwd:{project} #tool:review-loop\n"
    findings = lint_queue(content)
    assert findings == []
    assert exit_code_for(findings) == 0


# ---------------------------------------------------------------------------
# cwd: validation
# ---------------------------------------------------------------------------

def test_missing_directory_flagged_as_error(tmp_path, monkeypatch):
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])
    missing = tmp_path / "does_not_exist"
    content = f"## Queue\n- [ ] Review cwd:{missing} #tool:review-loop\n"
    findings = lint_queue(content)
    assert "invalid_cwd" in _codes(findings)
    assert exit_code_for(findings) == 2


def test_cwd_outside_allowed_roots_flagged(tmp_path, monkeypatch):
    sandbox = tmp_path / "allowed"
    sandbox.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [sandbox])
    content = f"## Queue\n- [ ] Review cwd:{outside} #tool:review-loop\n"
    findings = lint_queue(content)
    assert "invalid_cwd" in _codes(findings)


def test_task_without_cwd_tag_is_silent(tmp_path):
    content = "## Queue\n- [ ] Plain task without cwd\n"
    findings = lint_queue(content)
    assert "invalid_cwd" not in _codes(findings)


# ---------------------------------------------------------------------------
# Tool tag validation
# ---------------------------------------------------------------------------

def test_unknown_tool_tag_flagged(tmp_path):
    content = "## Queue\n- [ ] Do thing #tool:this-does-not-exist\n"
    findings = lint_queue(content)
    assert "unknown_tool" in _codes(findings)
    assert exit_code_for(findings) == 2


def test_known_tool_tag_passes():
    content = "## Queue\n- [ ] Review #tool:review-loop\n"
    findings = lint_queue(content)
    assert "unknown_tool" not in _codes(findings)


# ---------------------------------------------------------------------------
# Model alias validation
# ---------------------------------------------------------------------------

def test_unknown_claude_alias_flagged():
    content = "## Queue\n- [ ] Task #claude_giga\n"
    findings = lint_queue(content)
    assert "unknown_model" in _codes(findings)


def test_unknown_or_alias_flagged():
    content = "## Queue\n- [ ] Task #or_doesnotexist\n"
    findings = lint_queue(content)
    assert "unknown_model" in _codes(findings)


def test_known_model_alias_passes():
    content = "## Queue\n- [ ] Task #claude_opus\n"
    findings = lint_queue(content)
    assert "unknown_model" not in _codes(findings)


def test_cross_provider_model_leakage_flagged():
    """#claude_opus + explicit #gemini = error."""
    content = "## Queue\n- [ ] Task #gemini #claude_opus\n"
    findings = lint_queue(content)
    assert "model_provider_mismatch" in _codes(findings)


def test_model_alias_matches_explicit_provider_passes():
    content = "## Queue\n- [ ] Task #claude #claude_opus\n"
    findings = lint_queue(content)
    assert "model_provider_mismatch" not in _codes(findings)


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------

def test_openrouter_tag_without_key_is_warning(monkeypatch):
    monkeypatch.setattr(queue_linter, "OPENROUTER_API_KEY", "")
    content = "## Queue\n- [ ] Task #or_glm\n"
    findings = lint_queue(content)
    assert "openrouter_missing_key" in _codes(findings)
    # warning only — exit code should be 1, not 2 (assuming or_glm is a known alias)
    assert exit_code_for(findings) >= 1


def test_openrouter_tag_with_key_passes(monkeypatch):
    monkeypatch.setattr(queue_linter, "OPENROUTER_API_KEY", "sk-xxx")
    content = "## Queue\n- [ ] Task #or_glm\n"
    findings = lint_queue(content)
    assert "openrouter_missing_key" not in _codes(findings)


def test_bare_openrouter_tag_treated_like_or_alias(monkeypatch):
    monkeypatch.setattr(queue_linter, "OPENROUTER_API_KEY", "")
    content = "## Queue\n- [ ] Task #openrouter\n"
    findings = lint_queue(content)
    assert "openrouter_missing_key" in _codes(findings)


# ---------------------------------------------------------------------------
# Duplicate IDs
# ---------------------------------------------------------------------------

def test_duplicate_id_flagged_on_both_lines():
    content = (
        "## Queue\n"
        "- [ ] First task #id:foo\n"
        "- [ ] Second task #id:foo\n"
    )
    findings = lint_queue(content)
    dup = [f for f in findings if f.code == "duplicate_id"]
    assert len(dup) == 2
    assert {f.line_no for f in dup} == {2, 3}


def test_unique_ids_pass():
    content = (
        "## Queue\n"
        "- [ ] First #id:a\n"
        "- [ ] Second #id:b\n"
    )
    findings = lint_queue(content)
    assert "duplicate_id" not in _codes(findings)


# ---------------------------------------------------------------------------
# #needs: dependencies
# ---------------------------------------------------------------------------

def test_needs_pointing_to_unknown_id_is_warning():
    content = "## Queue\n- [ ] Dependent #needs:ghost\n"
    findings = lint_queue(content)
    assert "unknown_needs" in _codes(findings)
    assert exit_code_for(findings) == 1


def test_needs_pointing_to_completed_task_passes():
    content = (
        "## Queue\n"
        "- [x] First #id:foo ✅ 2026-01-01 12:00 (claude)\n"
        "- [ ] Dependent #needs:foo\n"
    )
    findings = lint_queue(content)
    assert "unknown_needs" not in _codes(findings)


def test_needs_pointing_to_open_task_passes():
    content = (
        "## Queue\n"
        "- [ ] First #id:foo\n"
        "- [ ] Dependent #needs:foo\n"
    )
    findings = lint_queue(content)
    assert "unknown_needs" not in _codes(findings)


def test_needs_partial_resolution_flags_only_missing():
    content = (
        "## Queue\n"
        "- [ ] First #id:a\n"
        "- [ ] Dependent #needs:a,b\n"
    )
    findings = lint_queue(content)
    needs_findings = [f for f in findings if f.code == "unknown_needs"]
    assert len(needs_findings) == 1
    assert "b" in needs_findings[0].message
    assert "a" not in needs_findings[0].message.split(":")[-1]  # 'a' not in missing list


# ---------------------------------------------------------------------------
# #parallel
# ---------------------------------------------------------------------------

def test_parallel_without_subtasks_is_warning():
    content = "## Queue\n- [ ] Parent #parallel\n"
    findings = lint_queue(content)
    assert "parallel_no_subtasks" in _codes(findings)


def test_parallel_with_single_subtask_is_warning():
    content = (
        "## Queue\n"
        "- [ ] Parent #parallel\n"
        "  - only one #claude\n"
    )
    findings = lint_queue(content)
    assert "parallel_no_subtasks" in _codes(findings)


def test_parallel_with_distinct_cwds_is_clean(tmp_path, monkeypatch):
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    content = (
        "## Queue\n"
        "- [ ] Parent #parallel\n"
        f"  - work A cwd:{a} #claude\n"
        f"  - work B cwd:{b} #gemini\n"
    )
    findings = lint_queue(content)
    assert "parallel_no_subtasks" not in _codes(findings)
    assert "parallel_shared_cwd" not in _codes(findings)


def test_parallel_subtasks_without_cwd_is_info(tmp_path, monkeypatch):
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])
    content = (
        "## Queue\n"
        "- [ ] Parent #parallel\n"
        "  - first #claude\n"
        "  - second #gemini\n"
    )
    findings = lint_queue(content)
    assert "parallel_shared_cwd" in _codes(findings)
    # info-only: no warnings or errors required
    assert all(f.level != LEVEL_ERROR for f in findings if f.code == "parallel_shared_cwd")


# ---------------------------------------------------------------------------
# Exit codes & formatting
# ---------------------------------------------------------------------------

def test_exit_code_clean():
    assert exit_code_for([]) == 0


def test_exit_code_only_warnings():
    findings = [queue_linter.LintFinding(LEVEL_WARN, 1, "x", "msg")]
    assert exit_code_for(findings) == 1


def test_exit_code_with_errors():
    findings = [
        queue_linter.LintFinding(LEVEL_WARN, 1, "x", "warn"),
        queue_linter.LintFinding(LEVEL_ERROR, 2, "y", "err"),
    ]
    assert exit_code_for(findings) == 2


def test_format_findings_clean_message():
    out = format_findings([])
    assert "keine Probleme" in out


def test_format_findings_includes_summary_counts():
    findings = [
        queue_linter.LintFinding(LEVEL_ERROR, 1, "x", "err1"),
        queue_linter.LintFinding(LEVEL_ERROR, 2, "y", "err2"),
        queue_linter.LintFinding(LEVEL_WARN, 3, "z", "warn1"),
    ]
    out = format_findings(findings)
    assert "2 error" in out
    assert "1 warning" in out


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

def test_run_lint_returns_zero_on_clean_file(tmp_path, monkeypatch, capsys):
    queue = tmp_path / "agent-queue.md"
    queue.write_text("## Queue\n- [ ] Plain task\n", encoding="utf-8")
    monkeypatch.setattr(queue_linter, "QUEUE_FILE", queue)
    rc = queue_linter.run_lint()
    assert rc == 0


def test_run_lint_returns_two_on_unknown_tool(tmp_path, monkeypatch, capsys):
    queue = tmp_path / "agent-queue.md"
    queue.write_text("## Queue\n- [ ] Bad #tool:nonexistent\n", encoding="utf-8")
    monkeypatch.setattr(queue_linter, "QUEUE_FILE", queue)
    rc = queue_linter.run_lint()
    assert rc == 2


def test_run_lint_handles_missing_queue_file(tmp_path, monkeypatch):
    queue = tmp_path / "does-not-exist.md"
    monkeypatch.setattr(queue_linter, "QUEUE_FILE", queue)
    rc = queue_linter.run_lint()
    assert rc == 0  # missing file = nothing to lint
