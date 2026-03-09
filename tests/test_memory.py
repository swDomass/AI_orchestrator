"""Tests for the three-layer memory system (memory.py).

Layer 1: Curated MEMORY.md
Layer 2: Daily append-only logs
Layer 3: TF-IDF search (existing, tested indirectly)
"""

import os
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def memory_root(tmp_path):
    """Create a fresh memory module pointing at tmp_path/vault."""
    vault = tmp_path / "vault"
    vault.mkdir()
    # Patch config before importing memory
    with patch("config._load_dotenv"):
        import importlib
        import config
        old_vault = config.VAULT_PATH
        config.VAULT_PATH = vault

        import memory as mem
        # Patch module-level paths
        mem._MEMORY_ROOT = vault / "99_System" / "AI" / "memory"
        mem._TASK_RESULTS_DIR = mem._MEMORY_ROOT / "task_results"
        mem._ARCHIVE_DIR = mem._MEMORY_ROOT / "archive"
        mem._DAILY_DIR = mem._MEMORY_ROOT / "daily"
        mem._CURATED_MEMORY_FILE = mem._MEMORY_ROOT / "MEMORY.md"

        yield mem

        config.VAULT_PATH = old_vault


# ── Layer 1: Curated MEMORY.md ──────────────────────────────────────────────


class TestCuratedMemory:
    def test_no_file_returns_empty(self, memory_root):
        assert memory_root.get_curated_memory() == ""

    def test_reads_file_content(self, memory_root):
        memory_root._CURATED_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        memory_root._CURATED_MEMORY_FILE.write_text(
            "# Long-term patterns\n\n- Always use pytest\n- Windows-first\n",
            encoding="utf-8",
        )
        result = memory_root.get_curated_memory()
        assert "Always use pytest" in result
        assert "Windows-first" in result

    def test_truncates_long_content(self, memory_root):
        memory_root._CURATED_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        long_content = "x" * 10_000
        memory_root._CURATED_MEMORY_FILE.write_text(long_content, encoding="utf-8")
        result = memory_root.get_curated_memory(max_chars=500)
        assert len(result) <= 510  # 500 + "..." + newline
        assert result.endswith("...")

    def test_custom_max_chars(self, memory_root):
        memory_root._CURATED_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        memory_root._CURATED_MEMORY_FILE.write_text("a" * 200, encoding="utf-8")
        result = memory_root.get_curated_memory(max_chars=100)
        assert len(result) <= 110


# ── Layer 2: Daily Logs ─────────────────────────────────────────────────────


class TestDailyLog:
    def test_append_creates_file(self, memory_root):
        ok = memory_root.append_daily_log(
            "Fix auth bug",
            "Fixed 3 issues in auth.py",
            "claude",
            45.0,
            cwd="/d/project",
            success=True,
        )
        assert ok is True

        today = date.today()
        path = memory_root._daily_log_path(today)
        assert path.exists()

        content = path.read_text(encoding="utf-8")
        assert f"# Memory {today.isoformat()}" in content
        assert "Fix auth bug" in content
        assert "claude" in content
        assert "success" in content

    def test_append_multiple_entries(self, memory_root):
        memory_root.append_daily_log("Task 1", "Result 1", "claude", 10.0)
        memory_root.append_daily_log("Task 2", "Result 2", "gemini", 20.0)

        path = memory_root._daily_log_path(date.today())
        content = path.read_text(encoding="utf-8")
        assert "Task 1" in content
        assert "Task 2" in content
        assert content.count("## ") == 2  # Two time-stamped sections

    def test_append_failed_task(self, memory_root):
        memory_root.append_daily_log("Failing task", "Error msg", "claude", 5.0, success=False)

        path = memory_root._daily_log_path(date.today())
        content = path.read_text(encoding="utf-8")
        assert "failed" in content

    def test_truncates_long_result(self, memory_root):
        long_result = "x" * 1000
        memory_root.append_daily_log("Task", long_result, "claude", 5.0)

        path = memory_root._daily_log_path(date.today())
        content = path.read_text(encoding="utf-8")
        assert "..." in content
        # Should not contain full 1000 chars
        assert len(content) < 800

    def test_daily_log_path_format(self, memory_root):
        d = date(2026, 3, 9)
        path = memory_root._daily_log_path(d)
        assert path.name == "Memory 2026-03-09.md"
        assert path.parent == memory_root._DAILY_DIR


class TestDailyContext:
    def test_no_logs_returns_empty(self, memory_root):
        assert memory_root.get_daily_context() == ""

    def test_reads_today(self, memory_root):
        memory_root.append_daily_log("Today task", "Today result", "claude", 10.0)

        ctx = memory_root.get_daily_context()
        assert "Today task" in ctx
        assert f"# Memory {date.today().isoformat()}" in ctx

    def test_reads_today_and_yesterday(self, memory_root):
        # Write today's log
        memory_root.append_daily_log("Today task", "Today result", "claude", 10.0)

        # Manually create yesterday's log
        yesterday = date.today() - timedelta(days=1)
        ypath = memory_root._daily_log_path(yesterday)
        ypath.parent.mkdir(parents=True, exist_ok=True)
        ypath.write_text(
            f"# Memory {yesterday.isoformat()}\n\n## 23:00 — Yesterday task\n- result\n",
            encoding="utf-8",
        )

        ctx = memory_root.get_daily_context()
        assert "Today task" in ctx
        assert "Yesterday task" in ctx
        # Today should come first
        today_pos = ctx.index("Today task")
        yesterday_pos = ctx.index("Yesterday task")
        assert today_pos < yesterday_pos

    def test_ignores_older_logs(self, memory_root):
        # Create a log from 2 days ago
        old = date.today() - timedelta(days=2)
        old_path = memory_root._daily_log_path(old)
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_text("# Old content\n", encoding="utf-8")

        ctx = memory_root.get_daily_context()
        assert ctx == ""

    def test_truncates_to_max_chars(self, memory_root):
        # Write a very long daily log
        memory_root._ensure_dirs()
        path = memory_root._daily_log_path(date.today())
        path.write_text("x" * 20_000, encoding="utf-8")

        ctx = memory_root.get_daily_context(max_chars=500)
        assert len(ctx) <= 510
        assert ctx.endswith("...")


# ── store_result also writes daily log ──────────────────────────────────────


class TestStoreResultDailyIntegration:
    def test_store_result_appends_daily_log(self, memory_root):
        memory_root.store_result(
            "Test task",
            "Some output",
            "claude",
            30.0,
            cwd="/d/project",
            success=True,
        )

        # Task result file should exist
        results = list(memory_root._TASK_RESULTS_DIR.glob("*.md"))
        assert len(results) == 1

        # Daily log should also exist
        path = memory_root._daily_log_path(date.today())
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "Test task" in content


# ── TF-IDF search (existing, basic smoke test) ─────────────────────────────


class TestTfIdfSearch:
    def test_search_empty_returns_empty(self, memory_root):
        assert memory_root.search_memory("anything") == []

    def test_search_finds_stored_result(self, memory_root):
        memory_root.store_result(
            "Fix auth bug in login module",
            "Fixed authentication bypass in login.py",
            "claude",
            30.0,
        )
        results = memory_root.search_memory("auth login bug")
        assert len(results) > 0
        assert "auth" in results[0]["task"].lower() or "login" in results[0]["summary"].lower()

    def test_get_context_for_task(self, memory_root):
        memory_root.store_result("Setup pytest", "Configured pytest for project", "gemini", 10.0)
        ctx = memory_root.get_context_for_task("run pytest tests")
        assert ctx  # Should return something (either TF-IDF match or recent fallback)
