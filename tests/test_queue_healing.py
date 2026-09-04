"""Tests for queue_healing.py — auto-unblock proposals + actions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import queue_healing as qh
from queue_healing import (
    HealCandidate,
    apply_drop,
    apply_retry_dep,
    apply_unblock,
    detect_candidates,
    format_proposal,
    heal_once,
    record_notification,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path: Path):
    qh.set_ledger_path(tmp_path / "queue-healing.jsonl")
    yield
    qh.reset_for_tests()


class _FakeQueueItem:
    def __init__(self, task_text: str, line_no: int = 1, blocked_reason: str = ""):
        self.task_text = task_text
        self.line_no = line_no
        self.blocked_reason = blocked_reason
        self.subtasks = ()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_detect_no_candidates_when_nothing_blocked():
    items = [_FakeQueueItem("Run tests #id:task1")]
    content = "## Queue\n- [ ] Run tests #id:task1\n"
    assert detect_candidates(items, content) == []


def test_detect_failed_dep_yields_unblock_or_retry():
    items = [
        _FakeQueueItem(
            "Run B #id:task-b #needs:task-a",
            blocked_reason="needs task-a",
        ),
    ]
    content = (
        "## Queue\n"
        "- [-] Run A #id:task-a ❌ 2026-05-01 10:00 (failed)\n"
        "- [ ] Run B #id:task-b #needs:task-a\n"
    )
    cands = detect_candidates(items, content)
    assert len(cands) == 1
    assert cands[0].task_id == "task-b"
    assert cands[0].failed_deps == ("task-a",)
    assert cands[0].action == "unblock_or_retry"


def test_detect_requires_task_id():
    """Tasks without #id: cannot be addressed by /unblock — skip them."""
    items = [_FakeQueueItem(
        "Run B #needs:task-a", blocked_reason="needs task-a",
    )]
    content = "## Queue\n- [-] A #id:task-a\n- [ ] Run B #needs:task-a\n"
    assert detect_candidates(items, content) == []


def test_detect_long_block_with_no_other_progress():
    """Task blocked >24h and no other task can resolve the dep."""
    past = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
    items = [
        _FakeQueueItem(
            f"Run B #id:task-b #needs:task-a <!-- retry: {past} -->",
            blocked_reason="needs task-a",
        ),
    ]
    content = (
        "## Queue\n"
        f"- [ ] Run B #id:task-b #needs:task-a <!-- retry: {past} -->\n"
    )
    cands = detect_candidates(items, content)
    assert len(cands) == 1
    assert cands[0].action == "drop_or_wait"


def test_detect_skips_when_dep_is_actively_being_worked_on():
    """If another open task IS the missing dep, don't propose dropping."""
    past = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
    items = [
        _FakeQueueItem("Run A #id:task-a", line_no=1, blocked_reason=""),
        _FakeQueueItem(
            f"Run B #id:task-b #needs:task-a <!-- retry: {past} -->",
            line_no=2, blocked_reason="needs task-a",
        ),
    ]
    content = (
        "## Queue\n"
        "- [ ] Run A #id:task-a\n"
        f"- [ ] Run B #id:task-b #needs:task-a <!-- retry: {past} -->\n"
    )
    assert detect_candidates(items, content) == []


# ---------------------------------------------------------------------------
# Notifications + ledger
# ---------------------------------------------------------------------------

def test_format_proposal_unblock():
    c = HealCandidate(
        task_id="task-b", task_text="Run B", line_no=2,
        reason="dep failed", action="unblock_or_retry",
        failed_deps=("task-a",), blocked_since_age_hours=10.0,
    )
    msg = format_proposal(c)
    assert "/unblock task-b" in msg
    assert "/retry task-a" in msg
    assert "/drop task-b" in msg


def test_format_proposal_drop_or_wait():
    c = HealCandidate(
        task_id="task-b", task_text="Run B", line_no=2,
        reason="stale", action="drop_or_wait",
        failed_deps=(), blocked_since_age_hours=48.0,
    )
    msg = format_proposal(c)
    assert "/unblock task-b" in msg
    assert "/drop task-b" in msg


def test_record_notification_writes_ledger():
    record_notification("task-b", "unblock_or_retry", detail="dep failed")
    entries = qh._read_ledger()
    assert len(entries) == 1
    assert entries[0]["task_id"] == "task-b"
    assert entries[0]["action"] == "unblock_or_retry"


