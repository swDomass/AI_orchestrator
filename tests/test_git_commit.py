"""Tests for ``git_commit.commit_run_result`` against REAL git repositories.

Per ``CLAUDE.md``'s testing conventions and the module's own precedent
(``tests/test_worktree_gate.py``): a mocked ``subprocess.run`` would only prove
that the code calls the functions it calls, and would miss exactly the failure
class this module exists to avoid -- real git index/worktree/ref behaviour
(rename entries in ``--porcelain -z``, ``checkout HEAD --`` semantics on a
foreign-staged path, ``update-ref`` collision detection, ...). Every scenario
below drives a real ``git init``-ed repo in ``tmp_path`` and verifies the
outcome with real ``git`` commands, not with assertions on mock call args.

Bugs found while writing these tests are reported in the final report, not
silently worked around or marked ``xfail`` here -- the tests pin the module's
REAL behaviour, per the task's hard rule.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import config
import git_commit
import orchestrator
import queue_manager

# ---------------------------------------------------------------------------
# Real git fixtures / helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo``, return raw stdout. Raises (via ``check=True``)
    on a non-zero exit -- every call here is setup or verification and MUST
    succeed. A deliberately-failing git call (e.g. the branch-collision setup)
    is issued via bare ``subprocess.run`` instead, not through this helper."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo), check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    return result.stdout


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    # Deterministic byte content regardless of the developer's global git
    # config: no CRLF rewriting on `checkout` (would corrupt the exact-content
    # assertions below) and no octal-escaping of non-ASCII paths in the plain
    # (non -z) verification commands used throughout this file.
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "core.quotepath", "false")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "repo")


@pytest.fixture(autouse=True)
def _git_auto_commit_enabled(monkeypatch):
    """Every scenario below exercises the feature turned ON, regardless of
    the developer's local .env (measured default True, but not guaranteed)."""
    monkeypatch.setattr(config, "GIT_AUTO_COMMIT", True)


# ===========================================================================
# 1. Normalfall
# ===========================================================================


def test_normal_run_commits_exactly_the_changed_and_new_paths(repo):
    branch_before = _git(repo, "branch", "--show-current").strip()
    head_sha = _git(repo, "rev-parse", "HEAD").strip()

    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    (repo / "new_file.txt").write_text("brand new\n", encoding="utf-8")
    snap_after = orchestrator._snapshot_dir(str(repo))

    outcome = git_commit.commit_run_result(
        str(repo), "Do the thing #id:normalrun", "claude+dev-loop", snap_before, snap_after,
    )

    assert outcome.error is None
    assert outcome.skipped is None
    assert outcome.branch is not None
    assert outcome.files == 2

    # Branch exists and contains exactly the two touched paths.
    committed = set(_git(repo, "diff", "--name-only", head_sha, outcome.branch).strip().splitlines())
    assert committed == {"README.md", "new_file.txt"}
    commit_count = _git(repo, "rev-list", "--count", f"{head_sha}..{outcome.branch}").strip()
    assert commit_count == "1"

    # Current branch is unmoved.
    branch_after = _git(repo, "branch", "--show-current").strip()
    assert branch_after == branch_before

    # Working tree is clean and restored to HEAD's content.
    assert _git(repo, "status", "--porcelain").strip() == ""
    assert (repo / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert not (repo / "new_file.txt").exists()


def test_lazy_snap_after_import_when_caller_omits_it(repo):
    """``snap_after=None`` makes the function import+call ``orchestrator._snapshot_dir``
    itself (module docstring, Ablauf Schritt 2) -- not a Pflicht-Szenario by
    number, but directly documented behaviour worth pinning once."""
    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "touched.txt").write_text("data\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(str(repo), "Task #id:lazysnap", "claude", snap_before)

    assert outcome.error is None
    assert outcome.branch is not None
    assert outcome.files == 1


# ===========================================================================
# 2. Fremde Datei bleibt
# ===========================================================================


def test_foreign_untracked_file_outside_the_path_set_is_left_alone(repo):
    # Present in BOTH snap_before and (implicitly, since untouched) snap_after
    # -- never a diff candidate, so it must never even be looked at.
    (repo / "foreign.txt").write_text("someone else's work\n", encoding="utf-8")
    head_sha = _git(repo, "rev-parse", "HEAD").strip()

    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "touched.txt").write_text("run output\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(str(repo), "Task #id:foreigntest", "codex", snap_before)

    assert outcome.error is None
    assert outcome.branch is not None
    assert outcome.files == 1

    committed = set(_git(repo, "diff", "--name-only", head_sha, outcome.branch).strip().splitlines())
    assert committed == {"touched.txt"}
    assert "foreign.txt" not in committed
    assert (repo / "foreign.txt").read_text(encoding="utf-8") == "someone else's work\n"
    assert "?? foreign.txt" in _git(repo, "status", "--porcelain")


# ===========================================================================
# 3. HEAD bewegt sich nie
# ===========================================================================


def test_head_and_symbolic_ref_never_move(repo):
    """The measured `nightstash` failure: HEAD ended up on a foreign branch."""
    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "file.txt").write_text("x\n", encoding="utf-8")

    head_sha_before = _git(repo, "rev-parse", "HEAD").strip()
    symbolic_before = _git(repo, "symbolic-ref", "HEAD").strip()

    outcome = git_commit.commit_run_result(str(repo), "Task #id:headcheck", "claude", snap_before)

    assert outcome.branch is not None  # sanity: a real commit actually happened

    head_sha_after = _git(repo, "rev-parse", "HEAD").strip()
    symbolic_after = _git(repo, "symbolic-ref", "HEAD").strip()

    assert head_sha_after == head_sha_before
    assert symbolic_after == symbolic_before


# ===========================================================================
# 4. Fremd gestagter Index (MM) -- the module's most important guarantee
# ===========================================================================


