"""I9 tests for scripts/build_audit_pack.py.

Hermetic: all filesystem activity confined to ``tmp_path``. Git invocations
are monkey-patched at the ``subprocess.run`` level so a CI runner without
git installed still passes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from scripts import build_audit_pack
from tools.crosschecks.audit_trail import (
    APPROVALS_FILE,
    append_audit_entry,
    audit_dir,
)


# ── Fixtures / helpers ──────────────────────────────────────────────────────


_TS_SLUG = "20260516-080000"
_RUN_ID = "run-id-abc-123"
_EMBEDDING_MODEL = "all-MiniLM-L6-v2"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _make_run_dir(
    root_cwd: Path,
    *,
    run_id: str = _RUN_ID,
    ts_slug: str = _TS_SLUG,
    embedding_model: str = _EMBEDDING_MODEL,
) -> Path:
    """Build a realistic scientific-investigation run dir under root_cwd."""
    run_dir = root_cwd / "docs" / f"scientific-investigation-{ts_slug}"
    (run_dir / "draft").mkdir(parents=True, exist_ok=True)
    (run_dir / "traces").mkdir(parents=True, exist_ok=True)
    (run_dir / "audit").mkdir(parents=True, exist_ok=True)

    (run_dir / "plan.md").write_text("# plan\n", encoding="utf-8")
    (run_dir / "decision_log.md").write_text("# decisions\n", encoding="utf-8")
    (run_dir / "draft" / "proof.md").write_text("draft proof", encoding="utf-8")
    (run_dir / "traces" / "phase3.jsonl").write_text(
        '{"phase":"3"}\n', encoding="utf-8"
    )

    _write_json(
        run_dir / "audit" / "manifest.json",
        {
            "run_id": run_id,
            "ts_utc": "2026-05-16T08:00:00Z",
            "task": "demo task",
            "provider": "claude",
            "root_cwd": str(root_cwd),
            "git_commit_sha": "deadbeefcafebabe",
            "embedding_model": embedding_model,
            "tags": {},
            "tool_version": "scientific-investigation/v5/I4",
        },
    )

    # Use the real audit-trail helper so the file is realistic JSONL.
    audit_dir(run_dir)
    append_audit_entry(
        run_dir,
        {
            "type": "preregistration_threshold",
            "criterion_id": "F1",
            "source": "norm_reference",
            "reference": "DIN-EN-60068-2 §4.3 — fixture",
        },
    )
    return run_dir


def _make_fake_repo(repo_root: Path, *, with_requirements: bool = True) -> None:
    """Build a stub repo layout the script knows how to inspect."""
    (repo_root / "tools").mkdir(parents=True, exist_ok=True)
    (repo_root / "tools" / "crosschecks").mkdir(parents=True, exist_ok=True)
    for rel in (
        "tools/scientific_investigation.py",
        "tools/scientific_investigation_phases.py",
        "tools/scientific_investigation_phase2.py",
        "tools/scientific_investigation_phase3.py",
    ):
        (repo_root / rel).write_text(f"# stub {rel}\n", encoding="utf-8")
    for rel in (
        "tools/crosschecks/__init__.py",
        "tools/crosschecks/audit_trail.py",
        "tools/crosschecks/cherrypicking_detector.py",
    ):
        (repo_root / rel).write_text(f"# stub {rel}\n", encoding="utf-8")
    if with_requirements:
        (repo_root / "requirements.txt").write_text(
            "pyyaml==6.0\nrequests==2.31.0\n", encoding="utf-8"
        )


@pytest.fixture(autouse=True)
def _patch_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make git deterministic: HEAD always resolves to a known SHA.

    Individual tests can override by re-patching subprocess.run.
    """

    def _fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if (
            isinstance(cmd, list)
            and len(cmd) >= 3
            and cmd[0] == "git"
            and cmd[1] == "rev-parse"
            and cmd[2] == "HEAD"
        ):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="abc123def456\n", stderr=""
            )
        # Fall back to the real implementation for anything unexpected.
        return subprocess.run(cmd, *args, **kwargs)  # pragma: no cover

    monkeypatch.setattr(build_audit_pack.subprocess, "run", _fake_run)


