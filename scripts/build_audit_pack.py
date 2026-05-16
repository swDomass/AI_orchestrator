"""Phase 9 Audit-Pack builder (Plan v5 §2.9, increment I9, K14).

Standalone CLI utility — NOT a registered ``#tool:`` handler. The user
triggers it manually to package a finished scientific-investigation run
into a single shareable / archivable ZIP file.

The script is read-only with respect to the run directory: it never
modifies any file under ``docs/scientific-investigation-{ts}/``. It only
*writes* the output ZIP at the configured path.

Discovery
---------
Given a ``--run-id``, the script walks
``{root_cwd}/docs/scientific-investigation-*/audit/manifest.json`` and
picks the one whose ``run_id`` field matches. Zero matches → exit code 2
with a clear error. Multiple matches (should never happen because run-IDs
are UUID4) → exit code 3 with the list of conflicting directories.

ZIP contents
------------
* The entire run directory tree (``plan.md``, ``decision_log.md``,
  ``draft/``, ``audit/``, ``traces/``, …) under the same relative paths.
* ``audit/approvals.jsonl`` is included automatically because it lives
  inside the run directory.
* A new top-level metadata file ``audit_pack_meta.json`` (see
  :class:`AuditPackMeta`) added at the ZIP root with build-time provenance:
  the code-commit SHA at pack time, Python version, embedding model
  (carried forward from the manifest), SHA256 hashes of the tracked
  mechanism modules, and the project's ``requirements.txt`` contents as a
  string.

Usage
-----
::

    python scripts/build_audit_pack.py --run-id run-id-abc
    python scripts/build_audit_pack.py --run-id run-id-abc --cwd D:/project
    python scripts/build_audit_pack.py --run-id run-id-abc --output out.zip

Exit codes: 0 success, 1 unexpected error, 2 no matching run, 3 multiple
matching runs, 4 invalid arguments.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make the project root importable when this script is executed directly
# (``python scripts/build_audit_pack.py``). Tests invoke ``main(...)`` via
# normal imports, so this is the fallback path for the CLI use case.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.crosschecks.audit_trail import compute_sha256  # noqa: E402

logger = logging.getLogger(__name__)

# Mirrors ``tools.scientific_investigation.RUN_DIR_PREFIX``. Hard-coding it
# here keeps the script importable without pulling in the full tool module
# (which itself imports providers/config and would slow CLI startup).
_RUN_DIR_GLOB = "docs/scientific-investigation-*"
_MANIFEST_RELPATH = "audit/manifest.json"

# Modules whose SHA256 we record so an auditor can verify the analysis
# pipeline was the same code the manifest claims. The glob is resolved at
# build time so newly added crosscheck modules show up automatically.
_TRACKED_FILES: tuple[str, ...] = (
    "tools/scientific_investigation.py",
    "tools/scientific_investigation_phases.py",
    "tools/scientific_investigation_phase2.py",
    "tools/scientific_investigation_phase3.py",
)
_TRACKED_GLOBS: tuple[str, ...] = (
    "tools/crosschecks/*.py",
)


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class AuditPackMeta:
    """Provenance metadata written as ``audit_pack_meta.json`` in the ZIP."""

    build_ts_utc: str
    run_id: str
    code_commit_sha: str
    python_version: str
    embedding_model: str
    mechanism_hashes: dict[str, str] = field(default_factory=dict)
    requirements_lock: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


@dataclass
class _ResolvedRun:
    """A run directory whose manifest matched the requested ``run_id``."""

    run_dir: Path
    manifest_path: Path
    manifest: dict[str, Any]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_commit_sha(cwd: Path) -> str:
    """Return current git HEAD SHA, or empty string if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        logger.warning(
            "git rev-parse returned %d in %s; code_commit_sha left empty",
            result.returncode, cwd,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("git rev-parse failed in %s: %s", cwd, exc)
    return ""


def _read_requirements(repo_root: Path) -> str:
    """Best-effort read of ``requirements.txt`` from the repo root."""
    req = repo_root / "requirements.txt"
    if not req.exists():
        logger.warning("requirements.txt not found at %s; field left empty", req)
        return ""
    try:
        return req.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read %s: %s", req, exc)
        return ""


def _collect_mechanism_hashes(repo_root: Path) -> dict[str, str]:
    """SHA256 of every tracked module, keyed by POSIX-style relative path.

    Missing files are skipped (with a warning) so a partial checkout
    doesn't break the pack — the absence is visible in the resulting dict.
    """
    out: dict[str, str] = {}
    seen: set[Path] = set()
    candidates: list[Path] = []
    for rel in _TRACKED_FILES:
        candidates.append(repo_root / rel)
    for glob_pat in _TRACKED_GLOBS:
        candidates.extend(sorted(repo_root.glob(glob_pat)))
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if not path.exists() or not path.is_file():
            logger.warning("tracked file missing: %s", path)
            continue
        rel_key = path.relative_to(repo_root).as_posix()
        digest = compute_sha256(path)
        if not digest:
            logger.warning("could not hash %s; skipping", path)
            continue
        out[rel_key] = digest
    return out


def _discover_run(root_cwd: Path, run_id: str) -> _ResolvedRun:
    """Find the run directory whose manifest's ``run_id`` matches.

    Raises :class:`LookupError` for zero matches and :class:`RuntimeError`
    for multiple matches. The caller maps these to exit codes.
    """
    matches: list[_ResolvedRun] = []
    docs_dir = root_cwd / "docs"
    if not docs_dir.exists():
        raise LookupError(
            f"No 'docs' directory under {root_cwd}; nothing to search."
        )
    for run_dir in sorted(root_cwd.glob(_RUN_DIR_GLOB)):
        if not run_dir.is_dir():
            continue
        manifest_path = run_dir / _MANIFEST_RELPATH
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "skipping unreadable manifest %s: %s", manifest_path, exc,
            )
            continue
        if manifest.get("run_id") == run_id:
            matches.append(
                _ResolvedRun(
                    run_dir=run_dir,
                    manifest_path=manifest_path,
                    manifest=manifest,
                )
            )
    if not matches:
        raise LookupError(
            f"No scientific-investigation run with run_id={run_id!r} "
            f"under {docs_dir}."
        )
    if len(matches) > 1:
        paths = ", ".join(str(m.run_dir) for m in matches)
        raise RuntimeError(
            f"Multiple runs match run_id={run_id!r}: {paths}. "
            "This should never happen for UUID4 run-IDs — please inspect."
        )
    return matches[0]