def test_heal_once_notifies_each_candidate_at_most_once():
    items = [_FakeQueueItem("Run B #id:task-b #needs:task-a",
                            blocked_reason="needs task-a")]
    content = "## Queue\n- [-] A #id:task-a\n- [ ] Run B #id:task-b #needs:task-a\n"

    sent: list[str] = []
    def notify(msg: str):
        sent.append(msg)

    heal_once(lambda: items, lambda: content, notify_fn=notify)
    heal_once(lambda: items, lambda: content, notify_fn=notify)

    assert len(sent) == 1
    assert "task-b" in sent[0]


# ---------------------------------------------------------------------------
# Mutating actions
# ---------------------------------------------------------------------------

def test_apply_unblock_promotes_failed_dep(monkeypatch):
    written: dict[str, str] = {}
    content = (
        "## Queue\n"
        "- [-] A #id:task-a ❌ 2026-01-01 10:00 (failed)\n"
        "- [ ] B #id:task-b #needs:task-a\n"
    )

    def fake_apply(transform):
        new = transform(content)
        if new is None:
            return False
        written["out"] = new
        return True

    monkeypatch.setattr("queue_manager._apply_update", fake_apply)
    ok, _ = apply_unblock("task-b")
    assert ok
    assert "- [x] A #id:task-a" in written["out"]
    assert "- [ ] B #id:task-b" in written["out"]


def test_apply_unblock_noop_when_no_failed_dep(monkeypatch):
    content = "## Queue\n- [ ] B #id:task-b\n"
    monkeypatch.setattr(
        "queue_manager._apply_update",
        lambda transform: transform(content) is not None
    )
    ok, _ = apply_unblock("task-b")
    assert not ok


def test_apply_drop_marks_task_failed(monkeypatch):
    written: dict[str, str] = {}
    content = "## Queue\n- [ ] Run B #id:task-b\n"

    def fake_apply(transform):
        new = transform(content)
        if new is None:
            return False
        written["out"] = new
        return True

    monkeypatch.setattr("queue_manager._apply_update", fake_apply)
    ok, msg = apply_drop("task-b")
    assert ok
    assert "- [-] Run B #id:task-b" in written["out"]
    assert "drop via queue-healing" in written["out"]


def test_apply_retry_dep_resets_failed_to_open(monkeypatch):
    written: dict[str, str] = {}
    content = (
        "## Queue\n"
        "- [-] A #id:task-a ❌ 2026-01-01 10:00 (failed)\n"
        "- [ ] B #id:task-b #needs:task-a\n"
    )

    def fake_apply(transform):
        new = transform(content)
        if new is None:
            return False
        written["out"] = new
        return True

    monkeypatch.setattr("queue_manager._apply_update", fake_apply)
    ok, _ = apply_retry_dep(["task-a"])
    assert ok
    assert "- [ ] A #id:task-a" in written["out"]
    assert "❌" not in written["out"]


def test_apply_retry_dep_rejects_empty_input():
    ok, msg = apply_retry_dep([])
    assert not ok


def test_was_recently_notified_respects_cooldown():
    record_notification("task-b", "unblock_or_retry")
    assert qh._was_recently_notified("task-b", "unblock_or_retry") is True
    assert qh._was_recently_notified("task-b", "drop") is False
    assert qh._was_recently_notified("other", "unblock_or_retry") is False


# ---------------------------------------------------------------------------
# The orchestrator's terminal-failure shape: `- [x] … ❌ ts (provider)`
#
# Healing exists for exactly this situation — a dependency that can never resolve
# on its own. It read `[-]` only, so the new shape would have been invisible to it
# and, worse, `_find_completed_ids` would have counted the failure as a completion.
# ---------------------------------------------------------------------------

_FAILED_DONE_LINE = "- [x] A #id:task-a \u274c 2026-09-04 01:21 (claude+dev-loop)\n"


def test_detect_sees_orchestrator_failure_as_a_failed_dep():
    items = [
        _FakeQueueItem("Run B #id:task-b #needs:task-a", blocked_reason="needs task-a"),
    ]
    content = "## Queue\n" + _FAILED_DONE_LINE + "- [ ] Run B #id:task-b #needs:task-a\n"
    cands = detect_candidates(items, content)
    assert len(cands) == 1
    assert cands[0].failed_deps == ("task-a",)


def test_find_completed_ids_excludes_the_failure_stamp():
    assert qh._find_completed_ids(_FAILED_DONE_LINE) == set()
    assert qh._find_completed_ids(
        "- [x] A #id:task-a \u2705 2026-09-04 01:21 (claude)\n"
    ) == {"task-a"}


