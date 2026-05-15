"""Tests for providers/openrouter.py — HTTP-based OpenRouter provider.

Mocks urllib.request.urlopen to exercise request format, token extraction,
and error mapping without making real HTTP calls.
"""

import json
import urllib.error
from io import BytesIO

import pytest

from providers.openrouter import OpenRouterProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_response(body):
    """Return a urlopen-compatible context manager that yields `body`."""
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
        url="https://or.test/v1/chat/completions",
        code=code,
        msg="error",
        hdrs=None,
        fp=BytesIO(body),
    )


@pytest.fixture
def provider(monkeypatch):
    """Provider with deterministic config (independent of the user's .env)."""
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "test-key-12345")
    monkeypatch.setattr("config.OPENROUTER_BASE_URL", "https://or.test/v1")
    monkeypatch.setattr("config.OPENROUTER_DEFAULT_MODEL", "test/default-model")
    return OpenRouterProvider()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_supports_sessions_is_false():
    assert OpenRouterProvider.supports_sessions is False


def test_provider_without_key_is_not_configured(monkeypatch):
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "")
    p = OpenRouterProvider()
    assert p.is_configured() is False


def test_provider_with_key_is_configured(provider):
    assert provider.is_configured() is True


def test_run_without_key_returns_auth_error(monkeypatch):
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "")
    p = OpenRouterProvider()
    result = p.run("hello")
    assert result.success is False
    assert "auth_error" in result.error


# ---------------------------------------------------------------------------
# Successful responses
# ---------------------------------------------------------------------------


def test_run_success_extracts_basic_tokens(provider, monkeypatch):
    body = json.dumps({
        "choices": [{"message": {"content": "Hi there"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    })
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.success is True
    assert result.output == "Hi there"
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.cache_read_input_tokens == 0
    assert result.cache_creation_input_tokens == 0


def test_run_extracts_cached_tokens(provider, monkeypatch):
    body = json.dumps({
        "choices": [{"message": {"content": "cached"}}],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 800},
        },
    })
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.cache_read_input_tokens == 800


def test_run_strips_whitespace_from_output(provider, monkeypatch):
    body = json.dumps({
        "choices": [{"message": {"content": "  \n\nhello  \n"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.output == "hello"


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def test_run_429_sets_cooldown_and_returns_rate_limit(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(_make_http_error(429, b'{"error":"rate"}')),
    )
    assert provider.is_cooling_down() is False
    result = provider.run("test")
    assert result.success is False
    assert "rate_limit" in result.error
    assert provider.is_cooling_down() is True


def test_run_401_returns_auth_error_no_cooldown(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(_make_http_error(401, b"bad key")),
    )
    result = provider.run("test")
    assert result.success is False
    assert "auth_error" in result.error
    assert provider.is_cooling_down() is False


def test_run_403_returns_auth_error(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(_make_http_error(403, b"forbidden")),
    )
    result = provider.run("test")
    assert result.success is False
    assert "auth_error" in result.error


def test_run_500_returns_unreachable(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(_make_http_error(500, b"oops")),
    )
    result = provider.run("test")
    assert result.success is False
    assert "unreachable" in result.error


def test_run_400_returns_generic_http_error(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(_make_http_error(400, b"bad request")),
    )
    result = provider.run("test")
    assert result.success is False
    assert "http_400" in result.error


def test_run_url_error_returns_unreachable(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError("DNS failure")),
    )
    result = provider.run("test")
    assert result.success is False
    assert "unreachable" in result.error


def test_run_timeout(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: (_ for _ in ()).throw(TimeoutError()),
    )
    result = provider.run("test")
    assert result.success is False
    assert result.error == "timeout"


def test_run_invalid_json_returns_parse_error(provider, monkeypatch):
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: _fake_response("<html>not json</html>"),
    )
    result = provider.run("test")
    assert result.success is False
    assert "parse_error" in result.error


def test_run_no_choices_returns_parse_error(provider, monkeypatch):
    body = json.dumps({"usage": {"prompt_tokens": 1, "completion_tokens": 0}})
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.success is False
    assert "parse_error" in result.error


def test_run_empty_content_returns_parse_error(provider, monkeypatch):
    body = json.dumps({
        "choices": [{"message": {"content": "   "}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 0},
    })
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.success is False
    assert "parse_error" in result.error


def test_run_embedded_error_returns_api_error(provider, monkeypatch):
    body = json.dumps({"error": {"message": "model not available"}})
    monkeypatch.setattr(
        "providers.openrouter.urllib.request.urlopen",
        lambda req, timeout: _fake_response(body),
    )
    result = provider.run("test")
    assert result.success is False
    assert "api_error" in result.error
    assert "model not available" in result.error


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


def _capture_request(monkeypatch, success_body=None):
    """Install a fake urlopen that records the outgoing request and returns success."""
    captured = {}
    body = success_body or json.dumps({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _fake_response(body)

    monkeypatch.setattr("providers.openrouter.urllib.request.urlopen", fake_urlopen)
    return captured


def test_request_url_and_method(provider, monkeypatch):
    captured = _capture_request(monkeypatch)
    provider.run("hello")
    assert captured["url"] == "https://or.test/v1/chat/completions"
    assert captured["method"] == "POST"


def test_request_auth_header(provider, monkeypatch):
    captured = _capture_request(monkeypatch)
    provider.run("hello")
    assert captured["headers"]["authorization"] == "Bearer test-key-12345"


def test_request_content_type_json(provider, monkeypatch):
    captured = _capture_request(monkeypatch)
    provider.run("hello")
    assert captured["headers"]["content-type"] == "application/json"


def test_request_includes_user_message(provider, monkeypatch):
    captured = _capture_request(monkeypatch)
    provider.run("my prompt text")
    assert captured["body"]["messages"] == [{"role": "user", "content": "my prompt text"}]


def test_request_uses_default_model_without_forced(provider, monkeypatch):
    captured = _capture_request(monkeypatch)
    provider.run("hello")
    assert captured["body"]["model"] == "test/default-model"


def test_request_uses_forced_model_over_default(provider, monkeypatch):
    captured = _capture_request(monkeypatch)
    provider._forced_model = "z-ai/glm-5"
    provider.run("hello")
    assert captured["body"]["model"] == "z-ai/glm-5"


def test_request_timeout_passed_to_urlopen(provider, monkeypatch):
    captured = _capture_request(monkeypatch)
    provider.run("hello", timeout=42)
    assert captured["timeout"] == 42


def test_session_params_accepted_but_ignored(provider, monkeypatch):
    """session_id/resume must be accepted without raising even though sessions are unsupported."""
    _capture_request(monkeypatch)
    result = provider.run("test", session_id="some-uuid", resume=True)
    assert result.success is True
