"""Tests for openrouter_budget.fetch_budget() — GET /api/v1/key.

Mocks urllib.request.urlopen (same pattern as tests/test_providers_openrouter.py)
so no real HTTP calls happen. Every non-success case must collapse to
(None, None, None) and fetch_budget() must never raise.
"""

import json
import urllib.error
from email.message import Message
from io import BytesIO

import pytest

import openrouter_budget

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_providers_openrouter.py)
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
        url="https://openrouter.test/api/v1/key",
        code=code,
        msg="error",
        hdrs=Message(),
        fp=BytesIO(body),
    )


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch):
    """Give every test a deterministic key + base URL, overridable per test."""
    monkeypatch.setattr(openrouter_budget.config, "OPENROUTER_API_KEY", "test-key-12345")
    monkeypatch.setattr(openrouter_budget.config, "OPENROUTER_BASE_URL", "https://openrouter.test/api/v1")
    yield


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


def test_fetch_budget_success_returns_full_triple(monkeypatch):
    """Measured shape (2026-09-04): payload lives under the top-level "data" key."""
    body = json.dumps({
        "data": {
            "limit": 5,
            "limit_remaining": 4.89,
            "limit_reset": "daily",
            "usage_daily": 0.108,
        }
    })
    monkeypatch.setattr(
        openrouter_budget.urllib.request, "urlopen",
        lambda req, timeout: _fake_response(body),
    )
    limit, remaining, reset_cadence = openrouter_budget.fetch_budget()
    assert limit == 5.0
    assert remaining == 4.89
    assert reset_cadence == "daily"


def test_fetch_budget_sends_bearer_auth_header(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["req"] = req
        captured["timeout"] = timeout
        return _fake_response(json.dumps({"data": {"limit": 5, "limit_remaining": 4.89, "limit_reset": "daily"}}))

    monkeypatch.setattr(openrouter_budget.urllib.request, "urlopen", fake_urlopen)
    openrouter_budget.fetch_budget(timeout=7.5)

    assert captured["req"].full_url == "https://openrouter.test/api/v1/key"
    assert captured["req"].get_header("Authorization") == "Bearer test-key-12345"
    assert captured["timeout"] == 7.5


# ---------------------------------------------------------------------------
# Failure cases — every one must collapse to (None, None, None), never raise
# ---------------------------------------------------------------------------


def test_fetch_budget_http_401_returns_all_none(monkeypatch):
    monkeypatch.setattr(
        openrouter_budget.urllib.request, "urlopen",
        lambda req, timeout: (_ for _ in ()).throw(_make_http_error(401, b'{"error": "invalid key"}')),
    )
    assert openrouter_budget.fetch_budget() == (None, None, None)


def test_fetch_budget_url_error_returns_all_none(monkeypatch):
    monkeypatch.setattr(
        openrouter_budget.urllib.request, "urlopen",
        lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError("DNS failure")),
    )
    assert openrouter_budget.fetch_budget() == (None, None, None)


def test_fetch_budget_timeout_returns_all_none(monkeypatch):
    monkeypatch.setattr(
        openrouter_budget.urllib.request, "urlopen",
        lambda req, timeout: (_ for _ in ()).throw(TimeoutError()),
    )
    assert openrouter_budget.fetch_budget() == (None, None, None)


def test_fetch_budget_broken_json_returns_all_none(monkeypatch):
    monkeypatch.setattr(
        openrouter_budget.urllib.request, "urlopen",
        lambda req, timeout: _fake_response("<html>not json</html>"),
    )
    assert openrouter_budget.fetch_budget() == (None, None, None)


def test_fetch_budget_missing_fields_returns_all_none(monkeypatch):
    """"data" key present but empty — no limit/limit_remaining at all."""
    monkeypatch.setattr(
        openrouter_budget.urllib.request, "urlopen",
        lambda req, timeout: _fake_response(json.dumps({"data": {}})),
    )
    assert openrouter_budget.fetch_budget() == (None, None, None)


def test_fetch_budget_missing_data_key_returns_all_none(monkeypatch):
    """Top-level "data" key itself absent (KeyError branch)."""
    monkeypatch.setattr(
        openrouter_budget.urllib.request, "urlopen",
        lambda req, timeout: _fake_response(json.dumps({"unexpected": "shape"})),
    )
    assert openrouter_budget.fetch_budget() == (None, None, None)


def test_fetch_budget_null_limit_returns_all_none(monkeypatch):
    """Structurally valid response reporting no spending cap (limit AND
    limit_remaining both JSON null together — the observed shape)."""
    body = json.dumps({"data": {"limit": None, "limit_remaining": None, "limit_reset": None}})
    monkeypatch.setattr(
        openrouter_budget.urllib.request, "urlopen",
        lambda req, timeout: _fake_response(body),
    )
    assert openrouter_budget.fetch_budget() == (None, None, None)


def test_fetch_budget_no_api_key_returns_all_none_without_network_call(monkeypatch):
    monkeypatch.setattr(openrouter_budget.config, "OPENROUTER_API_KEY", "")
    called = {"n": 0}

    def fake_urlopen(req, timeout):
        called["n"] += 1
        return _fake_response(json.dumps({"data": {"limit": 5, "limit_remaining": 4.89, "limit_reset": "daily"}}))

    monkeypatch.setattr(openrouter_budget.urllib.request, "urlopen", fake_urlopen)

    assert openrouter_budget.fetch_budget() == (None, None, None)
    assert called["n"] == 0


def test_fetch_budget_non_numeric_limit_returns_all_none(monkeypatch):
    body = json.dumps({"data": {"limit": "not-a-number", "limit_remaining": 4.89, "limit_reset": "daily"}})
    monkeypatch.setattr(
        openrouter_budget.urllib.request, "urlopen",
        lambda req, timeout: _fake_response(body),
    )
    assert openrouter_budget.fetch_budget() == (None, None, None)


def test_fetch_budget_never_raises_on_unexpected_exception(monkeypatch):
    """Defense in depth: even an unanticipated OSError subclass must not escape."""
    def blow_up(req, timeout):
        raise OSError("weird proxy failure")

    monkeypatch.setattr(openrouter_budget.urllib.request, "urlopen", blow_up)
    assert openrouter_budget.fetch_budget() == (None, None, None)
