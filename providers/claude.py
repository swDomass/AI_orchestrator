"""Claude Code CLI provider.

Runs claude with full tool access (Read, Write, Edit, Bash, Glob, Grep) in non-interactive mode.
Uses Anthropic subscription auth - no API key needed.
Uses --output-format stream-json --verbose to capture incremental NDJSON events
(liveness signal for the hang watchdog) and actual token usage for capacity
estimation. The final type=="result" event carries .result + .usage.
"""

import json
import shutil
import subprocess
import sys
from providers.base import BaseProvider, RunResult
from providers.process_runner import run_with_watchdog
from config import TASK_TIMEOUT_SEC, TASK_IDLE_TIMEOUT_SEC

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
            # stream-json emits one NDJSON event per line (init/assistant/tool_use/
            # result) → incremental liveness signal for the hang watchdog. --verbose
            # is required for stream-json in --print mode.
            "--output-format", "stream-json",
            "--verbose",
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
            result = run_with_watchdog(
                cmd,
                input_text=task,
                cwd=cwd,
                idle_timeout=TASK_IDLE_TIMEOUT_SEC,
                hard_timeout=timeout,
                shell=sys.platform == "win32",
                liveness_lines=True,  # NDJSON-event-aware: tool_use pauses idle timer
            )

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()

            # Parse the final NDJSON result event ONCE; derive output + tokens.
            result_event = self._extract_result_event(stdout)
            output = self._output_from_event(result_event, fallback=stdout)
            tokens = self._tokens_from_event(result_event)

            # A clean run MUST carry a top-level type=="result" event. With
            # --output-format stream-json the absence of a result line is a real
            # failure mode (truncated/partial stream, --verbose noise without a
            # result, stream-json error). Without this guard, rc=0 + raw NDJSON
            # falling back into `output` would be finalized as success=True and
            # falsely satisfy #needs: deps. Require result_event explicitly.
            if (
                result.returncode == 0
                and result_event is not None
                and output
                and self._is_success_output(
                    output=output,
                    json_payload=result_event,
                )
            ):
                return RunResult(success=True, output=output, **tokens)

            # Keyword detection for rate_limit/session_missing must NOT scan the
            # assistant's answer text — a SUCCESS result's prose mentioning "rate
            # limit"/"quota" would false-positive a bogus cooldown. Scan only
            # signal-bearing surfaces: stderr, dedicated rate_limit_event / error
            # NDJSON lines, the result event's subtype, and the result text ONLY
            # when it is an error result.
            scan_parts = [stderr]
            for line in stdout.splitlines():
                if "rate_limit_event" in line or '"type":"error"' in line:
                    scan_parts.append(line)
            if result_event is not None:
                subtype = result_event.get("subtype")
                if isinstance(subtype, str):
                    scan_parts.append(subtype)
                if subtype != "success":
                    err_text = result_event.get("result")
                    if isinstance(err_text, str):
                        scan_parts.append(err_text)
            combined = " ".join(scan_parts).lower()
            # Typed error: --resume against a non-existent UUID errors with this
            # exact phrase. Tools should fall back to a fresh session + state inject.
            if "no conversation found with session id" in combined:
                return RunResult(success=False, error="session_missing", **tokens)
            if any(kw in combined for kw in ("rate limit", "usage limit", "quota", "overloaded")):
                return RunResult(success=False, error="rate_limit", **tokens)

            # rc==0 but no result event → incomplete/partial stream. Surface a
            # clear error instead of passing the raw NDJSON blob through as if
            # it were the answer.
            if result.returncode == 0 and result_event is None:
                return RunResult(
                    success=False,
                    error=stderr or "incomplete stream-json output (no result event)",
                    **tokens,
                )

            return RunResult(
                success=False,
                error=stderr or output or "empty output",
                **tokens,
            )

        except subprocess.TimeoutExpired as exc:
            kind = getattr(exc, "timeout_kind", "hard")
            return RunResult(success=False, error="hang" if kind == "idle" else "timeout")
        except FileNotFoundError:
            return RunResult(success=False, error="claude CLI not found")
        except (OSError, ValueError) as e:
            return RunResult(success=False, error=str(e))

    @staticmethod
    def _extract_result_event(stdout: str) -> dict | None:
        """Return the LAST NDJSON event with type=='result', or None.

        stream-json emits one JSON object per line (init, assistant, tool_use,
        result). The final result event carries .result (text) and .usage. A
        single-object json payload on one line is parsed as one line too, so the
        legacy json-mode tests still pass through unchanged.
        """
        if not stdout:
            return None
        last_result = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(evt, dict) and evt.get("type") == "result":
                last_result = evt
        return last_result

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

    _ZERO_TOKENS = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }

    @staticmethod
    def _output_from_event(result_event: dict | None, fallback: str) -> str:
        """Output text from a result event: its ``.result`` string, else the
        re-serialized event (so structured errors don't re-include CLI noise),
        else ``fallback`` (raw stdout) when there is no result event."""
        if result_event is None:
            return fallback
        result = result_event.get("result")
        return result if isinstance(result, str) else json.dumps(result_event)

    @staticmethod
    def _tokens_from_event(result_event: dict | None) -> dict[str, int]:
        """The four RunResult token fields from a result event's ``.usage``
        (all-zero when absent). Field parity feeds quota calibration/analytics."""
        if result_event is None:
            return dict(ClaudeProvider._ZERO_TOKENS)
        usage = result_event.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        return {key: int(usage.get(key, 0) or 0) for key in ClaudeProvider._ZERO_TOKENS}

    @staticmethod
    def _parse_json_response(stdout: str) -> tuple[str, dict[str, int]]:
        """Parse Claude CLI JSON/NDJSON response → (output_text, token_dict).

        Falls back to (stdout, all-zero dict) when no result event is present.
        Kept as a single entry point for direct callers/tests; ``run()`` derives
        output + tokens from a once-extracted event via the helpers above.
        """
        if not stdout:
            return stdout, dict(ClaudeProvider._ZERO_TOKENS)
        event = ClaudeProvider._extract_result_event(stdout)
        return (
            ClaudeProvider._output_from_event(event, fallback=stdout),
            ClaudeProvider._tokens_from_event(event),
        )
