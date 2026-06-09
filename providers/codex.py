"""OpenAI Codex CLI provider.

Uses `codex exec` for non-interactive execution with full tool access.
Uses ChatGPT subscription auth - no API key needed.
"""

import shutil
import subprocess
import sys
from providers.base import BaseProvider, RunResult
from providers.process_runner import run_with_watchdog
from config import TASK_TIMEOUT_SEC, CLI_IDLE_TIMEOUT_NO_LIVENESS_SEC

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
        # codex CLI ≥ 0.130 removed `--ask-for-approval` and deprecated `--full-auto`.
        # Replacements: `-c approval_policy=never` for non-interactive runs and
        # `--sandbox <mode>` for the sandbox policy.
        cmd.extend(["-c", "approval_policy=never"])
        cmd.extend(["--sandbox", "read-only" if read_only else "workspace-write"])
        # The orchestrator dispatches tasks to arbitrary cwds, many of which are
        # not git repos nor in codex' trusted-projects list (e.g. content
        # folders). Without this flag codex aborts with "Not inside a trusted
        # directory and --skip-git-repo-check was not specified." The
        # orchestrator already decides what to run, so this guard is redundant.
        cmd.append("--skip-git-repo-check")
        cmd.append("-")
        return cmd

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
            print(f"  [codex → {model_label}] Führe Task aus...")
        else:
            print(f"  [codex] Führe Task aus...")
        try:
            cmd = self._build_command(read_only=read_only, model=model_label)
            result = run_with_watchdog(
                cmd,
                input_text=task,
                cwd=cwd,
                idle_timeout=CLI_IDLE_TIMEOUT_NO_LIVENESS_SEC,
                hard_timeout=timeout,
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

        except subprocess.TimeoutExpired as exc:
            kind = getattr(exc, "timeout_kind", "hard")
            return RunResult(success=False, error="hang" if kind == "idle" else "timeout")
        except FileNotFoundError:
            return RunResult(success=False, error="codex CLI not found")
        except (OSError, ValueError) as e:
            return RunResult(success=False, error=str(e))
