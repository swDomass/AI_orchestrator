"""Gemini provider — HTTP API (preferred) with legacy CLI fallback.

The consumer Gemini CLI (Code Assist for individuals / AI Pro / AI Ultra) was
shut down 2026-06-18. When GEMINI_API_KEY is set, this provider calls the Google
Gemini REST API directly via urllib (stdlib only, mirroring providers/openrouter.py).
Without a key it falls back to the legacy `gemini` CLI for Standard/Enterprise
subscription users who retain CLI access.

The orchestrator assembles the system prompt + SAFETY_RULES into the task text
itself (config.get_system_prompt), so the whole task is sent as the single user
turn — no separate systemInstruction (which would duplicate the safety rules and
matches how the legacy CLI received the whole task via stdin).

read_only has no semantic effect in HTTP mode (the REST API has no tool/exec
surface — every call is pure text-in/text-out). supports_sessions stays False.
"""

import json
import logging
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

import config
from providers.base import BaseProvider, RunResult
from providers.process_runner import run_with_watchdog
from config import TASK_TIMEOUT_SEC, CLI_IDLE_TIMEOUT_NO_LIVENESS_SEC

logger = logging.getLogger(__name__)

_GEMINI_CMD = shutil.which("gemini") or "gemini"

_AUTH_STATUS_CODES = (401, 403)
_RATE_LIMIT_STATUS = 429
# finishReason values that mean the model was blocked — treat as a refusal even
# if some partial text leaked through (the content was flagged).
_BLOCKED_FINISH_REASONS = {
    "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
}