def test_foreign_staged_tracked_file_mm_is_skipped_index_and_worktree_untouched(repo):
    """The literal scenario named in the task: an ALREADY-TRACKED file (README.md,
    part of the initial commit) staged with one change, then edited again in the
    worktree without re-staging -- status code `MM` (X='M', Y='M'), the harshest
    case for `git checkout HEAD -- <path>` (module docstring's "empirical
    correction mid-build": that command discards BOTH the index and worktree
    content for a path whose index already differs from HEAD)."""
    head_sha = _git(repo, "rev-parse", "HEAD").strip()

    # Simulate: something (a prior `git add` -- the user, or the provider
    # itself, both allowed per module docstring) staged a change to README.md
    # BEFORE this run's snapshot baseline.
    (repo / "README.md").write_text("staged version\n", encoding="utf-8")
    _git(repo, "add", "README.md")

    snap_before = orchestrator._snapshot_dir(str(repo))

    # The run itself further edits the file on disk without re-staging, and
    # also makes an ordinary change so the commit isn't empty.
    (repo / "README.md").write_text("worktree version\n", encoding="utf-8")
    (repo / "clean.txt").write_text("normal change\n", encoding="utf-8")
    # Sanity: the status code is really MM at the moment commit_run_result sees it
    # (only after the SECOND, unstaged edit -- right after `git add` alone, Y is
    # still ' ' because index and worktree match). clean.txt is untracked ("??")
    # alongside it, so check the specific line rather than the whole output.
    assert "MM README.md" in _git(repo, "status", "--porcelain").splitlines()

    outcome = git_commit.commit_run_result(str(repo), "Task #id:foreignstagedmm", "claude", snap_before)

    assert outcome.error is None
    assert outcome.branch is not None
    assert outcome.files == 1  # only clean.txt

    committed = set(_git(repo, "diff", "--name-only", head_sha, outcome.branch).strip().splitlines())
    assert committed == {"clean.txt"}
    assert "README.md" not in committed

    # The staged (index) content is exactly what it was before this call.
    assert _git(repo, "show", ":README.md").strip() == "staged version"
    # The working-tree content is exactly what the run wrote -- neither
    # reverted to HEAD nor to the staged version.
    assert (repo / "README.md").read_text(encoding="utf-8") == "worktree version\n"


def test_foreign_staged_new_file_am_is_skipped_index_and_worktree_untouched(repo):
    """Same rule, different status code: a brand-NEW file staged (`A `), then
    further edited in the worktree without re-staging (`AM`) -- covers the
    'X not in (\" \", \"?\")' rule for X='A', not just X='M'."""
    head_sha = _git(repo, "rev-parse", "HEAD").strip()

    (repo / "mixed.txt").write_text("staged version\n", encoding="utf-8")
    _git(repo, "add", "mixed.txt")

    snap_before = orchestrator._snapshot_dir(str(repo))

    (repo / "mixed.txt").write_text("worktree version\n", encoding="utf-8")
    (repo / "clean.txt").write_text("normal change\n", encoding="utf-8")
    assert "AM mixed.txt" in _git(repo, "status", "--porcelain").splitlines()  # sanity: really AM, post-edit

    outcome = git_commit.commit_run_result(str(repo), "Task #id:foreignstagedam", "claude", snap_before)

    assert outcome.error is None
    assert outcome.branch is not None
    assert outcome.files == 1  # only clean.txt

    committed = set(_git(repo, "diff", "--name-only", head_sha, outcome.branch).strip().splitlines())
    assert committed == {"clean.txt"}
    assert "mixed.txt" not in committed

    assert _git(repo, "show", ":mixed.txt").strip() == "staged version"
    assert (repo / "mixed.txt").read_text(encoding="utf-8") == "worktree version\n"


# ===========================================================================
# 5. Gelöschte Datei
# ===========================================================================


def test_deleted_tracked_file_is_committed_then_restored_in_the_worktree(repo):
    (repo / "doomed.txt").write_text("will be deleted\n", encoding="utf-8")
    _git(repo, "add", "doomed.txt")
    _git(repo, "commit", "-q", "-m", "add doomed.txt")
    head_sha = _git(repo, "rev-parse", "HEAD").strip()

    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "doomed.txt").unlink()

    outcome = git_commit.commit_run_result(str(repo), "Task #id:deletetest", "claude", snap_before)

    assert outcome.error is None
    assert outcome.branch is not None
    assert outcome.files == 1

    assert _git(repo, "diff", "--name-status", head_sha, outcome.branch).strip() == "D\tdoomed.txt"

    # Working tree restored: the deletion lives in the branch, not the tree.
    assert (repo / "doomed.txt").exists()
    assert (repo / "doomed.txt").read_text(encoding="utf-8") == "will be deleted\n"
    assert _git(repo, "status", "--porcelain").strip() == ""


# ===========================================================================
# 6. Gitignorierte Datei
# ===========================================================================


def test_gitignored_candidate_never_enters_the_commit(repo):
    (repo / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "add gitignore")
    head_sha = _git(repo, "rev-parse", "HEAD").strip()

    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "ignored.log").write_text("noise\n", encoding="utf-8")
    (repo / "real.txt").write_text("real change\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(str(repo), "Task #id:ignoretest", "claude", snap_before)

    assert outcome.error is None
    assert outcome.branch is not None
    assert outcome.files == 1

    committed = set(_git(repo, "diff", "--name-only", head_sha, outcome.branch).strip().splitlines())
    assert committed == {"real.txt"}
    assert "ignored.log" not in committed
    assert (repo / "ignored.log").read_text(encoding="utf-8") == "noise\n"


# ===========================================================================
# 7. Umlaute und Leerzeichen im Dateinamen
# ===========================================================================


def test_filename_with_umlauts_and_spaces_is_committed_correctly(repo):
    """Measured: `--porcelain -z` delivers unquoted paths; without `-z` a
    non-ASCII name comes back octal-escaped (`core.quotepath`) and would
    silently fall out of the candidate/status intersection."""
    head_sha = _git(repo, "rev-parse", "HEAD").strip()
    name = "Prüfbericht Größe.txt"

    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / name).write_text("inhalt\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(str(repo), "Task #id:umlauttest", "claude", snap_before)

    assert outcome.error is None
    assert outcome.branch is not None
    assert outcome.files == 1

    committed = set(_git(repo, "diff", "--name-only", head_sha, outcome.branch).strip().splitlines())
    assert committed == {name}
    assert _git(repo, "show", f"{outcome.branch}:{name}").strip() == "inhalt"

    # Untracked before the commit -> cleaned up afterwards, same as any other
    # brand-new file (Normalfall).
    assert not (repo / name).exists()
    assert _git(repo, "status", "--porcelain").strip() == ""


# ===========================================================================
# 8. Alle skipped-Codes
# ===========================================================================


def test_skip_disabled(monkeypatch, repo):
    monkeypatch.setattr(config, "GIT_AUTO_COMMIT", False)
    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "x.txt").write_text("y", encoding="utf-8")

    outcome = git_commit.commit_run_result(str(repo), "Task", "claude", snap_before)

    assert outcome.skipped == "disabled"
    assert outcome.error is None
    assert outcome.branch is None


