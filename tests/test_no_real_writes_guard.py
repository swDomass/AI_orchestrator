"""Proves the KERN-2 test isolation is not just present but load-bearing.

Betriebsprüfung 2026-09-16, finding #2: several tests drove the real
``orchestrator.run_once()`` success path without mocking ``memory_module.
store_result``, and two tools default an unset ``cwd`` to the PROCESS cwd
(``Path(".")``) — both landed real files in the real vault / this repo's own
``docs/``. Runde 1 patched `memory.py`'s derived path constants by hand and
missed `_LESSONS_FILE` (`memory.py:906`); code review (Runde 2) traced this to
a measured, live incident (2026-09-16 14:19:33, live orchestrator idle,
`archive_old_memories()` moved a real file — see `tests/conftest.py`'s module
docstring for the file:line evidence). Runde 2 redirects `config.VAULT_PATH`
itself at the root (module level, before `memory` is ever imported) instead of
enumerating derived constants, plus an operation-level hard refusal in
`memory.py` as a second, independent layer. ``tests/conftest.py``'s
``_isolate_memory_and_docs_output`` (process cwd) and
``_guard_real_vault_and_docs_untouched`` (session-wide count + hash check)
close the rest. This file does not re-test THOSE mechanisms' plumbing — it
tests that they actually change behaviour, per the "Guard vorhanden ≠ Guard
wirksam" lesson: a guard that only proves it EXISTS (e.g. grepping for the
fixture name) can still be inert. Every test below creates the violation the
guard is meant to catch and shows the catch happening.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    import config
    import memory as memory_module

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. Direct proof that the (session-scoped) fixture actually redirects (not
#    just that it runs without error). Checked against "not the real thing"
#    rather than a specific tmp_path, because the fixture is session-scoped —
#    see its docstring in conftest.py for why a per-test scope left a gap.
# ---------------------------------------------------------------------------

def test_memory_paths_point_away_from_the_real_vault(real_vault_path):
    """The module-level root redirect in conftest.py runs before `memory` is ever
    imported, so by the time THIS test body runs, memory.py's module constants
    must already differ from what they would be under the real vault. Compares
    against `real_vault_path` (the pre-redirect value), NOT `config.VAULT_PATH` —
    that attribute IS the redirected one by now, so comparing against itself would
    trivially "pass" without proving anything."""
    real_task_results = real_vault_path / "99_System" / "AI" / "memory" / "task_results"
    real_daily = real_vault_path / "99_System" / "AI" / "memory" / "daily"
    assert real_task_results != memory_module._TASK_RESULTS_DIR
    assert real_daily != memory_module._DAILY_DIR
    assert not memory_module._TASK_RESULTS_DIR.is_relative_to(real_vault_path)
    assert config.VAULT_PATH != real_vault_path, (
        "sanity: config.VAULT_PATH must itself be the redirected value here"
    )


def test_process_cwd_is_redirected_away_from_the_repo_root():
    """The autouse fixture's `os.chdir` — this is what makes `Path(".")` (the
    fallback every leaking tool uses) land somewhere harmless instead of the
    repo root, for the whole session."""
    assert Path(".").resolve() != _REPO_ROOT


# ---------------------------------------------------------------------------
# 1b. The hard safety net in memory.py itself — a second, independent layer
# alongside the root redirect. Measured 2026-09-16 14:19:33 (live orchestrator
# idle): `archive_old_memories()` (`orchestrator.py:2025`) moved a real vault
# file during a test run, traced by code review to `_LESSONS_FILE` (`memory.py:
# 906`) never having been redirected by the Runde-1 fixture, and its writer
# `_cleanup_lessons()` (`memory.py:1258`) never calling `_ensure_dirs()`.
# `memory._refuse_real_vault_writes_under_pytest(path)` is now called at every
# write/move/delete this module performs (not only inside `_ensure_dirs()`),
# checked against an independent env-var hint rather than memory.py's own
# (already-redirected) constants — see that function's docstring for why a
# self-referential check would be a tautology.
# ---------------------------------------------------------------------------

def test_memory_refuses_to_write_into_the_real_vault_even_if_isolation_is_bypassed(
    monkeypatch, real_vault_path,
):
    """Simulates exactly the failure mode the root redirect exists to prevent: the
    path constants point at the real vault WHILE pytest is running
    (PYTEST_CURRENT_TEST is set automatically for the duration of this very test).
    _ensure_dirs() must refuse rather than create real directories."""
    real_root = real_vault_path / "99_System" / "AI" / "memory"
    monkeypatch.setattr(memory_module, "_MEMORY_ROOT", real_root)
    monkeypatch.setattr(memory_module, "_TASK_RESULTS_DIR", real_root / "task_results")
    monkeypatch.setattr(memory_module, "_ARCHIVE_DIR", real_root / "archive")
    monkeypatch.setattr(memory_module, "_DAILY_DIR", real_root / "daily")
    monkeypatch.setattr(memory_module, "_CURATED_MEMORY_FILE", real_root / "MEMORY.md")

    assert os.environ.get("PYTEST_CURRENT_TEST"), "precondition: pytest sets this during a test"
    with pytest.raises(RuntimeError, match="refuses to write into the real vault"):
        memory_module._ensure_dirs()


def test_memory_refuses_a_subdirectory_of_the_real_vault_too(monkeypatch, real_vault_path):
    """Gegenprobe on the boundary check: not just an exact match on _MEMORY_ROOT —
    anything resolving INSIDE the real vault's memory tree must be refused."""
    real_subdir = real_vault_path / "99_System" / "AI" / "memory" / "some_future_subdir"
    monkeypatch.setattr(memory_module, "_MEMORY_ROOT", real_subdir)
    monkeypatch.setattr(memory_module, "_TASK_RESULTS_DIR", real_subdir / "task_results")
    monkeypatch.setattr(memory_module, "_ARCHIVE_DIR", real_subdir / "archive")
    monkeypatch.setattr(memory_module, "_DAILY_DIR", real_subdir / "daily")
    monkeypatch.setattr(memory_module, "_CURATED_MEMORY_FILE", real_subdir / "MEMORY.md")

    with pytest.raises(RuntimeError, match="refuses to write into the real vault"):
        memory_module._ensure_dirs()


