"""
Per-tool preflight hooks — deterministic context collection BEFORE the LLM call.

Cheaper than having the model rediscover basics every iteration. Each hook
returns a bounded Markdown block (cap PREFLIGHT_MAX_CHARS chars). Hooks run
with a wall-clock timeout (PREFLIGHT_TIMEOUT_SEC) and degrade silently to ""
on failure — preflight must NEVER block a task.

Per-tool collectors
-------------------

* ``dev-loop``                — git status, package manager, test command,
                                last test failure (if any)
* ``review-loop``             — git diff length, changed file list,
                                file-type histogram
* ``security-audit`` /
  ``deep-security-audit``     — dependency manifests, exposed config files,
                                quick grep for credentials
* ``critical-review``         — plan file size, section structure, mtime
* ``research-qa``             — repo size, language histogram, README presence

Cache
-----

Hook output is cached at ``{cwd}/.<tool>/preflight-{YYYY-MM-DD}.md`` so a
re-run within the same day reuses the previous block. Cache misses are
written through after a successful collection.

Public API
----------

* ``collect(tool_name, cwd)``  — entry point. Returns the formatted block.
* ``collect_cached(tool_name, cwd)`` — same with on-disk caching.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

PREFLIGHT_MAX_CHARS = 8000          # ≈ 2k tokens per hook
PREFLIGHT_TIMEOUT_SEC = 5
PREFLIGHT_CACHE_TTL_HOURS = 24

# Tool aliases — multiple tool names route to the same collector.
_COLLECTORS: dict[str, Callable[[Path], str]] = {}


def register(tool_name: str):
    """Decorator: register a preflight collector for ``tool_name``."""
    def wrap(func: Callable[[Path], str]) -> Callable[[Path], str]:
        _COLLECTORS[tool_name] = func
        return func
    return wrap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_chars: int = PREFLIGHT_MAX_CHARS) -> str:
    """Cap output, append a marker on truncation so the model knows."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[preflight truncated]"


def _run(cmd: list[str], cwd: Path, timeout: int = PREFLIGHT_TIMEOUT_SEC) -> str:
    """Run a subprocess and return stdout, swallowing all errors."""
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return result.stdout or ""
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return ""


def _is_git_repo(cwd: Path) -> bool:
    return (cwd / ".git").exists()


def _detect_test_command(cwd: Path) -> str:
    """Best-effort detection of the project's test runner."""
    if (cwd / "pytest.ini").exists() or (cwd / "pyproject.toml").exists() or any(cwd.glob("test_*.py")):
        return "pytest"
    if (cwd / "package.json").exists():
        try:
            text = (cwd / "package.json").read_text(encoding="utf-8", errors="replace")
            if '"test"' in text:
                return "npm test"
        except OSError:
            pass
    if (cwd / "Cargo.toml").exists():
        return "cargo test"
    if (cwd / "go.mod").exists():
        return "go test ./..."
    return ""


def _detect_package_manager(cwd: Path) -> str:
    for marker, pm in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("package-lock.json", "npm"),
        ("poetry.lock", "poetry"),
        ("uv.lock", "uv"),
        ("Pipfile.lock", "pipenv"),
        ("requirements.txt", "pip"),
        ("pyproject.toml", "pip"),
        ("Cargo.lock", "cargo"),
        ("go.sum", "go"),
    ):
        if (cwd / marker).exists():
            return pm
    return ""


def _file_type_histogram(files: list[str], top_n: int = 5) -> str:
    counter: Counter[str] = Counter()
    for f in files:
        ext = Path(f).suffix or "<no-ext>"
        counter[ext] += 1
    if not counter:
        return ""
    parts = [f"{ext}={n}" for ext, n in counter.most_common(top_n)]
    return ", ".join(parts)


def _language_histogram(cwd: Path, max_files: int = 2000) -> str:
    """Approximate language mix by extension, skipping common vendored dirs."""
    counter: Counter[str] = Counter()
    skip = {".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build", "target"}
    count = 0
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            ext = Path(f).suffix.lower()
            if ext:
                counter[ext] += 1
            count += 1
            if count > max_files:
                break
        if count > max_files:
            break
    if not counter:
        return ""
    return ", ".join(f"{ext}={n}" for ext, n in counter.most_common(8))


