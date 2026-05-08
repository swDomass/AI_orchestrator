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
    supports_sessions = True

    @staticmethod
    def _build_command(
        read_only: bool,
        session_id: str | None = None,
        resume: bool = False,
    ) -> list[str]:
        cmd = [
            _CLAUDE_CMD,
            "--print",
            "--output-format", "json",
            # Move per-machine sections (cwd, env, git status) from system-prompt
            # into the first user message → static system-prompt → Anthropic prompt
            # cache hits across sequential subprocess calls (1h TTL).
            "--exclude-dynamic-system-prompt-sections",
        ]
        # Session flags: --session-id starts a NEW session with the given UUID;
        # --resume continues an EXISTING session. Caller must track state.
        # If CLAUDE_SESSION_ENABLED is False, session_id is silently ignored.
        from config import CLAUDE_SESSION_ENABLED
        if session_id and CLAUDE_SESSION_ENABLED:
            if resume:
                cmd.extend(["--resume", session_id])
            else:
                cmd.extend(["--session-id", session_id])
        if read_only:
            # Task is included so read-only multi-agent flows (deep-security-audit
            # _run_subagent_mode style) can fan out to subagents even without
            # write permissions. Task subagents inherit the parent's tool scope.
            cmd.extend(["--allowedTools", "Read,Glob,Grep,Task"])
        else:
            cmd.extend([
                "--dangerously-skip-permissions",
                # Task is required for tools that orchestrate via Claude's
                # internal subagent system (deep-security-audit subagent-mode).
                # Without it, the master prompt's "spawn 6 Task subagents in
                # parallel" silently degrades to monolithic single-perspective.
                "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep,Task",
            ])
        return cmd

    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = TASK_TIMEOUT_SEC,
        read_only: bool = False,
        session_id: str | None = None,
        resume: bool = False,
    ) -> RunResult:
        model_label = self._forced_model
        if model_label:
            print(f"  [claude → {model_label}] Führe Task aus...")
        else:
            print(f"  [claude] Führe Task aus...")
        cmd = self._build_command(read_only=read_only, session_id=session_id, resume=resume)
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
            output, tokens = self._parse_json_response(stdout)
            json_payload = self._extract_json_payload(stdout)

            if result.returncode == 0 and output and self._is_success_output(
                output=output,
                json_payload=json_payload,
            ):
                return RunResult(success=True, output=output, **tokens)

            combined = (output + stderr).lower()
            # Typed error: --resume against a non-existent UUID errors with this
            # exact phrase. Tools should fall back to a fresh session + state inject.
            if "no conversation found with session id" in combined:
                return RunResult(success=False, error="session_missing", **tokens)
            if any(kw in combined for kw in ("rate limit", "usage limit", "quota", "overloaded")):
                return RunResult(success=False, error="rate_limit", **tokens)

            return RunResult(
                success=False,
                error=stderr or output or "empty output",
                **tokens,
            )

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
    def _parse_json_response(stdout: str) -> tuple[str, dict[str, int]]:
        """Parse Claude CLI JSON response.

        Returns (output_text, token_dict). Token dict keys match RunResult fields:
            input_tokens, output_tokens,
            cache_creation_input_tokens, cache_read_input_tokens.
        Falls back to (stdout, all-zero dict) if not valid JSON.
        """
        zero = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        if not stdout:
            return stdout, dict(zero)

        data = ClaudeProvider._extract_json_payload(stdout)
        if data is None:
            return stdout, dict(zero)

        # Re-serialize parsed JSON so structured errors do not re-include CLI noise.
        json_str = json.dumps(data)
        result = data.get("result")
        output = result if isinstance(result, str) else json_str
        usage = data.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        tokens = {
            key: int(usage.get(key, 0) or 0) for key in zero
        }
        return output, tokens
