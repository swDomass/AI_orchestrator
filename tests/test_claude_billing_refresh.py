"""limits._refresh_token must not hand the API-credit key to the OAuth keep-alive.

With ORCH_CLAUDE_API_KEY configured, an ANTHROPIC_API_KEY in the refresh child's
environment would make `claude --print` authenticate with the key instead of the
subscription token, so the token cclimits depends on would stop being refreshed.
"""

import os
from types import SimpleNamespace

import pytest

import config
import limits

KEY = "sk-ant-test-refresh-0000"


@pytest.fixture
def refresh_calls(monkeypatch):
    """Key configured; subprocess.run in limits records every call.

    Strategy 1 (`auth status`) reports an expired token, so strategy 2
    (`claude --print`) runs and succeeds.
    """
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_KEY", KEY)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ORCH_CLAUDE_API_KEY", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if "auth" in cmd:
            return SimpleNamespace(returncode=1, stdout="token expired", stderr="")
        return SimpleNamespace(returncode=0, stdout="pong", stderr="")

    monkeypatch.setattr(limits.subprocess, "run", fake_run)
    return calls


def test_refresh_token_children_get_no_api_key(refresh_calls):
    assert limits._refresh_token("claude") is True
    assert len(refresh_calls) == 2  # auth status + claude --print
    for _cmd, kwargs in refresh_calls:
        env = kwargs.get("env")
        assert env is None or "ANTHROPIC_API_KEY" not in env
        assert env is None or KEY not in env.values()
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "ORCH_CLAUDE_API_KEY" not in os.environ


def test_refresh_token_passes_no_explicit_env(refresh_calls):
    """Characterisation: children inherit os.environ, which is key-free (config pops it)."""
    limits._refresh_token("claude")
    assert refresh_calls
    assert all("env" not in kwargs for _cmd, kwargs in refresh_calls)
