"""OpenRouter HTTP provider.

Calls the OpenRouter chat-completions endpoint directly via urllib.request
(stdlib only — no `requests` dependency). Pay-per-token, OpenAI-compatible API.

Designed exclusively for single-call, non-agentic tasks (heartbeat LLM-check,
memory summaries, telegram /chat). Never enters the default fallback chain —
activation requires an explicit #openrouter or #or_* tag in the task text.

`supports_sessions = False`: each call is stateless. Multi-turn conversations
would require client-side message-history persistence (not implemented).

`read_only` is accepted but has no semantic effect — the provider has no tool
execution surface, so all calls are inherently read-only.
"""

import json
import urllib.error
import urllib.request

import config
from providers.base import BaseProvider, RunResult


_AUTH_STATUS_CODES = (401, 403)
_RATE_LIMIT_STATUS = 429


class OpenRouterProvider(BaseProvider):
    name = "openrouter"
    supports_sessions = False

    def __init__(self) -> None:
        super().__init__()
        self._api_key: str = config.OPENROUTER_API_KEY
        self._base_url: str = config.OPENROUTER_BASE_URL.rstrip("/")
        self._default_model: str = config.OPENROUTER_DEFAULT_MODEL

    def is_configured(self) -> bool:
        """True if an API key is present. Dispatcher uses this to decide registration."""
        return bool(self._api_key)

    def run(
        self,
        task: str,
        cwd: str | None = None,                  # accepted but unused (no fs access)
        timeout: int = 120,                       # default lower than other providers
        read_only: bool = False,                  # accepted but no semantic effect
        session_id: str | None = None,            # accepted but ignored
        resume: bool = False,                     # accepted but ignored
    ) -> RunResult:
        if not self._api_key:
            return RunResult(success=False, error="auth_error: OPENROUTER_API_KEY not set")

        model = self._forced_model or self._default_model
        print(f"  [openrouter → {model}] Führe Task aus...")

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": task}],
        }
        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/swDomass/AI_orchestrator",
                "X-Title": "AI_orchestrator",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return self._handle_http_error(e)
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", str(e))
            return RunResult(success=False, error=f"unreachable: {reason}")
        except TimeoutError:
            return RunResult(success=False, error="timeout")
        except (OSError, ValueError) as e:
            return RunResult(success=False, error=f"network: {e}")

        return self._parse_success_response(raw)

    def _handle_http_error(self, err: urllib.error.HTTPError) -> RunResult:
        status = err.code
        try:
            body_bytes = err.read() or b""
        except (OSError, ValueError):
            body_bytes = b""
        detail = body_bytes.decode("utf-8", errors="replace")[:300]

        if status == _RATE_LIMIT_STATUS:
            self.set_cooldown()
            return RunResult(success=False, error=f"rate_limit: {detail}")
        if status in _AUTH_STATUS_CODES:
            return RunResult(success=False, error=f"auth_error: {detail}")
        if 500 <= status < 600:
            return RunResult(success=False, error=f"unreachable: HTTP {status} {detail}")
        return RunResult(success=False, error=f"http_{status}: {detail}")

    @staticmethod
    def _parse_success_response(raw: str) -> RunResult:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return RunResult(success=False, error=f"parse_error: invalid JSON ({raw[:200]})")

        if not isinstance(data, dict):
            return RunResult(success=False, error="parse_error: response is not an object")

        # OpenRouter sometimes returns 200 with an embedded error object
        if "error" in data and "choices" not in data:
            err_obj = data["error"]
            msg = err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
            return RunResult(success=False, error=f"api_error: {msg}")

        choices = data.get("choices") or []
        if not choices:
            return RunResult(success=False, error="parse_error: no choices in response")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return RunResult(success=False, error="parse_error: empty content")

        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        # Some providers behind OpenRouter expose prompt_tokens_details.cached_tokens
        cached = 0
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens") or 0)

        return RunResult(
            success=True,
            output=content.strip(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cached,
            # OpenRouter does not expose Anthropic-style cache_creation tokens
            cache_creation_input_tokens=0,
        )