# ---------------------------------------------------------------------------
# Per-tool collectors
# ---------------------------------------------------------------------------

@register("dev-loop")
def _preflight_dev_loop(cwd: Path) -> str:
    parts: list[str] = ["## Preflight: dev-loop"]
    if _is_git_repo(cwd):
        status = _run(["git", "status", "--short"], cwd)
        if status.strip():
            parts.append("### Git status (changed files)\n```\n" + status.strip()[:2000] + "\n```")
        else:
            parts.append("### Git status\nClean working tree.")
        branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd).strip()
        if branch:
            parts.append(f"### Branch\n`{branch}`")

    pm = _detect_package_manager(cwd)
    if pm:
        parts.append(f"### Package manager\n`{pm}`")

    test_cmd = _detect_test_command(cwd)
    if test_cmd:
        parts.append(f"### Test command\n`{test_cmd}`")

    return _truncate("\n\n".join(parts))


@register("review-loop")
def _preflight_review_loop(cwd: Path) -> str:
    parts: list[str] = ["## Preflight: review-loop"]
    if not _is_git_repo(cwd):
        parts.append("Nicht im Git-Repo — Diff-Statistik nicht verfügbar.")
        return _truncate("\n\n".join(parts))

    diff_stat = _run(["git", "diff", "HEAD", "--stat"], cwd)
    if diff_stat.strip():
        parts.append("### Diff stat\n```\n" + diff_stat.strip()[:1500] + "\n```")

    names_only = _run(["git", "diff", "HEAD", "--name-only"], cwd)
    changed = [line.strip() for line in names_only.splitlines() if line.strip()]
    if changed:
        parts.append(f"### Changed files ({len(changed)})\n" + "\n".join(f"- {f}" for f in changed[:30]))
        hist = _file_type_histogram(changed)
        if hist:
            parts.append(f"### File-type histogram\n{hist}")

    return _truncate("\n\n".join(parts))


def _preflight_security(cwd: Path, tool_label: str) -> str:
    parts: list[str] = [f"## Preflight: {tool_label}"]

    # Dependency manifests
    manifests: list[str] = []
    for name in ("requirements.txt", "pyproject.toml", "Pipfile",
                 "package.json", "yarn.lock", "Cargo.toml", "go.mod"):
        if (cwd / name).exists():
            manifests.append(name)
    if manifests:
        parts.append("### Dependency manifests\n" + ", ".join(f"`{m}`" for m in manifests))

    # Exposed config files (anything that often holds secrets)
    sensitive = []
    for pat in (".env", ".env.local", "credentials*", "config.json",
                "secrets*.yaml", "*.pem", "*.key"):
        sensitive.extend(str(p.relative_to(cwd)) for p in cwd.glob(pat))
    if sensitive:
        parts.append("### Exposed config files (manual review!)\n"
                     + "\n".join(f"- `{s}`" for s in sorted(set(sensitive))[:20]))

    # Lightweight credential grep via git (fast, ignores .gitignore)
    if _is_git_repo(cwd) and shutil.which("git"):
        grep = _run(
            ["git", "grep", "-nE",
             r"(api[_-]?key|secret|password|token)\s*=\s*['\"]"],
            cwd, timeout=PREFLIGHT_TIMEOUT_SEC,
        )
        hits = [line for line in grep.splitlines() if line.strip()][:15]
        if hits:
            parts.append("### Potential credential leaks\n```\n"
                         + "\n".join(hits) + "\n```")

    return _truncate("\n\n".join(parts))


@register("security-audit")
def _preflight_security_audit(cwd: Path) -> str:
    return _preflight_security(cwd, "security-audit")


@register("deep-security-audit")
def _preflight_deep_security_audit(cwd: Path) -> str:
    return _preflight_security(cwd, "deep-security-audit")