def _default_output_path(root_cwd: Path, run_dir: Path) -> Path:
    """Default ZIP filename: ``<run_dir>.zip`` next to the run directory."""
    return root_cwd / "docs" / f"{run_dir.name}.zip"


def _add_run_dir_to_zip(
    zf: zipfile.ZipFile,
    run_dir: Path,
    *,
    arc_prefix: str,
) -> int:
    """Recursively add the run directory contents to the ZIP.

    Files are stored under ``{arc_prefix}/<relative-path>``. Returns the
    number of files added (used by tests/logging).
    """
    count = 0
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(run_dir)
        arcname = f"{arc_prefix}/{rel.as_posix()}"
        zf.write(path, arcname=arcname)
        count += 1
    return count


def build_audit_pack(
    *,
    run_id: str,
    root_cwd: Path,
    output: Path | None = None,
    repo_root: Path | None = None,
) -> Path:
    """Build the audit-pack ZIP for the given run.

    Returns the absolute path to the produced ZIP. Raises ``LookupError``
    (no match) or ``RuntimeError`` (multiple matches) — both are mapped to
    non-zero exit codes by :func:`main`.
    """
    root_cwd = root_cwd.resolve()
    repo_root = (repo_root or _REPO_ROOT).resolve()
    resolved = _discover_run(root_cwd, run_id)
    output_path = (output or _default_output_path(root_cwd, resolved.run_dir)).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    meta = AuditPackMeta(
        build_ts_utc=_utc_now_iso(),
        run_id=run_id,
        code_commit_sha=_git_commit_sha(root_cwd),
        python_version=sys.version,
        embedding_model=str(resolved.manifest.get("embedding_model", "")),
        mechanism_hashes=_collect_mechanism_hashes(repo_root),
        requirements_lock=_read_requirements(repo_root),
    )

    logger.info(
        "Building audit pack: run_id=%s run_dir=%s output=%s",
        run_id, resolved.run_dir, output_path,
    )

    # Use ZIP_DEFLATED for reasonable compression; stdlib-only.
    with zipfile.ZipFile(output_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        files_added = _add_run_dir_to_zip(
            zf, resolved.run_dir, arc_prefix=resolved.run_dir.name,
        )
        zf.writestr("audit_pack_meta.json", meta.to_json())
        logger.info(
            "Audit pack written: %d run files + meta", files_added,
        )

    return output_path


# ── CLI ──────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_audit_pack",
        description=(
            "Package a scientific-investigation run directory into a "
            "shareable ZIP with provenance metadata (Plan v5 §2.9)."
        ),
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="UUID of the investigation run (matched against audit/manifest.json).",
    )
    parser.add_argument(
        "--cwd",
        default=".",
        help="Project root CWD that contains docs/scientific-investigation-*/ (default: '.').",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Destination ZIP path. Default: <cwd>/docs/scientific-investigation-{ts}.zip.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse already printed the error; map to our convention.
        return int(exc.code) if isinstance(exc.code, int) else 4

    root_cwd = Path(args.cwd).resolve()
    if not root_cwd.exists():
        print(f"error: --cwd does not exist: {root_cwd}", file=sys.stderr)
        return 4
    output = Path(args.output).resolve() if args.output else None

    try:
        out_path = build_audit_pack(
            run_id=args.run_id,
            root_cwd=root_cwd,
            output=output,
        )
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # pragma: no cover — defensive top-level guard
        logger.exception("unexpected failure while building audit pack")
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"audit pack written: {out_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
