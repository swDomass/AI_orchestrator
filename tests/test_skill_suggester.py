"""Tests for skill_suggester.py — draft-only, pattern-gated."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import skill_suggester as ss
from skill_suggester import (
    CandidatePattern,
    _normalize_task_shape,
    find_candidates,
    pattern_id,
    suggest_once,
    write_draft,
)


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path: Path):
    ss.set_ledger_path(tmp_path / "skill-suggestions.jsonl")
    yield
    ss.reset_for_tests()


def _rec(*, task_text="fix login bug", tool="dev-loop", cwd="/proj",
         exit_status="ok", ts_offset_days=0) -> dict:
    ts = (datetime.now() - timedelta(days=ts_offset_days)).strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "task_text": task_text,
        "tool": tool,
        "cwd": cwd,
        "exit_status": exit_status,
        "ts_start": ts,
    }


# ---------------------------------------------------------------------------
# Pattern extraction
# ---------------------------------------------------------------------------

def test_normalize_task_shape_filters_stopwords():
    shape = _normalize_task_shape("Fix the login bug for user authentication")
    assert "the" not in shape
    assert "for" not in shape
    assert "login" in shape


def test_normalize_task_shape_caps_top_n():
    text = " ".join(f"word{i}" for i in range(20))
    shape = _normalize_task_shape(text, top_n=3)
    assert len(shape) == 3


def test_normalize_task_shape_sorted():
    shape = _normalize_task_shape("login authentication bug fix module")
    assert list(shape) == sorted(shape)


def test_normalize_task_shape_returns_empty_for_stopwords_only():
    shape = _normalize_task_shape("the and for with from")
    assert shape == ()


def test_pattern_id_is_stable():
    a = pattern_id("dev-loop", "/proj", ("auth", "bug", "login"))
    b = pattern_id("dev-loop", "/proj", ("auth", "bug", "login"))
    assert a == b


def test_pattern_id_differs_on_different_tool():
    a = pattern_id("dev-loop", "/proj", ("auth",))
    b = pattern_id("review-loop", "/proj", ("auth",))
    assert a != b


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_find_candidates_needs_min_occurrences():
    records = [
        _rec(task_text="fix login bug for user auth"),
        _rec(task_text="fix login bug for user auth"),
    ]
    assert find_candidates(records) == []


def test_find_candidates_returns_pattern_at_threshold():
    records = [
        _rec(task_text="fix login bug for user auth"),
        _rec(task_text="fix login bug for user auth"),
        _rec(task_text="fix login bug for user auth"),
    ]
    cands = find_candidates(records)
    assert len(cands) == 1
    assert cands[0].occurrences == 3
    assert cands[0].tool == "dev-loop"


def test_find_candidates_ignores_failed_runs():
    records = [_rec(exit_status="error") for _ in range(5)]
    assert find_candidates(records) == []


def test_find_candidates_ignores_runs_outside_window():
    records = [_rec(ts_offset_days=60) for _ in range(5)]
    assert find_candidates(records) == []


def test_find_candidates_skips_without_tool_or_cwd():
    records = [_rec(tool="") for _ in range(5)]
    assert find_candidates(records) == []
    records = [_rec(cwd="") for _ in range(5)]
    assert find_candidates(records) == []


def test_find_candidates_groups_similar_task_shapes():
    """Phrasing variations with identical top-keywords cluster together."""
    records = [
        _rec(task_text="fix login bug user auth module"),
        _rec(task_text="user auth login bug fix module"),
        _rec(task_text="module bug login user auth fix"),
    ]
    cands = find_candidates(records)
    assert len(cands) == 1
    assert cands[0].occurrences == 3


# ---------------------------------------------------------------------------
# Draft writing
# ---------------------------------------------------------------------------

def test_write_draft_creates_skill_md(tmp_path):
    cand = CandidatePattern(
        pattern_id="x",
        tool="dev-loop",
        cwd="/proj",
        task_shape=("auth", "bug", "login"),
        occurrences=3,
        sample_task_texts=("fix bug A", "fix bug B"),
    )
    drafts = tmp_path / "Skills-Drafts"
    path = write_draft(cand, drafts)
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "name: dev-loop-" in content
    assert "## Pattern (auto-detected)" in content
    assert "dev-loop" in content
    assert "fix bug A" in content


def test_write_draft_uses_llm_summary_when_provided(tmp_path):
    cand = CandidatePattern(
        pattern_id="x",
        tool="dev-loop",
        cwd="/proj",
        task_shape=("auth",),
        occurrences=3,
        sample_task_texts=(),
    )
    path = write_draft(cand, tmp_path, summary="Custom LLM description here.")
    content = path.read_text(encoding="utf-8")
    assert "Custom LLM description here." in content


# ---------------------------------------------------------------------------
# suggest_once flow
# ---------------------------------------------------------------------------

def test_suggest_once_writes_draft_and_notifies(tmp_path):
    records = [_rec(task_text="fix login bug user auth module") for _ in range(3)]
    drafts = tmp_path / "Skills-Drafts"
    notified: list[str] = []

    written = suggest_once(
        drafts_root=drafts,
        records=records,
        notify_fn=notified.append,
    )
    assert len(written) == 1
    cand, path = written[0]
    assert path.exists()
    assert len(notified) == 1
    assert "Skill-Vorschlag" in notified[0]


def test_suggest_once_respects_cooldown(tmp_path):
    records = [_rec(task_text="fix login bug user auth module") for _ in range(3)]
    drafts = tmp_path / "Skills-Drafts"

    written1 = suggest_once(drafts_root=drafts, records=records, notify_fn=None)
    assert len(written1) == 1

    # Second pass within cooldown — should be empty
    written2 = suggest_once(drafts_root=drafts, records=records, notify_fn=None)
    assert written2 == []


def test_suggest_once_handles_summary_failure(tmp_path):
    """A broken summary_fn must not crash the suggester."""
    records = [_rec(task_text="fix login bug user auth module") for _ in range(3)]
    drafts = tmp_path / "Skills-Drafts"

    def boom(cand):
        raise RuntimeError("kapow")

    written = suggest_once(drafts_root=drafts, records=records, summary_fn=boom)
    assert len(written) == 1  # falls back to generic template


def test_suggest_once_empty_records_returns_empty(tmp_path):
    drafts = tmp_path / "Skills-Drafts"
    assert suggest_once(drafts_root=drafts, records=[]) == []


def test_suggest_once_excludes_failed_runs(tmp_path):
    records = [_rec(task_text="x", exit_status="error") for _ in range(5)]
    drafts = tmp_path / "Skills-Drafts"
    assert suggest_once(drafts_root=drafts, records=records) == []