def test_skip_no_snapshot_without_cwd():
    outcome = git_commit.commit_run_result(None, "Task", "claude", {"a": (0.0, 1)})
    assert outcome.skipped == "no_snapshot"
    assert outcome.error is None


def test_skip_no_snapshot_without_snap_before(repo):
    outcome = git_commit.commit_run_result(str(repo), "Task", "claude", None)
    assert outcome.skipped == "no_snapshot"


def test_skip_not_a_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    outcome = git_commit.commit_run_result(str(plain), "Task", "claude", {"a": (0.0, 1)})
    assert outcome.skipped == "not_a_repo"


def test_skip_no_head_on_unborn_repo(tmp_path):
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git(unborn, "init", "-q")
    _git(unborn, "config", "user.email", "test@example.invalid")
    _git(unborn, "config", "user.name", "Test")

    outcome = git_commit.commit_run_result(str(unborn), "Task", "claude", {"a": (0.0, 1)})

    assert outcome.skipped == "no_head"


def test_skip_no_changes(repo):
    snap = orchestrator._snapshot_dir(str(repo))
    outcome = git_commit.commit_run_result(str(repo), "Task", "claude", snap, snap_after=snap)
    assert outcome.skipped == "no_changes"


def test_skip_too_many_files(repo):
    snap_before = orchestrator._snapshot_dir(str(repo))
    for i in range(config.GIT_COMMIT_MAX_FILES + 1):
        (repo / f"generated_{i}.txt").write_text("x", encoding="utf-8")
    snap_after = orchestrator._snapshot_dir(str(repo))

    outcome = git_commit.commit_run_result(str(repo), "Task", "claude", snap_before, snap_after)

    assert outcome.skipped == "too_many_files"
    assert outcome.error is None
    assert outcome.branch is None


# ===========================================================================
# 9. Branch-Kollision
# ===========================================================================


def test_second_run_same_task_same_day_gets_suffix_2(repo):
    snap_before1 = orchestrator._snapshot_dir(str(repo))
    (repo / "one.txt").write_text("first run\n", encoding="utf-8")
    outcome1 = git_commit.commit_run_result(str(repo), "Task #id:collide", "claude", snap_before1)
    assert outcome1.branch is not None

    snap_before2 = orchestrator._snapshot_dir(str(repo))
    (repo / "two.txt").write_text("second run\n", encoding="utf-8")
    outcome2 = git_commit.commit_run_result(str(repo), "Task #id:collide", "claude", snap_before2)

    assert outcome2.branch is not None
    assert outcome2.branch != outcome1.branch
    assert outcome2.branch == outcome1.branch + "_2"


def test_branch_collision_exhausted_leaves_worktree_and_index_untouched(monkeypatch, repo):
    monkeypatch.setattr(config, "GIT_COMMIT_BRANCH_MAX_ATTEMPTS", 2)

    task = "Task #id:exhaustcollide"
    slug = git_commit._task_slug(task)
    date = datetime.now().strftime("%Y-%m-%d")
    base = f"{config.GIT_COMMIT_BRANCH_PREFIX}{slug}-{date}"
    # Pre-occupy BOTH ref names the (patched) 2-attempt loop can try.
    _git(repo, "branch", base)
    _git(repo, "branch", f"{base}_2")

    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "touched.txt").write_text("data\n", encoding="utf-8")
    status_before = _git(repo, "status", "--porcelain")

    outcome = git_commit.commit_run_result(str(repo), task, "claude", snap_before)

    assert outcome.error == "branch_collision"
    assert outcome.branch is None
    assert outcome.sha is None
    assert outcome.files == 0

    # Nothing was deleted or reverted -- no branch means no cleanup step ran.
    assert (repo / "touched.txt").read_text(encoding="utf-8") == "data\n"
    assert _git(repo, "status", "--porcelain") == status_before


# ===========================================================================
# 10. Slug
# ===========================================================================


def test_slug_prefers_id_tag_over_hash():
    task = "Do the thing #id:myslug123"
    clean = queue_manager.strip_metadata_tags(task)
    hash_slug = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:8]

    slug = git_commit._task_slug(task)

    assert slug == "myslug123"
    assert slug != hash_slug


def test_slug_falls_back_to_hash_when_id_tag_sanitizes_to_empty():
    # "---" passes ID_TAG_RE (`-` is explicitly allowed in the value class)
    # but `_slugify` strips leading/trailing '-'/'.' and collapses repeats,
    # so a value that is ENTIRELY '-'/'.'/'_' sanitizes to "".
    task = "Do the thing #id:---"
    clean = queue_manager.strip_metadata_tags(task)
    expected_hash = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:8]

    assert queue_manager.extract_id_tag(task) == "---"  # the tag DID match
    assert git_commit._task_slug(task) == expected_hash


def test_slug_is_the_task_hash_prefix_without_an_id_tag():
    task = "Just do the thing, no id here"
    clean = queue_manager.strip_metadata_tags(task)
    expected_hash = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:8]

    slug = git_commit._task_slug(task)

    assert slug == expected_hash
    assert re.fullmatch(r"[0-9a-f]{8}", slug)


# ===========================================================================
# 11. commit_run_result wirft nie -- außer KeyboardInterrupt/SystemExit
# ===========================================================================


def test_never_raises_when_git_is_unresolvable(tmp_path, monkeypatch):
    """Patch `subprocess.run` itself to an OSError, simulating a git binary
    that cannot be found/executed at all. `_is_git_repo`'s own try/except
    absorbs it (same reasoning as every other precondition check) -- the
    call must still come back as a normal CommitOutcome, never an exception."""
    def _boom(*_args, **_kwargs):
        raise OSError("git executable not found")

    monkeypatch.setattr(subprocess, "run", _boom)

    outcome = git_commit.commit_run_result(str(tmp_path), "Task", "claude", {"a": (0.0, 1)})

    assert outcome.skipped == "not_a_repo"
    assert outcome.error is None


