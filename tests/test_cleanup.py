"""Tests for all cleanup / retention logic.

Covers:
- memory.py: _cleanup_archive, _cleanup_daily_logs, _cleanup_lessons, archive_old_memories
- queue_manager.py: append_log → queue-events.log, _cleanup_queue_events_log,
                    cleanup_done_tasks, _move_old_done_tasks, _prune_erledigt_file
"""

import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

with patch("config._load_dotenv"):
    import config


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def mem(tmp_path):
    """Fresh memory module wired to tmp_path vault."""
    with patch("config._load_dotenv"):
        import memory as m
        m._MEMORY_ROOT = tmp_path / "mem"
        m._TASK_RESULTS_DIR = m._MEMORY_ROOT / "task_results"
        m._ARCHIVE_DIR = m._MEMORY_ROOT / "archive"
        m._DAILY_DIR = m._MEMORY_ROOT / "daily"
        m._LESSONS_FILE = m._MEMORY_ROOT / "lessons.md"
        m._CURATED_MEMORY_FILE = m._MEMORY_ROOT / "MEMORY.md"
        m._archive_last_run_date = None  # reset rate limit
        m._ensure_dirs()
        yield m


def _write_task_result(directory: Path, name: str, ts: datetime) -> Path:
    """Write a minimal memory task result file."""
    content = (
        "---\n"
        'task: "test task"\n'
        "provider: claude\n"
        "cwd: /d/test\n"
        "duration_sec: 10.0\n"
        f"timestamp: {ts.isoformat(timespec='seconds')}\n"
        "success: true\n"
        "---\n\nSome result.\n"
    )
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


# ── memory._cleanup_archive ──────────────────────────────────────────────────

class TestCleanupArchive:
    def test_deletes_old_files(self, mem):
        old_ts = datetime.now() - timedelta(days=config.MEMORY_ARCHIVE_DELETE_DAYS + 1)
        recent_ts = datetime.now() - timedelta(days=1)
        _write_task_result(mem._ARCHIVE_DIR, "old.md", old_ts)
        _write_task_result(mem._ARCHIVE_DIR, "recent.md", recent_ts)

        deleted = mem._cleanup_archive()

        assert deleted == 1
        assert not (mem._ARCHIVE_DIR / "old.md").exists()
        assert (mem._ARCHIVE_DIR / "recent.md").exists()

    def test_keeps_all_when_none_old(self, mem):
        _write_task_result(mem._ARCHIVE_DIR, "a.md", datetime.now() - timedelta(days=1))
        assert mem._cleanup_archive() == 0
        assert (mem._ARCHIVE_DIR / "a.md").exists()

    def test_empty_archive_returns_zero(self, mem):
        assert mem._cleanup_archive() == 0

    def test_missing_archive_dir_returns_zero(self, mem):
        mem._ARCHIVE_DIR.rmdir()
        assert mem._cleanup_archive() == 0


# ── memory._cleanup_daily_logs ───────────────────────────────────────────────

class TestCleanupDailyLogs:
    def _write_daily(self, daily_dir: Path, d: date) -> Path:
        path = daily_dir / f"Memory {d.isoformat()}.md"
        path.write_text(f"# Memory {d.isoformat()}\n", encoding="utf-8")
        return path

    def test_deletes_old_daily_logs(self, mem):
        old_date = date.today() - timedelta(days=config.MEMORY_DAILY_LOG_RETENTION_DAYS + 1)
        today = date.today()
        self._write_daily(mem._DAILY_DIR, old_date)
        self._write_daily(mem._DAILY_DIR, today)

        deleted = mem._cleanup_daily_logs()

        assert deleted == 1
        assert not (mem._DAILY_DIR / f"Memory {old_date.isoformat()}.md").exists()
        assert (mem._DAILY_DIR / f"Memory {today.isoformat()}.md").exists()

    def test_keeps_recent_logs(self, mem):
        recent = date.today() - timedelta(days=1)
        self._write_daily(mem._DAILY_DIR, recent)
        assert mem._cleanup_daily_logs() == 0

    def test_ignores_non_matching_files(self, mem):
        (mem._DAILY_DIR / "random.md").write_text("x", encoding="utf-8")
        assert mem._cleanup_daily_logs() == 0
        assert (mem._DAILY_DIR / "random.md").exists()


# ── memory._cleanup_lessons ──────────────────────────────────────────────────

