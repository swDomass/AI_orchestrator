from types import SimpleNamespace
from unittest.mock import patch

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