@register("critical-review")
def _preflight_critical_review(cwd: Path) -> str:
    """Critical-review operates on a plan markdown file. We scan for .md
    files in cwd and report the most recently modified ones."""
    parts: list[str] = ["## Preflight: critical-review"]
    md_files = sorted(cwd.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    if not md_files:
        parts.append("Keine .md-Dateien im CWD gefunden.")
        return _truncate("\n\n".join(parts))

    rows = []
    for p in md_files:
        try:
            size_kb = round(p.stat().st_size / 1024, 1)
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            headings = []
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[:200]:
                if re.match(r"^#{1,3}\s", line):
                    headings.append(line.strip())
            head_summary = "; ".join(headings[:5])
            rows.append(f"- `{p.name}` — {size_kb} KB — {mtime} — Headings: {head_summary}")
        except OSError:
            continue
    parts.append("### Plan candidates\n" + "\n".join(rows))
    return _truncate("\n\n".join(parts))


@register("research-qa")
def _preflight_research_qa(cwd: Path) -> str:
    parts: list[str] = ["## Preflight: research-qa"]

    readme = next((p for p in (cwd / "README.md", cwd / "README.rst", cwd / "readme.md") if p.exists()), None)
    if readme:
        parts.append(f"### README\n`{readme.name}` ({round(readme.stat().st_size / 1024, 1)} KB)")
    else:
        parts.append("### README\nNicht vorhanden.")

    hist = _language_histogram(cwd)
    if hist:
        parts.append(f"### Language histogram\n{hist}")

    if _is_git_repo(cwd):
        commits = _run(["git", "rev-list", "--count", "HEAD"], cwd).strip()
        if commits:
            parts.append(f"### Commit count\n{commits}")

    return _truncate("\n\n".join(parts))


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def collect(tool_name: str, cwd: str | Path | None) -> str:
    """Run the preflight hook for ``tool_name`` against ``cwd``.

    Returns an empty string when:
      * cwd is missing or not a directory
      * no hook is registered for tool_name
      * the hook exceeds PREFLIGHT_TIMEOUT_SEC × 2 (hard guard) or raises
    """
    if not cwd:
        return ""
    cwd_path = Path(cwd)
    if not cwd_path.is_dir():
        return ""
    hook = _COLLECTORS.get(tool_name)
    if hook is None:
        return ""

    result_box: dict[str, str] = {"out": ""}
    error_box: dict[str, str] = {}

    def _runner() -> None:
        try:
            result_box["out"] = hook(cwd_path)
        except Exception as exc:  # noqa: BLE001 — preflight must never block
            error_box["err"] = repr(exc)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(PREFLIGHT_TIMEOUT_SEC * 2)
    if t.is_alive():
        logger.debug("preflight %s timed out", tool_name)
        return ""
    if "err" in error_box:
        logger.debug("preflight %s failed: %s", tool_name, error_box["err"])
        return ""
    return _truncate(result_box["out"])


def _cache_file(tool_name: str, cwd_path: Path) -> Path:
    return cwd_path / f".{tool_name}" / f"preflight-{date.today().isoformat()}.md"


def collect_cached(tool_name: str, cwd: str | Path | None) -> str:
    """Same as ``collect`` but writes/reads ``{cwd}/.<tool>/preflight-{date}.md``.

    Cache age: 24h (file-name encodes the date). Older daily files stay around
    for inspection; tool dirs are gitignored by convention.
    """
    if not cwd:
        return ""
    cwd_path = Path(cwd)
    if not cwd_path.is_dir():
        return ""
    if tool_name not in _COLLECTORS:
        return ""

    cache = _cache_file(tool_name, cwd_path)
    try:
        if cache.exists():
            age_h = (datetime.now().timestamp() - cache.stat().st_mtime) / 3600
            if age_h < PREFLIGHT_CACHE_TTL_HOURS:
                return cache.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass

    fresh = collect(tool_name, cwd_path)
    if fresh:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(fresh, encoding="utf-8")
        except OSError as exc:
            logger.debug("preflight cache write failed: %s", exc)
    return fresh


def list_registered_tools() -> list[str]:
    return sorted(_COLLECTORS.keys())