def test_memory_does_not_refuse_a_harmless_path():
    """Gegenprobe: the check is specific to the real vault, not a blanket refusal —
    the session fixture's own (harmless) redirection must keep working normally."""
    memory_module._ensure_dirs()  # must not raise
    assert memory_module._TASK_RESULTS_DIR.exists()


# ---------------------------------------------------------------------------
# 2. Root-cause proof: with the isolation fixture's chdir undone FOR JUST THIS
#    TEST (simulating "no isolation" surgically, no subprocess needed), the
#    exact tool this whole fix is about really does write into the process
#    cwd's docs/ — proving the leak mechanism, not just asserting a path.
# ---------------------------------------------------------------------------

class _ScriptedProvider:
    """Minimal stand-in, same shape as tests/test_critical_review.py's."""

    def __init__(self, name: str, outputs: list[str]):
        self.name = name
        self._outputs = list(outputs)

    def run(self, task, cwd=None, timeout=0, read_only=False, **kwargs):
        from providers.base import RunResult
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))


def test_tool_with_no_cwd_writes_into_whatever_the_process_cwd_is(tmp_path, monkeypatch):
    """Undoes the autouse chdir ON PURPOSE, inside a throwaway tmp_path (never the
    repo root), then proves `CriticalReviewTool.run(..., cwd=None)` really does write
    relative to the process cwd rather than somewhere fixed — this is the exact
    fallback (`Path(".")`) that leaked into the live repo's docs/ 4491 times."""
    from tools.critical_review import CriticalReviewTool

    # Same as test_critical_review.py's `_patch` fixture: the tool checks live provider
    # availability before running, which otherwise depends on ambient limits/quota
    # state left over from whichever tests happened to run earlier (order-dependent,
    # caught here by the -p randomly seed matrix).
    monkeypatch.setattr("tools.critical_review.is_cached_provider_available", lambda _n: True)
    monkeypatch.setattr("tools.critical_review.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.critical_review.notify_tool_progress", lambda *a, **kw: None)

    repo_docs = _REPO_ROOT / "docs"
    before = sum(1 for p in repo_docs.rglob("*") if p.is_file())

    surrogate_cwd = tmp_path / "surrogate_process_cwd"
    surrogate_cwd.mkdir()
    monkeypatch.chdir(surrogate_cwd)

    provider = _ScriptedProvider("claude", ["pass 1 output", "pass 2 output"])
    result = CriticalReviewTool().run("Review", provider)  # no cwd= at all

    assert result.success is True
    assert (surrogate_cwd / "docs").is_dir(), (
        "expected the tool to have created docs/ relative to the process cwd "
        "when no cwd= is given — this is the exact mechanism the repo's docs/ "
        "leak came from"
    )
    # The point of the fix: proving the mechanism here must not itself touch the
    # repo's own docs/ (this test deliberately chdir'd into a tmp_path surrogate).
    after = sum(1 for p in repo_docs.rglob("*") if p.is_file())
    assert after == before, "this test itself must not add files to the repo's docs/"