class TestCleanupLessons:
    def _write_lessons(self, path: Path, entries: list[tuple[str, str]]) -> None:
        """entries: list of (date_str, body)"""
        lines = ["# Lessons Learned\n"]
        for date_str, body in entries:
            lines.append(f"\n## {date_str} | tool | project\n{body}\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(lines), encoding="utf-8")

    def test_removes_old_entries(self, mem):
        old_date = (datetime.now() - timedelta(days=config.MEMORY_LESSONS_RETENTION_DAYS + 1)).strftime("%Y-%m-%d")
        recent_date = datetime.now().strftime("%Y-%m-%d")
        self._write_lessons(mem._LESSONS_FILE, [
            (old_date, "- **Pattern:** old\n- **Fix:** x\n"),
            (recent_date, "- **Pattern:** recent\n- **Fix:** y\n"),
        ])

        removed = mem._cleanup_lessons()

        assert removed == 1
        content = mem._LESSONS_FILE.read_text(encoding="utf-8")
        assert old_date not in content
        assert recent_date in content

    def test_keeps_all_recent(self, mem):
        recent = datetime.now().strftime("%Y-%m-%d")
        self._write_lessons(mem._LESSONS_FILE, [(recent, "- **Pattern:** x\n")])
        assert mem._cleanup_lessons() == 0

    def test_no_file_returns_zero(self, mem):
        assert mem._cleanup_lessons() == 0

    def test_header_preserved(self, mem):
        recent = datetime.now().strftime("%Y-%m-%d")
        self._write_lessons(mem._LESSONS_FILE, [(recent, "- **Pattern:** x\n")])
        mem._cleanup_lessons()
        content = mem._LESSONS_FILE.read_text(encoding="utf-8")
        assert "# Lessons Learned" in content


# ── memory.archive_old_memories — 2-stage ────────────────────────────────────

class TestArchiveOldMemories:
    def test_archives_to_archive_dir(self, mem):
        old_ts = datetime.now() - timedelta(days=config.MEMORY_MAX_AGE_DAYS + 1)
        _write_task_result(mem._TASK_RESULTS_DIR, "old.md", old_ts)

        count = mem.archive_old_memories()

        assert count == 1
        assert not (mem._TASK_RESULTS_DIR / "old.md").exists()
        assert (mem._ARCHIVE_DIR / "old.md").exists()

    def test_delete_from_archive_after_archive_delete_days(self, mem):
        very_old_ts = datetime.now() - timedelta(days=config.MEMORY_ARCHIVE_DELETE_DAYS + 1)
        _write_task_result(mem._ARCHIVE_DIR, "very_old.md", very_old_ts)

        mem.archive_old_memories()

        assert not (mem._ARCHIVE_DIR / "very_old.md").exists()

    def test_rate_limit_once_per_day(self, mem):
        mem._archive_last_run_date = date.today()
        old_ts = datetime.now() - timedelta(days=config.MEMORY_MAX_AGE_DAYS + 1)
        _write_task_result(mem._TASK_RESULTS_DIR, "old.md", old_ts)

        count = mem.archive_old_memories()

        assert count == 0
        assert (mem._TASK_RESULTS_DIR / "old.md").exists()  # not archived (rate limited)

    def test_keeps_recent_files(self, mem):
        _write_task_result(mem._TASK_RESULTS_DIR, "recent.md", datetime.now())
        count = mem.archive_old_memories()
        assert count == 0
        assert (mem._TASK_RESULTS_DIR / "recent.md").exists()


# ── queue_manager: append_log → queue-events.log ─────────────────────────────

@pytest.fixture()
def qm(tmp_path, monkeypatch):
    """Fresh queue_manager with paths wired to tmp_path."""
    with patch("config._load_dotenv"):
        import queue_manager as qm_mod
        monkeypatch.setattr("queue_manager.QUEUE_EVENTS_LOG_FILE", tmp_path / "queue-events.log")
        qm_mod._events_log_cleanup_last_date = None
        yield qm_mod, tmp_path


class TestAppendLog:
    def test_writes_to_log_file(self, qm):
        mod, tmp = qm
        mod.append_log("Task erledigt via claude")
        log = tmp / "queue-events.log"
        assert log.exists()
        content = log.read_text(encoding="utf-8")
        assert "Task erledigt via claude" in content
        assert "|" in content

    def test_timestamp_format(self, qm):
        mod, tmp = qm
        mod.append_log("hello")
        content = (tmp / "queue-events.log").read_text(encoding="utf-8")
        # Line format: "YYYY-MM-DD HH:MM | message"
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \| hello", content)

    def test_does_not_write_to_queue_md(self, qm, tmp_path):
        mod, tmp = qm
        queue_md = tmp / "agent-queue.md"
        queue_md.write_text("# Queue\n## Queue\n", encoding="utf-8")
        with patch("queue_manager.QUEUE_FILE", queue_md):
            mod.append_log("some event")
        content = queue_md.read_text(encoding="utf-8")
        assert "some event" not in content

    def test_threadsafe_concurrent_writes(self, qm):
        mod, tmp = qm
        errors = []

        def _write(i):
            try:
                mod.append_log(f"event {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        lines = (tmp / "queue-events.log").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 10


class TestCleanupQueueEventsLog:
    def test_prunes_old_entries(self, qm):
        mod, tmp = qm
        log = tmp / "queue-events.log"
        old_ts = (datetime.now() - timedelta(days=config.QUEUE_EVENTS_LOG_RETENTION_DAYS + 1)).strftime("%Y-%m-%d %H:%M")
        recent_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        log.write_text(
            f"{old_ts} | old event\n"
            f"{recent_ts} | recent event\n",
            encoding="utf-8",
        )
        mod._events_log_cleanup_last_date = None  # force run
        with patch("queue_manager.QUEUE_EVENTS_LOG_FILE", log):
            mod._cleanup_queue_events_log()

        content = log.read_text(encoding="utf-8")
        assert "old event" not in content
        assert "recent event" in content

    def test_rate_limited_once_per_day(self, qm):
        mod, tmp = qm
        log = tmp / "queue-events.log"
        old_ts = (datetime.now() - timedelta(days=config.QUEUE_EVENTS_LOG_RETENTION_DAYS + 1)).strftime("%Y-%m-%d %H:%M")
        log.write_text(f"{old_ts} | old event\n", encoding="utf-8")
        mod._events_log_cleanup_last_date = date.today()  # already ran today

        with patch("queue_manager.QUEUE_EVENTS_LOG_FILE", log):
            mod._cleanup_queue_events_log()

        # Should NOT prune (rate limited)
        assert "old event" in log.read_text(encoding="utf-8")


# ── queue_manager: cleanup_done_tasks ────────────────────────────────────────

@pytest.fixture()
def queue_env(tmp_path, monkeypatch):
    """Wire queue_manager to a fresh tmp queue file and erledigt file."""
    with patch("config._load_dotenv"):
        import queue_manager as qm_mod
        queue_file = tmp_path / "agent-queue.md"
        erledigt_file = tmp_path / "agent-queue-erledigt.md"
        monkeypatch.setattr("queue_manager.QUEUE_FILE", queue_file)
        monkeypatch.setattr("queue_manager._ERLEDIGT_FILE", erledigt_file)
        monkeypatch.setattr("queue_manager.QUEUE_EVENTS_LOG_FILE", tmp_path / "queue-events.log")
        qm_mod._done_cleanup_last_run_date = None
        yield qm_mod, queue_file, erledigt_file


def _ts(hours_ago: float) -> str:
    """Return a timestamp string N hours ago in the done-task format."""
    dt = datetime.now() - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%d %H:%M")


class TestCleanupDoneTasks:
    def test_moves_old_done_task(self, queue_env):
        mod, qf, ef = queue_env
        ts = _ts(config.QUEUE_DONE_MOVE_HOURS + 1)
        qf.write_text(
            f"# Agent Queue\n\n## Queue\n"
            f"- [x] Fix bug ✅ {ts} (claude)\n"
            f"- [ ] Open task\n",
            encoding="utf-8",
        )

        moved = mod.cleanup_done_tasks()

        assert moved == 1
        queue = qf.read_text(encoding="utf-8")
        assert "Fix bug" not in queue
        assert "Open task" in queue
        erledigt = ef.read_text(encoding="utf-8")
        assert "Fix bug" in erledigt

    def test_keeps_recent_done_task(self, queue_env):
        mod, qf, ef = queue_env
        ts = _ts(1)  # 1 hour ago — under 48h threshold
        qf.write_text(
            f"# Agent Queue\n\n## Queue\n"
            f"- [x] Recent task ✅ {ts} (claude)\n",
            encoding="utf-8",
        )

        moved = mod.cleanup_done_tasks()

        assert moved == 0
        assert "Recent task" in qf.read_text(encoding="utf-8")
        assert not ef.exists()

    def test_groups_by_date_in_erledigt(self, queue_env):
        mod, qf, ef = queue_env
        old_ts = _ts(config.QUEUE_DONE_MOVE_HOURS + 2)
        date_str = (datetime.now() - timedelta(hours=config.QUEUE_DONE_MOVE_HOURS + 2)).strftime("%Y-%m-%d")
        qf.write_text(
            f"# Agent Queue\n\n## Queue\n"
            f"- [x] Task A ✅ {old_ts} (claude)\n"
            f"- [x] Task B ✅ {old_ts} (gemini)\n",
            encoding="utf-8",
        )

        mod.cleanup_done_tasks()

        erledigt = ef.read_text(encoding="utf-8")
        assert f"## {date_str}" in erledigt
        assert "Task A" in erledigt
        assert "Task B" in erledigt

    def test_rate_limited_once_per_day(self, queue_env):
        mod, qf, ef = queue_env
        mod._done_cleanup_last_run_date = date.today()
        ts = _ts(config.QUEUE_DONE_MOVE_HOURS + 1)
        qf.write_text(
            f"# Agent Queue\n\n## Queue\n"
            f"- [x] Task ✅ {ts} (claude)\n",
            encoding="utf-8",
        )

        moved = mod.cleanup_done_tasks()
        assert moved == 0
        assert "Task" in qf.read_text(encoding="utf-8")

    def test_failed_task_also_moved(self, queue_env):
        mod, qf, ef = queue_env
        ts = _ts(config.QUEUE_DONE_MOVE_HOURS + 1)
        qf.write_text(
            f"# Agent Queue\n\n## Queue\n"
            f"- [-] Failed task ✅ {ts} (failed)\n",
            encoding="utf-8",
        )

        moved = mod.cleanup_done_tasks()
        assert moved == 1
        assert "Failed task" not in qf.read_text(encoding="utf-8")
        assert "Failed task" in ef.read_text(encoding="utf-8")

    def test_subtask_lines_moved_with_parent(self, queue_env):
        mod, qf, ef = queue_env
        ts = _ts(config.QUEUE_DONE_MOVE_HOURS + 1)
        qf.write_text(
            f"# Agent Queue\n\n## Queue\n"
            f"- [x] Parallel task ✅ {ts} (parallel)\n"
            f"  - subtask A done\n"
            f"  - subtask B done\n"
            f"- [ ] Open task\n",
            encoding="utf-8",
        )

        moved = mod.cleanup_done_tasks()
        assert moved == 1
        queue = qf.read_text(encoding="utf-8")
        assert "subtask A" not in queue
        erledigt = ef.read_text(encoding="utf-8")
        assert "subtask A" in erledigt
        assert "subtask B" in erledigt

    def test_empty_queue_no_error(self, queue_env):
        mod, qf, ef = queue_env
        qf.write_text("# Agent Queue\n\n## Queue\n", encoding="utf-8")
        assert mod.cleanup_done_tasks() == 0

    def test_no_queue_file_no_error(self, queue_env):
        mod, qf, ef = queue_env
        assert mod.cleanup_done_tasks() == 0


# ── _prune_erledigt_file ─────────────────────────────────────────────────────

class TestPruneErledigtFile:
    def test_prunes_old_sections(self, queue_env):
        mod, qf, ef = queue_env
        old_date = (date.today() - timedelta(days=config.QUEUE_DONE_DELETE_DAYS + 1)).isoformat()
        today = date.today().isoformat()
        ef.write_text(
            f"# Agent Queue — Erledigt\n\n"
            f"## {today}\n\n- [x] Recent task ✅ {today} 10:00 (claude)\n\n"
            f"## {old_date}\n\n- [x] Old task ✅ {old_date} 10:00 (claude)\n",
            encoding="utf-8",
        )

        pruned = mod._prune_erledigt_file()

        assert pruned == 1
        content = ef.read_text(encoding="utf-8")
        assert old_date not in content
        assert today in content

    def test_keeps_all_when_none_old(self, queue_env):
        mod, qf, ef = queue_env
        today = date.today().isoformat()
        ef.write_text(
            f"# Agent Queue — Erledigt\n\n## {today}\n\n- [x] Task\n",
            encoding="utf-8",
        )
        assert mod._prune_erledigt_file() == 0

    def test_missing_file_returns_zero(self, queue_env):
        mod, qf, ef = queue_env
        assert mod._prune_erledigt_file() == 0
