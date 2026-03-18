"""Tests for orchestrator._build_prompt() and run_once() basics."""

from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest


def _make_prompt(task="Test task", provider_name="claude", skill_name=None, memory_context=""):
    """Import and call _build_prompt with mocks to avoid vault/memory side effects."""
    mock_memory = SimpleNamespace(
        get_curated_memory=lambda: "",
        get_daily_context=lambda: "",
    )
    with patch("orchestrator.memory_module", mock_memory), \
         patch("orchestrator.inject_file_context", return_value=""):
        from orchestrator import _build_prompt
        return _build_prompt(task, provider_name, skill_name=skill_name, memory_context=memory_context)


def test_build_prompt_includes_system_prompt(monkeypatch):
    monkeypatch.setattr("config.load_soul", lambda: {"base": "I am a helpful assistant."})
    prompt = _make_prompt()
    assert "I am a helpful assistant" in prompt


def test_build_prompt_includes_memory_context():
    prompt = _make_prompt(memory_context="Previous task: fixed auth bug in login.py")
    assert "Previous task: fixed auth bug" in prompt


def test_build_prompt_returns_string():
    result = _make_prompt()
    assert isinstance(result, str)


def test_build_prompt_with_skill_name(monkeypatch):
    mock_skill = SimpleNamespace(prompt="Review all code changes carefully.", name="review-loop")
    monkeypatch.setattr("skills.load_skill", lambda name, vault_path=None: mock_skill)
    prompt = _make_prompt(skill_name="review-loop")
    assert "Review all code" in prompt


def test_run_once_returns_true_on_empty_queue(monkeypatch):
    monkeypatch.setattr("orchestrator.read_queue_items", lambda: [])
    mock_memory = SimpleNamespace(archive_old_memories=lambda: 0)
    monkeypatch.setattr("orchestrator.memory_module", mock_memory)
    from orchestrator import run_once
    result = run_once(dry_run=True)
    assert result is True


def test_run_once_dry_run_processes_without_execution(monkeypatch):
    task = SimpleNamespace(task_text="Fix bug cwd:.", line_no=1, subtasks=())
    monkeypatch.setattr("orchestrator.read_queue_items", lambda: [task])
    mock_memory = SimpleNamespace(
        archive_old_memories=lambda: 0,
        get_context_for_task=lambda *a, **kw: "",
        get_curated_memory=lambda: "",
        get_daily_context=lambda: "",
    )
    monkeypatch.setattr("orchestrator.memory_module", mock_memory)
    monkeypatch.setattr("orchestrator.inject_file_context", lambda *a, **kw: "")

    # Dry-run should not raise even without a real provider
    from orchestrator import run_once
    # Just verify it doesn't crash in dry-run mode
    try:
        run_once(dry_run=True)
    except SystemExit:
        pass  # acceptable in dry-run
