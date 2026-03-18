"""Claude Code CLI provider.

Runs claude with full tool access (Read, Write, Edit, Bash, Glob, Grep) in non-interactive mode.
Uses Anthropic subscription auth - no API key needed.
Uses --output-format json to capture actual token usage for capacity estimation.
"""

import json
import shutil
import subprocess
import sys
from providers.base import BaseProvider, RunResult
from config import TASK_TIMEOUT_SEC

_CLAUDE_CMD = shutil.which("claude") or "claude"


class ClaudeProvider(BaseProvider):
    name = "claude"

    @staticmethod
    def _build_command(read_only: bool) -> list[str]:
        cmd = [
            _CLAUDE_CMD,
            "--print",
            "--output-format", "json",
        ]
        if read_only:
            cmd.extend(["--allowedTools", "Read,Glob,Grep"])
        else:
            cmd.extend([
                "--dangerously-skip-permissions",
                "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep",
            ])
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
            print(f"  [claude → {model_label}] Führe Task aus...")
        else:
            print(f"  [claude] Führe Task aus...")
        cmd = self._build_command(read_only=read_only)
        if self._forced_model:
            cmd.extend(["--model", self._forced_model])
        try:
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

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()

            # Try parsing JSON output for token counts
            output, input_tokens, output_tokens = self._parse_json_response(stdout)
            json_payload = self._extract_json_payload(stdout)

            if result.returncode == 0 and output and self._is_success_output(
                output=output,
                json_payload=json_payload,
            ):
                return RunResult(
                    success=True, output=output,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                )

            combined = (output + stderr).lower()
            if any(kw in combined for kw in ("rate limit", "usage limit", "quota", "overloaded")):
                return RunResult(success=False, error="rate_limit",
                                 input_tokens=input_tokens, output_tokens=output_tokens)

            return RunResult(success=False, error=stderr or output or "empty output",
                             input_tokens=input_tokens, output_tokens=output_tokens)

        except subprocess.TimeoutExpired:
            return RunResult(success=False, error="timeout")
        except FileNotFoundError:
            return RunResult(success=False, error="claude CLI not found")
        except (OSError, ValueError) as e:
            return RunResult(success=False, error=str(e))

    @staticmethod
    def _extract_json_payload(stdout: str) -> dict | None:
        if not stdout:
            return None

        # Look for first { and last } to handle CLI noise/warnings (P3 finding)
        start = stdout.find("{")
        end = stdout.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None

        try:
            data = json.loads(stdout[start:end+1])
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _is_success_output(output: str, json_payload: dict | None) -> bool:
        if not output:
            return False
        if json_payload is None:
            return True

        subtype = json_payload.get("subtype")
        if isinstance(subtype, str):
            return subtype == "success"

        result = json_payload.get("result")
        return isinstance(result, str) and bool(result.strip())

    @staticmethod
    def _parse_json_response(stdout: str) -> tuple[str, int, int]:
        """Parse Claude CLI JSON response.

        Returns (output_text, input_tokens, output_tokens).
        Falls back to (stdout, 0, 0) if not valid JSON.
        """
        if not stdout:
            return stdout, 0, 0

        data = ClaudeProvider._extract_json_payload(stdout)
        if data is None:
            return stdout, 0, 0

        # Re-serialize parsed JSON so structured errors do not re-include CLI noise.
        json_str = json.dumps(data)
        result = data.get("result")
        output = result if isinstance(result, str) else json_str
        usage = data.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        return (
            output,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
        )