def _safe_int(value) -> int:
    """Best-effort int for usageMetadata fields — never raise on odd API shapes
    (string/float/None), since token accounting must not crash a run."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


class GeminiProvider(BaseProvider):
    name = "gemini"

    def is_configured(self) -> bool:
        """True if the HTTP API key is set. When False the provider falls back
        to the legacy `gemini` CLI (which only works for Standard/Enterprise
        accounts after the consumer CLI shutdown)."""
        return bool(config.GEMINI_API_KEY)

    def run(
        self,
        task: str,
        cwd: str | None = None,
        timeout: int = TASK_TIMEOUT_SEC,
        read_only: bool = False,
        session_id: str | None = None,  # accepted but ignored (supports_sessions=False)
        resume: bool = False,            # accepted but ignored
    ) -> RunResult:
        if config.GEMINI_API_KEY:
            return self._run_http(task, timeout=timeout)
        return self._run_cli(task, cwd=cwd, timeout=timeout, read_only=read_only)

    # ------------------------------------------------------------------ HTTP API

    def _run_http(self, task: str, timeout: int) -> RunResult:
        model = self._forced_model or config.GEMINI_DEFAULT_MODEL
        print(f"  [gemini → {model} (http)] Führe Task aus...")

        payload = {
            "contents": [{"role": "user", "parts": [{"text": task}]}],
            "generationConfig": {"maxOutputTokens": config.GEMINI_MAX_OUTPUT_TOKENS},
        }
        body = json.dumps(payload).encode("utf-8")
        url = f"{config.GEMINI_BASE_URL.rstrip('/')}/models/{model}:generateContent"
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": config.GEMINI_API_KEY,
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return self._handle_http_error(e)
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", str(e))
            logger.warning("[gemini] network unreachable: %s", reason)
            return RunResult(success=False, error="unreachable")
        except TimeoutError:
            return RunResult(success=False, error="timeout")
        except (OSError, ValueError) as e:
            return RunResult(success=False, error=f"network: {e}")

        return self._parse_http_response(raw)

    def _handle_http_error(self, err: urllib.error.HTTPError) -> RunResult:
        status = err.code
        try:
            detail = (err.read() or b"").decode("utf-8", errors="replace")[:300]
        except (OSError, ValueError):
            detail = ""

        # rate_limit / unreachable MUST be bare codes: the orchestrator retry
        # loop matches them by exact equality (orchestrator._execute_with_retries
        # `error in ("rate_limit","unreachable","timeout","hang")` and the
        # `error == "rate_limit"` quota-reset path). A suffixed string would miss
        # those branches. Detail goes to the log instead.
        if status == _RATE_LIMIT_STATUS:
            self.set_cooldown()
            if detail:
                logger.warning("[gemini] HTTP 429 rate limit: %s", detail)
            return RunResult(success=False, error="rate_limit")
        if status in _AUTH_STATUS_CODES:
            return RunResult(success=False, error=f"auth_error: {detail}")
        if 500 <= status < 600:
            logger.warning("[gemini] HTTP %d: %s", status, detail)
            return RunResult(success=False, error="unreachable")
        return RunResult(success=False, error=f"http_{status}: {detail}")

    @staticmethod
    def _parse_http_response(raw: str) -> RunResult:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return RunResult(success=False, error=f"parse_error: invalid JSON ({raw[:200]})")
        if not isinstance(data, dict):
            return RunResult(success=False, error="parse_error: response is not an object")

        # Prompt-level block (no candidates produced at all).
        feedback = data.get("promptFeedback") or {}
        block_reason = feedback.get("blockReason")
        if block_reason:
            return RunResult(success=False, error=f"model_refusal: prompt blocked ({block_reason})")

        candidates = data.get("candidates") or []
        if not candidates:
            return RunResult(success=False, error="parse_error: no candidates in response")

        cand = candidates[0] if isinstance(candidates[0], dict) else {}
        finish = cand.get("finishReason")
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(
            p.get("text", "") for p in parts if isinstance(p, dict)
        ).strip()

        # finishReason failures take precedence over any partial text: a blocked
        # or truncated response is unreliable for an orchestrator that acts on it,
        # so fail loud rather than finalize an incomplete second-opinion review.
        if finish in _BLOCKED_FINISH_REASONS:
            return RunResult(success=False, error=f"model_refusal: finishReason={finish}")
        if finish == "MAX_TOKENS":
            detail = f"{len(text)} chars produced" if text else "empty — thinking consumed the budget"
            return RunResult(
                success=False,
                error=f"output truncated (MAX_TOKENS, {detail} — raise GEMINI_MAX_OUTPUT_TOKENS)",
            )
        if not text:
            return RunResult(success=False, error=f"empty output (finishReason={finish})")

        usage = data.get("usageMetadata") or {}
        input_tokens = _safe_int(usage.get("promptTokenCount"))
        # gemini-3.x are thinking models: thinking tokens are consumed/billed as
        # output, so fold thoughtsTokenCount into output_tokens for accurate cost.
        output_tokens = (
            _safe_int(usage.get("candidatesTokenCount"))
            + _safe_int(usage.get("thoughtsTokenCount"))
        )
        cache_read = _safe_int(usage.get("cachedContentTokenCount"))

        return RunResult(
            success=True,
            output=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            # Gemini does not expose Anthropic-style cache_creation tokens.
            cache_creation_input_tokens=0,
        )

    # ------------------------------------------------------- legacy CLI fallback

    def _run_cli(
        self,
        task: str,
        cwd: str | None,
        timeout: int,
        read_only: bool,
    ) -> RunResult:
        """No GEMINI_API_KEY set — use the legacy `gemini` CLI (Standard/Enterprise
        subscription auth). Consumer CLI auth was shut down 2026-06-18."""
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

            # A truncated prompt still yields rc==0 plus a plausible answer to
            # whatever fragment arrived — see providers/process_runner._feed_stdin.
            # Bare code — matches the exact-equality convention noted above.
            # getattr(): provider tests fake the result as a SimpleNamespace.
            # Deliberately NOT checked up front: a child dying early (rate limit)
            # ALSO breaks the pipe, so an early check would mask that better
            # classification. Applied where it is the only explanation.
            stdin_error = getattr(result, "stdin_error", None)

            if result.returncode == 0 and output:
                if stdin_error:
                    return RunResult(success=False, error="stdin_incomplete", output=stdin_error)
                return RunResult(success=True, output=output)

            combined = (output + stderr).lower()
            if any(kw in combined for kw in ("rate limit", "quota", "429", "resource exhausted")):
                return RunResult(success=False, error="rate_limit")
            if any(kw in combined for kw in ("unavailable", "503", "connection", "unreachable", "network")):
                return RunResult(success=False, error="unreachable")

            # Nothing better matched — but ONLY trust this at rc == 0. At
            # rc != 0 the broken pipe is almost always a SYMPTOM: any CLI dying
            # early (not logged in, model not found, a panic) also breaks the
            # feeder, because the prompt (~100 KB) far exceeds the OS pipe
            # buffer. Those keep their real error; the diagnosis is appended.
            if stdin_error and result.returncode == 0:
                return RunResult(success=False, error="stdin_incomplete", output=stdin_error)

            return RunResult(
                success=False,
                error=stderr or output or "empty output",
                output=(output + "\n[stdin] " + stdin_error) if stdin_error else output,
            )

        except subprocess.TimeoutExpired as exc:
            kind = getattr(exc, "timeout_kind", "hard")
            return RunResult(success=False, error="hang" if kind == "idle" else "timeout")
        except FileNotFoundError:
            return RunResult(success=False, error="gemini CLI not found")
        except (OSError, ValueError) as e:
            return RunResult(success=False, error=str(e))
