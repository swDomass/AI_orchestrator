from types import SimpleNamespace

import limits


def test_refresh_token_claude_returns_true_when_auth_status_shows_valid(monkeypatch):
    """auth status returns 0 AND output does NOT contain 'expired' → token is valid."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="Logged in as user@example.com", stderr="")

    monkeypatch.setattr(limits.subprocess, "run", fake_run)

    assert limits._refresh_token("claude") is True
    assert len(calls) == 1
    assert calls[0][0] == [limits._CLAUDE_CMD, "auth", "status"]


def test_refresh_token_claude_falls_through_when_auth_status_shows_expired(monkeypatch):
    """auth status returns 0 but output says 'expired' → must try Strategy 2."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if "auth" in cmd:
            return SimpleNamespace(returncode=0, stdout="Token expired", stderr="")
        # Strategy 2 succeeds
        return SimpleNamespace(returncode=0, stdout="pong", stderr="")

    monkeypatch.setattr(limits.subprocess, "run", fake_run)

    assert limits._refresh_token("claude") is True
    assert len(calls) == 2
    assert calls[0][0] == [limits._CLAUDE_CMD, "auth", "status"]
    assert calls[1][0][:2] == [limits._CLAUDE_CMD, "--print"]


def test_refresh_token_claude_returns_false_when_all_strategies_fail(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="auth failed")

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


