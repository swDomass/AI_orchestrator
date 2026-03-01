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
    calls = []

    def ok_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        limits.subprocess,
        "run",
        ok_run,
    )
    assert limits._refresh_token("gemini") is True
    assert calls[0][0] == [limits._GEMINI_CMD, "--prompt", "", "--yolo", "--output-format", "text"]
    assert calls[0][1]["input"] == "ping"

    monkeypatch.setattr(
        limits.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    assert limits._refresh_token("gemini") is False


def test_refresh_token_gemini_accepts_cached_credentials_hint(monkeypatch):
    monkeypatch.setattr(
        limits.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Loaded cached credentials.",
        ),
    )
    assert limits._refresh_token("gemini") is True


def test_get_limits_requeries_after_expired_even_when_refresh_returns_false(monkeypatch):
    expired = {
        "claude": {"status": "ok", "five_hour": {"remaining": "50%", "resets_in": "1h"}},
        "gemini": {"status": "error", "error": "expired"},
        "codex": {"status": "ok", "primary_window": {"remaining": "40%", "resets_in": "2h"}},
    }
    fresh = {
        "claude": {"status": "ok", "five_hour": {"remaining": "50%", "resets_in": "1h"}},
        "gemini": {
            "status": "ok",
            "models": {
                "gemini-2.5-pro": {"remaining": "99%", "resets_in": "30m"},
            },
        },
        "codex": {"status": "ok", "primary_window": {"remaining": "40%", "resets_in": "2h"}},
    }

    calls = {"n": 0}

    def fake_run_cclimits():
        calls["n"] += 1
        return expired if calls["n"] == 1 else fresh

    monkeypatch.setattr(limits, "_run_cclimits", fake_run_cclimits)
    monkeypatch.setattr(limits, "_refresh_token", lambda provider: False)
    monkeypatch.setattr(limits.time, "sleep", lambda *_args, **_kwargs: None)

    got = limits.get_limits()

    assert calls["n"] >= 2
    assert got.gemini.available is True
    assert got.gemini.remaining_pct == 99.0