# ---------------------------------------------------------------------------
# 3. Mutation proof: disable the isolation fixture (via its documented,
#    normally-never-set kill switch) in a SUBPROCESS pointed at a scratch fake
#    vault, and assert the session guard fails the run. This is the
#    "Guard vorhanden != Guard wirksam" check for the guard itself, and it is
#    automated (not a one-off manual demo) so it keeps proving this on every
#    future run.
#
# The docs/ half of the same guard was verified the same way, manually, while
# building this fix (fixture body neutralized, conftest.py backed up first,
# SHA-256-verified identical after restoring) — not repeated here as an
# automated subprocess test, because doing so safely would require the
# subprocess to run with THIS repo as its cwd, which reintroduces the exact
# real docs/ write this whole fix exists to prevent every time the meta-test
# runs. The memory-vault half below is fully safe to automate because
# ORCH_VAULT_PATH can redirect the "real" vault the guard watches to a scratch
# directory without touching anything in this repo or the live vault.
# ---------------------------------------------------------------------------

def test_disabling_the_fixture_no_longer_leaks_because_memory_itself_refuses(tmp_path):
    """Was the mutation proof for `_guard_real_vault_and_docs_untouched` alone: disable
    `_isolate_memory_and_docs_output` (kill switch) and expect the OUTER count-based
    guard to catch the resulting leak. That framing is now stale — with `memory.
    _refuse_real_vault_writes_under_pytest()` added as a second, independent layer,
    disabling ONLY the conftest fixture no longer produces a leak for the guard to
    catch: `memory.py`'s own check fires first and refuses the write outright (the
    task still finalizes; `store_result` returning None is treated the same as any
    other memory-store failure — see `memory.store_result`'s own try/except).

    This test now proves that stronger claim directly: with the fixture disabled and
    `ORCH_VAULT_PATH` pointed at a scratch stand-in "vault", running the exact test
    that used to leak creates NO files under that stand-in vault at all — not "the
    guard noticed after the fact", but "nothing arrived to notice". The direct,
    in-process tests above already prove `_ensure_dirs()` raises; this is the
    end-to-end confirmation through a real subprocess and a real (if fake) queue run.
    """
    fake_vault = tmp_path / "fake_vault"
    fake_task_results = fake_vault / "99_System" / "AI" / "memory" / "task_results"
    fake_task_results.mkdir(parents=True)
    (fake_vault / "99_System" / "AI" / "memory" / "daily").mkdir(parents=True)
    (fake_vault / "99_System" / "AI").joinpath("policy.yaml").write_text("{}\n", encoding="utf-8")

    scratch_cwd = tmp_path / "subprocess_cwd"
    scratch_cwd.mkdir()

    env = dict(os.environ)
    env["ORCH_VAULT_PATH"] = str(fake_vault)
    env["_ORCH_TEST_DISABLE_MEMORY_DOCS_ISOLATION"] = "1"

    proc = subprocess.run(
        [
            sys.executable, "-X", "utf8", "-m", "pytest", "-p", "no:randomly", "-q",
            str(_REPO_ROOT / "tests" / "test_worktree_gate.py")
            + "::test_run_once_runs_a_plain_task_in_a_dirty_repo",
        ],
        cwd=str(scratch_cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,  # asserted explicitly below with a descriptive message
    )

    assert proc.returncode == 0, (
        f"the queue test itself must still pass — a refused memory write must not "
        f"fail the TASK.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    leaked = list(fake_task_results.iterdir())
    assert leaked == [], (
        f"expected memory.py's hard safety net to refuse the write even with the "
        f"conftest fixture disabled — found real files instead: {leaked}"
    )


# ---------------------------------------------------------------------------
# 4. OSError fallback proof: when Path.resolve() fails (e.g., on systems where
#    symlink resolution or permission issues prevent the normal path), the guard
#    must still catch the violation using the os.path-based fallback.
# ---------------------------------------------------------------------------

def test_memory_refuses_writes_even_when_resolve_fails(monkeypatch, real_vault_path):
    """Path.resolve() can fail on some systems (broken symlinks, permissions).
    The guard must not silently pass when OSError is raised — it must fall back
    to os.path.normcase/abspath comparison. This test sabotages Path.resolve to
    always raise OSError and proves the fallback fires."""
    real_root = real_vault_path / "99_System" / "AI" / "memory"
    monkeypatch.setattr(memory_module, "_MEMORY_ROOT", real_root)
    monkeypatch.setattr(memory_module, "_TASK_RESULTS_DIR", real_root / "task_results")
    monkeypatch.setattr(memory_module, "_ARCHIVE_DIR", real_root / "archive")
    monkeypatch.setattr(memory_module, "_DAILY_DIR", real_root / "daily")
    monkeypatch.setattr(memory_module, "_CURATED_MEMORY_FILE", real_root / "MEMORY.md")

    # Sabotage Path.resolve to raise OSError
    original_resolve = Path.resolve
    def broken_resolve(self):
        raise OSError("Simulated resolve failure")
    monkeypatch.setattr(Path, "resolve", broken_resolve)

    assert os.environ.get("PYTEST_CURRENT_TEST"), "precondition: pytest sets this during a test"
    with pytest.raises(RuntimeError, match="refuses to write into the real vault"):
        memory_module._ensure_dirs()


def test_memory_refuses_subdirectory_even_with_resolve_failure(monkeypatch, real_vault_path):
    """The OSError fallback must also catch subdirectories of the real vault,
    not just the exact real root."""
    real_subdir = real_vault_path / "99_System" / "AI" / "memory" / "future_subdir"
    monkeypatch.setattr(memory_module, "_MEMORY_ROOT", real_subdir)
    monkeypatch.setattr(memory_module, "_TASK_RESULTS_DIR", real_subdir / "task_results")
    monkeypatch.setattr(memory_module, "_ARCHIVE_DIR", real_subdir / "archive")
    monkeypatch.setattr(memory_module, "_DAILY_DIR", real_subdir / "daily")
    monkeypatch.setattr(memory_module, "_CURATED_MEMORY_FILE", real_subdir / "MEMORY.md")

    # Sabotage Path.resolve to raise OSError
    original_resolve = Path.resolve
    def broken_resolve(self):
        raise OSError("Simulated resolve failure")
    monkeypatch.setattr(Path, "resolve", broken_resolve)

    with pytest.raises(RuntimeError, match="refuses to write into the real vault"):
        memory_module._ensure_dirs()


def test_memory_allows_paths_outside_real_vault_even_with_resolve_failure(monkeypatch):
    """The OSError fallback must not become a blanket refusal — paths genuinely
    outside the real vault must still be allowed even when resolve() fails."""
    safe_subdir = Path("/tmp/safe_test_dir")  # definitely outside the real vault
    monkeypatch.setattr(memory_module, "_MEMORY_ROOT", safe_subdir)
    monkeypatch.setattr(memory_module, "_TASK_RESULTS_DIR", safe_subdir / "task_results")
    monkeypatch.setattr(memory_module, "_ARCHIVE_DIR", safe_subdir / "archive")
    monkeypatch.setattr(memory_module, "_DAILY_DIR", safe_subdir / "daily")
    monkeypatch.setattr(memory_module, "_CURATED_MEMORY_FILE", safe_subdir / "MEMORY.md")

    # Sabotage Path.resolve to raise OSError
    def broken_resolve(self):
        raise OSError("Simulated resolve failure")
    monkeypatch.setattr(Path, "resolve", broken_resolve)

    # Must not raise — the path is outside the real vault
    memory_module._ensure_dirs()