def test_get_limits_requeries_after_successful_refresh(monkeypatch):
    """After a successful token refresh, get_limits re-queries cclimits."""
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
    monkeypatch.setattr(limits, "_refresh_token", lambda provider: True)
    monkeypatch.setattr(limits.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(limits, "_limits_cache", None)

    got = limits.get_limits()

    assert calls["n"] >= 2
    assert got.gemini.available is True
    assert got.gemini.remaining_pct == 99.0


def test_get_limits_requeries_even_when_refresh_returns_false(monkeypatch):
    """Re-query also happens if refresh command returns non-zero once."""
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
    monkeypatch.setattr(limits, "_limits_cache", None)

    got = limits.get_limits()

    assert calls["n"] >= 2
    assert got.gemini.available is True


def test_get_limits_caches_for_ttl_and_force_refresh_bypasses(monkeypatch):
    calls = {"n": 0}
    t = {"now": 0.0}

    def fake_monotonic():
        return t["now"]

    def fake_get_limits_fresh():
        calls["n"] += 1
        return limits.AllLimits(
            claude=limits.ProviderLimits(available=True, remaining_pct=float(calls["n"]), resets_in_sec=3600),
            gemini=limits.ProviderLimits(error="missing"),
            codex=limits.ProviderLimits(error="missing"),
        )

    monkeypatch.setattr(limits.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(limits, "_get_limits_fresh", fake_get_limits_fresh)
    monkeypatch.setattr(limits, "_limits_cache", None)

    first = limits.get_limits()
    second = limits.get_limits()
    assert first.claude.remaining_pct == 1.0
    assert second.claude.remaining_pct == 1.0
    assert calls["n"] == 1

    t["now"] += limits._LIMITS_CACHE_TTL_SEC + 1
    third = limits.get_limits()
    assert third.claude.remaining_pct == 2.0
    assert calls["n"] == 2

    forced = limits.get_limits(force_refresh=True)
    assert forced.claude.remaining_pct == 3.0
    assert calls["n"] == 3


def test_get_limits_cache_expires_by_earliest_reset(monkeypatch):
    calls = {"n": 0}
    t = {"now": 0.0}

    def fake_monotonic():
        return t["now"]

    def fake_get_limits_fresh():
        calls["n"] += 1
        return limits.AllLimits(
            claude=limits.ProviderLimits(available=False, remaining_pct=0.0, resets_in_sec=30),
            gemini=limits.ProviderLimits(error="missing"),
            codex=limits.ProviderLimits(error="missing"),
        )

    monkeypatch.setattr(limits.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(limits, "_get_limits_fresh", fake_get_limits_fresh)
    monkeypatch.setattr(limits, "_limits_cache", None)

    limits.get_limits()
    t["now"] += 20
    limits.get_limits()
    assert calls["n"] == 1

    t["now"] += 11
    limits.get_limits()
    assert calls["n"] == 2


def test_get_limits_retries_timeouts_soon(monkeypatch):
    calls = {"n": 0}
    t = {"now": 100.0}

    def fake_monotonic():
        return t["now"]

    def fake_get_limits_fresh():
        calls["n"] += 1
        return limits.AllLimits(
            claude=limits.ProviderLimits(error="cclimits timeout"),
            gemini=limits.ProviderLimits(error="cclimits timeout"),
            codex=limits.ProviderLimits(error="cclimits timeout"),
        )

    monkeypatch.setattr(limits.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(limits, "_get_limits_fresh", fake_get_limits_fresh)
    monkeypatch.setattr(limits, "_limits_cache", None)

    limits.get_limits()
    limits.get_limits()
    assert calls["n"] == 1

    t["now"] += limits._LIMITS_ERROR_CACHE_TTL_SEC + 1
    limits.get_limits()
    assert calls["n"] == 2


def test_get_limits_retries_non_resettable_error_snapshots_soon(monkeypatch):
    calls = {"n": 0}
    t = {"now": 10.0}

    def fake_monotonic():
        return t["now"]

    def fake_get_limits_fresh():
        calls["n"] += 1
        return limits.AllLimits(
            claude=limits.ProviderLimits(error="token expired"),
            gemini=limits.ProviderLimits(error="unknown"),
            codex=limits.ProviderLimits(error="missing"),
        )

    monkeypatch.setattr(limits.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(limits, "_get_limits_fresh", fake_get_limits_fresh)
    monkeypatch.setattr(limits, "_limits_cache", None)

    limits.get_limits()
    limits.get_limits()
    assert calls["n"] == 1

    t["now"] += limits._LIMITS_ERROR_CACHE_TTL_SEC + 1
    limits.get_limits()
    assert calls["n"] == 2


# ── WindowData population tests ───────────────────────────────────────────────

def test_parse_claude_windows_populated():
    """_parse_claude with five_hour + seven_day → windows has both entries."""
    data = {
        "status": "ok",
        "five_hour": {"remaining": "80%", "resets_in": "2h 30m"},
        "seven_day": {"remaining": "60%", "resets_in": "3d 2h"},
    }
    result = limits._parse_claude(data)
    assert "five_hour" in result.windows
    assert "seven_day" in result.windows
    assert abs(result.windows["five_hour"].remaining_pct - 80.0) < 0.1
    assert abs(result.windows["five_hour"].resets_in_sec - 9000) < 60
    assert abs(result.windows["seven_day"].remaining_pct - 60.0) < 0.1


def test_parse_claude_windows_empty_when_no_window_data():
    """_parse_claude with status=ok but no window keys → error, windows is empty."""
    data = {"status": "ok"}
    result = limits._parse_claude(data)
    assert result.error == "no window data"
    assert result.windows == {}


def test_parse_codex_windows_populated():
    """_parse_codex → windows populated with primary_window and secondary_window."""
    data = {
        "status": "ok",
        "primary_window": {"remaining": "70%", "resets_in": "1h"},
        "secondary_window": {"remaining": "90%", "resets_in": "4h"},
    }
    result = limits._parse_codex(data)
    assert "primary_window" in result.windows
    assert "secondary_window" in result.windows
    assert abs(result.windows["primary_window"].remaining_pct - 70.0) < 0.1
    assert abs(result.windows["secondary_window"].remaining_pct - 90.0) < 0.1


def test_parse_gemini_windows_populated():
    """_parse_gemini → windows populated per model with safe key names."""
    data = {
        "status": "ok",
        "models": {
            "gemini-2.5-pro": {"remaining": "95%", "resets_in": "30m"},
            "gemini-2.0-flash": {"remaining": "80%", "resets_in": "1h"},
        },
    }
    result = limits._parse_gemini(data)
    # Keys are sanitized: "gemini-2.5-pro" → "gemini_2_5_pro"
    assert "gemini_2_5_pro" in result.windows
    assert "gemini_2_0_flash" in result.windows
    assert abs(result.windows["gemini_2_5_pro"].remaining_pct - 95.0) < 0.1
