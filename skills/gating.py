import os
import shutil
import sys
from skills.discovery import SkillConfig


def check_requirements(skill: SkillConfig) -> tuple[bool, list[str]]:
    """Returns (available, reasons_list). reasons is empty when available=True."""
    reasons: list[str] = []
    req = skill.requires

    for bin_name in req.get("bins", []):
        if not shutil.which(bin_name):
            reasons.append(f"binary not found: {bin_name}")

    for env_var in req.get("env", []):
        if not os.environ.get(env_var):
            reasons.append(f"env var not set: {env_var}")

    allowed_os = req.get("os", [])
    if allowed_os and sys.platform not in allowed_os:
        reasons.append(f"OS {sys.platform!r} not in {allowed_os}")

    # Provider check: required providers must not be permanently unavailable
    # (rate-limited state is transient — gating only blocks on missing CLI)
    for prov in req.get("providers", []):
        cmd = {"claude": "claude", "gemini": "gemini", "codex": "codex"}.get(prov)
        if cmd and not shutil.which(cmd) and not shutil.which(cmd + ".cmd"):
            reasons.append(f"required provider CLI not found: {prov}")

    return (len(reasons) == 0, reasons)
