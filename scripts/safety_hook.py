#!/usr/bin/env python3
"""Claude Code PreToolUse hook — hard-deny dangerous commands.

Loaded as a PreToolUse hook in .claude/settings.local.json.
Receives tool invocation JSON on stdin, returns a decision on stdout.

Uses the shared SAFETY_DENY_PATTERNS from config.py so the same rules
apply to both the hard hook (Claude) and the soft prompt injection
(Gemini, Codex).
"""

import json
import re
import sys
from pathlib import Path

# Add project root to path so we can import config
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from config import SAFETY_DENY_PATTERNS
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
        (_fallback_git_pattern(r"push\s+.*--force"), "git push --force"),
        (_fallback_git_pattern(r"push\s+.*-f\b"), "git push -f"),
        (_fallback_git_pattern(r"reset\s+--hard"), "git reset --hard"),
        # No `commit` entry — dropped 2026-08-17 together with config.py's, see the
        # rationale there. Only the irreversible half (push) stays blocked.
        (_fallback_git_pattern(r"push(?![\w-])"), "git push"),
        (r"DROP\s+(TABLE|DATABASE|SCHEMA)", "DROP TABLE/DATABASE"),
        (r"format\s+[A-Za-z]:", "format drive"),
        (r"mkfs\b", "mkfs"),
    ]

# Pre-compile patterns for performance
_COMPILED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE), desc) for pat, desc in SAFETY_DENY_PATTERNS
]


def check_command(command: str) -> str | None:
    """Return deny reason if command matches a deny pattern, else None.

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
        if regex.search(command):
            return desc
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Can't parse input — allow (don't break the session)
        json.dump({"decision": "approve"}, sys.stdout)
        return

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # Only inspect Bash commands — other tools (Read, Write, Edit, etc.) are safe
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        reason = check_command(command)
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
            return

    json.dump({"decision": "approve"}, sys.stdout)


if __name__ == "__main__":
    main()