def test_apply_unblock_promotes_an_orchestrator_failure(monkeypatch):
    written: dict[str, str] = {}
    content = "## Queue\n" + _FAILED_DONE_LINE + "- [ ] B #id:task-b #needs:task-a\n"

    def fake_apply(transform):
        new = transform(content)
        if new is None:
            return False
        written["out"] = new
        return True

    monkeypatch.setattr("queue_manager._apply_update", fake_apply)
    ok, _ = apply_unblock("task-b")
    assert ok
    # Same checkbox, success stamp — that is what "treat the dep as done" means.
    assert "- [x] A #id:task-a \u2705 2026-09-04 01:21 (claude+dev-loop)" in written["out"]
    assert "\u274c" not in written["out"]


def test_apply_retry_dep_reopens_an_orchestrator_failure(monkeypatch):
    written: dict[str, str] = {}
    content = "## Queue\n" + _FAILED_DONE_LINE + "- [ ] B #id:task-b #needs:task-a\n"

    def fake_apply(transform):
        new = transform(content)
        if new is None:
            return False
        written["out"] = new
        return True

    monkeypatch.setattr("queue_manager._apply_update", fake_apply)
    ok, _ = apply_retry_dep(["task-a"])
    assert ok
    assert "- [ ] A #id:task-a" in written["out"]
    assert "\u274c" not in written["out"]


def test_apply_retry_dep_never_reopens_a_successful_task(monkeypatch):
    """/retry names an id, not a status — a succeeded task must stay done."""
    content = (
        "## Queue\n"
        "- [x] A #id:task-a \u2705 2026-09-04 01:21 (claude)\n"
        "- [ ] B #id:task-b #needs:task-a\n"
    )
    monkeypatch.setattr(
        "queue_manager._apply_update",
        lambda transform: transform(content) is not None,
    )
    ok, _ = apply_retry_dep(["task-a"])
    assert not ok


# ---------------------------------------------------------------------------
# /retry must not eat the task text (Codex P2)
# ---------------------------------------------------------------------------

def test_apply_retry_dep_keeps_a_description_containing_the_emoji(monkeypatch):
    r"""The old `re.sub` cut at the FIRST ❌ and destroyed the task.

    `- [x] Replace ❌ with ✅ #id:a ❌ 2026-… (claude)` collapsed to `- [ ] Replace`
    — instruction and #id: gone, so the reopened task was unrunnable and could
    never satisfy anything again.
    """
    written: dict[str, str] = {}
    content = (
        "## Queue\n"
        "- [x] Replace \u274c with \u2705 in the report #id:task-a "
        "\u274c 2026-09-04 01:21 (claude+dev-loop)\n"
        "- [ ] B #id:task-b #needs:task-a\n"
    )

    def fake_apply(transform):
        new = transform(content)
        if new is None:
            return False
        written["out"] = new
        return True

    monkeypatch.setattr("queue_manager._apply_update", fake_apply)
    ok, _ = apply_retry_dep(["task-a"])
    assert ok
    line = next(ln for ln in written["out"].splitlines() if "task-a" in ln)
    assert line == "- [ ] Replace \u274c with \u2705 in the report #id:task-a", line


def test_strip_failure_stamp_only_removes_the_trailing_stamp():
    from queue_manager import strip_failure_stamp

    assert strip_failure_stamp(
        "- [x] A \u274c B #id:x \u274c 2026-09-04 01:21 (claude)"
    ) == "- [x] A \u274c B #id:x"
    # No stamp → untouched.
    assert strip_failure_stamp("- [x] A \u274c B #id:x") == "- [x] A \u274c B #id:x"


def test_unblock_actually_satisfies_the_dependency(monkeypatch):
    """A promoted line must READ as satisfied, not merely look promoted.

    Swapping only the checkbox turned `- [-] … ❌ …` (written by /drop) into
    `- [x] … ❌ …`, which `_collect_completed_ids()` classifies as a FAILURE — so
    `/unblock` reported success and changed nothing for the dependent.
    """
    import queue_manager

    written: dict[str, str] = {}
    content = (
        "## Queue\n"
        "- [-] A #id:task-a \u274c 2026-09-04 01:21 (drop via queue-healing)\n"
        "- [ ] B #id:task-b #needs:task-a\n"
    )

    def fake_apply(transform):
        new = transform(content)
        if new is None:
            return False
        written["out"] = new
        return True

    monkeypatch.setattr("queue_manager._apply_update", fake_apply)
    ok, _ = apply_unblock("task-b")
    assert ok
    assert "task-a" in queue_manager._collect_completed_ids(written["out"]), written["out"]
