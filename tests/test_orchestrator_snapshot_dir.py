"""Regression tests for ``orchestrator._snapshot_dir``.

Measured 2026-09-09/10: a 0-byte file literally named ``nul`` (left in
``finanzen/kapitaluebersicht`` by a ``> nul`` shell redirect) made
``os.path.relpath`` raise ``ValueError`` — not an ``OSError``, so the
function's own ``except OSError`` did not catch it. The exception escaped
``run_once``/``main``, the process exited 1, ``run_orchestrator.ps1``
restarted it, and it hit the same first queue task again: 96 restarts over
11.5 h, zero tasks executed, and nothing in ``logs/orchestrator.log``.
"""
import os
from pathlib import Path

import pytest

import orchestrator


def _build_tree(root: Path) -> None:
    (root / "top.txt").write_text("a", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "nested.txt").write_text("b", encoding="utf-8")
    (root / "sub" / "deeper").mkdir()
    (root / "sub" / "deeper" / "leaf.txt").write_text("c", encoding="utf-8")


def test_relative_keys_match_relpath_for_ordinary_paths(tmp_path: Path):
    """The per-directory join must produce what per-file relpath did.

    Pins the refactor: rel paths are now built from ``os.walk``'s ``root``
    instead of resolving each file, and that must not change a single key.

    "Ordinary" is load-bearing (external review, 2026-09-10): equivalence is
    NOT universal. Two Windows names reachable only through the extended-length
    prefix diverge deliberately — a bare ``nul`` (which used to raise instead of
    producing a key at all) and a trailing dot or space, where the old
    ``abspath`` normalisation silently turned ``report.`` into ``report``. The
    new code reports the name that is actually on disk, which is what a change
    detector wants. This test therefore covers the paths a repo really has.
    """
    _build_tree(tmp_path)

    snapshot = orchestrator._snapshot_dir(str(tmp_path))

    expected = {
        os.path.relpath(os.path.join(walk_root, name), str(tmp_path))
        for walk_root, _dirs, files in os.walk(str(tmp_path))
        for name in files
    }
    assert set(snapshot) == expected
    # Superset, not equality: an autouse conftest fixture seeds an
    # `_empty_vault/` tree inside tmp_path, which os.walk legitimately sees.
    assert set(snapshot) >= {
        "top.txt",
        os.path.join("sub", "nested.txt"),
        os.path.join("sub", "deeper", "leaf.txt"),
    }


def test_file_paths_are_never_passed_to_relpath(tmp_path: Path, monkeypatch):
    """Cross-platform guard for the invariant the Windows crash exposed.

    ``relpath`` may only see directories produced by ``os.walk``. If a file
    path ever reaches it again, this raises the same ``ValueError`` the
    ``nul`` device did — on every OS, not just Windows.
    """
    _build_tree(tmp_path)
    real_relpath = os.path.relpath

    def guarded(path, start=None):
        if os.path.isfile(path):
            raise ValueError(f"relpath must not be called on a file: {path}")
        return real_relpath(path, start)

    monkeypatch.setattr(orchestrator.os.path, "relpath", guarded)

    snapshot = orchestrator._snapshot_dir(str(tmp_path))

    assert "top.txt" in snapshot
    assert os.path.join("sub", "deeper", "leaf.txt") in snapshot


def test_embedded_null_in_cwd_does_not_escape(tmp_path: Path):
    """The outer handler must swallow ``ValueError`` too, not just ``OSError``.

    Raised by external review: ``os.walk``'s own ``scandir`` rejects an embedded
    NUL character with ``ValueError``, which is thrown past the inner per-
    directory guard. Same defect class as the ``nul`` file — an exception type
    the handler did not name, escaping into ``run_once`` and killing the daemon.
    """
    poisoned = str(tmp_path) + "\x00suffix"

    with pytest.raises(ValueError):  # the raw call the function has to absorb
        next(os.walk(poisoned))

    assert orchestrator._snapshot_dir(poisoned) == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows reserved device names")
def test_only_bare_nul_actually_breaks_relpath():
    """Pin the measured blast radius, so the docstring cannot quietly overclaim.

    The first version of the fix asserted that reserved device names in general
    (``con``, ``aux``, ``com1`` ...) trigger this. Measured on Windows 11 /
    CPython 3.14.2 that is false — only bare ``nul`` is rewritten by
    ``_getfullpathname``. If a future Windows or CPython widens it, this fails
    and the claim gets re-measured instead of being trusted.
    """
    base = "C:\\some\\dir"
    with pytest.raises(ValueError):
        os.path.relpath(os.path.join(base, "nul"), base)

    for benign in ("con", "aux", "prn", "com1", "lpt1", "nul.txt"):
        assert os.path.relpath(os.path.join(base, benign), base) == benign


@pytest.mark.skipif(os.name != "nt", reason="Windows reserved device names")
def test_windows_reserved_device_name_does_not_crash(tmp_path: Path):
    """The real thing: a directory entry named ``nul`` next to normal files.

    Such a file can only be created (and removed) through the ``\\\\?\\``
    prefix, which bypasses Win32 path parsing — the same way the one in
    ``kapitaluebersicht`` got there.
    """
    _build_tree(tmp_path)
    device = "\\\\?\\" + str(tmp_path / "nul")
    with open(device, "wb") as handle:
        handle.write(b"")

    try:
        assert "nul" in os.listdir(tmp_path)
        with pytest.raises(ValueError):  # the crash this test exists for
            os.path.relpath(str(tmp_path / "nul"), str(tmp_path))

        snapshot = orchestrator._snapshot_dir(str(tmp_path))

        # No exception, and the real files are still accounted for.
        assert "top.txt" in snapshot
        assert os.path.join("sub", "nested.txt") in snapshot
        assert os.path.join("sub", "deeper", "leaf.txt") in snapshot
    finally:
        os.remove(device)


@pytest.mark.skipif(os.name != "nt", reason="Windows reserved device names")
def test_diff_snapshot_is_unaffected_by_a_reserved_device_name(tmp_path: Path):
    """End-to-end: snapshot → change a file → diff, with ``nul`` present.

    The real changes must still be reported. The ``nul`` assertion is a canary,
    not a contract (external review, 2026-09-10): ``os.stat`` on the device
    returns ``(mtime=0.0, size=0)`` — measured stable across calls on Windows 11
    / CPython 3.14.2, but undocumented. If Windows ever makes those values move,
    this fails and tells us the change summary has started reporting a phantom
    modification every run, which is exactly what we would want to hear.
    """
    _build_tree(tmp_path)
    device = "\\\\?\\" + str(tmp_path / "nul")
    with open(device, "wb") as handle:
        handle.write(b"")

    try:
        before = orchestrator._snapshot_dir(str(tmp_path))
        (tmp_path / "top.txt").write_text("changed", encoding="utf-8")
        (tmp_path / "fresh.txt").write_text("new", encoding="utf-8")
        after = orchestrator._snapshot_dir(str(tmp_path))

        summary = orchestrator._diff_snapshot(before, after)

        assert "fresh.txt" in summary
        assert "top.txt" in summary
        assert "nul" not in summary
    finally:
        os.remove(device)
