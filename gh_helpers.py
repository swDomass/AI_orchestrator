"""Thin subprocess wrapper around the GitHub CLI (`gh`).

Used by PR-Babysitter (P2/P5) and CI-Watcher (P4). Centralised here so a
single point handles the "gh is not installed" / "gh not authenticated"
diagnostics and so we don't duplicate the JSON output parsing.

All functions return (data, error) tuples where ``error`` is empty on success
and contains a human-readable diagnosis otherwise. Functions never raise.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


# A typed error class is more Pythonic, but the (data, error) tuple keeps
# callers honest about checking — no silent crash on a missing repo.

def gh_available() -> bool:
    """Return True iff `gh` is on PATH and runs without auth error."""
    if shutil.which("gh") is None and shutil.which("gh.exe") is None:
        return False
    return True


def _run_gh(
    args: list[str],
    *,
    timeout_sec: int,
    cwd: str | None = None,
) -> tuple[str, str]:
    """Run gh with the given args. Returns (stdout, error_message).

    error_message is empty on success. Typed errors for the common cases:
    - "gh_not_found"  — binary missing from PATH
    - "gh_auth"       — token expired / not authenticated
    - "gh_not_found_repo" — gh exited because the repo doesn't exist / no access
    - "gh_timeout"
    - "gh_error: <stderr>"
    """
    if not gh_available():
        return "", "gh_not_found"
    try:
        result = subprocess.run(
            ["gh", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
    except FileNotFoundError:
        return "", "gh_not_found"
    except subprocess.TimeoutExpired:
        return "", "gh_timeout"
    except OSError as exc:
        return "", f"gh_error: {exc}"

    if result.returncode == 0:
        return result.stdout, ""

    stderr = (result.stderr or result.stdout or "").strip()
    lower = stderr.lower()
    if "authentication" in lower or "not logged" in lower or "no token" in lower:
        return "", "gh_auth"
    if "could not resolve" in lower or "404" in lower or "not found" in lower:
        return "", "gh_not_found_repo"
    # Truncate verbose tracebacks
    return "", f"gh_error: {stderr[:200]}"


def _parse_json(payload: str) -> tuple[Any, str]:
    if not payload.strip():
        return None, "gh_empty"
    try:
        return json.loads(payload), ""
    except json.JSONDecodeError as exc:
        return None, f"gh_bad_json: {exc}"


# ── PR-Babysitter API (P2) ────────────────────────────────────────────────────

def list_open_prs(
    repo: str,
    *,
    timeout_sec: int,
    limit: int = 20,
    labels: list[str] | None = None,
) -> tuple[list[dict], str]:
    """List open PRs in `repo`. Optional label filter.

    Returns (list_of_pr_dicts, error). Each PR dict has: number, title,
    headRefName, labels (list of {name}), updatedAt.
    """
    fields = "number,title,headRefName,labels,updatedAt"
    args = ["pr", "list", "-R", repo, "--state", "open",
            "--limit", str(limit), "--json", fields]
    out, err = _run_gh(args, timeout_sec=timeout_sec)
    if err:
        return [], err
    data, jerr = _parse_json(out)
    if jerr:
        return [], jerr
    if not isinstance(data, list):
        return [], "gh_bad_json: expected list"

    if labels:
        wanted = {l.lower() for l in labels}
        filtered: list[dict] = []
        for pr in data:
            pr_labels = {str(l.get("name", "")).lower() for l in (pr.get("labels") or [])}
            if wanted & pr_labels:
                filtered.append(pr)
        return filtered, ""
    return data, ""


def view_pr(
    repo: str,
    number: int,
    *,
    timeout_sec: int,
) -> tuple[dict, str]:
    """Fetch full PR details: comments, statusCheckRollup, commits."""
    fields = "number,headRefName,comments,statusCheckRollup,commits,updatedAt"
    args = ["pr", "view", str(number), "-R", repo, "--json", fields]
    out, err = _run_gh(args, timeout_sec=timeout_sec)
    if err:
        return {}, err
    data, jerr = _parse_json(out)
    if jerr:
        return {}, jerr
    if not isinstance(data, dict):
        return {}, "gh_bad_json: expected object"
    return data, ""


# ── CI-Watcher API (P4) ───────────────────────────────────────────────────────

def list_failed_runs(
    repo: str,
    *,
    timeout_sec: int,
    limit: int = 20,
) -> tuple[list[dict], str]:
    """List recently failed GitHub-Action runs in `repo`.

    Returns (list_of_run_dicts, error). Each run dict has: databaseId,
    headBranch, headSha, name, displayTitle, createdAt, conclusion.
    """
    fields = "databaseId,headBranch,headSha,name,displayTitle,createdAt,conclusion"
    args = ["run", "list", "-R", repo, "--status", "failure",
            "--limit", str(limit), "--json", fields]
    out, err = _run_gh(args, timeout_sec=timeout_sec)
    if err:
        return [], err
    data, jerr = _parse_json(out)
    if jerr:
        return [], jerr
    if not isinstance(data, list):
        return [], "gh_bad_json: expected list"
    return data, ""
