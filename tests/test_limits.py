from types import SimpleNamespace

import limits


def test_refresh_token_claude_returns_true_when_auth_status_succeeds(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(limits.subprocess, "run", fake_run)

    assert limits._refresh_token("claude") is True
    assert len(calls) == 1
    assert calls[0][0] == [limits._CLAUDE_CMD, "auth", "status"]


def test_refresh_token_claude_returns_false_when_all_strategies_fail(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(limits.subprocess, "run", fake_run)

    assert limits._refresh_token("claude") is False
    assert len(calls) == 2
    assert calls[0][0] == [limits._CLAUDE_CMD, "auth", "status"]
    assert calls[1][0][:2] == [limits._CLAUDE_CMD, "--print"]


def test_refresh_token_gemini_respects_command_return_code(monkeypatch):
    monkeypatch.setattr(
        limits.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    assert limits._refresh_token("gemini") is True

    monkeypatch.setattr(
        limits.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    assert limits._refresh_token("gemini") is False
