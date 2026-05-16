"""Helper for invoking sub-tools (DevLoop, etc.) with a separate state-CWD
while keeping the project root on the import path.

The scientific-investigation tool spawns dev-loop sub-tasks for each
investigation step. Each sub-task gets its own state directory under
``{root_cwd}/.scientific-investigation/{run_id}/sub-tasks/{sub_id}/`` so
state files (TODOs, traces) cannot collide. But the actual code under test
lives at ``{root_cwd}/src/`` — not in the sub-state-CWD — so we set
``PYTHONPATH`` to the root before invoking the sub-tool.

Plan §2.3 calls this the "Sub-CWD Mechanik (PYTHONPATH-Spec)".
"""

from __future__ import annotations

import os
from pathlib import Path


def build_sub_env(
    root_cwd: Path,
    *,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return an env-dict with ``PYTHONPATH`` prepended by ``root_cwd``.

    Idempotent: if ``root_cwd`` is already the first PYTHONPATH entry the
    env is returned unchanged. Existing PYTHONPATH entries are preserved
    after ``root_cwd``.
    """
    env = dict(base_env if base_env is not None else os.environ)
    root_str = str(root_cwd)
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if not parts or parts[0] != root_str:
        parts = [root_str] + [p for p in parts if p != root_str]
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def ensure_sub_state_dir(
    root_cwd: Path,
    run_id: str,
    sub_task_id: str,
) -> Path:
    """Create and return the sub-task state directory.

    Layout: ``{root_cwd}/.scientific-investigation/{run_id}/sub-tasks/{sub_task_id}/``
    """
    sub_state_cwd = (
        root_cwd
        / ".scientific-investigation"
        / run_id
        / "sub-tasks"
        / sub_task_id
    )
    sub_state_cwd.mkdir(parents=True, exist_ok=True)
    return sub_state_cwd
