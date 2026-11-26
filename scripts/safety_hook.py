#!/usr/bin/env python3
"""Claude Code PreToolUse hook — hard-deny dangerous commands.

Registered user-wide as a PreToolUse hook in ~/.claude/settings.json with the
matcher "Bash|PowerShell" (NOT in .claude/settings.local.json). Receives tool
invocation JSON on stdin. Deny case: prints a block decision on stdout. Allow
case: prints NOTHING and exits 0 — "no decision", so Claude Code's normal
permission flow still applies (an explicit "approve"/"allow" would skip the
permission prompt; this hook may deny, never approve).

Uses the shared SAFETY_DENY_PATTERNS from config.py so the same rules
apply to both the hard hook (Claude) and the soft prompt injection
(Gemini, Codex).

Push exception (2026-11-24): a plain `git push` is denied only when the session
is NOT attended. Attended means CLAUDE_CODE_SESSION_ATTENDED == "1" (Claude Code
sets it for every child/hook process; `--print` and background sessions get "0")
and no ORCH_UNATTENDED marker (set by the orchestrator). Fail-closed: any other
value, a missing variable, or any non-empty marker means unattended. Force-push
forms and all other patterns are denied in every session.
"""

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

# Snapshot BEFORE importing config: config loads .env into os.environ on import,
# and a .env must never be able to set the "attended" signal.
_ENV_AT_START: dict[str, str] = dict(os.environ)

# Add project root to path so we can import config
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from config import ORCH_UNATTENDED_ENV, SAFETY_ATTENDED_ALLOWED, SAFETY_DENY_PATTERNS
except ImportError:
    # Fallback: minimal inline patterns if config import fails. Mirrors (does
    # NOT import) config.py's pattern-building blocks so the hook still
    # blocks correctly even when the repo is broken/uninstallable — the
    # duplication is deliberate, see config.py's SAFETY_DENY_PATTERNS
    # docstring. _CMD_START/_GIT_BIN keep `git commit`/`git push` matches
    # anchored to a real command start (not inside a quoted string, another
    # program's `-c` argument, or a comment) — see the identically named
    # constants in config.py for the full rationale, including why a real
    # shell interpreter's `-c`/`-Command` argument (bash, sh, zsh, dash,
    # pwsh, powershell) and the command-taking builtins `eval`/`exec` are ALSO
    # command starts, unlike e.g. `python -c`.
    _PATH_PREFIX = r"(?:[^\s;&|()\"'`]*[\\/])?"
    _SHELL_INTERP = r"(?:bash|sh|zsh|dash|pwsh|powershell(?:\.exe)?)"
    _SHELL_C_FLAG = r"-(?:c|com(?:m(?:a(?:n(?:d)?)?)?)?)\b"
    _SHELL_CMD_BUILTIN = r"(?:eval|exec)"
    _CMD_START = (
        r"(?:^|[\r\n;&|]|\(|`)\s*"
        rf"(?:{_PATH_PREFIX}{_SHELL_INTERP}\s+{_SHELL_C_FLAG}\s*[\"']?\s*)?"
        rf"(?:{_SHELL_CMD_BUILTIN}\s+[\"']?\s*){{0,2}}"
    )
    _GIT_BIN = rf"{_PATH_PREFIX}git"
    _GIT_OPT = r"(?:-[cC]\s+(?:\"[^\"]*\"|'[^']*'|\S+)|--no-pager)"

    def _fallback_git_pattern(rest: str) -> str:
        return rf"{_CMD_START}{_GIT_BIN}(?:\s+{_GIT_OPT})*\s+{rest}"

    SAFETY_DENY_PATTERNS = [
        (r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|--force\s+).*(/|\\)", "rm -rf"),
        # Force forms mirror config.py (the attended session relies on them alone).
        (_fallback_git_pattern(r"push\s+[\s\S]*--for(?!mat)"), "git push --force"),
        (_fallback_git_pattern(r"push\s+[\s\S]*(?-i:(?<![\w-])-[a-z0-9]*f)"), "git push -f"),
        (_fallback_git_pattern(r"push\s+[\s\S]*--m(?:i|\b)"), "git push --mirror"),
        (_fallback_git_pattern(r"push\s+(?:[\s\S]*?\s)?[\"']?\+\S"), "git push +refspec"),
        # Remote deletion, blocked in every session type (mirrors config.py).
        (_fallback_git_pattern(r"push\s+[\s\S]*--de"), "git push --delete"),
        (_fallback_git_pattern(r"push\s+[\s\S]*(?-i:(?<![\w-])-[a-z0-9]*d)"), "git push -d"),
        (_fallback_git_pattern(r"push\s+(?:[\s\S]*?\s)?[\"']?:[^\s\"']"), "git push :refspec"),
        (_fallback_git_pattern(r"push\s+[\s\S]*--pru"), "git push --prune"),
        (_fallback_git_pattern(r"reset\s+--hard"), "git reset --hard"),
        # No `commit` entry — dropped 2026-08-17 together with config.py's, see the
        # rationale there. Only the irreversible half (push) stays blocked.
        (_fallback_git_pattern(r"push(?![\w-])"), "git push"),
        # Publishing approval only via Telegram (mirrors config.py, same regex and reason).
        (
            r"^(?![\s\S]*--probelauf)(?=[\s\S]*(?:wl_socialmedia\.posten|posten[\\/]+befehl\.py)[\s\S]*\bfreigabe\b)",
            "posten freigabe ohne --probelauf (nur über Telegram)",
        ),
        (r"DROP\s+(TABLE|DATABASE|SCHEMA)", "DROP TABLE/DATABASE"),
        (r"format\s+[A-Za-z]:", "format drive"),
        (r"mkfs\b", "mkfs"),
    ]
    SAFETY_ATTENDED_ALLOWED = frozenset({"git push"})
    ORCH_UNATTENDED_ENV = "ORCH_UNATTENDED"

