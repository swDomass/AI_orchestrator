"""Tests for #worktree-tagged parallel runs (P1).

Mocks `subprocess.run` so tests never spawn real `git worktree` commands.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

import parallel_runner as parallel_runner_module
from parallel_runner import (
    SubTask,
    SubTaskResult,
    _create_worktree,
    _is_clean_git_repo,
    _remove_worktree,
    _worktree_id,
    run_parallel,
)
from limits import AllLimits


@pytest.fixture(autouse=True)
def _mock_dotenv():
    with patch("config._load_dotenv"):
        yield


# ── _is_clean_git_repo ────────────────────────────────────────────────────────

class TestIsCleanGitRepo:
    def test_clean_repo(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            if "rev-parse" in cmd:
                return SimpleNamespace(returncode=0, stdout=".git\n", stderr="")
            if "status" in cmd:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected git call: {cmd}")
        monkeypatch.setattr(subprocess, "run", fake_run)
        ok, reason = _is_clean_git_repo(Path("C:/proj"))
        assert ok is True
        assert reason == ""

    def test_not_a_repo(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=128, stdout="", stderr="fatal: not a git repo")
        monkeypatch.setattr(subprocess, "run", fake_run)
        ok, reason = _is_clean_git_repo(Path("C:/notrepo"))
        assert ok is False
        assert "not a git repository" in reason

    def test_dirty_repo(self, monkeypatch):
        responses = iter([
            SimpleNamespace(returncode=0, stdout=".git", stderr=""),
            SimpleNamespace(returncode=0, stdout=" M file.py\n", stderr=""),
        ])
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: next(responses))
        ok, reason = _is_clean_git_repo(Path("C:/dirty"))
        assert ok is False
        assert "uncommitted" in reason

    def test_subprocess_error(self, monkeypatch):
        def fake_run(*a, **k):
            raise OSError("git not found")
        monkeypatch.setattr(subprocess, "run", fake_run)
        ok, reason = _is_clean_git_repo(Path("C:/x"))
        assert ok is False
        assert "git check error" in reason


# ── _worktree_id ──────────────────────────────────────────────────────────────

def test_worktree_id_deterministic():
    a = _worktree_id("parent", "C:/proj", 0)
    b = _worktree_id("parent", "C:/proj", 0)
    assert a == b
    assert a.startswith("parallel-")
    assert len(a) == len("parallel-") + 8


def test_worktree_id_differs_by_group():
    a = _worktree_id("parent", "C:/proj1", 0)
    b = _worktree_id("parent", "C:/proj2", 0)
    assert a != b


# ── _create_worktree ──────────────────────────────────────────────────────────

class TestCreateWorktree:
    def test_success(self, tmp_path, monkeypatch):
        parent = tmp_path / "repo"
        parent.mkdir()
        wt_id = "parallel-abc12345"
        expected_path = parent / ".worktrees" / wt_id

        def fake_run(cmd, **kwargs):
            # Simulate git creating the directory
            expected_path.mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        path, err = _create_worktree(parent, wt_id)
        assert err == ""
        assert path == expected_path
        assert path.is_dir()

    def test_git_failure(self, tmp_path, monkeypatch):
        parent = tmp_path / "repo"
        parent.mkdir()
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="fatal: branch in use"),
        )
        path, err = _create_worktree(parent, "parallel-xyz")
        assert path is None
        assert "git worktree add failed" in err
        assert "branch in use" in err


# ── _remove_worktree ──────────────────────────────────────────────────────────

class TestRemoveWorktree:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        assert _remove_worktree(Path("C:/repo"), Path("C:/repo/.worktrees/parallel-X")) is True

    def test_failure_logs_and_returns_false(self, monkeypatch, caplog):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="locked"),
        )
        with caplog.at_level("WARNING"):
            ok = _remove_worktree(Path("C:/repo"), Path("C:/repo/.worktrees/X"))
        assert ok is False
        assert any("worktree remove failed" in r.message for r in caplog.records)


# ── run_parallel with #worktree (integration with mocks) ──────────────────────

@pytest.fixture
def _mock_subtask_runner(monkeypatch):
    """Stub _run_single_subtask to skip provider/tool execution."""
    def stub(subtask, idx, limits, memory_context, pause_event, profile=None):
        return SubTaskResult(
            text=subtask.text, provider_name="mock", success=True,
            output=f"ok-{idx} cwd={subtask.cwd}", error="",
        )
    monkeypatch.setattr(parallel_runner_module, "_run_single_subtask", stub)


def _stub_parse_subtask(monkeypatch, mapping: dict[str, SubTask]) -> None:
    monkeypatch.setattr(parallel_runner_module, "_parse_subtask", lambda t: mapping[t])


class TestRunParallelWithWorktree:
    def test_no_worktree_tag_skips_setup(self, monkeypatch, _mock_subtask_runner):
        """Without #worktree, no git commands run and cwd is untouched."""
        _stub_parse_subtask(monkeypatch, {
            "a": SubTask(text="a", provider_forced=None, cwd="C:/proj", tool_name=None, timeout=10),
        })
        # Will raise if any git call slips through.
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected subprocess call"))
        )
        results = run_parallel("parent (no worktree tag)", ("a",), AllLimits())
        assert len(results) == 1
        assert results[0].success is True
        assert "cwd=C:/proj" in results[0].output

    def test_precheck_fails_short_circuits_group(self, monkeypatch, _mock_subtask_runner):
        """Dirty repo → all subtasks of that group fail before any run."""
        _stub_parse_subtask(monkeypatch, {
            "a": SubTask(text="a", provider_forced=None, cwd="C:/proj", tool_name=None, timeout=10),
            "b": SubTask(text="b", provider_forced=None, cwd="C:/proj", tool_name=None, timeout=10),
        })
        # Simulate dirty repo on every status call
        responses = [
            SimpleNamespace(returncode=0, stdout=".git", stderr=""),
            SimpleNamespace(returncode=0, stdout=" M foo.py\n", stderr=""),
        ]
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: responses.pop(0))

        # _run_single_subtask must NOT be called for short-circuited groups
        call_count = {"n": 0}
        def stub(*a, **k):
            call_count["n"] += 1
            return SubTaskResult(text="x", provider_name="mock", success=True, output="", error="")
        monkeypatch.setattr(parallel_runner_module, "_run_single_subtask", stub)

        results = run_parallel("parent #worktree", ("a", "b"), AllLimits())
        assert all(not r.success for r in results)
        assert all(r.provider_name == "worktree" for r in results)
        assert all("uncommitted" in r.error for r in results)
        assert call_count["n"] == 0

    def test_setup_creates_worktree_and_rewrites_cwd(self, monkeypatch, tmp_path):
        """Successful setup: subtask sees worktree path as cwd."""
        repo = tmp_path / "repo"
        repo.mkdir()
        wt_id = _worktree_id("parent #worktree", "C:/proj", 0)
        wt_path = Path("C:/proj") / ".worktrees" / wt_id

        _stub_parse_subtask(monkeypatch, {
            "a": SubTask(text="a", provider_forced=None, cwd="C:/proj", tool_name=None, timeout=10),
        })

        # Sequence of git calls: rev-parse (repo), status (clean), worktree add (ok)
        gitcalls = []
        def fake_run(cmd, **kwargs):
            gitcalls.append(cmd)
            if "rev-parse" in cmd:
                return SimpleNamespace(returncode=0, stdout=".git", stderr="")
            if "status" in cmd:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "worktree" in cmd and "add" in cmd:
                # We can't actually create the dir under C:\proj on disk.
                # Patch _create_worktree to bypass this filesystem check.
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "worktree" in cmd and "remove" in cmd:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected: {cmd}")
        monkeypatch.setattr(subprocess, "run", fake_run)

        # _create_worktree relies on target.is_dir() — patch it directly to
        # return a fake path that has is_dir() == True.
        def fake_create(parent_cwd, worktree_id):
            return wt_path, ""
        monkeypatch.setattr(parallel_runner_module, "_create_worktree", fake_create)

        captured_cwds: list[str | None] = []
        def stub(subtask, idx, limits, memory_context, pause_event, profile=None):
            captured_cwds.append(subtask.cwd)
            return SubTaskResult(text=subtask.text, provider_name="mock",
                                 success=True, output="ok", error="")
        monkeypatch.setattr(parallel_runner_module, "_run_single_subtask", stub)

        # Track remove calls separately
        remove_calls = []
        monkeypatch.setattr(
            parallel_runner_module, "_remove_worktree",
            lambda base, wt: remove_calls.append((base, wt)) or True,
        )

        results = run_parallel("parent #worktree", ("a",), AllLimits())
        assert len(results) == 1
        assert results[0].success is True
        # Subtask saw the rewritten cwd
        assert captured_cwds == [str(wt_path)]
        # Cleanup happened after success
        assert len(remove_calls) == 1

    def test_keep_worktree_skips_cleanup(self, monkeypatch):
        wt_path = Path("C:/proj/.worktrees/parallel-deadbeef")
        _stub_parse_subtask(monkeypatch, {
            "a": SubTask(text="a", provider_forced=None, cwd="C:/proj", tool_name=None, timeout=10),
        })
        monkeypatch.setattr(
            parallel_runner_module, "_is_clean_git_repo",
            lambda p: (True, ""),
        )
        monkeypatch.setattr(
            parallel_runner_module, "_create_worktree",
            lambda parent, wt_id: (wt_path, ""),
        )
        monkeypatch.setattr(
            parallel_runner_module, "_run_single_subtask",
            lambda *a, **k: SubTaskResult(text=a[0].text, provider_name="mock",
                                          success=True, output="ok", error=""),
        )
        remove_calls = []
        monkeypatch.setattr(
            parallel_runner_module, "_remove_worktree",
            lambda base, wt: remove_calls.append(wt) or True,
        )

        results = run_parallel("parent #worktree #keep-worktree", ("a",), AllLimits())
        assert results[0].success is True
        # With #keep-worktree, remove must NOT be called
        assert remove_calls == []

    def test_subtask_failure_retains_worktree_path_in_error(self, monkeypatch):
        wt_path = Path("C:/proj/.worktrees/parallel-cafe1234")
        _stub_parse_subtask(monkeypatch, {
            "a": SubTask(text="a", provider_forced=None, cwd="C:/proj", tool_name=None, timeout=10),
        })
        monkeypatch.setattr(parallel_runner_module, "_is_clean_git_repo", lambda p: (True, ""))
        monkeypatch.setattr(parallel_runner_module, "_create_worktree",
                            lambda parent, wt_id: (wt_path, ""))
        monkeypatch.setattr(
            parallel_runner_module, "_run_single_subtask",
            lambda *a, **k: SubTaskResult(text=a[0].text, provider_name="mock",
                                          success=False, output="", error="boom"),
        )
        remove_calls = []
        monkeypatch.setattr(
            parallel_runner_module, "_remove_worktree",
            lambda base, wt: remove_calls.append(wt) or True,
        )

        results = run_parallel("parent #worktree", ("a",), AllLimits())
        assert results[0].success is False
        assert "boom" in results[0].error
        assert str(wt_path) in results[0].error
        # Failed run leaves worktree intact for inspection
        assert remove_calls == []

    def test_missing_cwd_blocks_worktree_setup(self, monkeypatch, _mock_subtask_runner):
        """#worktree without any cwd → group is marked as failed with clear error."""
        _stub_parse_subtask(monkeypatch, {
            "a": SubTask(text="a", provider_forced=None, cwd=None, tool_name=None, timeout=10),
        })
        # No git call should happen — fail before that.
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("git should not be called"))
        )
        results = run_parallel("parent #worktree", ("a",), AllLimits())
        assert results[0].success is False
        assert "cwd" in results[0].error.lower()


# ── strip_metadata_tags / extract_*_tag ───────────────────────────────────────

class TestTagPlumbing:
    def test_extract_worktree_tag(self):
        from queue_manager import extract_worktree_tag
        assert extract_worktree_tag("parent #worktree") is True
        assert extract_worktree_tag("parent #parallel") is False
        # Must not collide with #keep-worktree on tag boundary
        assert extract_worktree_tag("parent #keep-worktree #worktree") is True

    def test_extract_keep_worktree_tag(self):
        from queue_manager import extract_keep_worktree_tag
        assert extract_keep_worktree_tag("parent #keep-worktree") is True
        assert extract_keep_worktree_tag("parent #worktree") is False

    def test_strip_metadata_removes_both(self):
        from queue_manager import strip_metadata_tags
        out = strip_metadata_tags("Do thing #worktree #keep-worktree #parallel")
        assert "#worktree" not in out
        assert "#keep-worktree" not in out
        assert "#parallel" not in out
        assert "Do thing" in out