def test_never_raises_on_a_mid_flow_failure_and_reports_it_as_error(repo, monkeypatch):
    """Force a RuntimeError deep in the pipeline (Ablauf Schritt 6, well past
    every precondition and after real candidates were computed against the
    real repo) and confirm the outer `except BaseException` absorbs it."""
    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "x.txt").write_text("y", encoding="utf-8")

    def _boom(_cwd, _index_path):
        raise RuntimeError("boom")

    monkeypatch.setattr(git_commit, "_child_env", _boom)

    outcome = git_commit.commit_run_result(str(repo), "Task", "claude", snap_before)

    assert outcome.skipped is None
    assert outcome.branch is None
    assert outcome.error is not None
    assert "boom" in outcome.error


@pytest.mark.parametrize("exc_cls", [KeyboardInterrupt, SystemExit])
def test_keyboard_interrupt_and_system_exit_propagate(repo, monkeypatch, exc_cls):
    """The one explicit exception to 'never raises' -- see the module's own
    comment above its `except (KeyboardInterrupt, SystemExit): raise`."""
    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "x.txt").write_text("y", encoding="utf-8")

    def _boom(_cwd, _index_path):
        raise exc_cls()

    monkeypatch.setattr(git_commit, "_child_env", _boom)

    with pytest.raises(exc_cls):
        git_commit.commit_run_result(str(repo), "Task", "claude", snap_before)


# ===========================================================================
# 12. _parse_status_porcelain_z -- rename entry must not shift later entries
# ===========================================================================


def test_parse_status_porcelain_z_rename_does_not_shift_following_entries():
    """Hand-built raw bytes matching the real `git status --porcelain -z -uall`
    shape for a staged rename (verified empirically against a real repo:
    `XY PATH\\0ORIGIN_PATH\\0` -- the origin path is a SEPARATE, SUBSEQUENT
    NUL field, not embedded in the renamed entry's own field). A parser that
    fails to consume that second field would read `old_name.txt` as if it
    were the next entry's status/path and garbage out everything after it --
    here that would be `z.txt`."""
    raw = "M  a.txt\0R  new_name.txt\0old_name.txt\0A  z.txt\0"

    result = git_commit._parse_status_porcelain_z(raw)

    assert result == {"a.txt": "M ", "new_name.txt": "R ", "z.txt": "A "}
    assert "old_name.txt" not in result


# ===========================================================================
# 13. Git-Zustände, für die es KEINE Sonderbehandlung im Code gibt
#
# Beide Fälle fielen im externen Review an (Punkt 1 und 2 der Fragenliste) und
# wurden danach am echten Repo nachgemessen. Sie stehen hier, weil sie NICHT
# durch eigenen Code abgedeckt sind, sondern als Nebenwirkung der
# _TOUCHABLE_INDEX_X-Regel und der "HEAD wird nie bewegt"-Architektur richtig
# herauskommen. Genau deshalb sind sie fragil gegen eine spätere Vereinfachung:
# wer die Erlaubnisliste zu einer Verbotsliste macht oder auf einen
# checkout-basierten Ansatz umstellt, bricht beides, ohne es zu merken.
# ===========================================================================


