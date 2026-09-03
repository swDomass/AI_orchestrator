from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import os
import shutil
import subprocess
import time

import pytest

import orchestrator
from config import (
    GIT_SNAPSHOT_MAX_AGE_DAYS,
    GIT_SNAPSHOT_MAX_COUNT,
    GIT_SNAPSHOT_PROTECT_DAYS,
    GIT_SNAPSHOT_REF_PREFIX,
)

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

# Any command that would touch the user's own stash list. The whole point of the
# 2026-09-03 change is that none of these are ever issued.
_FORBIDDEN_STASH_SUBCOMMANDS = ("store", "push", "save", "drop", "pop", "clear", "apply")


def _completed(stdout: str = "", returncode: int = 0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def _git_init(cwd: Path) -> None:
    """Initialize a minimal git repo for tests that need one."""
    subprocess.run(["git", "init"], cwd=cwd, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=cwd, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=cwd, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=cwd, capture_output=True)


def _git(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        env=({**os.environ, **env} if env else None),
    )


def _refs(cwd: Path) -> list[str]:
    return _git(cwd, "for-each-ref", "--format=%(refname)").stdout.split()


def _seed_ref(cwd: Path, refname: str, age_days: float) -> None:
    """Create an empty commit back-dated by age_days and point refname at it."""
    stamp = str(int(time.time() - age_days * 86400))
    _git(cwd, "commit", "--allow-empty", "-m", f"seed {refname}",
         env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})
    sha = _git(cwd, "rev-parse", "HEAD").stdout.strip()
    _git(cwd, "update-ref", refname, sha)


# --- half 1: the snapshot lands in our own ref namespace, never in refs/stash ---

@patch("orchestrator.GIT_AUTO_STASH", True)
@patch("orchestrator._is_git_repo", return_value=True)
@patch("orchestrator.subprocess.run")
def test_git_snapshot_writes_a_ref_and_never_the_stash(mock_run, _mock_repo):
    mock_run.side_effect = [
        _completed(stdout="abc123\n"),  # git stash create
        _completed(),                   # git update-ref <ref> abc123 ""
        _completed(stdout=""),          # git for-each-ref (prune finds nothing)
    ]

    ref_name = orchestrator._git_snapshot("C:/repo")

    commands = [call.args[0] for call in mock_run.call_args_list]
    assert commands[0][:3] == ["git", "stash", "create"]
    assert commands[1][:2] == ["git", "update-ref"]
    assert commands[1][2] == ref_name
    assert commands[1][3] == "abc123"
    # empty oldvalue == create-only, so a same-second collision cannot overwrite
    assert commands[1][4] == ""

    assert ref_name is not None
    assert ref_name.startswith(GIT_SNAPSHOT_REF_PREFIX)
    # the user's stash list is never a target, under any subcommand
    for cmd in commands:
        assert "stash" not in cmd or cmd[:3] == ["git", "stash", "create"]
        for forbidden in _FORBIDDEN_STASH_SUBCOMMANDS:
            assert cmd[:3] != ["git", "stash", forbidden]
    assert all("push" not in cmd for cmd in commands)
    assert all("refs/stash" not in cmd for cmd in commands)


@patch("orchestrator.GIT_AUTO_STASH", True)
@patch("orchestrator._is_git_repo", return_value=True)
@patch("orchestrator.subprocess.run")
def test_git_snapshot_logs_the_ref_name_so_the_user_can_find_it(mock_run, _mock_repo, capsys):
    mock_run.side_effect = [
        _completed(stdout="abc123\n"),
        _completed(),
        _completed(stdout=""),
    ]

    ref_name = orchestrator._git_snapshot("C:/repo")

    out = capsys.readouterr().out
    # `git stash list` no longer shows the snapshot, so the ref name and the
    # restore command are the only way back to it.
    assert ref_name in out
    assert f"git stash apply {ref_name}" in out


@patch("orchestrator.GIT_AUTO_STASH", True)
@patch("orchestrator._is_git_repo", return_value=True)
@patch("orchestrator.subprocess.run")
def test_git_snapshot_retries_with_a_suffix_on_a_same_second_collision(mock_run, _mock_repo):
    mock_run.side_effect = [
        _completed(stdout="abc123\n"),                                  # stash create
        _completed(returncode=128),                                     # ref exists
        _completed(),                                                   # suffixed ref ok
        _completed(stdout=""),                                          # prune
    ]

    ref_name = orchestrator._git_snapshot("C:/repo")

    commands = [call.args[0] for call in mock_run.call_args_list]
    first_ref, second_ref = commands[1][2], commands[2][2]
    assert first_ref != second_ref
    assert second_ref == f"{first_ref}_2"
    assert ref_name == second_ref


