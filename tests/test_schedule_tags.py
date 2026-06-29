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


def test_mark_done_preserves_at_anchor_on_every_reschedule(mock_queue_file):
    """Anchored recurring (#at:HH:MM #every:Nd): the #at: anchor is PRESERVED
    (normalized to bare HH:MM) and the next retry lands at the anchor time-of-day —
    no drift to the completion time. (Replaces the old strip-the-#at: contract.)"""
    anchor = datetime.now() - timedelta(hours=1)  # today's slot is 1h in the past
    at_value = anchor.strftime("%H:%M")
    _write_queue(
        mock_queue_file,
        f"- [ ] Cron task #at:{at_value} #every:24h #tool:review-loop",
    )
    items = queue_manager.read_queue_items()
    assert len(items) == 1
    task = items[0]
    ok = queue_manager.mark_done(task.task_text, "claude", line_no=task.line_no)
    assert ok

    content = mock_queue_file.read_text(encoding="utf-8")
    assert f"#at:{at_value}" in content   # anchor preserved (normalized HH:MM)
    assert "#every:24h" in content        # schedule tag preserved
    assert "- [x]" not in content         # stays open (recurring)

    m = re.search(r"<!-- retry: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) -->", content)
    assert m is not None
    scheduled = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
    assert scheduled > datetime.now()  # strictly in the future (no immediate re-fire)
    assert (scheduled.hour, scheduled.minute) == (anchor.hour, anchor.minute)


def test_mark_done_full_iso_at_anchor_normalized_to_hhmm(mock_queue_file):
    """A full-ISO #at: on a recurring task is normalized to bare HH:MM (stale date
    dropped) while keeping the time-of-day as the anchor."""
    anchor = datetime.now() - timedelta(hours=2)
    full_iso = anchor.strftime("%Y-%m-%dT%H:%M")
    hhmm = anchor.strftime("%H:%M")
    _write_queue(mock_queue_file, f"- [ ] Cron #at:{full_iso} #every:24h")
    items = queue_manager.read_queue_items()
    task = items[0]
    assert queue_manager.mark_done(task.task_text, "claude", line_no=task.line_no)

    content = mock_queue_file.read_text(encoding="utf-8")
    assert full_iso not in content       # stale dated form gone
    assert f"#at:{hhmm}" in content      # normalized to HH:MM


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


# ---------------------------------------------------------------------------
# #freshonly / #grace extraction
# ---------------------------------------------------------------------------

def test_has_freshonly_tag_true():
    assert queue_manager.has_freshonly_tag("Daily brief #every:24h #freshonly") is True


def test_has_freshonly_tag_false_and_word_boundary():
    assert queue_manager.has_freshonly_tag("Plain #every:24h") is False
    # must be tag-shaped, not a substring of a longer token
    assert queue_manager.has_freshonly_tag("text #freshonlyish") is False
    assert queue_manager.has_freshonly_tag("text not#freshonly") is False


def test_extract_grace_returns_seconds():
    assert queue_manager.extract_grace_tag("Task #grace:4h") == 4 * 3600
    assert queue_manager.extract_grace_tag("Task #grace:30m") == 30 * 60
    assert queue_manager.extract_grace_tag("Task #grace:2d") == 2 * 86400


def test_extract_grace_returns_none_when_missing_or_invalid():
    assert queue_manager.extract_grace_tag("Task #every:24h") is None
    assert queue_manager.extract_grace_tag("Task #grace:2w") is None


def test_strip_metadata_removes_freshonly_and_grace():
    task = "Run brief #every:24h #at:08:00 #freshonly #grace:4h #claude_sonnet"
    stripped = queue_manager.strip_metadata_tags(task)
    assert "#freshonly" not in stripped
    assert "#grace:" not in stripped
    assert "#at:" not in stripped
    assert "#every:" not in stripped
    assert "Run brief" in stripped


# ---------------------------------------------------------------------------
# _next_anchor_occurrence — pure, deterministic
# ---------------------------------------------------------------------------

def test_next_anchor_daily_today_if_ahead():
    now = datetime(2026, 6, 29, 6, 0)
    nxt = queue_manager._next_anchor_occurrence((8, 0), 24 * 3600, now)
    assert nxt == datetime(2026, 6, 29, 8, 0)  # 08:00 still ahead today


def test_next_anchor_daily_tomorrow_if_passed():
    now = datetime(2026, 6, 29, 11, 0)
    nxt = queue_manager._next_anchor_occurrence((8, 0), 24 * 3600, now)
    assert nxt == datetime(2026, 6, 30, 8, 0)  # 08:00 already gone → tomorrow


def test_next_anchor_weekly_steps_seven_days():
    now = datetime(2026, 6, 29, 11, 0)
    nxt = queue_manager._next_anchor_occurrence((8, 0), 7 * 86400, now)
    assert nxt == datetime(2026, 7, 6, 8, 0)  # +7d at anchor time


