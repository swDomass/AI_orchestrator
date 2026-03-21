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
    # Fallback: minimal inline patterns if config import fails
    SAFETY_DENY_PATTERNS = [
        (r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|--force\s+).*(/|\\)", "rm -rf"),
        (r"git\s+push\s+.*--force", "git push --force"),
        (r"git\s+push\s+.*-f\b", "git push -f"),
        (r"git\s+reset\s+--hard", "git reset --hard"),
        (r"DROP\s+(TABLE|DATABASE|SCHEMA)", "DROP TABLE/DATABASE"),
        (r"format\s+[A-Za-z]:", "format drive"),
        (r"mkfs\b", "mkfs"),
    ]

# Pre-compile patterns for performance
_COMPILED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE), desc) for pat, desc in SAFETY_DENY_PATTERNS
]


def check_command(command: str) -> str | None:
    """Return deny reason if command matches a deny pattern, else None."""
    for regex, desc in _COMPILED_PATTERNS:
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
            json.dump({
                "decision": "deny",
                "reason": f"SAFETY HOOK: Blocked dangerous command ({reason})",
            }, sys.stdout)
            return

    json.dump({"decision": "approve"}, sys.stdout)


if __name__ == "__main__":
    main()
