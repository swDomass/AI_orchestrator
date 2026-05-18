"""Tests for preflight.py — per-tool context collectors."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import preflight


def _git_init(cwd: Path) -> None:
    """Initialize a minimal git repo for tests that need one."""
    subprocess.run(["git", "init"], cwd=cwd, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=cwd, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=cwd, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=cwd, capture_output=True)


def test_collect_returns_empty_for_missing_cwd():
    assert preflight.collect("dev-loop", None) == ""
    assert preflight.collect("dev-loop", "/nonexistent/path") == ""


def test_collect_returns_empty_for_unknown_tool(tmp_path: Path):
    assert preflight.collect("unknown-tool", tmp_path) == ""


def test_list_registered_tools_includes_known():
    names = preflight.list_registered_tools()
    assert "dev-loop" in names
    assert "review-loop" in names
    assert "security-audit" in names
    assert "deep-security-audit" in names
    assert "critical-review" in names
    assert "research-qa" in names


def test_dev_loop_preflight_no_git(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    out = preflight.collect("dev-loop", tmp_path)
    assert "## Preflight: dev-loop" in out
    assert "pip" in out
    assert "pytest" in out


def test_dev_loop_preflight_detects_npm(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    out = preflight.collect("dev-loop", tmp_path)
    assert "npm" in out
    assert "npm test" in out


def test_review_loop_preflight_no_git(tmp_path: Path):
    out = preflight.collect("review-loop", tmp_path)
    assert "## Preflight: review-loop" in out
    assert "Diff-Statistik nicht verfügbar" in out


def test_review_loop_preflight_with_git(tmp_path: Path):
    _git_init(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "b.js").write_text("var y = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)

    out = preflight.collect("review-loop", tmp_path)
    assert "Changed files" in out
    assert "a.py" in out
    assert "b.js" in out


def test_security_audit_preflight_detects_manifests(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("requests==2\n", encoding="utf-8")
    out = preflight.collect("security-audit", tmp_path)
    assert "Dependency manifests" in out
    assert "requirements.txt" in out


def test_security_audit_preflight_flags_env_file(tmp_path: Path):
    (tmp_path / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    out = preflight.collect("security-audit", tmp_path)
    assert "Exposed config files" in out
    assert ".env" in out


def test_critical_review_preflight_lists_md_files(tmp_path: Path):
    (tmp_path / "plan.md").write_text("# Plan\n\n## Step 1\n\nDo X.\n", encoding="utf-8")
    out = preflight.collect("critical-review", tmp_path)
    assert "Plan candidates" in out
    assert "plan.md" in out
    assert "# Plan" in out


def test_critical_review_preflight_no_md(tmp_path: Path):
    out = preflight.collect("critical-review", tmp_path)
    assert "Keine .md-Dateien" in out


def test_research_qa_preflight_with_readme(tmp_path: Path):
    (tmp_path / "README.md").write_text("# Project\n\nHello.\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    out = preflight.collect("research-qa", tmp_path)
    assert "README.md" in out
    assert "Language histogram" in out
    assert ".py" in out


def test_research_qa_preflight_no_readme(tmp_path: Path):
    (tmp_path / "main.go").write_text("package main\n", encoding="utf-8")
    out = preflight.collect("research-qa", tmp_path)
    assert "Nicht vorhanden" in out


def test_collect_cached_writes_to_disk(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
    out = preflight.collect_cached("security-audit", tmp_path)
    assert out != ""
    cache_dir = tmp_path / ".security-audit"
    assert cache_dir.exists()
    cache_files = list(cache_dir.glob("preflight-*.md"))
    assert len(cache_files) == 1


def test_collect_cached_returns_same_content_on_second_call(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
    first = preflight.collect_cached("security-audit", tmp_path)
    # Modify state — second call should still return cached content
    (tmp_path / "credentials.json").write_text('{"key": "x"}', encoding="utf-8")
    second = preflight.collect_cached("security-audit", tmp_path)
    assert first == second


def test_collect_cached_skips_when_no_hook(tmp_path: Path):
    assert preflight.collect_cached("nonsense", tmp_path) == ""


def test_truncate_respects_cap():
    big = "x" * (preflight.PREFLIGHT_MAX_CHARS + 1000)
    out = preflight._truncate(big)
    assert len(out) <= preflight.PREFLIGHT_MAX_CHARS + len("\n...[preflight truncated]")
    assert out.endswith("[preflight truncated]")


def test_hook_exception_returns_empty(monkeypatch, tmp_path: Path):
    def boom(cwd):
        raise RuntimeError("kapow")
    monkeypatch.setitem(preflight._COLLECTORS, "boomtool", boom)
    assert preflight.collect("boomtool", tmp_path) == ""