# ── Tests ───────────────────────────────────────────────────────────────────


def test_main_creates_zip_when_run_id_matches(tmp_path: Path) -> None:
    root_cwd = tmp_path / "project"
    repo_root = tmp_path / "repo"
    _make_run_dir(root_cwd)
    _make_fake_repo(repo_root)

    # Steer the script to use our fake repo by patching the module constant.
    original_repo = build_audit_pack._REPO_ROOT
    build_audit_pack._REPO_ROOT = repo_root
    try:
        rc = build_audit_pack.main(
            ["--run-id", _RUN_ID, "--cwd", str(root_cwd)]
        )
    finally:
        build_audit_pack._REPO_ROOT = original_repo

    assert rc == 0
    zip_path = root_cwd / "docs" / f"scientific-investigation-{_TS_SLUG}.zip"
    assert zip_path.exists(), "ZIP was not written at the default path"

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())

    arc_prefix = f"scientific-investigation-{_TS_SLUG}"
    assert f"{arc_prefix}/plan.md" in names
    assert f"{arc_prefix}/decision_log.md" in names
    assert f"{arc_prefix}/draft/proof.md" in names
    assert f"{arc_prefix}/traces/phase3.jsonl" in names
    assert f"{arc_prefix}/audit/manifest.json" in names
    assert f"{arc_prefix}/audit/{APPROVALS_FILE}" in names
    assert "audit_pack_meta.json" in names


