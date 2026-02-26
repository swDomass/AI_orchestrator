from types import SimpleNamespace
from unittest.mock import patch
import os

import orchestrator


def _completed(stdout: str = "", returncode: int = 0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


@patch("orchestrator.GIT_AUTO_STASH", True)
@patch("orchestrator._is_git_repo", return_value=True)
@patch("orchestrator.subprocess.run")
def test_git_snapshot_is_non_destructive(mock_run, _mock_repo):
    mock_run.side_effect = [
        _completed(stdout="abc123\n"),  # git stash create
        _completed(),                   # git stash store
    ]

    stash_name = orchestrator._git_snapshot("C:/repo")

    commands = [call.args[0] for call in mock_run.call_args_list]
    assert commands[0][:3] == ["git", "stash", "create"]
    assert commands[1][:3] == ["git", "stash", "store"]
    assert all("push" not in cmd for cmd in commands)
    assert stash_name is not None


@patch("orchestrator.subprocess.run")
def test_git_diff_summary_includes_untracked_files(mock_run):
    mock_run.side_effect = [
        _completed(stdout=" foo.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n"),
        _completed(stdout="new_a.txt\nnested/new_b.txt\n"),
    ]

    summary = orchestrator._git_diff_summary("C:/repo")

    assert "foo.py" in summary
    assert "Untracked (2): new_a.txt, nested/new_b.txt" in summary


def test_snapshot_dir_tracks_nested_files_and_ignores_directory_only_mtime_changes(tmp_path):
    nested_dir = tmp_path / "nested"
    nested_dir.mkdir()
    nested_file = nested_dir / "file.txt"
    nested_file.write_text("a", encoding="utf-8")

    before = orchestrator._snapshot_dir(str(tmp_path))
    assert "nested\\file.txt" in before or "nested/file.txt" in before

    # Directory mtime changes alone should not count because snapshots store only files.
    os.utime(nested_dir, None)
    after_dir_only = orchestrator._snapshot_dir(str(tmp_path))
    assert before == after_dir_only

    nested_file.write_text("ab", encoding="utf-8")
    after_file_edit = orchestrator._snapshot_dir(str(tmp_path))
    assert before != after_file_edit