# Pre-compile patterns for performance
_COMPILED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE), desc) for pat, desc in SAFETY_DENY_PATTERNS
]


def is_attended(environ: Mapping[str, str] | None = None) -> bool:
    """True only for an attended session without an orchestrator marker (fail-closed).

    Exactly "1" in CLAUDE_CODE_SESSION_ATTENDED and an empty/missing marker;
    anything else ("true", " 1", "", "0", missing, marker "0") is unattended.
    """
    env = _ENV_AT_START if environ is None else environ
    return env.get("CLAUDE_CODE_SESSION_ATTENDED") == "1" and not env.get(ORCH_UNATTENDED_ENV)


def check_command(command: str, attended: bool = False) -> str | None:
    """Return deny reason if command matches a deny pattern, else None.

    With attended=True the patterns named in SAFETY_ATTENDED_ALLOWED (plain
    push) are skipped; the scan CONTINUES so later patterns still fire
    (`git push; DROP TABLE x`). Default False = every pattern applies.

    Cheap prefilter: every git-specific pattern's description starts with
    "git " and its regex always requires the literal substring "git" to be
    present (see _GIT_BIN in config.py) — so when "git" doesn't occur in the
    command at all, those patterns cannot match and are skipped without
    running their (comparatively expensive, boundary-scanning) regex. This
    keeps a long, git-free command line fast; a command that does contain
    "git" still pays the full regex cost, same as before.
    """
    has_git = "git" in command.lower()
    for regex, desc in _COMPILED_PATTERNS:
        if not has_git and desc.startswith("git "):
            continue
        if attended and desc in SAFETY_ATTENDED_ALLOWED:
            continue
        if regex.search(command):
            return desc
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Can't parse input — no decision (don't break the session)
        return

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # Only inspect shell commands — other tools (Read, Write, Edit, etc.) are safe.
    # Claude Code's PowerShell tool takes its input as `command`, same as Bash
    # (tool definition, checked 2026-10-28).
    if tool_name in ("Bash", "PowerShell"):
        command = tool_input.get("command", "")
        reason = check_command(command, attended=is_attended())
        if reason:
            message = f"SAFETY HOOK: Blocked dangerous command ({reason})"
            # Claude Code recognizes exactly two blocking shapes:
            #   modern: hookSpecificOutput.permissionDecision == "deny"
            #   legacy: decision == "block"
            # The previously emitted "decision": "deny" was neither and therefore
            # never blocked anything (verified against the CLI bundle 2026-08-15).
            # Both shapes are emitted so the hook keeps working across versions.
            json.dump({
                "decision": "block",
                "reason": message,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": message,
                },
            }, sys.stdout)

    # Allow case: print nothing, exit 0 — normal permission flow decides.


if __name__ == "__main__":
    main()
