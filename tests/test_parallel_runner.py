import pytest
import threading
from pathlib import Path
import queue_manager
from parallel_runner import _parse_subtask, SubTask, run_parallel, format_parallel_result, SubTaskResult
from limits import AllLimits

def test_parse_subtask(tmp_path, monkeypatch):
    # Disable ALLOWED_CWD_ROOTS check for the test
    monkeypatch.setattr(queue_manager, "ALLOWED_CWD_ROOTS", [])
    
    # Create a dummy directory to satisfy extract_cwd's is_dir() check
    d = tmp_path / "proj"
    d.mkdir()
    cwd_str = str(d.resolve())
    
    text = f"Review project X #claude #tool:review-loop cwd:{cwd_str} #timeout:300s"
    st = _parse_subtask(text)
    assert st.text == text
    assert st.provider_forced == "claude"
    assert st.tool_name == "review-loop"
    assert Path(st.cwd).resolve() == d.resolve()
    assert st.timeout == 300

def test_parse_subtask_defaults():
    text = "Review project X"
    st = _parse_subtask(text)
    assert st.text == text
    assert st.provider_forced is None
    assert st.tool_name is None
    assert st.cwd is None
    assert st.timeout == 300 # TASK_TIMEOUT_SEC default (5 min)

def test_format_parallel_result():
    results = [
        SubTaskResult(text="Task 1", provider_name="claude", success=True, output="Output 1"),
        SubTaskResult(text="Task 2", provider_name="gemini", success=False, error="Error 2", output=""),
    ]
    formatted = format_parallel_result(results)
    assert "**Subtask 1** (claude): PASS" in formatted
    assert "Output 1" in formatted
    assert "**Subtask 2** (gemini): FAIL" in formatted
    assert "Error 2" in formatted