def test_next_anchor_multiday_measures_from_now_not_today():
    """Multi-day interval with now BEFORE today's anchor must NOT collapse to today's
    slot — cadence is measured from now (+step_days), only the time-of-day is anchored.
    (Regression for the same-day-collapse bug on #every:7d #at:19:00.)"""
    now = datetime(2026, 6, 29, 8, 30)  # before a 19:00 anchor
    nxt = queue_manager._next_anchor_occurrence((19, 0), 7 * 86400, now)
    assert nxt == datetime(2026, 7, 6, 19, 0)    # now + 7d at 19:00
    assert nxt != datetime(2026, 6, 29, 19, 0)   # not the same-day collapse


def test_anchor_time_of_day_parses_hhmm_and_iso():
    assert queue_manager._anchor_time_of_day("x #at:19:00 #every:24h") == (19, 0)
    assert queue_manager._anchor_time_of_day("x #at:2026-05-17T22:30 #every:24h") == (22, 30)
    assert queue_manager._anchor_time_of_day("x #every:24h") is None


# ---------------------------------------------------------------------------
# realign_stale_freshonly
# ---------------------------------------------------------------------------

def test_realign_skips_stale_freshonly_without_running(mock_queue_file):
    """A #freshonly task whose slot is long past (beyond grace) is realigned to its
    next anchor and NOT executed (stays open, no [x])."""
    now = datetime(2026, 6, 29, 8, 30)
    stale = "2026-06-28 19:00"  # yesterday's slot, ~13.5h ago
    _write_queue(
        mock_queue_file,
        f"- [ ] Tagesabschluss #at:19:00 #every:24h #freshonly <!-- retry: {stale} -->",
    )
    n = queue_manager.realign_stale_freshonly(now=now)
    assert n == 1
    content = mock_queue_file.read_text(encoding="utf-8")
    assert "- [x]" not in content
    # realigned to today 19:00 (next 19:00 strictly after 08:30)
    assert "<!-- retry: 2026-06-29 19:00 -->" in content
    assert "#freshonly" in content  # tag untouched


def test_realign_leaves_fresh_freshonly_within_grace(mock_queue_file):
    """Within the grace window the task is left for read_queue_items to run."""
    now = datetime(2026, 6, 29, 8, 30)
    slot = "2026-06-29 08:00"  # 30 min ago, default grace 2h
    line = f"- [ ] Morning brief #at:08:00 #every:24h #freshonly <!-- retry: {slot} -->"
    _write_queue(mock_queue_file, line)
    n = queue_manager.realign_stale_freshonly(now=now)
    assert n == 0
    content = mock_queue_file.read_text(encoding="utf-8")
    assert f"<!-- retry: {slot} -->" in content  # untouched


def test_realign_respects_custom_grace(mock_queue_file):
    """#grace:4h keeps a 3h-late evening task fresh (would be stale under default 2h)."""
    now = datetime(2026, 6, 29, 22, 0)
    slot = "2026-06-29 19:00"  # 3h ago
    line = f"- [ ] Tagesabschluss #at:19:00 #every:24h #freshonly #grace:4h <!-- retry: {slot} -->"
    _write_queue(mock_queue_file, line)
    assert queue_manager.realign_stale_freshonly(now=now) == 0  # within 4h grace


def test_realign_ignores_non_freshonly_stale_task(mock_queue_file):
    """Tasks without #freshonly keep catch-up semantics — never realigned."""
    now = datetime(2026, 6, 29, 8, 30)
    stale = "2026-06-20 08:00"
    line = f"- [ ] Weekly audit #every:7d <!-- retry: {stale} -->"
    _write_queue(mock_queue_file, line)
    assert queue_manager.realign_stale_freshonly(now=now) == 0
    content = mock_queue_file.read_text(encoding="utf-8")
    assert f"<!-- retry: {stale} -->" in content  # untouched → will catch up


def test_realign_then_read_filters_stale_task(mock_queue_file):
    """End-to-end: after realign, read_queue_items no longer returns the stale task.

    Uses real-clock-relative values (anchor ~2h ahead) so the subsequent
    read_queue_items() — which reads the real wall clock — sees a future marker
    regardless of when the test runs."""
    now = datetime.now()
    anchor = (now + timedelta(hours=2)).strftime("%H:%M")     # comfortably in the future
    stale = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")  # well beyond grace
    _write_queue(
        mock_queue_file,
        f"- [ ] Recap #at:{anchor} #every:24h #freshonly <!-- retry: {stale} -->",
    )
    assert queue_manager.realign_stale_freshonly(now=now) == 1
    # marker now points ~2h into the future → read filters it out
    items = queue_manager.read_queue_items()
    assert items == []


