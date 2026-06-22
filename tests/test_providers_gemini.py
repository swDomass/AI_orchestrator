"""Tests for providers/gemini.py — dual-mode (HTTP API + legacy CLI fallback).

HTTP mode (GEMINI_API_KEY set) is the default after the consumer Gemini CLI
shutdown (2026-06-18); the CLI path is kept for Standard/Enterprise users.
Mocks urllib.request.urlopen (HTTP) and run_with_watchdog (CLI) so no real
network or subprocess calls happen.
"""

import json
import types
import urllib.error
from io import BytesIO

import pytest

from providers.gemini import GeminiProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_response(body):
    """Return a urlopen-compatible context manager yielding `body`."""
    class _Resp:
        def __init__(self, b):
            self._b = b if isinstance(b, bytes) else b.encode("utf-8")

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return _Resp(body)


def _make_http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://gem.test/v1beta/models/m:generateContent",
        code=code,
        msg="error",
        hdrs=None,
        fp=BytesIO(body),
    )


def _candidate(text, finish="STOP", usage=None):
    """Build a minimal generateContent success payload."""
    out = {
        "candidates": [{
            "content": {"parts": [{"text": text}] if text is not None else []},
            "finishReason": finish,
        }],
    }
    if usage is not None:
        out["usageMetadata"] = usage
    return json.dumps(out)


