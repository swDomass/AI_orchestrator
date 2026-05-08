"""Gemini CLI provider.

Runs gemini with --yolo (auto-approve all tools) in non-interactive mode.
Uses Google OAuth subscription auth - no API key needed.
By default Gemini CLI picks the tier internally; use --model (via _forced_model)
to pin to a specific model ID (e.g. gemini-3-pro-preview, gemini-3-flash-preview).
"""

import shutil
import subprocess
import sys
from providers.base import BaseProvider, RunResult
from config import TASK_TIMEOUT_SEC

_GEMINI_CMD = shutil.which("gemini") or "gemini"


class GeminiProvider(BaseProvider):
    name = "gemini"

    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = TASK_TIMEOUT_SEC,
        read_only: bool = False,
        session_id: str | None = None,  # accepted but ignored (supports_sessions=False)
        resume: bool = False,            # accepted but ignored
    ) -> RunResult:
        model_label = self._forced_model
        if model_label:
            print(f"  [gemini → {model_label}] Führe Task aus...")
        else:
            print(f"  [gemini] Führe Task aus...")
        try:
            cmd = [
                _GEMINI_CMD,
                "--prompt", "",
                "--output-format", "text",
            ]
            if model_label:
                cmd.extend(["--model", model_label])
            if read_only:
                # In non-interactive mode, default approval excludes shell/edit/write tools.
                cmd.extend(["--approval-mode", "default"])
            else:
                cmd.append("--yolo")
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
            if any(kw in combined for kw in ("rate limit", "quota", "429", "resource exhausted")):
                return RunResult(success=False, error="rate_limit")
            if any(kw in combined for kw in ("unavailable", "503", "connection", "unreachable", "network")):
                return RunResult(success=False, error="unreachable")

            return RunResult(success=False, error=stderr or output or "empty output")

        except subprocess.TimeoutExpired:
            return RunResult(success=False, error="timeout")
        except FileNotFoundError:
            return RunResult(success=False, error="gemini CLI not found")
        except (OSError, ValueError) as e:
            return RunResult(success=False, error=str(e))
