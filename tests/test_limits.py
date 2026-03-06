import threading
from types import SimpleNamespace

import limits
import pytest


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


def _reset_bg_state(monkeypatch):
    """Helper: reset all background-thread state so each test starts clean."""
    monkeypatch.setattr(limits, "_limits_cache", None)
    monkeypatch.setattr(limits, "_bg_thread", None)
    monkeypatch.setattr(limits, "_bg_wake", threading.Event())
    monkeypatch.setattr(limits, "_cache_ready", threading.Event())


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
            "models": {"gemini-2.5-pro": {"remaining": "99%", "resets_in": "30m"}},
        },
        "codex": {"status": "ok", "primary_window": {"remaining": "40%", "resets_in": "2h"}},
    }
    calls = {"n": 0}

    def fake_run_cclimits():
        calls["n"] += 1
        return expired if calls["n"] == 1 else fresh

    _reset_bg_state(monkeypatch)
    monkeypatch.setattr(limits, "_run_cclimits", fake_run_cclimits)
    monkeypatch.setattr(limits, "_refresh_token", lambda provider: True)
    monkeypatch.setattr(limits.time, "sleep", lambda *_args, **_kwargs: None)

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
            "models": {"gemini-2.5-pro": {"remaining": "99%", "resets_in": "30m"}},
        },
        "codex": {"status": "ok", "primary_window": {"remaining": "40%", "resets_in": "2h"}},
    }
    calls = {"n": 0}

    def fake_run_cclimits():
        calls["n"] += 1
        return expired if calls["n"] == 1 else fresh

    _reset_bg_state(monkeypatch)
    monkeypatch.setattr(limits, "_run_cclimits", fake_run_cclimits)
    monkeypatch.setattr(limits, "_refresh_token", lambda provider: False)
    monkeypatch.setattr(limits.time, "sleep", lambda *_args, **_kwargs: None)

    got = limits.get_limits()

    assert calls["n"] >= 2
    assert got.gemini.available is True


# ── Background-thread polling interval tests ──────────────────────────────────

def test_compute_next_poll_sec_returns_available_interval():
    result = limits.AllLimits(
        claude=limits.ProviderLimits(available=True, remaining_pct=50.0, resets_in_sec=3600),
        gemini=limits.ProviderLimits(error="missing"),
        codex=limits.ProviderLimits(error="missing"),
    )
    assert limits._compute_next_poll_sec(result) == limits._BG_POLL_AVAILABLE_SEC


def test_compute_next_poll_sec_returns_reset_time_when_at_limit():
    result = limits.AllLimits(
        claude=limits.ProviderLimits(available=False, remaining_pct=0.0, resets_in_sec=120),
        gemini=limits.ProviderLimits(error="missing"),
        codex=limits.ProviderLimits(error="missing"),
    )
    assert limits._compute_next_poll_sec(result) == 120


def test_compute_next_poll_sec_clamps_reset_to_minimum():
    """Reset in 5 s → clamped to 60 s minimum so we don't spin."""
    result = limits.AllLimits(
        claude=limits.ProviderLimits(available=False, remaining_pct=0.0, resets_in_sec=5),
        gemini=limits.ProviderLimits(error="missing"),
        codex=limits.ProviderLimits(error="missing"),
    )
    assert limits._compute_next_poll_sec(result) == 60


def test_compute_next_poll_sec_returns_error_interval_for_timeout():
    result = limits.AllLimits(
        claude=limits.ProviderLimits(error="cclimits timeout"),
        gemini=limits.ProviderLimits(error="cclimits timeout"),
        codex=limits.ProviderLimits(error="cclimits timeout"),
    )
    assert limits._compute_next_poll_sec(result) == limits._BG_POLL_ERROR_SEC


def test_compute_next_poll_sec_returns_error_interval_for_transient():
    result = limits.AllLimits(
        claude=limits.ProviderLimits(error="token expired"),
        gemini=limits.ProviderLimits(error="unknown"),
        codex=limits.ProviderLimits(error="missing"),
    )
    assert limits._compute_next_poll_sec(result) == limits._BG_POLL_ERROR_SEC


def test_get_limits_returns_cached_result_without_extra_call(monkeypatch):
    """With a populated cache, get_limits() returns immediately without calling _get_limits_fresh."""
    call_count = {"n": 0}

    def fake_fresh():
        call_count["n"] += 1
        return limits.AllLimits(claude=limits.ProviderLimits(available=True, remaining_pct=80.0))

    pre_cached = limits.AllLimits(
        claude=limits.ProviderLimits(available=True, remaining_pct=42.0),
        gemini=limits.ProviderLimits(error="missing"),
        codex=limits.ProviderLimits(error="missing"),
    )
    cache_ready = threading.Event()
    cache_ready.set()
    monkeypatch.setattr(limits, "_limits_cache", (pre_cached, 0.0))
    monkeypatch.setattr(limits, "_cache_ready", cache_ready)
    monkeypatch.setattr(limits, "_start_bg_thread", lambda: None)  # prevent thread from overwriting cache
    monkeypatch.setattr(limits, "_get_limits_fresh", fake_fresh)

    result = limits.get_limits()
    assert result.claude.remaining_pct == 42.0
    assert call_count["n"] == 0