def test_main_errors_when_no_matching_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root_cwd = tmp_path / "project"
    _make_run_dir(root_cwd, run_id="some-other-id")

    rc = build_audit_pack.main(
        ["--run-id", "nonexistent-run", "--cwd", str(root_cwd)]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "no scientific-investigation run" in captured.err.lower()
    assert "nonexistent-run" in captured.err


def test_main_errors_when_multiple_matching_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root_cwd = tmp_path / "project"
    _make_run_dir(root_cwd, ts_slug="20260516-080000")
    _make_run_dir(root_cwd, ts_slug="20260516-090000")  # same run_id

    rc = build_audit_pack.main(
        ["--run-id", _RUN_ID, "--cwd", str(root_cwd)]
    )
    captured = capsys.readouterr()
    assert rc == 3
    assert "multiple runs" in captured.err.lower()


def test_audit_pack_meta_contains_required_fields(tmp_path: Path) -> None:
    root_cwd = tmp_path / "project"
    repo_root = tmp_path / "repo"
    _make_run_dir(root_cwd)
    _make_fake_repo(repo_root)

    out_path = tmp_path / "out.zip"
    build_audit_pack.build_audit_pack(
        run_id=_RUN_ID,
        root_cwd=root_cwd,
        output=out_path,
        repo_root=repo_root,
    )

    with zipfile.ZipFile(out_path, "r") as zf:
        meta_raw = zf.read("audit_pack_meta.json").decode("utf-8")
    meta = json.loads(meta_raw)

    for key in (
        "build_ts_utc",
        "run_id",
        "code_commit_sha",
        "python_version",
        "embedding_model",
        "mechanism_hashes",
        "requirements_lock",
    ):
        assert key in meta, f"missing key {key!r} in audit_pack_meta.json"

    assert meta["run_id"] == _RUN_ID
    assert meta["embedding_model"] == _EMBEDDING_MODEL
    assert meta["code_commit_sha"] == "abc123def456"
    assert meta["python_version"] == sys.version
    assert "pyyaml" in meta["requirements_lock"]
    assert isinstance(meta["mechanism_hashes"], dict)
    # All four core modules + crosschecks/* glob hits must appear.
    assert "tools/scientific_investigation.py" in meta["mechanism_hashes"]
    assert "tools/scientific_investigation_phases.py" in meta["mechanism_hashes"]
    assert "tools/scientific_investigation_phase2.py" in meta["mechanism_hashes"]
    assert "tools/scientific_investigation_phase3.py" in meta["mechanism_hashes"]
    assert "tools/crosschecks/audit_trail.py" in meta["mechanism_hashes"]
    assert "tools/crosschecks/cherrypicking_detector.py" in meta["mechanism_hashes"]


def test_mechanism_hashes_change_when_module_changes(tmp_path: Path) -> None:
    root_cwd = tmp_path / "project"
    repo_root = tmp_path / "repo"
    _make_run_dir(root_cwd)
    _make_fake_repo(repo_root)

    out_a = tmp_path / "a.zip"
    out_b = tmp_path / "b.zip"

    build_audit_pack.build_audit_pack(
        run_id=_RUN_ID, root_cwd=root_cwd, output=out_a, repo_root=repo_root,
    )

    # Mutate one tracked module — the hash for that file must shift.
    tracked = repo_root / "tools" / "scientific_investigation.py"
    tracked.write_text("# stub mutated content\n", encoding="utf-8")

    build_audit_pack.build_audit_pack(
        run_id=_RUN_ID, root_cwd=root_cwd, output=out_b, repo_root=repo_root,
    )

    def _meta(path: Path) -> dict[str, Any]:
        with zipfile.ZipFile(path, "r") as zf:
            return json.loads(zf.read("audit_pack_meta.json").decode("utf-8"))

    meta_a = _meta(out_a)
    meta_b = _meta(out_b)
    key = "tools/scientific_investigation.py"
    assert meta_a["mechanism_hashes"][key] != meta_b["mechanism_hashes"][key]
    # Untouched module's hash stays stable.
    other = "tools/scientific_investigation_phases.py"
    assert meta_a["mechanism_hashes"][other] == meta_b["mechanism_hashes"][other]


def test_main_tolerates_missing_requirements(tmp_path: Path) -> None:
    root_cwd = tmp_path / "project"
    repo_root = tmp_path / "repo"
    _make_run_dir(root_cwd)
    _make_fake_repo(repo_root, with_requirements=False)

    out_path = tmp_path / "out.zip"
    build_audit_pack.build_audit_pack(
        run_id=_RUN_ID,
        root_cwd=root_cwd,
        output=out_path,
        repo_root=repo_root,
    )

    with zipfile.ZipFile(out_path, "r") as zf:
        meta = json.loads(zf.read("audit_pack_meta.json").decode("utf-8"))
    assert meta["requirements_lock"] == ""


def test_main_tolerates_non_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_cwd = tmp_path / "project"
    repo_root = tmp_path / "repo"
    _make_run_dir(root_cwd)
    _make_fake_repo(repo_root)

    # Override the autouse git fake so rev-parse looks like a real failure.
    def _fail_git(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(cmd, list) and cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=128,
                stdout="",
                stderr="fatal: not a git repository\n",
            )
        return subprocess.run(cmd, *args, **kwargs)  # pragma: no cover

    monkeypatch.setattr(build_audit_pack.subprocess, "run", _fail_git)

    out_path = tmp_path / "out.zip"
    build_audit_pack.build_audit_pack(
        run_id=_RUN_ID,
        root_cwd=root_cwd,
        output=out_path,
        repo_root=repo_root,
    )

    with zipfile.ZipFile(out_path, "r") as zf:
        meta = json.loads(zf.read("audit_pack_meta.json").decode("utf-8"))
    assert meta["code_commit_sha"] == ""


def test_main_errors_when_cwd_does_not_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = build_audit_pack.main(
        ["--run-id", _RUN_ID, "--cwd", str(tmp_path / "missing")]
    )
    captured = capsys.readouterr()
    assert rc == 4
    assert "does not exist" in captured.err
