"""Security-focused tests: path traversal, CWD validation, policy bypass."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from queue_manager import extract_cwd


# ── Path Traversal ───────────────────────────────────────────────────────────

def test_cwd_rejects_parent_traversal(tmp_path, monkeypatch):
    """cwd:../../../etc/passwd must be rejected."""
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [tmp_path])
    result = extract_cwd(f"Fix bug cwd:../../../etc/passwd")
    assert result is None


def test_cwd_rejects_nonexistent_directory():
    result = extract_cwd("Fix bug cwd:/nonexistent/path/12345")
    assert result is None


def test_cwd_rejects_outside_allowed_roots(tmp_path, monkeypatch):
    """A valid directory outside ALLOWED_CWD_ROOTS must be rejected."""
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [Path("C:/nonexistent_allowed_root")])
    result = extract_cwd(f"Fix bug cwd:{tmp_path}")
    assert result is None


def test_cwd_accepts_path_within_allowed_roots(tmp_path, monkeypatch):
    """A valid directory within ALLOWED_CWD_ROOTS must be accepted."""
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [tmp_path.parent])
    result = extract_cwd(f"Fix bug cwd:{tmp_path}")
    assert result is not None


def test_cwd_empty_allowed_roots_accepts_any(tmp_path, monkeypatch):
    """When ALLOWED_CWD_ROOTS is empty, any valid directory is accepted."""
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])
    result = extract_cwd(f"Fix bug cwd:{tmp_path}")
    assert result is not None


# ── Shell Metacharacters ─────────────────────────────────────────────────────

def test_cwd_with_shell_metacharacters():
    """Shell metacharacters in cwd path should not cause issues."""
    result = extract_cwd("Fix bug cwd:$(rm -rf /)")
    assert result is None  # not a valid directory


def test_cwd_with_semicolon_injection():
    result = extract_cwd("Fix bug cwd:/tmp; rm -rf /")
    assert result is None


# ── Policy Case-Sensitivity ──────────────────────────────────────────────────

def test_policy_tags_are_case_insensitive():
    """Tags like #CLAUDE or #Claude should be recognized the same as #claude."""
    from dispatcher import has_explicit_provider_tag
    assert has_explicit_provider_tag("Fix bug #CLAUDE") is True
    assert has_explicit_provider_tag("Fix bug #Claude") is True
    assert has_explicit_provider_tag("Fix bug #claude") is True


# ── CWD with Spaces ─────────────────────────────────────────────────────────

def test_cwd_quoted_path_with_spaces(tmp_path, monkeypatch):
    """Quoted paths with spaces should work."""
    spaced_dir = tmp_path / "path with spaces"
    spaced_dir.mkdir()
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])
    result = extract_cwd(f'Fix bug cwd:"{spaced_dir}"')
    assert result is not None
