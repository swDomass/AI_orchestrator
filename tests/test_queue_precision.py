from unittest.mock import patch
import pytest
from queue_manager import _replace_open_task_line

def test_replace_open_task_line_with_identical_tasks_and_subtasks(capsys):
    content = """## Queue
- [ ] Identical Task #parallel
  - Subtask A
  - Subtask B
- [ ] Identical Task #parallel
  - Subtask C
  - Subtask D
"""
    # Try to mark the second one done (line 5)
    task_text = "Identical Task #parallel"
    subtasks_1 = ("Subtask A", "Subtask B")
    subtasks_2 = ("Subtask C", "Subtask D")
    
    # 1. Precise match for second task
    updated = _replace_open_task_line(
        content,
        line_no=5,
        task_text=task_text,
        replacement="- [x] Identical Task #parallel ✅ Done",
        subtasks=subtasks_2
    )
    
    assert "- [ ] Identical Task #parallel\n  - Subtask A" in updated
    assert "- [x] Identical Task #parallel ✅ Done\n  - Subtask C" in updated

    # 2. Shifted match: second task moved to line 6 because something was inserted
    shifted_content = "## Queue\n- [ ] New Task\n" + content[len("## Queue\n"):]
    # Identical Task 2 is now at line 6
    updated_shifted = _replace_open_task_line(
        shifted_content,
        line_no=5, # Old line no
        task_text=task_text,
        replacement="- [x] Identical Task #parallel ✅ Done",
        subtasks=subtasks_2
    )
    
    assert "- [ ] Identical Task #parallel\n  - Subtask A" in updated_shifted
    assert "- [x] Identical Task #parallel ✅ Done\n  - Subtask C" in updated_shifted
    
    out, _ = capsys.readouterr()
    assert "re-synchronisiert" in out

def test_replace_open_task_line_fails_on_subtask_mismatch():
    content = """## Queue
- [ ] Task A #parallel
  - Sub A
- [ ] Task A #parallel
  - Sub B
"""
    # Try to match Task A with Sub C (which doesn't exist)
    updated = _replace_open_task_line(
        content,
        line_no=2,
        task_text="Task A #parallel",
        replacement="- [x] Done",
        subtasks=("Sub C",)
    )
    assert updated is None
