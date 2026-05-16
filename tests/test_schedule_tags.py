"""Tests for #at: (one-time future start) and #every: (recurring schedule) tags.

Both reuse the existing retry primitive — #at: extends read_queue_items()
filtering, #every: extends mark_done/finalize_task_with_result rewriting.
"""

import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


with patch("config._load_dotenv"):
    import queue_manager


@pytest.fixture
def mock_queue_file(tmp_path):
    q_file = tmp_path / "agent-queue.md"
    lock_file = q_file.with_name(f"{q_file.name}.lock")
    with patch("queue_manager.QUEUE_FILE", q_file):
        yield q_file


def _write_queue(q_file: Path, body: str) -> None:
    q_file.write_text("# Queue\n\n## Queue\n" + body + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# extract_at_tag
# ---------------------------------------------------------------------------

def test_extract_at_returns_iso_timestamp():
    task = "Review #at:2026-05-17T22:00 #tool:review-loop"
    assert queue_manager.extract_at_tag(task) == "2026-05-17T22:00"


def test_extract_at_returns_space_separated_timestamp():
    task = "Review #at:2026-05-17 22:00 #tool:review-loop"
    assert queue_manager.extract_at_tag(task) == "2026-05-17 22:00"


def test_extract_at_returns_hh_mm_short_form():
    task = "Review #at:22:00 #tool:review-loop"
    assert queue_manager.extract_at_tag(task) == "22:00"


def test_extract_at_returns_none_when_missing():
    assert queue_manager.extract_at_tag("Plain task") is None


def test_extract_at_ignores_word_boundary():
    # `not#at:foo` shouldn't match — must be tag-shaped
    assert queue_manager.extract_at_tag("text not#at:22:00") is None


# ---------------------------------------------------------------------------
# extract_every_tag
# ---------------------------------------------------------------------------

def test_extract_every_returns_seconds_for_minutes():
    assert queue_manager.extract_every_tag("Nightly #every:30m") == 30 * 60


def test_extract_every_returns_seconds_for_hours():
    assert queue_manager.extract_every_tag("Daily #every:24h") == 24 * 3600


def test_extract_every_returns_seconds_for_days():
    assert queue_manager.extract_every_tag("Weekly #every:7d") == 7 * 86400


def test_extract_every_returns_seconds_for_seconds():
    assert queue_manager.extract_every_tag("Quick #every:45s") == 45


def test_extract_every_returns_none_when_missing():
    assert queue_manager.extract_every_tag("Plain task") is None


def test_extract_every_returns_none_for_invalid_unit():
    # 'w' is not a supported unit
    assert queue_manager.extract_every_tag("Task #every:2w") is None


# ---------------------------------------------------------------------------
# strip_metadata_tags
# ---------------------------------------------------------------------------

def test_strip_metadata_removes_at_tag():
    task = "Review repo #at:2026-05-17T22:00 #tool:review-loop"
    stripped = queue_manager.strip_metadata_tags(task)
    assert "#at:" not in stripped
    assert "Review repo" in stripped


def test_strip_metadata_removes_every_tag():
    task = "Daily review #every:24h #tool:review-loop"
    stripped = queue_manager.strip_metadata_tags(task)
    assert "#every:" not in stripped
    assert "Daily review" in stripped


# ---------------------------------------------------------------------------
# read_queue_items honors #at: as scheduling filter
# ---------------------------------------------------------------------------

def test_read_queue_skips_task_with_future_at_tag(mock_queue_file):
    future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    _write_queue(mock_queue_file, f"- [ ] Future task #at:{future}")
    items = queue_manager.read_queue_items()
    assert items == []


def test_read_queue_includes_task_with_past_at_tag(mock_queue_file):
    past = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    _write_queue(mock_queue_file, f"- [ ] Overdue task #at:{past}")
    items = queue_manager.read_queue_items()
    assert len(items) == 1
    assert "Overdue task" in items[0].task_text


def test_read_queue_retry_annotation_wins_over_at(mock_queue_file):
    """Once a transient retry is set, #at: is irrelevant. Retry tag is the
    active timing signal."""
    past_at = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    future_retry = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    _write_queue(
        mock_queue_file,
        f"- [ ] Task #at:{past_at} <!-- retry: {future_retry} -->",
    )
    items = queue_manager.read_queue_items()
    assert items == []  # retry-annotation is in the future → still filtered


# ---------------------------------------------------------------------------
# #every: rewrites completion into a fresh retry instead of [x]
# ---------------------------------------------------------------------------

def test_mark_done_reschedules_every_task(mock_queue_file):
    _write_queue(
        mock_queue_file,
        "- [ ] Nightly review #every:24h #tool:review-loop",
    )

    items = queue_manager.read_queue_items()
    assert len(items) == 1
    task = items[0]

    ok = queue_manager.mark_done(task.task_text, "claude", line_no=task.line_no)
    assert ok

    content = mock_queue_file.read_text(encoding="utf-8")
    # Task stays open (NOT marked [x])
    assert "- [x]" not in content
    assert "- [ ] Nightly review" in content
    # And carries a future retry annotation roughly 24h ahead
    m = re.search(r"<!-- retry: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) -->", content)
    assert m is not None
    scheduled = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
    expected = datetime.now() + timedelta(hours=24)
    delta = abs((scheduled - expected).total_seconds())
    assert delta < 120, f"Expected retry ~24h out, got delta {delta}s"


def test_finalize_reschedules_every_task(mock_queue_file):
    _write_queue(
        mock_queue_file,
        "- [ ] Weekly audit #every:7d cwd:/tmp #tool:review-loop",
    )
    items = queue_manager.read_queue_items()
    assert len(items) == 1
    task = items[0]

    ok = queue_manager.finalize_task_with_result(
        task.task_text, "result text", "claude", line_no=task.line_no
    )
    assert ok

    content = mock_queue_file.read_text(encoding="utf-8")
    assert "- [x]" not in content
    assert "- [ ] Weekly audit" in content


def test_mark_done_strips_at_tag_on_every_reschedule(mock_queue_file):
    """First fire of `#at:X #every:Y` consumes the `#at:`. After completion,
    only `#every:` + new retry annotation remain — `#at:` is stale."""
    past_at = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    _write_queue(
        mock_queue_file,
        f"- [ ] Cron task #at:{past_at} #every:24h #tool:review-loop",
    )
    items = queue_manager.read_queue_items()
    assert len(items) == 1
    task = items[0]
    ok = queue_manager.mark_done(task.task_text, "claude", line_no=task.line_no)
    assert ok

    content = mock_queue_file.read_text(encoding="utf-8")
    assert "#at:" not in content  # stale tag stripped
    assert "#every:24h" in content  # schedule tag preserved


def test_mark_done_without_every_marks_x_as_before(mock_queue_file):
    """Regression guard: ordinary completion path unaffected."""
    _write_queue(mock_queue_file, "- [ ] One-shot task")
    items = queue_manager.read_queue_items()
    ok = queue_manager.mark_done(items[0].task_text, "claude", line_no=items[0].line_no)
    assert ok
    content = mock_queue_file.read_text(encoding="utf-8")
    assert "- [x] One-shot task" in content
    assert "- [ ]" not in content


# ---------------------------------------------------------------------------
# Queue linter validates the tag syntax
# ---------------------------------------------------------------------------

with patch("config._load_dotenv"):
    import queue_linter


def test_linter_accepts_well_formed_at_tag():
    findings = queue_linter.lint_queue(
        "## Queue\n- [ ] Task #at:2026-05-17T22:00\n"
    )
    assert "invalid_at" not in {f.code for f in findings}


def test_linter_accepts_hh_mm_at_tag():
    findings = queue_linter.lint_queue("## Queue\n- [ ] Task #at:22:00\n")
    assert "invalid_at" not in {f.code for f in findings}


def test_linter_rejects_malformed_at_tag():
    findings = queue_linter.lint_queue("## Queue\n- [ ] Task #at:tomorrow\n")
    codes = {f.code for f in findings}
    assert "invalid_at" in codes


def test_linter_accepts_well_formed_every_tag():
    findings = queue_linter.lint_queue("## Queue\n- [ ] Task #every:24h\n")
    assert "invalid_every" not in {f.code for f in findings}


def test_linter_rejects_invalid_every_unit():
    findings = queue_linter.lint_queue("## Queue\n- [ ] Task #every:2w\n")
    assert "invalid_every" in {f.code for f in findings}


def test_linter_rejects_every_without_number():
    findings = queue_linter.lint_queue("## Queue\n- [ ] Task #every:daily\n")
    assert "invalid_every" in {f.code for f in findings}
