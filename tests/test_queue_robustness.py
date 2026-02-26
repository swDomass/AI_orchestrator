
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Mock config to avoid loading real .env
with patch("config._load_dotenv"):
    import queue_manager
    from queue_manager import read_queue, mark_done, ensure_queue_file

@pytest.fixture
def mock_queue_file(tmp_path):
    q_file = tmp_path / "agent-queue.md"
    # Patch the QUEUE_FILE in queue_manager
    with patch("queue_manager.QUEUE_FILE", q_file):
        yield q_file

def test_mark_done_whitespace_mismatch(mock_queue_file):
    # Setup queue with extra spaces
    content = """
## Queue
- [ ]   Task with extra spaces   
"""
    mock_queue_file.write_text(content, encoding="utf-8")
    
    # Read queue
    tasks = read_queue()
    assert len(tasks) == 1
    task_text = tasks[0]
    assert task_text == "Task with extra spaces" # read_queue strips it
    
    # Try to mark done
    mark_done(task_text, "test-provider")
    
    # Check file content
    new_content = mock_queue_file.read_text(encoding="utf-8")
    
    # Expectation: The task should be marked done (x)
    # Reality: It probably won't be because "- [ ] Task with extra spaces" doesn't match "- [ ]   Task with extra spaces   "
    assert "- [x]" in new_content, f"Task was not marked done. Content:\n{new_content}"

def test_read_queue_retry_filtering(mock_queue_file):
    from datetime import datetime, timedelta
    
    # Calculate future and past times
    now = datetime.now()
    future_time = (now + timedelta(minutes=10)).strftime("%H:%M")
    past_time = (now - timedelta(minutes=10)).strftime("%H:%M")
    
    content = f"""
## Queue
- [ ] Task Ready
- [ ] Task Future <!-- retry: {future_time} -->
- [ ] Task Past <!-- retry: {past_time} -->
"""
    mock_queue_file.write_text(content, encoding="utf-8")
    
    tasks = read_queue()
    
    assert "Task Ready" in tasks
    assert "Task Past" in tasks
    assert "Task Future" not in tasks


def test_read_queue_ignores_checkboxes_outside_queue_section(mock_queue_file):
    content = """
## Queue
- [ ] Real Queue Task

## Ergebnisse
### 2026-02-24 | codex
Provider output:
- [ ] This is just a checklist item in the result text
"""
    mock_queue_file.write_text(content, encoding="utf-8")

    tasks = read_queue()

    assert tasks == ["Real Queue Task"]


def test_retry_is_not_due_across_midnight():
    now = queue_manager.datetime(2026, 2, 24, 23, 50)

    assert queue_manager._retry_is_due("23:45", now=now) is True
    assert queue_manager._retry_is_due("00:15", now=now) is False


def test_retry_is_due_with_absolute_timestamp_across_day_boundary():
    now = queue_manager.datetime(2026, 2, 25, 3, 0)

    assert queue_manager._retry_is_due("2026-02-24 14:00", now=now) is True
    assert queue_manager._retry_is_due("2026-02-25 14:00", now=now) is False


def test_read_queue_retry_filtering_with_absolute_retry_markers(mock_queue_file):
    from datetime import datetime, timedelta

    now = datetime.now().replace(second=0, microsecond=0)
    future_time = (now + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
    past_time = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")

    content = f"""
## Queue
- [ ] Task Future <!-- retry: {future_time} -->
- [ ] Task Past <!-- retry: {past_time} -->
"""
    mock_queue_file.write_text(content, encoding="utf-8")

    tasks = read_queue()

    assert "Task Past" in tasks
    assert "Task Future" not in tasks

