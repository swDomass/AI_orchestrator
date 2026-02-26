"""OpenAI Codex CLI provider.

Uses `codex exec` for non-interactive execution with full tool access.
Uses ChatGPT subscription auth - no API key needed.
"""

import subprocess
import sys
from providers.base import BaseProvider, RunResult
from config import TASK_TIMEOUT_SEC

_CODEX_CMD = "codex"


class CodexProvider(BaseProvider):
    name = "codex"

    def run(self, task: str, cwd: str | None = None, timeout: int = TASK_TIMEOUT_SEC) -> RunResult:
        print(f"  [codex] Führe Task aus...")
        try:
            result = subprocess.run(
                [
                    _CODEX_CMD,
                    "exec",
                    "--full-auto",
                    "-",
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
            if any(kw in combined for kw in ("rate limit", "quota", "429", "too many")):
                return RunResult(success=False, error="rate_limit")
            if any(kw in combined for kw in ("unavailable", "connection", "timeout", "network")):
                return RunResult(success=False, error="unreachable")

            return RunResult(success=False, error=stderr or output or "empty output")

        except subprocess.TimeoutExpired:
            return RunResult(success=False, error="timeout")
        except FileNotFoundError:
            return RunResult(success=False, error="codex CLI not found")
        except Exception as e:
            return RunResult(success=False, error=str(e))
