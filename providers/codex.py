"""OpenAI Codex CLI provider.

Uses `codex exec` for non-interactive execution with full tool access.
Uses ChatGPT subscription auth - no API key needed.
"""

import shutil
import subprocess
import sys
from providers.base import BaseProvider, RunResult
from config import TASK_TIMEOUT_SEC

_CODEX_CMD = shutil.which("codex") or "codex"


class CodexProvider(BaseProvider):
    name = "codex"

    @staticmethod
    def _build_command(read_only: bool, model: str | None = None) -> list[str]:
        cmd = [
            _CODEX_CMD,
            "exec",
        ]
        if model:
            cmd.extend(["--model", model])
        if read_only:
            # research-qa runs fully unattended, so read-only mode must also avoid
            # approval prompts while still enforcing a read-only sandbox.
            cmd.extend(["--ask-for-approval", "never", "--sandbox", "read-only"])
        else:
            cmd.append("--full-auto")
        cmd.append("-")
        return cmd

    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = TASK_TIMEOUT_SEC,
        read_only: bool = False,
    ) -> RunResult:
        model_label = self._forced_model
        if model_label:
            print(f"  [codex → {model_label}] Führe Task aus...")
        else:
            print(f"  [codex] Führe Task aus...")
        try:
            cmd = self._build_command(read_only=read_only, model=model_label)
            result = subprocess.run(
                cmd,
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
        except (OSError, ValueError) as e:
            return RunResult(success=False, error=str(e))