def test_detached_head_commits_and_stays_detached(repo):
    """Ein detached HEAD bleibt detached, und der Commit gelingt trotzdem.

    HEAD wird per Konstruktion nie bewegt, deshalb gibt es hier nichts zu
    reparieren -- aber ohne Test wäre das eine Behauptung, kein Befund.
    """
    _git(repo, "checkout", "-q", "--detach")
    head_before = _git(repo, "rev-parse", "HEAD").strip()
    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "README.md").write_text("vom lauf\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(str(repo), "t #id:det", "claude", snap_before)

    assert outcome.branch == "orch/det-" + datetime.now().strftime("%Y-%m-%d")
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before, "HEAD darf sich nicht bewegen"
    # symbolic-ref schlägt fehl, solange HEAD detached ist -- also bare subprocess.
    sym = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert sym.returncode != 0, "HEAD muss detached geblieben sein"
    assert _git(repo, "status", "--porcelain").strip() == ""
    assert (repo / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_merge_conflict_in_progress_is_left_completely_alone(repo):
    """Ein laufender Merge mit Konflikt überlebt den Commit unbeschädigt.

    Der Konfliktpfad trägt Index-Status ``U`` und fällt damit AUTOMATISCH unter
    die _TOUCHABLE_INDEX_X-Regel -- es gibt keinen Merge-Sonderfall im Code.
    Die Arbeit des Laufs (eine neue, unbeteiligte Datei) wird trotzdem
    committet, statt den ganzen Lauf wegen des Merges zu verwerfen.
    """
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "README.md").write_text("feat\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "feat")
    _git(repo, "checkout", "-q", "master")
    (repo / "README.md").write_text("master\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "master")
    # Der Merge MUSS scheitern -- deshalb bare subprocess, nicht _git.
    subprocess.run(["git", "merge", "feat"], cwd=str(repo),
                   capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert (repo / ".git" / "MERGE_HEAD").exists(), "Setup: Merge muss laufen"
    assert _git(repo, "status", "--porcelain").strip() == "UU README.md"

    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "neu.txt").write_text("vom lauf\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(str(repo), "t #id:mrg", "claude", snap_before)

    assert outcome.branch is not None and outcome.files == 1
    assert "neu.txt" in _git(repo, "show", "--stat", "--format=", outcome.branch)
    assert "README.md" not in _git(repo, "show", "--stat", "--format=", outcome.branch)
    assert (repo / ".git" / "MERGE_HEAD").exists(), "der laufende Merge muss intakt bleiben"
    assert _git(repo, "status", "--porcelain").strip() == "UU README.md"


def test_pathspec_chunks_are_bounded_by_bytes_and_by_count(repo):
    """Beide Schranken des Chunkers greifen, jede für sich.

    Der Byte-Deckel ist der tragende: 100 Pfade nahe MAX_PATH kämen auf ~26000
    Zeichen und damit gefährlich nah an das CreateProcess-Limit von 32767 --
    aufgefallen im externen Review, wo die Blockgröße noch fix bei 100 lag.
    """
    long_paths = ["x" * 250 + str(i) for i in range(50)]
    chunks = git_commit._pathspec_chunks(long_paths)
    assert sum(len(c) for c in chunks) == len(long_paths), "kein Pfad darf verlorengehen"
    for chunk in chunks:
        assert sum(len(p.encode("utf-8")) + 1 for p in chunk) <= config.GIT_COMMIT_ARGV_BUDGET_BYTES

    short_paths = [f"a{i}.txt" for i in range(1000)]
    chunks = git_commit._pathspec_chunks(short_paths)
    assert sum(len(c) for c in chunks) == len(short_paths)
    assert max(len(c) for c in chunks) <= config.GIT_COMMIT_PATHS_PER_CALL

    assert git_commit._pathspec_chunks([]) == []
    # Ein einzelner Pfad ÜBER dem Budget darf nicht verschwinden -- lieber ein
    # zu langer Aufruf (den git dann meldet) als ein still ausgelassener Pfad.
    huge = ["y" * (config.GIT_COMMIT_ARGV_BUDGET_BYTES + 500)]
    assert git_commit._pathspec_chunks(huge) == [huge]


# ===========================================================================
# 14. Befunde aus dem externen Review (Codex + Mistral), jeder mit Gegenprobe
#
# Jeder Test hier hält einen Defekt fest, der VOR dem Review real war und am
# echten Repo reproduziert wurde. Ohne diese Tests wären die Fixes Behauptungen.
# ===========================================================================


def test_pathspec_metacharacters_cannot_reach_a_foreign_file(repo):
    """``--`` literalisiert Pathspecs NICHT -- git liest sie als Glob-Muster.

    Gemessen vor dem Fix: mit einer Datei ``config[.]py`` im Baum stagte
    ``git add -A -- 'config[.]py'`` zusätzlich die fremde ``config.py``
    (Index-Spalte ``M``), deren X-Spalte nie geprüft wurde. Das Cleanup hätte sie
    danach auf HEAD zurückgesetzt -- fremde uncommittete Arbeit vernichtet und
    ihr Inhalt in unserem Branch. ``:(literal)`` schließt das.
    """
    (repo / "config.py").write_text("original\n", encoding="utf-8")
    _git(repo, "add", "config.py")
    _git(repo, "commit", "-q", "-m", "config")
    (repo / "config.py").write_text("FREMDE UNCOMMITTETE ARBEIT\n", encoding="utf-8")

    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "config[.]py").write_text("vom lauf\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(str(repo), "t #id:glb", "claude", snap_before)

    assert outcome.branch is not None and outcome.files == 1
    stat = _git(repo, "show", "--stat", "--format=", outcome.branch)
    assert "config[.]py" in stat
    assert "config.py |" not in stat, "die fremde Datei darf nicht im Commit sein"
    assert (repo / "config.py").read_text(encoding="utf-8") == "FREMDE UNCOMMITTETE ARBEIT\n"


def test_cwd_below_the_repo_root_still_commits(repo):
    """``git status`` liefert Root-relative Pfade, ``_snapshot_dir`` cwd-relative.

    Vor dem Fix war die Schnittmenge bei einem ``cwd:`` unterhalb des Repo-Roots
    immer leer -- das Feature war dort still tot, und der Baum blieb schmutzig.
    ``git rev-parse --show-prefix`` überbrückt die Differenz.
    """
    (repo / "sub").mkdir()
    (repo / "sub" / "a.txt").write_text("v1\n", encoding="utf-8")
    (repo / "root.txt").write_text("root\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "sub")
    # Fremde Arbeit AUSSERHALB des cwd -- darf nicht angefasst werden.
    (repo / "root.txt").write_text("FREMD\n", encoding="utf-8")

    sub = repo / "sub"
    snap_before = orchestrator._snapshot_dir(str(sub))
    (sub / "a.txt").write_text("v2\n", encoding="utf-8")
    (sub / "neu.txt").write_text("neu\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(str(sub), "t #id:sub", "claude", snap_before)

    assert outcome.branch is not None and outcome.files == 2, outcome
    stat = _git(repo, "show", "--stat", "--format=", outcome.branch)
    assert "sub/a.txt" in stat and "sub/neu.txt" in stat
    assert (sub / "a.txt").read_text(encoding="utf-8") == "v1\n"
    assert not (sub / "neu.txt").exists()
    assert (repo / "root.txt").read_text(encoding="utf-8") == "FREMD\n"
    assert _git(repo, "status", "--porcelain").strip() == "M root.txt"


def test_orchestrator_state_files_are_never_committed(repo, monkeypatch):
    """Die Queue-Datei ist nie Task-Arbeit, auch wenn sie im ``cwd`` liegt.

    ``finalize_task_with_result`` schreibt den Erfolgs-Stempel VOR diesem Modul
    (eine GUARDRAIL des Auftrags), er steckt also in ``snap_after``. Würde der
    Commit ihn mitnehmen und den Pfad danach auf HEAD zurücksetzen, wäre die
    Finalisierung im Arbeitsbaum rückgängig gemacht: die Zeile erschiene wieder
    offen und der Task liefe bei jedem Poll erneut -- still, unbeaufsichtigt,
    endlos.

    Heute nicht erreichbar (der Vault ist kein Git-Repo, gemessen 2026-09-10),
    aber ein ``git init`` dort würde reichen. Aus dem externen Review.
    """
    queue = repo / "99_System" / "AI" / "agent-queue.md"
    queue.parent.mkdir(parents=True)
    queue.write_text("- [ ] task\n", encoding="utf-8")
    (repo / "code.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "with queue")
    monkeypatch.setattr(queue_manager, "QUEUE_FILE", queue)

    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "code.py").write_text("v2 vom lauf\n", encoding="utf-8")
    queue.write_text("- [x] task erledigt 2026-09-10 03:00 (claude)\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(str(repo), "t #id:q", "claude", snap_before)

    assert outcome.branch is not None and outcome.files == 1
    stat = _git(repo, "show", "--stat", "--format=", outcome.branch)
    assert "code.py" in stat
    assert "agent-queue.md" not in stat, "die Queue-Datei darf nie im Commit landen"
    # Und der Stempel muss im Arbeitsbaum stehen bleiben.
    assert "erledigt" in queue.read_text(encoding="utf-8")


def test_a_staged_rename_is_left_alone(repo):
    """Rename/Copy in IRGENDEINER Statusspalte macht den Pfad unanfassbar.

    Der Parser verwirft den Ursprungspfad (er konsumiert das Feld nur, damit die
    Folgeeinträge nicht verrutschen). Würden wir nur das Ziel committen, stünden
    im Branch der alte UND der neue Name.
    """
    (repo / "alt.txt").write_text("inhalt\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "alt")
    _git(repo, "mv", "alt.txt", "neu.txt")           # gestagter Rename
    assert "R" in _git(repo, "status", "--porcelain")

    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "neu.txt").write_text("vom lauf\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(str(repo), "t #id:rn", "claude", snap_before)

    assert outcome.branch is None, outcome
    assert "R" in _git(repo, "status", "--porcelain"), "der Rename muss unangetastet bleiben"
    assert (repo / "neu.txt").read_text(encoding="utf-8") == "vom lauf\n"


def test_partial_commit_reports_how_many_paths_were_left_behind(repo):
    """Ein Commit kann erfolgreich UND unvollständig sein.

    Ohne ``skipped_paths`` sieht der Aufrufer nur den grünen Branch und meldet
    morgens eine Vollständigkeit, die es nicht gibt -- obwohl im Baum Arbeit
    zurückblieb, an der der nächste dev-loop im selben Repo stirbt.
    """
    (repo / "sauber.txt").write_text("v1\n", encoding="utf-8")
    (repo / "gestaged.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "beide")
    (repo / "gestaged.txt").write_text("vom user gestaged\n", encoding="utf-8")
    _git(repo, "add", "gestaged.txt")

    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "sauber.txt").write_text("v2\n", encoding="utf-8")
    (repo / "gestaged.txt").write_text("vom user gestaged\nund vom lauf\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(str(repo), "t #id:part", "claude", snap_before)

    assert outcome.branch is not None
    assert outcome.files == 1, "nur der saubere Pfad"
    assert outcome.skipped_paths == 1, "der gestagte muss gezählt gemeldet werden"
    assert _git(repo, "show", ":gestaged.txt") == "vom user gestaged\n"


def test_branch_survives_an_exception_after_update_ref(repo, monkeypatch):
    """Fliegt nach dem ``update-ref`` etwas, darf der Branch nicht aus dem
    Rückgabewert verschwinden -- sonst meldet der Lauf einen Fehlschlag, während
    der Branch existiert, und der Morgen sucht etwas, das da ist."""
    (repo / "code.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "code.py").write_text("v2\n", encoding="utf-8")

    real_chunks = git_commit._pathspec_chunks
    state = {"calls": 0}

    def boom(paths):
        state["calls"] += 1
        if state["calls"] >= 2:          # 1. Aufruf = add, 2. = cleanup
            raise RuntimeError("kaputt nach update-ref")
        return real_chunks(paths)

    monkeypatch.setattr(git_commit, "_pathspec_chunks", boom)
    outcome = git_commit.commit_run_result(str(repo), "t #id:exc", "claude", snap_before)

    assert outcome.error is not None and "kaputt" in outcome.error
    assert outcome.branch is not None, "der angelegte Branch muss gemeldet werden"
    assert _git(repo, "branch", "--list", outcome.branch).strip() != ""


# ===========================================================================
# 15. Befunde der dritten externen Stimme (opencode)
# ===========================================================================


def test_commit_subject_respects_its_own_length_cap(repo):
    """Das ``orch: ``-Präfix zählt bei der Kappung MIT.

    Vorher kappte ``_truncate`` den Task-Text auf GIT_COMMIT_SUBJECT_MAX_LEN und
    stellte das Präfix DANACH voran -- die fertige Betreffzeile war also um genau
    dessen Länge zu lang, und der Kommentar an der Konstante ("a longer subject
    just wraps ugly") beschrieb den eigenen Code.
    """
    long_task = "Implementiere " + "sehr lange Beschreibung " * 10
    subject = git_commit._build_message(long_task, "claude", "slug").splitlines()[0]

    assert len(subject) <= config.GIT_COMMIT_SUBJECT_MAX_LEN, subject
    assert subject.startswith("orch: ")
    # Und eine kurze Zeile wird NICHT gekappt.
    short = git_commit._build_message("Kurz", "claude", "slug").splitlines()[0]
    assert short == "orch: Kurz"


def test_a_path_rewritten_during_the_commit_is_left_in_the_tree(repo, monkeypatch):
    """Der Guard vor dem Cleanup vergleicht INHALT, nicht nur den Statuscode.

    Die erste Fassung verglich ausschliesslich das porcelain-XY -- und eine zweite
    Session, die eine ohnehin schon geaenderte Datei WEITER schreibt, laesst XY auf
    `` M`` stehen, waehrend sich die Bytes aendern. Der Guard sah keine Aenderung,
    das Cleanup setzte auf HEAD zurueck, und der fremde Inhalt war weg -- genau die
    Datenverlust-Klasse, die dieser Guard schliessen sollte.

    Der Test dazu war in derselben Fassung wertlos: er stellte den Statuscode um
    und prüfte damit den Fall, den der Code ohnehin abdeckte, statt den, den die
    Behauptung verspricht. Beides im externen Review gefunden (Grok).

    Hier schreibt der Mock waehrend des Laufs echte Bytes in die Datei, ohne den
    Statuscode zu veraendern.
    """
    (repo / "code.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "code.py").write_text("v2 vom lauf\n", encoding="utf-8")

    real_status = git_commit._git_status_map
    calls = {"n": 0}

    def status_and_a_foreign_write(cwd):
        calls["n"] += 1
        if calls["n"] >= 2:
            # Zweite Aufnahme = direkt vor dem Cleanup. Eine fremde Session
            # schreibt weiter. Der Statuscode bleibt dabei ` M`.
            (repo / "code.py").write_text(
                "v2 vom lauf\nund noch etwas von jemand anderem\n", encoding="utf-8"
            )
        return real_status(cwd)

    monkeypatch.setattr(git_commit, "_git_status_map", status_and_a_foreign_write)
    outcome = git_commit.commit_run_result(str(repo), "t #id:race", "claude", snap_before)

    assert calls["n"] >= 2, "der Cleanup muss den Zustand erneut pruefen"
    assert outcome.branch is not None, "der Commit selbst gelingt weiterhin"
    # rstrip nur am Zeilenende — ein .strip() fraesse das fuehrende Leerzeichen des
    # Statuscodes weg, und genau dieses Leerzeichen IST die Aussage (Index sauber,
    # nur der Arbeitsbaum geaendert).
    assert _git(repo, "status", "--porcelain").rstrip("\r\n") == " M code.py", \
        "Gegenprobe: der Statuscode hat sich NICHT veraendert"
    assert (repo / "code.py").read_text(encoding="utf-8") == \
        "v2 vom lauf\nund noch etwas von jemand anderem\n", \
        "der fremde Inhalt darf NICHT vom Cleanup ueberschrieben werden"
    # Und der Commit traegt weiterhin den Stand, den der Lauf erzeugt hat.
    assert _git(repo, "show", f"{outcome.branch}:code.py") == "v2 vom lauf\n"


def test_a_path_staged_during_the_commit_is_left_in_the_tree(repo, monkeypatch):
    """Die zweite Haelfte desselben Guards: jemand STAGED einen unserer Pfade.

    Ohne den Statuscode-Vergleich wuerde `git checkout` dessen Index-Stand
    mitreissen -- dieselbe Fehlerklasse wie beim `MM`-Fall vor dem Lauf, nur
    zeitlich verschoben.
    """
    (repo / "code.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "code.py").write_text("v2 vom lauf\n", encoding="utf-8")

    real_status = git_commit._git_status_map
    calls = {"n": 0}

    def status_and_a_foreign_stage(cwd):
        calls["n"] += 1
        result = real_status(cwd)
        if calls["n"] >= 2:
            result["code.py"] = "M "     # jemand hat inzwischen gestaged
        return result

    monkeypatch.setattr(git_commit, "_git_status_map", status_and_a_foreign_stage)
    outcome = git_commit.commit_run_result(str(repo), "t #id:stg", "claude", snap_before)

    assert outcome.branch is not None
    assert (repo / "code.py").read_text(encoding="utf-8") == "v2 vom lauf\n", \
        "der Pfad darf nicht zurueckgesetzt werden"


def test_a_failed_status_recheck_skips_cleanup_entirely(repo, monkeypatch):
    """FAIL-CLOSED: ohne frischen Zustand wird gar nichts angefasst.

    Die erste Fassung setzte bei einem Fehler `status_now = None` und lief danach
    ueber ALLE Pfade -- also fail-OPEN genau in dem Moment, in dem unbekannt ist,
    ob die Pfade noch die sind, die gemessen wurden. Aus dem externen Review (Grok).
    """
    (repo / "code.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "code.py").write_text("v2 vom lauf\n", encoding="utf-8")

    real_status = git_commit._git_status_map
    calls = {"n": 0}

    def status_then_boom(cwd):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("git status kaputt")
        return real_status(cwd)

    monkeypatch.setattr(git_commit, "_git_status_map", status_then_boom)
    outcome = git_commit.commit_run_result(str(repo), "t #id:fc", "claude", snap_before)

    assert outcome.branch is not None, "der Commit steht trotzdem"
    assert outcome.error is not None and "status_recheck_failed" in outcome.error
    assert (repo / "code.py").read_text(encoding="utf-8") == "v2 vom lauf\n", \
        "ohne frischen Zustand wird der Baum NICHT angefasst"


def test_files_counts_the_commit_not_the_leftovers(repo, monkeypatch):
    """`files` beschreibt den COMMIT, auch wenn der Cleanup Pfade auslaesst.

    Sonst meldet die Morgenmeldung weniger Dateien, als `git show` zeigt.
    """
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    (repo / "b.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    (repo / "b.txt").write_text("v2\n", encoding="utf-8")

    real_status = git_commit._git_status_map
    calls = {"n": 0}

    def status_and_a_foreign_write(cwd):
        calls["n"] += 1
        if calls["n"] >= 2:
            (repo / "b.txt").write_text("v2\nfremd\n", encoding="utf-8")
        return real_status(cwd)

    monkeypatch.setattr(git_commit, "_git_status_map", status_and_a_foreign_write)
    outcome = git_commit.commit_run_result(str(repo), "t #id:cnt", "claude", snap_before)

    stat = _git(repo, "show", "--stat", "--format=", outcome.branch)
    assert "a.txt" in stat and "b.txt" in stat
    assert outcome.files == 2, "beide Dateien stecken im Commit, auch wenn b.txt liegen blieb"
    assert (repo / "b.txt").read_text(encoding="utf-8") == "v2\nfremd\n"

def test_identity_fallback_needs_both_name_and_email(repo, monkeypatch):
    """Der Fallback greift, sobald EINES von beiden fehlt.

    ``commit-tree`` braucht Name UND Mail. Vorher wurde nur ``user.email``
    geprüft: ein Repo mit gesetzter Mail, aber ohne Namen (weder lokal noch
    global) hätte den Fallback nicht ausgelöst und wäre an git's eigener
    Identity-Fehlermeldung gestorben -- Task erfolgreich, Commit unterblieben.
    """
    real_run = subprocess.run

    def only_email_configured(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "config", "user.name"]:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(git_commit.subprocess, "run", only_email_configured)
    env = git_commit._child_env(str(repo), str(repo / "idx"))

    assert env["GIT_AUTHOR_NAME"] == config.GIT_COMMIT_FALLBACK_AUTHOR_NAME
    assert env["GIT_COMMITTER_EMAIL"] == config.GIT_COMMIT_FALLBACK_AUTHOR_EMAIL
    # os.environ selbst wird dabei nie mutiert (geteilte Singletons in Threads).
    assert "GIT_INDEX_FILE" not in os.environ


# ===========================================================================
# 16. Befunde der adversarialen Runde — die zwei P1
# ===========================================================================


def test_unstaged_foreign_work_from_before_the_run_survives(repo):
    """Der teuerste Befund des ganzen Pakets, als Test.

    Die Zuordnung "was hat der Lauf geändert" ist ein ``(mtime, size)``-Vergleich,
    und dessen Fenster ist die **gesamte Laufzeit** — Minuten bis Stunden bei einem
    dev-loop, und ``--watch`` läuft, während der User an derselben Maschine
    arbeitet. Ohne Baseline liest sich jede Handbearbeitung in diesem Fenster als
    Arbeit des Laufs: sie wird unter unserer Commit-Message committet **und im
    Arbeitsbaum auf HEAD zurückgesetzt**. Im adversarialen Review am echten Repo
    gemessen: die Live-Bearbeitung war von der Platte weg und ``git status``
    meldete danach "sauber".

    Die Index-Regel (`_TOUCHABLE_INDEX_X`) fängt das NICHT — sie prüft, ob etwas
    **gestaged** ist, nicht wer es geschrieben hat, und niemand staged mitten im
    Tippen. Deshalb die zweite, unabhängige Regel: was vor dem Lauf schon schmutzig
    war, wird weder committet noch aufgeräumt.
    """
    (repo / "work.txt").write_text("V1\n", encoding="utf-8")
    (repo / "userfile.txt").write_text("USER V1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "beide")

    # Der Mensch tippt — VOR dem Lauf, nie gestaged.
    (repo / "userfile.txt").write_text("USER V2 - live edit, never staged\n", encoding="utf-8")
    dirty_before = git_commit.dirty_paths_snapshot(str(repo))
    assert dirty_before == frozenset({"userfile.txt"}), dirty_before

    snap_before = orchestrator._snapshot_dir(str(repo))
    # Der Lauf schreibt seine eigene Datei — und tippt der Mensch weiter, sieht auch
    # das nach (mtime,size) wie Arbeit des Laufs aus.
    (repo / "work.txt").write_text("V2 vom lauf\n", encoding="utf-8")
    (repo / "userfile.txt").write_text("USER V3 - noch mehr Handarbeit\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(
        str(repo), "t #id:probea", "claude", snap_before, dirty_before=dirty_before,
    )

    assert outcome.branch is not None
    assert outcome.files == 1, "nur die Datei des Laufs"
    stat = _git(repo, "show", "--stat", "--format=", outcome.branch)
    assert "work.txt" in stat
    assert "userfile.txt" not in stat, "fremde Handarbeit gehoert nicht in unseren Commit"
    assert (repo / "userfile.txt").read_text(encoding="utf-8") == \
        "USER V3 - noch mehr Handarbeit\n", "und schon gar nicht von der Platte geloescht"
    assert outcome.skipped_paths == 1, "der ausgelassene Pfad wird gemeldet"
    # Die eigene Arbeit ist trotzdem sauber abgelegt.
    assert (repo / "work.txt").read_text(encoding="utf-8") == "V1\n"


def test_no_dirty_baseline_means_no_commit_at_all(repo):
    """FAIL-CLOSED: ohne Baseline ist die Zuordnung unbelegbar, also passiert nichts.

    ``None`` heißt "nicht ermittelbar" und ist ausdrücklich NICHT dasselbe wie
    "nichts war schmutzig" — die Verwechslung wäre genau der Weg zurück in den
    Datenverlust oben.
    """
    (repo / "code.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    snap_before = orchestrator._snapshot_dir(str(repo))
    (repo / "code.py").write_text("v2\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(
        str(repo), "t #id:nb", "claude", snap_before, dirty_before=None,
    )

    assert outcome.branch is None
    assert outcome.skipped == "no_dirty_baseline"
    assert (repo / "code.py").read_text(encoding="utf-8") == "v2\n", "nichts angefasst"
    assert _git(repo, "branch", "--list", "orch/*").strip() == ""


def test_the_file_cap_counts_committable_paths_not_ignored_noise(repo):
    """Der Deckel misst die Pfade, die WIRKLICH committet würden.

    Vorher stand er vor dem git-Sichtbarkeitsfilter und zählte jede gitignorierte
    Datei mit, die der Lauf nebenbei angefasst hat. Gemessen im Repo, für das
    dieses Feature gebaut wurde: 5011 gitignorierte Dateien, 496 davon in 24 h
    verändert — ein 3-h-dev-loop, der pytest + ruff + mypy fährt, hätte den Deckel
    von 200 regelmäßig gerissen und das Feature **still** abgeschaltet.
    """
    (repo / ".gitignore").write_text("cache/\n", encoding="utf-8")
    (repo / "src.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")

    snap_before = orchestrator._snapshot_dir(str(repo))
    cache = repo / "cache"
    cache.mkdir()
    for i in range(config.GIT_COMMIT_MAX_FILES + 50):     # weit über dem Deckel
        (cache / f"n{i}.pyc").write_text("x", encoding="utf-8")
    (repo / "src.py").write_text("v2 echte arbeit\n", encoding="utf-8")

    outcome = git_commit.commit_run_result(
        str(repo), "t #id:cap", "claude", snap_before, dirty_before=frozenset(),
    )

    assert outcome.skipped != "too_many_files", "Rauschen darf den Deckel nicht reissen"
    assert outcome.branch is not None and outcome.files == 1
    assert "src.py" in _git(repo, "show", "--stat", "--format=", outcome.branch)
    assert (repo / "src.py").read_text(encoding="utf-8") == "v1\n"
    # Gegenprobe: ECHTE Dateien über dem Deckel greifen weiterhin.
    snap2 = orchestrator._snapshot_dir(str(repo))
    for i in range(config.GIT_COMMIT_MAX_FILES + 5):
        (repo / f"echt{i}.txt").write_text("x", encoding="utf-8")
    out2 = git_commit.commit_run_result(
        str(repo), "t #id:cap2", "claude", snap2, dirty_before=frozenset(),
    )
    assert out2.skipped == "too_many_files", out2


def test_a_configured_identity_is_not_overridden(repo, monkeypatch):
    """Gegenprobe zum Identity-Fallback: ist beides gesetzt, wird nichts gesetzt.

    Der Docstring verspricht "an already-configured identity is respected, never
    overridden" — von keinem Test gehalten, bis der adversariale Review es
    bemerkte: eine Mutation, die die Fallback-Variablen IMMER setzt, blieb grün.
    """
    env = git_commit._child_env(str(repo), str(repo / "idx"))

    assert "GIT_AUTHOR_NAME" not in env, "die Repo-Identitaet muss gewinnen"
    assert "GIT_COMMITTER_EMAIL" not in env
    assert env["GIT_INDEX_FILE"] == str(repo / "idx")