@pytest.fixture
def provider(monkeypatch):
    """HTTP-mode provider with deterministic config (independent of the user's .env)."""
    monkeypatch.setattr("config.GEMINI_API_KEY", "test-key-abc")
    monkeypatch.setattr("config.GEMINI_BASE_URL", "https://gem.test/v1beta")
    monkeypatch.setattr("config.GEMINI_DEFAULT_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr("config.GEMINI_MAX_OUTPUT_TOKENS", 8192)
    return GeminiProvider()


# ---------------------------------------------------------------------------
# Configuration / mode selection
# ---------------------------------------------------------------------------


def test_supports_sessions_is_false():
    assert GeminiProvider.supports_sessions is False


def test_is_configured_reflects_key(monkeypatch):
    monkeypatch.setattr("config.GEMINI_API_KEY", "")
    assert GeminiProvider().is_configured() is False
    monkeypatch.setattr("config.GEMINI_API_KEY", "k")
    assert GeminiProvider().is_configured() is True


def test_run_with_key_uses_http(provider, monkeypatch):
    """A key present must route to the HTTP path, never the CLI."""
    called = {"http": False, "cli": False}

    def fake_urlopen(req, timeout):
        called["http"] = True
        return _fake_response(_candidate("ok", usage={"promptTokenCount": 1, "candidatesTokenCount": 1}))

    monkeypatch.setattr("providers.gemini.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "providers.gemini.run_with_watchdog",
        lambda *a, **k: called.__setitem__("cli", True),
    )
    result = provider.run("test")
    assert result.success is True
    assert called["http"] is True and called["cli"] is False


def test_run_without_key_uses_cli(monkeypatch):
    """No key → fall back to the legacy CLI via run_with_watchdog."""
    monkeypatch.setattr("config.GEMINI_API_KEY", "")
    fake = types.SimpleNamespace(stdout="cli answer", stderr="", returncode=0)
    captured = {}

    def fake_watchdog(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input_text")
        return fake

    monkeypatch.setattr("providers.gemini.run_with_watchdog", fake_watchdog)
    result = GeminiProvider().run("the task")
    assert result.success is True
    assert result.output == "cli answer"
    assert captured["cmd"][0].endswith("gemini") or "gemini" in captured["cmd"][0]
    assert captured["input"] == "the task"


# ---------------------------------------------------------------------------
# HTTP — successful responses
# ---------------------------------------------------------------------------


def test_run_success_extracts_basic_tokens(provider, monkeypatch):
    body = _candidate("Hi there", usage={"promptTokenCount": 100, "candidatesTokenCount": 50})
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.success is True
    assert result.output == "Hi there"
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.cache_read_input_tokens == 0
    assert result.cache_creation_input_tokens == 0


def test_thinking_tokens_folded_into_output(provider, monkeypatch):
    """gemini-3.x thinking tokens are billed as output → fold into output_tokens."""
    body = _candidate("answer", usage={
        "promptTokenCount": 26,
        "candidatesTokenCount": 3,
        "thoughtsTokenCount": 147,
    })
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.output_tokens == 150  # 3 + 147


def test_cached_content_tokens_extracted(provider, monkeypatch):
    body = _candidate("cached", usage={
        "promptTokenCount": 1000,
        "candidatesTokenCount": 10,
        "cachedContentTokenCount": 800,
    })
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.cache_read_input_tokens == 800


def test_multiple_parts_concatenated_and_stripped(provider, monkeypatch):
    body = json.dumps({
        "candidates": [{
            "content": {"parts": [{"text": "  hello "}, {"text": "world  "}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2},
    })
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.output == "hello world"


# ---------------------------------------------------------------------------
# HTTP — error mapping
# ---------------------------------------------------------------------------


def test_run_429_sets_cooldown_and_returns_rate_limit(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(_make_http_error(429, b'{"error":"quota"}')),
    )
    assert provider.is_cooling_down() is False
    result = provider.run("test")
    assert result.success is False
    # bare code (no suffix): orchestrator retry loop matches by exact equality
    assert result.error == "rate_limit"
    assert provider.is_cooling_down() is True


def test_run_401_returns_auth_error_no_cooldown(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(_make_http_error(401, b"bad key")),
    )
    result = provider.run("test")
    assert result.success is False
    assert "auth_error" in result.error
    assert provider.is_cooling_down() is False


def test_run_403_returns_auth_error(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(_make_http_error(403, b"forbidden")),
    )
    result = provider.run("test")
    assert "auth_error" in result.error


def test_run_500_returns_unreachable(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(_make_http_error(500, b"oops")),
    )
    result = provider.run("test")
    assert result.error == "unreachable"  # bare code for exact-match retry path


def test_run_400_returns_generic_http_error(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(_make_http_error(400, b"bad")),
    )
    result = provider.run("test")
    assert "http_400" in result.error


def test_run_url_error_returns_unreachable(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError("DNS")),
    )
    result = provider.run("test")
    assert result.error == "unreachable"  # bare code for exact-match retry path


def test_run_timeout(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(TimeoutError()),
    )
    result = provider.run("test")
    assert result.error == "timeout"


def test_run_invalid_json_returns_parse_error(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: _fake_response("<html>nope</html>"),
    )
    result = provider.run("test")
    assert "parse_error" in result.error


def test_run_no_candidates_returns_parse_error(provider, monkeypatch):
    body = json.dumps({"usageMetadata": {"promptTokenCount": 1}})
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert "parse_error" in result.error


def test_prompt_block_returns_model_refusal(provider, monkeypatch):
    body = json.dumps({"promptFeedback": {"blockReason": "SAFETY"}})
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.success is False
    assert "model_refusal" in result.error
    assert "SAFETY" in result.error


def test_empty_text_with_safety_finish_is_refusal(provider, monkeypatch):
    body = _candidate(None, finish="SAFETY", usage={"promptTokenCount": 5})
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.success is False
    assert "model_refusal" in result.error


def test_empty_text_with_max_tokens_hints_at_cap(provider, monkeypatch):
    body = _candidate(None, finish="MAX_TOKENS", usage={"promptTokenCount": 5, "thoughtsTokenCount": 8192})
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.success is False
    assert "MAX_TOKENS" in result.error


def test_max_tokens_with_partial_text_is_failure(provider, monkeypatch):
    """Truncated (MAX_TOKENS) output must fail even with partial text — an
    incomplete second-opinion review should not be finalized as success."""
    body = _candidate("partial review that got cut o", finish="MAX_TOKENS",
                      usage={"promptTokenCount": 5, "candidatesTokenCount": 16384})
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.success is False
    assert "MAX_TOKENS" in result.error


def test_blocked_finish_with_partial_text_is_refusal(provider, monkeypatch):
    body = _candidate("some flagged text", finish="SAFETY",
                      usage={"promptTokenCount": 5, "candidatesTokenCount": 10})
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.success is False
    assert "model_refusal" in result.error


def test_malformed_usage_metadata_does_not_crash(provider, monkeypatch):
    """Odd usageMetadata shapes (string/None) must not raise — token accounting
    is best-effort and never crashes a run."""
    body = json.dumps({
        "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
        "usageMetadata": {
            "promptTokenCount": "abc",       # unparseable -> 0
            "candidatesTokenCount": None,     # -> 0
            "thoughtsTokenCount": "12.0",     # float-string -> 12
        },
    })
    monkeypatch.setattr(
        "providers.gemini.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.success is True
    assert result.output == "ok"
    assert result.input_tokens == 0
    assert result.output_tokens == 12


# ---------------------------------------------------------------------------
# HTTP — request construction
# ---------------------------------------------------------------------------


def _capture_request(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _fake_response(_candidate("ok", usage={"promptTokenCount": 1, "candidatesTokenCount": 1}))

    monkeypatch.setattr("providers.gemini.urllib.request.urlopen", fake_urlopen)
    return captured


def test_request_url_uses_model_and_generatecontent(provider, monkeypatch):
    captured = _capture_request(monkeypatch)
    provider.run("hi")
    assert captured["url"] == "https://gem.test/v1beta/models/gemini-3.5-flash:generateContent"
    assert captured["method"] == "POST"


def test_request_api_key_header(provider, monkeypatch):
    captured = _capture_request(monkeypatch)
    provider.run("hi")
    assert captured["headers"]["x-goog-api-key"] == "test-key-abc"
    assert captured["headers"]["content-type"] == "application/json"


def test_request_body_user_content(provider, monkeypatch):
    captured = _capture_request(monkeypatch)
    provider.run("my prompt")
    assert captured["body"]["contents"] == [
        {"role": "user", "parts": [{"text": "my prompt"}]}
    ]
    assert captured["body"]["generationConfig"]["maxOutputTokens"] == 8192


def test_request_forced_model_overrides_default(provider, monkeypatch):
    captured = _capture_request(monkeypatch)
    provider._forced_model = "gemini-3.1-pro-preview"
    provider.run("hi")
    assert captured["url"].endswith("/models/gemini-3.1-pro-preview:generateContent")


def test_request_timeout_passed(provider, monkeypatch):
    captured = _capture_request(monkeypatch)
    provider.run("hi", timeout=42)
    assert captured["timeout"] == 42


def test_session_params_accepted_but_ignored(provider, monkeypatch):
    _capture_request(monkeypatch)
    result = provider.run("test", session_id="uuid", resume=True)
    assert result.success is True
