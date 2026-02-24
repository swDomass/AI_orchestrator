"""Gemini CLI provider.

Runs gemini with --yolo (auto-approve all tools) in non-interactive mode.
Uses Google OAuth subscription auth - no API key needed.
Gemini CLI decides internally which model tier to use (Flash / Pro / etc.).
"""

import subprocess
import sys
from providers.base import BaseProvider, RunResult
from config import TASK_TIMEOUT_SEC

_GEMINI_CMD = "gemini.cmd" if sys.platform == "win32" else "gemini"


class GeminiProvider(BaseProvider):
    name = "gemini"

    def run(self, task: str, cwd: str | None = None, timeout: int = TASK_TIMEOUT_SEC) -> RunResult:
        print(f"  [gemini] Führe Task aus...")
        try:
            result = subprocess.run(
                [
                    _GEMINI_CMD,
                    "--prompt", task,
                    "--yolo",
                    "--output-format", "text",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )

            output = result.stdout.strip()
            stderr = result.stderr.strip()

            if result.returncode == 0 and output:
                return RunResult(success=True, output=output)

            combined = (output + stderr).lower()
            if any(kw in combined for kw in ("rate limit", "quota", "429", "resource exhausted")):
                return RunResult(success=False, error="rate_limit")
            if any(kw in combined for kw in ("unavailable", "503", "connection", "unreachable", "network")):
                return RunResult(success=False, error="unreachable")

            return RunResult(success=False, error=stderr or output or "empty output")

        except subprocess.TimeoutExpired:
            return RunResult(success=False, error="unreachable")
        except FileNotFoundError:
            return RunResult(success=False, error="gemini CLI not found")
        except Exception as e:
            return RunResult(success=False, error=str(e))
