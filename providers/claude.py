"""Claude Code CLI provider.

Runs claude with full tool access (Read, Write, Edit, Bash) in non-interactive mode.
Uses Anthropic subscription auth - no API key needed.
"""

import subprocess
import sys
from providers.base import BaseProvider, RunResult
from config import TASK_TIMEOUT_SEC

_CLAUDE_CMD = "claude"


class ClaudeProvider(BaseProvider):
    name = "claude"

    def run(self, task: str, cwd: str | None = None, timeout: int = TASK_TIMEOUT_SEC) -> RunResult:
        print(f"  [claude] Führe Task aus...")
        try:
            result = subprocess.run(
                [
                    _CLAUDE_CMD,
                    "--print",
                    "--dangerously-skip-permissions",
                    "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep",
                ],
                input=task,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=cwd,
                shell=sys.platform == "win32",
            )

            output = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()

            if result.returncode == 0 and output:
                return RunResult(success=True, output=output)

            combined = (output + stderr).lower()
            if any(kw in combined for kw in ("rate limit", "usage limit", "quota", "overloaded")):
                return RunResult(success=False, error="rate_limit")

            return RunResult(success=False, error=stderr or output or "empty output")

        except subprocess.TimeoutExpired:
            return RunResult(success=False, error="timeout")
        except FileNotFoundError:
            return RunResult(success=False, error="claude CLI not found")
        except Exception as e:
            return RunResult(success=False, error=str(e))