def test_get_limits_force_refresh_calls_fresh_and_updates_cache(monkeypatch):
    """force_refresh=True always calls _get_limits_fresh synchronously."""
    call_count = {"n": 0}

    def fake_fresh():
        call_count["n"] += 1
        return limits.AllLimits(
            claude=limits.ProviderLimits(available=True, remaining_pct=float(call_count["n"]) * 10),
            gemini=limits.ProviderLimits(error="missing"),
            codex=limits.ProviderLimits(error="missing"),
        )

    _reset_bg_state(monkeypatch)
    monkeypatch.setattr(limits, "_get_limits_fresh", fake_fresh)

    r1 = limits.get_limits(force_refresh=True)
    r2 = limits.get_limits(force_refresh=True)
    assert call_count["n"] == 2
    assert r1.claude.remaining_pct == 10.0
    assert r2.claude.remaining_pct == 20.0


def test_get_limits_caches_unavailable_fallback_after_initial_wait_timeout(monkeypatch):
    class FakeEvent:
        def __init__(self):
            self._is_set = False
            self.wait_calls = 0

        def wait(self, timeout=None):
            self.wait_calls += 1
            return self._is_set

        def set(self):
            self._is_set = True

    fake_ready = FakeEvent()
    monkeypatch.setattr(limits, "_limits_cache", None)
    monkeypatch.setattr(limits, "_cache_ready", fake_ready)
    monkeypatch.setattr(limits, "_start_bg_thread", lambda: None)

    first = limits.get_limits()
    second = limits.get_limits()

    assert first.claude.error == "cclimits unavailable"
    assert second.claude.error == "cclimits unavailable"
    assert limits._limits_cache is not None
    assert fake_ready.wait_calls == 2


def test_get_limits_fresh_serializes_concurrent_cclimits_calls(monkeypatch):
    call_state = {"active": 0, "calls": 0}
    state_lock = threading.Lock()
    first_entered = threading.Event()
    allow_first_to_finish = threading.Event()
    overlap_detected = threading.Event()

    def fake_run_cclimits():
        with state_lock:
            call_state["active"] += 1
            call_state["calls"] += 1
            call_no = call_state["calls"]
            if call_state["active"] > 1:
                overlap_detected.set()
        try:
            if call_no == 1:
                first_entered.set()
                allow_first_to_finish.wait(timeout=1)
            return {
                "claude": {"status": "ok", "five_hour": {"remaining": "50%", "resets_in": "1h"}},
                "gemini": {"status": "missing"},
                "codex": {"status": "missing"},
            }
        finally:
            with state_lock:
                call_state["active"] -= 1

    monkeypatch.setattr(limits, "_fresh_limits_lock", threading.Lock())  # isolate from bg threads
    monkeypatch.setattr(limits, "_run_cclimits", fake_run_cclimits)
    monkeypatch.setattr(limits, "_refresh_token", lambda _provider: False)

    results = []

    def worker():
        results.append(limits._get_limits_fresh())

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    assert first_entered.wait(timeout=1)
    t2.start()
    assert not overlap_detected.wait(timeout=0.1)
    allow_first_to_finish.set()
    t1.join(timeout=1)
    t2.join(timeout=1)

    assert call_state["calls"] == 2
    assert len(results) == 2
    assert all(r.claude.available for r in results)


def test_bg_refresh_loop_preserves_force_refresh_wakeup_until_wait(monkeypatch):
    class FakeWakeEvent:
        def __init__(self):
            self.flag = False
            self.wait_flags = []

        def clear(self):
            self.flag = False

        def set(self):
            self.flag = True

        def wait(self, timeout=None):
            self.wait_flags.append(self.flag)
            raise RuntimeError("stop loop")

    fake_wake = FakeWakeEvent()

    def fake_fresh():
        # Simulate force_refresh() waking the background thread after the loop
        # already started its iteration but before it begins waiting.
        fake_wake.set()
        return limits.AllLimits(
            claude=limits.ProviderLimits(available=True, remaining_pct=75.0, resets_in_sec=300),
            gemini=limits.ProviderLimits(error="missing"),
            codex=limits.ProviderLimits(error="missing"),
        )

    _reset_bg_state(monkeypatch)
    monkeypatch.setattr(limits, "_bg_wake", fake_wake)
    monkeypatch.setattr(limits, "_get_limits_fresh", fake_fresh)
    monkeypatch.setattr(limits, "_compute_next_poll_sec", lambda _result: 90)

    with pytest.raises(RuntimeError, match="stop loop"):
        limits._bg_refresh_loop()

    assert fake_wake.wait_flags == [True]


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
