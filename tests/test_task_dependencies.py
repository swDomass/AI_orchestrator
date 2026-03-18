"""Tests for #id: / #needs: task dependency system in queue_manager."""
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

import pytest


with patch("config._load_dotenv"):
    import queue_manager
    from queue_manager import (
        extract_id_tag,
        extract_needs_tags,
        strip_metadata_tags,
        _collect_completed_ids,
        read_queue_items,
        QueueTask,
    )


# ---------------------------------------------------------------------------
# Extractor unit tests
# ---------------------------------------------------------------------------

def test_extract_id_tag_basic():
    assert extract_id_tag("Build schema #id:db-setup") == "db-setup"


def test_extract_id_tag_case_insensitive():
    assert extract_id_tag("Task #ID:MyTask") == "mytask"


def test_extract_id_tag_none_when_absent():
    assert extract_id_tag("Task without id tag") is None


def test_extract_needs_tags_single():
    assert extract_needs_tags("Run migrations #needs:db-setup") == ["db-setup"]


def test_extract_needs_tags_comma():
    assert extract_needs_tags("Deploy #needs:build,test") == ["build", "test"]


def test_extract_needs_tags_empty_when_absent():
    assert extract_needs_tags("Independent task") == []


def test_extract_needs_tags_case_insensitive():
    assert extract_needs_tags("Task #NEEDS:StepA") == ["stepa"]


# ---------------------------------------------------------------------------
# strip_metadata_tags
# ---------------------------------------------------------------------------

def test_strip_removes_id_tag():
    result = strip_metadata_tags("Build schema #id:db-setup cwd:/tmp")
    assert "#id:" not in result
    assert "db-setup" not in result


def test_strip_removes_needs_tag():
    result = strip_metadata_tags("Run migrations #needs:db-setup")
    assert "#needs:" not in result
    assert "db-setup" not in result


def test_strip_preserves_task_text():
    result = strip_metadata_tags("Run migrations #needs:db-setup #id:migration")
    assert "Run migrations" in result


# ---------------------------------------------------------------------------
# _collect_completed_ids
# ---------------------------------------------------------------------------

def test_collect_ids_done():
    content = "- [x] Build schema #id:db-setup\n"
    assert "db-setup" in _collect_completed_ids(content)


def test_collect_ids_failed():
    content = "- [-] Build schema #id:db-setup\n"
    assert "db-setup" in _collect_completed_ids(content)


def test_collect_ids_ignores_open():
    content = "- [ ] Build schema #id:db-setup\n"
    assert "db-setup" not in _collect_completed_ids(content)


def test_collect_ids_multiple():
    content = (
        "- [x] Task A #id:step-a\n"
        "- [-] Task B #id:step-b\n"
        "- [ ] Task C #id:step-c\n"
    )
    completed = _collect_completed_ids(content)
    assert "step-a" in completed
    assert "step-b" in completed
    assert "step-c" not in completed


def test_collect_ids_no_id_tags():
    content = "- [x] Some task without id\n"
    assert _collect_completed_ids(content) == set()


# ---------------------------------------------------------------------------
# read_queue_items — dependency resolution (Pass 2)
# ---------------------------------------------------------------------------

def _make_queue_content(queue_section: str) -> str:
    return f"## Queue\n{queue_section}\n## Ergebnisse\n"


@pytest.fixture
def mock_queue_file(tmp_path):
    q_file = tmp_path / "agent-queue.md"
    with patch("queue_manager.QUEUE_FILE", q_file):
        yield q_file


def test_blocked_when_dep_open(mock_queue_file):
    mock_queue_file.write_text(
        _make_queue_content(
            "- [ ] Build schema #id:db-setup\n"
            "- [ ] Run migrations #needs:db-setup\n"
        ),
        encoding="utf-8",
    )
    items = read_queue_items()
    task2 = next(t for t in items if "migrations" in t.task_text)
    assert task2.blocked_reason != ""
    assert "db-setup" in task2.blocked_reason


def test_unblocked_when_dep_done(mock_queue_file):
    mock_queue_file.write_text(
        "## Ergebnisse\n"
        "- [x] Build schema #id:db-setup\n"
        "## Queue\n"
        "- [ ] Run migrations #needs:db-setup\n",
        encoding="utf-8",
    )
    items = read_queue_items()
    assert len(items) == 1
    assert items[0].blocked_reason == ""


def test_multi_dep_partial_blocked(mock_queue_file):
    mock_queue_file.write_text(
        "## Ergebnisse\n"
        "- [x] Step A done #id:step-a\n"
        "## Queue\n"
        "- [ ] Deploy #needs:step-a,step-b\n",
        encoding="utf-8",
    )
    items = read_queue_items()
    assert len(items) == 1
    assert "step-b" in items[0].blocked_reason
    assert "step-a" not in items[0].blocked_reason


def test_independent_unaffected(mock_queue_file):
    mock_queue_file.write_text(
        _make_queue_content(
            "- [ ] Task A #id:a\n"
            "- [ ] Independent task\n"
        ),
        encoding="utf-8",
    )
    items = read_queue_items()
    independent = next(t for t in items if "Independent" in t.task_text)
    assert independent.blocked_reason == ""


def test_all_deps_met_unblocked(mock_queue_file):
    mock_queue_file.write_text(
        "## Ergebnisse\n"
        "- [x] Step A #id:step-a\n"
        "- [x] Step B #id:step-b\n"
        "## Queue\n"
        "- [ ] Deploy #needs:step-a,step-b\n",
        encoding="utf-8",
    )
    items = read_queue_items()
    assert len(items) == 1
    assert items[0].blocked_reason == ""


# ---------------------------------------------------------------------------
# run_once — blocked tasks are skipped but not marked done
# ---------------------------------------------------------------------------

def test_run_once_skips_blocked(monkeypatch):
    """run_once must not dispatch a blocked task (select_provider must not be called)."""
    blocked_task = QueueTask(
        task_text="Run migrations #needs:db-setup",
        line_no=2,
        blocked_reason="needs db-setup",
    )

    select_provider_calls = []

    with patch("config._load_dotenv"):
        import orchestrator

    monkeypatch.setattr(orchestrator, "read_queue_items", lambda: [blocked_task])
    monkeypatch.setattr(
        orchestrator, "select_provider",
        lambda *a, **kw: select_provider_calls.append(a) or None,
    )
    monkeypatch.setattr(orchestrator, "mark_done", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)

    orchestrator.run_once()

    assert select_provider_calls == [], "select_provider must not be called for a blocked task"


def test_run_once_blocked_stays_in_queue(monkeypatch):
    """mark_done must NOT be called for a blocked task."""
    blocked_task = QueueTask(
        task_text="Run migrations #needs:db-setup",
        line_no=2,
        blocked_reason="needs db-setup",
    )

    mark_done_calls = []

    with patch("config._load_dotenv"):
        import orchestrator

    monkeypatch.setattr(orchestrator, "read_queue_items", lambda: [blocked_task])
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "mark_done", lambda *a, **kw: mark_done_calls.append(a))
    monkeypatch.setattr(orchestrator, "append_log", lambda *a, **kw: None)

    orchestrator.run_once()

    assert mark_done_calls == [], "mark_done must not be called for a blocked task"