def test_realign_first_run_no_marker_uses_at_anchor(mock_queue_file):
    """No retry marker yet: staleness is judged against the #at: anchor; a stale
    first slot gets a future retry marker so it doesn't fire late."""
    now = datetime(2026, 6, 29, 11, 0)  # 3h past an 08:00 anchor, default grace 2h
    _write_queue(
        mock_queue_file,
        "- [ ] Morning brief #at:08:00 #every:24h #freshonly",
    )
    assert queue_manager.realign_stale_freshonly(now=now) == 1
    content = mock_queue_file.read_text(encoding="utf-8")
    assert "<!-- retry: 2026-06-30 08:00 -->" in content


# ---------------------------------------------------------------------------
# Linter: #freshonly / #grace
# ---------------------------------------------------------------------------

def test_linter_accepts_freshonly_and_grace():
    findings = queue_linter.lint_queue(
        "## Queue\n- [ ] Brief #every:24h #at:08:00 #freshonly #grace:4h\n"
    )
    codes = {f.code for f in findings}
    assert "invalid_grace" not in codes
    assert "grace_without_freshonly" not in codes
    assert "freshonly_without_every" not in codes


def test_linter_rejects_invalid_grace():
    findings = queue_linter.lint_queue("## Queue\n- [ ] Brief #every:24h #freshonly #grace:2w\n")
    assert "invalid_grace" in {f.code for f in findings}


def test_linter_warns_grace_without_freshonly():
    findings = queue_linter.lint_queue("## Queue\n- [ ] Brief #every:24h #grace:4h\n")
    assert "grace_without_freshonly" in {f.code for f in findings}


def test_linter_warns_freshonly_without_every():
    findings = queue_linter.lint_queue("## Queue\n- [ ] One-shot #freshonly\n")
    assert "freshonly_without_every" in {f.code for f in findings}


def test_linter_warns_freshonly_with_value():
    findings = queue_linter.lint_queue("## Queue\n- [ ] Brief #every:24h #freshonly:false\n")
    assert "freshonly_with_value" in {f.code for f in findings}


def test_linter_warns_at_anchor_with_subday_every():
    findings = queue_linter.lint_queue("## Queue\n- [ ] Tick #at:08:00 #every:30m\n")
    assert "anchor_subday_interval" in {f.code for f in findings}


def test_linter_accepts_at_anchor_with_whole_day_every():
    findings = queue_linter.lint_queue("## Queue\n- [ ] Brief #at:08:00 #every:24h\n")
    assert "anchor_subday_interval" not in {f.code for f in findings}


# ---------------------------------------------------------------------------
# Realign — non-anchored / no-timing branches (P2-2) + boundary/contract (P3)
# ---------------------------------------------------------------------------

def test_realign_non_anchored_freshonly_uses_now_plus_every(mock_queue_file):
    """A stale #freshonly task WITHOUT an #at: anchor realigns to now+every (the
    non-anchor branch), staying open and unexecuted."""
    now = datetime(2026, 6, 29, 8, 30)
    stale = "2026-06-25 08:00"  # days ago, well beyond default 2h grace
    _write_queue(
        mock_queue_file,
        f"- [ ] Catchup-but-fresh #every:24h #freshonly <!-- retry: {stale} -->",
    )
    assert queue_manager.realign_stale_freshonly(now=now) == 1
    content = mock_queue_file.read_text(encoding="utf-8")
    assert "- [x]" not in content
    assert "<!-- retry: 2026-06-30 08:30 -->" in content  # now + 24h (no anchor)


def test_realign_freshonly_without_anchor_or_marker_untouched(mock_queue_file):
    """No #at: anchor and no retry marker → no timing info → left untouched (n==0)
    so read_queue_items runs it normally."""
    now = datetime(2026, 6, 29, 11, 0)
    line = "- [ ] First run #every:24h #freshonly"
    _write_queue(mock_queue_file, line)
    assert queue_manager.realign_stale_freshonly(now=now) == 0
    content = mock_queue_file.read_text(encoding="utf-8")
    assert line in content  # unchanged


def test_next_anchor_boundary_now_equals_anchor():
    """Contract: when now is exactly the anchor slot, the next occurrence is strictly
    in the future (+step_days), never now itself (no immediate re-fire)."""
    now = datetime(2026, 6, 29, 8, 0)
    nxt = queue_manager._next_anchor_occurrence((8, 0), 24 * 3600, now)
    assert nxt == datetime(2026, 6, 30, 8, 0)


def test_resolve_scheduled_dt_garbage_is_none():
    assert queue_manager._resolve_scheduled_dt("garbage") is None
    assert queue_manager._resolve_scheduled_dt("25:99") is None


def test_retry_is_due_garbage_fails_open():
    """Unparseable retry marker must fail open (True) so tasks never get stuck."""
    assert queue_manager._retry_is_due("garbage") is True