@patch("orchestrator.GIT_AUTO_STASH", True)
@patch("orchestrator._is_git_repo", return_value=True)
@patch("orchestrator.subprocess.run")
def test_git_snapshot_returns_none_when_the_ref_cannot_be_written(mock_run, _mock_repo):
    mock_run.side_effect = [_completed(stdout="abc123\n")] + [
        _completed(returncode=128) for _ in range(10)
    ]

    assert orchestrator._git_snapshot("C:/repo") is None


@requires_git
@patch("orchestrator.GIT_AUTO_STASH", True)
def test_git_snapshot_real_repo_creates_ref_leaves_stash_empty_and_restores(tmp_path):
    _git_init(tmp_path)
    tracked = tmp_path / "f.txt"
    tracked.write_text("original\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    tracked.write_text("MODIFIED\n", encoding="utf-8")

    ref_name = orchestrator._git_snapshot(str(tmp_path))

    assert ref_name is not None and ref_name.startswith(GIT_SNAPSHOT_REF_PREFIX)
    assert ref_name in _refs(tmp_path)
    # the user's stash list stays their own workspace
    assert _git(tmp_path, "stash", "list").stdout.strip() == ""

    # the restore path documented in README.md actually works
    tracked.write_text("original\n", encoding="utf-8")
    assert _git(tmp_path, "stash", "apply", ref_name).returncode == 0
    assert tracked.read_text(encoding="utf-8") == "MODIFIED\n"


# --- half 2: pruning hits our namespace and nothing else ---

def test_prune_only_ever_issues_deletes_inside_our_namespace():
    now = time.time()
    listed = [
        (f"{GIT_SNAPSHOT_REF_PREFIX}ancient", int(now - 400 * 86400), "sha_old"),
        (f"{GIT_SNAPSHOT_REF_PREFIX}fresh", int(now - 1 * 86400), "sha_new"),
    ]
    with patch("orchestrator._snapshot_refs", return_value=listed), \
         patch("orchestrator.subprocess.run", return_value=_completed()) as mock_run:
        deleted = orchestrator._prune_snapshot_refs("C:/repo", now=now)

    assert deleted == [f"{GIT_SNAPSHOT_REF_PREFIX}ancient"]
    for call in mock_run.call_args_list:
        cmd = call.args[0]
        assert cmd[:3] == ["git", "update-ref", "-d"]
        assert cmd[3].startswith(GIT_SNAPSHOT_REF_PREFIX)
        # the compare-and-swap old value keeps a concurrently-changed ref safe
        assert cmd[4] == "sha_old"


def test_prune_ignores_refs_outside_the_namespace_even_if_git_lists_them():
    """_snapshot_refs re-checks the prefix, so a foreign ref never reaches a delete."""
    now = time.time()
    listing = "\n".join([
        f"refs/heads/main\t{int(now - 400 * 86400)}\tsha_branch",
        f"refs/stash\t{int(now - 400 * 86400)}\tsha_stash",
        f"refs/other-backup/x\t{int(now - 400 * 86400)}\tsha_other",
        f"{GIT_SNAPSHOT_REF_PREFIX}ancient\t{int(now - 400 * 86400)}\tsha_ours",
    ])
    with patch("orchestrator.subprocess.run") as mock_run:
        mock_run.side_effect = [_completed(stdout=listing)] + [
            _completed() for _ in range(10)
        ]
        deleted = orchestrator._prune_snapshot_refs("C:/repo", now=now)

    assert deleted == [f"{GIT_SNAPSHOT_REF_PREFIX}ancient"]
    delete_targets = [
        call.args[0][3] for call in mock_run.call_args_list
        if call.args[0][:3] == ["git", "update-ref", "-d"]
    ]
    assert delete_targets == [f"{GIT_SNAPSHOT_REF_PREFIX}ancient"]


def _prune_with(listed, now):
    with patch("orchestrator._snapshot_refs", return_value=listed), \
         patch("orchestrator.subprocess.run", return_value=_completed()):
        return orchestrator._prune_snapshot_refs("C:/repo", now=now)


def test_protect_window_survives_both_caps():
    """Night tasks do not commit, so a young snapshot is the only undo there is."""
    now = time.time()
    young = int(now - (GIT_SNAPSHOT_PROTECT_DAYS - 1) * 86400)
    # far over the count cap, but every entry is inside the protection window
    listed = [(f"{GIT_SNAPSHOT_REF_PREFIX}s{i}", young - i, f"sha{i}")
              for i in range(GIT_SNAPSHOT_MAX_COUNT + 20)]

    assert _prune_with(listed, now) == []


def test_snapshot_older_than_the_age_cap_is_pruned():
    now = time.time()
    listed = [(f"{GIT_SNAPSHOT_REF_PREFIX}old",
               int(now - (GIT_SNAPSHOT_MAX_AGE_DAYS + 1) * 86400), "sha")]

    assert _prune_with(listed, now) == [f"{GIT_SNAPSHOT_REF_PREFIX}old"]


def test_snapshot_between_protect_and_age_cap_is_pruned_only_by_count():
    now = time.time()
    middle_age = (GIT_SNAPSHOT_PROTECT_DAYS + GIT_SNAPSHOT_MAX_AGE_DAYS) // 2
    ts = int(now - middle_age * 86400)
    # exactly at the cap -> nothing over the edge, all survive
    at_cap = [(f"{GIT_SNAPSHOT_REF_PREFIX}s{i}", ts - i, f"sha{i}")
              for i in range(GIT_SNAPSHOT_MAX_COUNT)]
    assert _prune_with(at_cap, now) == []

    # one over the cap -> only the oldest (last, newest-first) goes
    over_cap = [(f"{GIT_SNAPSHOT_REF_PREFIX}s{i}", ts - i, f"sha{i}")
                for i in range(GIT_SNAPSHOT_MAX_COUNT + 1)]
    assert _prune_with(over_cap, now) == [f"{GIT_SNAPSHOT_REF_PREFIX}s{GIT_SNAPSHOT_MAX_COUNT}"]


def test_count_cap_is_starved_by_the_protect_window_deliberately():
    """Over the count cap but all young -> nothing is pruned. Undo outranks tidiness."""
    now = time.time()
    ts = int(now - 3 * 86400)
    listed = [(f"{GIT_SNAPSHOT_REF_PREFIX}s{i}", ts - i, f"sha{i}")
              for i in range(GIT_SNAPSHOT_MAX_COUNT + 5)]

    assert _prune_with(listed, now) == []


def test_prune_never_raises_when_git_is_unavailable():
    with patch("orchestrator._snapshot_refs", side_effect=OSError("boom")):
        assert orchestrator._prune_snapshot_refs("C:/repo") == []


@requires_git
def test_prune_real_repo_touches_only_its_own_namespace(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    old = GIT_SNAPSHOT_MAX_AGE_DAYS + 100
    # decoys in every neighbouring namespace, all old enough to be pruned
    # if the scoping were wrong
    _seed_ref(tmp_path, "refs/heads/decoy", old)
    _seed_ref(tmp_path, "refs/tags/decoytag", old)
    _seed_ref(tmp_path, "refs/stash", old)
    _seed_ref(tmp_path, "refs/other-backup/20200101_000000", old)
    _seed_ref(tmp_path, "refs/orchestrator-backup-sibling/20200101_000000", old)
    # our own: one prunable, one inside the protection window
    _seed_ref(tmp_path, f"{GIT_SNAPSHOT_REF_PREFIX}20200101_000000", old)
    _seed_ref(tmp_path, f"{GIT_SNAPSHOT_REF_PREFIX}20990101_000000",
              GIT_SNAPSHOT_PROTECT_DAYS - 1)

    before = set(_refs(tmp_path))
    deleted = orchestrator._prune_snapshot_refs(str(tmp_path))

    assert deleted == [f"{GIT_SNAPSHOT_REF_PREFIX}20200101_000000"]
    after = set(_refs(tmp_path))
    assert before - after == {f"{GIT_SNAPSHOT_REF_PREFIX}20200101_000000"}
    for survivor in ("refs/heads/decoy", "refs/tags/decoytag", "refs/stash",
                     "refs/other-backup/20200101_000000",
                     "refs/orchestrator-backup-sibling/20200101_000000",
                     f"{GIT_SNAPSHOT_REF_PREFIX}20990101_000000"):
        assert survivor in after, f"prune left its namespace: {survivor}"


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
