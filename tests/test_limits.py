import threading
import time
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
    monkeypatch.setattr(limits, "_refresh_failed_until", {})


def test_get_limits_requeries_after_successful_refresh(monkeypatch):
    """After a successful token refresh, _get_limits_fresh re-queries cclimits."""
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

    # Call _get_limits_fresh directly to test refresh logic without bg-thread races
    got = limits._get_limits_fresh()

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

    # Call _get_limits_fresh directly to test refresh logic without bg-thread races
    got = limits._get_limits_fresh()

    assert calls["n"] >= 2
    assert got.gemini.available is True


def test_refresh_failed_cooldown_skips_retry(monkeypatch):
    """After a failed refresh, subsequent calls skip the refresh until cooldown expires."""
    expired = {
        "claude": {"status": "ok", "five_hour": {"remaining": "50%", "resets_in": "1h"}},
        "gemini": {"status": "error", "error": "expired"},
        "codex": {"status": "ok", "primary_window": {"remaining": "40%", "resets_in": "2h"}},
    }
    refresh_calls = {"n": 0}

    def fake_refresh(provider):
        refresh_calls["n"] += 1
        return False  # always fail

    _reset_bg_state(monkeypatch)
    monkeypatch.setattr(limits, "_run_cclimits", lambda: expired)
    monkeypatch.setattr(limits, "_refresh_token", fake_refresh)
    monkeypatch.setattr(limits.time, "sleep", lambda *_args, **_kwargs: None)

    # First call: refresh attempted and fails, cooldown set
    limits.get_limits(force_refresh=True)
    assert refresh_calls["n"] == 1
    assert "gemini" in limits._refresh_failed_until

    # Second call within cooldown: refresh must NOT be attempted again
    # (force_refresh bypasses the cache so _get_limits_fresh runs again)
    limits.get_limits(force_refresh=True)
    assert refresh_calls["n"] == 1  # still 1, not 2


def test_refresh_false_positive_sets_cooldown(monkeypatch):
    """If _refresh_token returns True but cclimits still shows expired, set cooldown."""
    # cclimits always reports gemini as expired (refresh was a false positive)
    expired = {
        "claude": {"status": "ok", "five_hour": {"remaining": "50%", "resets_in": "1h"}},
        "gemini": {"status": "error", "error": "expired"},
        "codex": {"status": "ok", "primary_window": {"remaining": "40%", "resets_in": "2h"}},
    }
    refresh_calls = {"n": 0}

    def fake_refresh(provider):
        refresh_calls["n"] += 1
        return True  # CLI says success, but token is still expired

    _reset_bg_state(monkeypatch)
    monkeypatch.setattr(limits, "_run_cclimits", lambda: expired)
    monkeypatch.setattr(limits, "_refresh_token", fake_refresh)
    monkeypatch.setattr(limits.time, "sleep", lambda *_args, **_kwargs: None)

    # First call: refresh "succeeds" but token still expired → cooldown set
    limits.get_limits(force_refresh=True)
    assert refresh_calls["n"] == 1
    assert "gemini" in limits._refresh_failed_until

    # Second call within cooldown: refresh must NOT be attempted again
    # (force_refresh bypasses the cache so _get_limits_fresh runs again)
    limits.get_limits(force_refresh=True)
    assert refresh_calls["n"] == 1  # still 1, cooldown prevents retry


def test_refresh_failed_cooldown_cleared_on_success(monkeypatch):
    """A successful refresh clears the cooldown for that provider."""
    expired = {
        "claude": {"status": "ok", "five_hour": {"remaining": "50%", "resets_in": "1h"}},
        "gemini": {"status": "error", "error": "expired"},
        "codex": {"status": "ok", "primary_window": {"remaining": "40%", "resets_in": "2h"}},
    }
    fresh = {
        "claude": {"status": "ok", "five_hour": {"remaining": "50%", "resets_in": "1h"}},
        "gemini": {"status": "ok", "models": {"gemini-2.5-pro": {"remaining": "99%", "resets_in": "30m"}}},
        "codex": {"status": "ok", "primary_window": {"remaining": "40%", "resets_in": "2h"}},
    }
    calls = {"cclimits": 0, "refresh": 0}

    def fake_cclimits():
        calls["cclimits"] += 1
        return expired if calls["cclimits"] == 1 else fresh

    _reset_bg_state(monkeypatch)
    # Pre-seed a failed cooldown
    monkeypatch.setattr(limits, "_refresh_failed_until", {"gemini": time.monotonic() - 1})
    monkeypatch.setattr(limits, "_run_cclimits", fake_cclimits)
    monkeypatch.setattr(limits, "_refresh_token", lambda p: True)  # success
    monkeypatch.setattr(limits.time, "sleep", lambda *_args, **_kwargs: None)

    got = limits.get_limits()
    # Cooldown was expired, so refresh ran and succeeded
    assert "gemini" not in limits._refresh_failed_until
    assert got.gemini.available is True


def test_post_refresh_requery_bypasses_disk_cache(monkeypatch):
    """After token refresh, re-queries must use use_cache=False to avoid reading
    the stale 'expired' result that the first cclimits call wrote to disk."""
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
    calls = []  # track (use_cache,) for each call

    def fake_run(timeout_sec, *, use_cache=True):
        calls.append(use_cache)
        # First call (cached): returns expired; subsequent (no-cache): returns fresh
        return expired if use_cache else fresh

    _reset_bg_state(monkeypatch)
    monkeypatch.setattr(limits, "_run_cclimits_with_timeout", fake_run)
    monkeypatch.setattr(limits, "_refresh_token", lambda provider: True)
    monkeypatch.setattr(limits.time, "sleep", lambda *_args, **_kwargs: None)

    got = limits._get_limits_fresh()

    # First call should use cache; post-refresh re-queries must NOT use cache
    assert calls[0] is True, "initial cclimits call should use disk cache"
    assert any(c is False for c in calls[1:]), "post-refresh re-queries must bypass disk cache"
    assert got.gemini.available is True


def test_force_fresh_bypasses_disk_cache(monkeypatch):
    """force_fresh=True makes the initial cclimits call bypass disk cache."""
    calls = []

    def fake_run(timeout_sec, *, use_cache=True):
        calls.append(use_cache)
        return {
            "claude": {"status": "ok", "five_hour": {"remaining": "80%", "resets_in": "2h"}},
            "gemini": {"status": "missing"},
            "codex": {"status": "missing"},
        }

    _reset_bg_state(monkeypatch)
    monkeypatch.setattr(limits, "_run_cclimits_with_timeout", fake_run)

    limits._get_limits_fresh(force_fresh=True)
    assert calls[0] is False, "force_fresh=True must bypass disk cache"


def test_force_refresh_passes_force_fresh(monkeypatch):
    """get_limits(force_refresh=True) delegates to _get_limits_fresh(force_fresh=True)."""
    kwargs_seen = {}

    def fake_fresh(**kwargs):
        kwargs_seen.update(kwargs)
        return limits.AllLimits(
            claude=limits.ProviderLimits(available=True, remaining_pct=50.0),
            gemini=limits.ProviderLimits(error="missing"),
            codex=limits.ProviderLimits(error="missing"),
        )

    _reset_bg_state(monkeypatch)
    monkeypatch.setattr(limits, "_get_limits_fresh", fake_fresh)
    limits.get_limits(force_refresh=True)
    assert kwargs_seen.get("force_fresh") is True


def test_bg_loop_survives_exception(monkeypatch):
    """Background refresh loop must not die on unexpected exceptions."""
    call_count = {"n": 0}

    def fake_fresh(on_preliminary=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated crash")
        # Second call: return valid data, then stop the loop
        return limits.AllLimits(
            claude=limits.ProviderLimits(available=True, remaining_pct=70.0),
            gemini=limits.ProviderLimits(error="missing"),
            codex=limits.ProviderLimits(error="missing"),
        )

    class FakeWakeEvent:
        def __init__(self):
            self.wait_count = 0

        def clear(self):
            pass

        def set(self):
            pass

        def wait(self, timeout=None):
            self.wait_count += 1
            if self.wait_count >= 2:
                # SystemExit (BaseException) bypasses except Exception
                raise SystemExit("stop loop")

    fake_wake = FakeWakeEvent()
    _reset_bg_state(monkeypatch)
    monkeypatch.setattr(limits, "_bg_wake", fake_wake)
    monkeypatch.setattr(limits, "_get_limits_fresh", fake_fresh)
    monkeypatch.setattr(limits, "_compute_next_poll_sec", lambda _result: 90)

    with pytest.raises(SystemExit, match="stop loop"):
        limits._bg_refresh_loop()

    # The loop survived the first crash and made a second call
    assert call_count["n"] == 2
    # _cache_ready must be set even after crash
    assert limits._cache_ready.is_set()


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

    def fake_fresh(**_kw):
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

    def fake_fresh(**_kw):
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
            # Use SystemExit (BaseException) to bypass the except Exception handler
            raise SystemExit("stop loop")

    fake_wake = FakeWakeEvent()

    def fake_fresh(on_preliminary=None):
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

    with pytest.raises(SystemExit, match="stop loop"):
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


# ── HTTP 429 handling tests ────────────────────────────────────────────────────

def _reset_429_state(monkeypatch):
    """Reset all 429 estimation state."""
    monkeypatch.setattr(limits, "_429_snapshots", {})
    monkeypatch.setattr(limits, "_429_estimated_usage", {})
    monkeypatch.setattr(limits, "_429_notified", set())
    # Disable local JSONL fallback so tests control which tier is exercised
    monkeypatch.setattr(limits, "_get_claude_limits_from_local", lambda *_: None)


def test_is_provider_429_detects_error():
    assert limits._is_provider_429({"error": "HTTP 429", "details": "Too Many Requests"})
    assert limits._is_provider_429({"error": "HTTP 429"})
    assert not limits._is_provider_429({"status": "ok"})
    assert not limits._is_provider_429({"error": "expired"})
    assert not limits._is_provider_429({})


def test_providers_with_429_returns_affected_set():
    raw = {
        "claude": {"error": "HTTP 429", "details": "Too Many Requests"},
        "gemini": {"status": "ok", "models": {}},
        "codex": {"status": "ok"},
    }
    assert limits._providers_with_429(raw) == {"claude"}


def test_providers_with_429_returns_empty_when_no_429():
    raw = {
        "claude": {"status": "ok", "five_hour": {"remaining": "50%"}},
        "gemini": {"status": "ok", "models": {}},
        "codex": {"status": "ok"},
    }
    assert limits._providers_with_429(raw) == set()


def test_estimate_task_usage_pct_by_duration():
    """Tier 3: duration heuristic when no tokens or text available."""
    assert limits.estimate_task_usage_pct(10) == 2.0
    assert limits.estimate_task_usage_pct(59) == 2.0
    assert limits.estimate_task_usage_pct(60) == 5.0
    assert limits.estimate_task_usage_pct(200) == 5.0
    assert limits.estimate_task_usage_pct(300) == 10.0
    assert limits.estimate_task_usage_pct(500) == 10.0
    assert limits.estimate_task_usage_pct(600) == 15.0
    assert limits.estimate_task_usage_pct(1200) == 15.0


def test_estimate_task_usage_pct_from_actual_tokens():
    """Tier 1: actual token counts from provider (e.g. Claude JSON output)."""
    # 10000 input + 2000 output * 5 weight = 20000 effective
    # 20000 / 15000 (claude default) ≈ 1.33%
    pct = limits.estimate_task_usage_pct(
        input_tokens=10000, output_tokens=2000, provider="claude",
    )
    assert abs(pct - 20000 / 15000) < 0.01

    # Large task: 50000 input + 10000 output = 100000 effective → 6.67%
    pct = limits.estimate_task_usage_pct(
        input_tokens=50000, output_tokens=10000, provider="claude",
    )
    assert abs(pct - 100000 / 15000) < 0.01


def test_estimate_task_usage_pct_from_text():
    """Tier 2: text-based estimate from prompt/output character lengths."""
    # 4000 chars prompt = ~1000 tokens input
    # 2000 chars output = ~500 tokens output * 5 weight = 2500
    # effective = 3500, / 15000 ≈ 0.233%
    pct = limits.estimate_task_usage_pct(
        prompt_text="x" * 4000, output_text="y" * 2000, provider="claude",
    )
    assert abs(pct - 3500 / 15000) < 0.01


def test_estimate_task_usage_pct_tokens_beat_text_and_duration():
    """Tier 1 (actual tokens) takes priority over Tier 2 (text) and Tier 3 (duration)."""
    pct_tokens = limits.estimate_task_usage_pct(
        duration_sec=600,  # would be 15% via duration
        input_tokens=1000, output_tokens=200,  # 1000 + 200*5 = 2000 / 15000 = 0.133%
        prompt_text="x" * 100_000,  # would be much larger via text
        provider="claude",
    )
    assert pct_tokens < 1.0  # tokens win over duration (15%) and text


def test_estimate_task_usage_pct_duration_can_beat_short_text():
    """Tier 3 (duration) wins over Tier 2 (text) if it is more conservative (P3 finding)."""
    # Duration: 600s → 15%
    # Text: ~0.023%
    pct = limits.estimate_task_usage_pct(
        duration_sec=600,
        prompt_text="x" * 400, output_text="y" * 200,
        provider="claude",
    )
    assert pct == 15.0  # duration wins (conservative fallback for tools)


def test_estimate_task_usage_pct_respects_provider_budget():
    """Different providers have different tokens_per_pct budgets."""
    kwargs = dict(input_tokens=10000, output_tokens=2000)
    effective = 10000 + 2000 * 5  # 20000

    pct_claude = limits.estimate_task_usage_pct(**kwargs, provider="claude")
    pct_gemini = limits.estimate_task_usage_pct(**kwargs, provider="gemini")

    # Gemini has higher budget → lower percentage for same tokens
    assert pct_claude > pct_gemini
    assert abs(pct_claude - effective / 15000) < 0.01
    assert abs(pct_gemini - effective / 100000) < 0.01


def test_estimate_task_usage_pct_minimum_clamp():
    """Token/text-based estimates are clamped to at least 0.1%."""
    pct = limits.estimate_task_usage_pct(input_tokens=1, output_tokens=0, provider="claude")
    assert pct == 0.1


def test_report_estimated_usage_accumulates_only_in_429_mode(monkeypatch):
    """report_estimated_usage only accumulates when _429_base_snapshot is set."""
    _reset_429_state(monkeypatch)

    # Without base snapshot, reporting does nothing
    limits.report_estimated_usage("claude", 5.0)
    assert limits._429_estimated_usage == {}

    # Set a base snapshot
    base_pl = limits.ProviderLimits(available=True, remaining_pct=80.0, windows={
        "five_hour": limits.WindowData(remaining_pct=80.0, resets_in_sec=3600),
    })
    monkeypatch.setattr(limits, "_429_snapshots", {"claude": (base_pl, time.monotonic())})

    limits.report_estimated_usage("claude", 5.0)
    limits.report_estimated_usage("claude", 3.0)
    assert limits._429_estimated_usage["claude"] == {"five_hour": 8.0}


def test_report_estimated_usage_scales_nested_long_windows(monkeypatch):
    _reset_429_state(monkeypatch)

    base_pl = limits.ProviderLimits(
        available=True,
        remaining_pct=80.0,
        windows={
            "five_hour": limits.WindowData(remaining_pct=80.0, resets_in_sec=3600),
            "seven_day": limits.WindowData(remaining_pct=90.0, resets_in_sec=86400),
        },
    )
    monkeypatch.setattr(limits, "_429_snapshots", {"claude": (base_pl, time.monotonic())})

    limits.report_estimated_usage("claude", 24.0)

    assert limits._429_estimated_usage["claude"]["five_hour"] == 24.0
    assert abs(limits._429_estimated_usage["claude"]["seven_day"] - 1.0) < 0.01


def test_build_429_fallback_provider_adjusts_remaining():
    base = limits.ProviderLimits(
        available=True,
        remaining_pct=80.0,
        resets_in_sec=3600,
        windows={
            "five_hour": limits.WindowData(remaining_pct=80.0, resets_in_sec=3600),
            "seven_day": limits.WindowData(remaining_pct=90.0, resets_in_sec=86400),
        },
    )
    # Use current time as snapshot_time (no elapsed)
    adjusted = limits._build_429_fallback_provider(base, 15.0, time.monotonic())
    assert adjusted.remaining_pct == 65.0
    assert adjusted.available is True
    assert adjusted.windows["five_hour"].remaining_pct == 65.0
    assert abs(adjusted.windows["seven_day"].remaining_pct - 89.375) < 0.01
    assert "15%" in adjusted.error


def test_build_429_fallback_provider_decrements_resets(monkeypatch):
    base = limits.ProviderLimits(
        available=True,
        remaining_pct=80.0,
        resets_in_sec=3600,
        windows={
            "five_hour": limits.WindowData(remaining_pct=80.0, resets_in_sec=3600),
        },
    )
    # Simulate snapshot taken 10 minutes (600s) ago
    snapshot_time = time.monotonic() - 600
    adjusted = limits._build_429_fallback_provider(base, 0.0, snapshot_time)
    
    # 3600 - 600 = 3000
    assert adjusted.resets_in_sec == 3000
    assert adjusted.windows["five_hour"].resets_in_sec == 3000


def test_build_429_fallback_provider_marks_unavailable_below_threshold():
    base = limits.ProviderLimits(
        available=True,
        remaining_pct=10.0,
        resets_in_sec=300,
        windows={"five_hour": limits.WindowData(remaining_pct=10.0, resets_in_sec=300)},
    )
    adjusted = limits._build_429_fallback_provider(base, 8.0, time.monotonic())
    assert adjusted.remaining_pct == 2.0
    assert adjusted.available is False


def test_optimistic_429_provider_is_available():
    p = limits._optimistic_429_provider()
    assert p.available is True
    assert p.remaining_pct == 100.0
    assert "429" in p.error


def test_get_limits_fresh_retries_on_429_then_succeeds(monkeypatch):
    """When cclimits returns 429, retry with backoff; if retry succeeds, use real data."""
    _reset_bg_state(monkeypatch)
    _reset_429_state(monkeypatch)

    calls = {"n": 0}
    def fake_run_cclimits():
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "claude": {"error": "HTTP 429", "details": "Too Many Requests"},
                "gemini": {"status": "ok", "models": {"flash": {"remaining": "99%", "resets_in": "1h"}}},
                "codex": {"status": "ok", "primary_window": {"remaining": "90%", "resets_in": "2h"}},
            }
        return {
            "claude": {"status": "ok", "five_hour": {"remaining": "75%", "resets_in": "2h"}},
            "gemini": {"status": "ok", "models": {"flash": {"remaining": "99%", "resets_in": "1h"}}},
            "codex": {"status": "ok", "primary_window": {"remaining": "90%", "resets_in": "2h"}},
        }

    monkeypatch.setattr(limits, "_fresh_limits_lock", threading.Lock())
    monkeypatch.setattr(limits, "_run_cclimits", fake_run_cclimits)
    monkeypatch.setattr(limits.time, "sleep", lambda *_: None)

    result = limits._get_limits_fresh()
    assert calls["n"] >= 2
    assert result.claude.available is True
    assert result.claude.remaining_pct == 75.0
    assert "claude" not in limits._429_snapshots


def test_get_limits_fresh_uses_short_retry_timeout_after_429(monkeypatch):
    _reset_bg_state(monkeypatch)
    _reset_429_state(monkeypatch)

    calls: list[int] = []

    def fake_run(timeout_sec, **_kwargs):
        calls.append(timeout_sec)
        return {
            "claude": {"error": "HTTP 429"},
            "gemini": {"status": "ok", "models": {"flash": {"remaining": "99%", "resets_in": "1h"}}},
            "codex": {"status": "ok", "primary_window": {"remaining": "90%", "resets_in": "2h"}},
        }

    monkeypatch.setattr(limits, "_fresh_limits_lock", threading.Lock())
    monkeypatch.setattr(limits, "_run_cclimits_with_timeout", fake_run)
    monkeypatch.setattr(limits.time, "sleep", lambda *_: None)
    import notifier
    monkeypatch.setattr(notifier, "notify_limits_429_fallback", lambda *a: None)

    limits._get_limits_fresh()

    assert calls == [
        limits._CCLIMITS_TIMEOUT_SEC,
        limits._CCLIMITS_429_RETRY_TIMEOUT_SEC,
        limits._CCLIMITS_429_RETRY_TIMEOUT_SEC,
    ]


def test_get_limits_fresh_falls_back_to_cache_on_persistent_429(monkeypatch):
    """When 429 persists after retries, fall back to cached data."""
    _reset_bg_state(monkeypatch)
    _reset_429_state(monkeypatch)

    def fake_run_cclimits():
        return {
            "claude": {"error": "HTTP 429", "details": "Too Many Requests"},
            "gemini": {"status": "ok", "models": {"flash": {"remaining": "99%", "resets_in": "1h"}}},
            "codex": {"status": "ok", "primary_window": {"remaining": "90%", "resets_in": "2h"}},
        }

    # Pre-populate cache with good data
    good_cache = limits.AllLimits(
        claude=limits.ProviderLimits(
            available=True, remaining_pct=60.0, resets_in_sec=1800,
            windows={"five_hour": limits.WindowData(remaining_pct=60.0, resets_in_sec=1800)},
        ),
        gemini=limits.ProviderLimits(available=True, remaining_pct=99.0),
        codex=limits.ProviderLimits(available=True, remaining_pct=90.0),
    )

    monkeypatch.setattr(limits, "_fresh_limits_lock", threading.Lock())
    monkeypatch.setattr(limits, "_limits_cache", (good_cache, time.monotonic()))
    monkeypatch.setattr(limits, "_run_cclimits", fake_run_cclimits)
    monkeypatch.setattr(limits.time, "sleep", lambda *_: None)
    # Suppress Telegram notifications in tests
    import notifier
    monkeypatch.setattr(notifier, "notify_limits_429_fallback", lambda *a: None)

    result = limits._get_limits_fresh()

    assert result.claude.available is True
    assert result.claude.remaining_pct == 60.0
    assert "429" in result.claude.error
    assert "claude" in limits._429_snapshots


def test_get_limits_fresh_ignores_stale_cache_on_persistent_429(monkeypatch):
    """A stale global cache must not seed the first 429 fallback snapshot."""
    _reset_bg_state(monkeypatch)
    _reset_429_state(monkeypatch)

    def fake_run_cclimits():
        return {
            "claude": {"error": "HTTP 429", "details": "Too Many Requests"},
            "gemini": {"status": "ok", "models": {"flash": {"remaining": "99%", "resets_in": "1h"}}},
            "codex": {"status": "ok", "primary_window": {"remaining": "90%", "resets_in": "2h"}},
        }

    stale_cache = limits.AllLimits(
        claude=limits.ProviderLimits(
            available=True, remaining_pct=60.0, resets_in_sec=1800,
            windows={"five_hour": limits.WindowData(remaining_pct=60.0, resets_in_sec=1800)},
        ),
        gemini=limits.ProviderLimits(available=True, remaining_pct=99.0),
        codex=limits.ProviderLimits(available=True, remaining_pct=90.0),
    )

    monkeypatch.setattr(limits, "_fresh_limits_lock", threading.Lock())
    monkeypatch.setattr(
        limits,
        "_limits_cache",
        (stale_cache, time.monotonic() - limits._429_MAX_BASE_AGE_SEC - 1),
    )
    monkeypatch.setattr(limits, "_run_cclimits", fake_run_cclimits)
    monkeypatch.setattr(limits.time, "sleep", lambda *_: None)
    import notifier
    monkeypatch.setattr(notifier, "notify_limits_429_fallback", lambda *a: None)

    result = limits._get_limits_fresh()

    assert result.claude.available is True
    assert result.claude.remaining_pct == 100.0
    assert "assumed available" in result.claude.error


def test_get_limits_fresh_ignores_error_cache_on_persistent_429(monkeypatch):
    """A fresh cache entry without real capacity data must not seed 429 fallback."""
    _reset_bg_state(monkeypatch)
    _reset_429_state(monkeypatch)

    def fake_run_cclimits():
        return {
            "claude": {"error": "HTTP 429", "details": "Too Many Requests"},
            "gemini": {"status": "ok", "models": {"flash": {"remaining": "99%", "resets_in": "1h"}}},
            "codex": {"status": "ok", "primary_window": {"remaining": "90%", "resets_in": "2h"}},
        }

    error_cache = limits.AllLimits(
        claude=limits.ProviderLimits(error="cclimits timeout"),
        gemini=limits.ProviderLimits(available=True, remaining_pct=99.0),
        codex=limits.ProviderLimits(available=True, remaining_pct=90.0),
    )

    monkeypatch.setattr(limits, "_fresh_limits_lock", threading.Lock())
    monkeypatch.setattr(limits, "_limits_cache", (error_cache, time.monotonic()))
    monkeypatch.setattr(limits, "_run_cclimits", fake_run_cclimits)
    monkeypatch.setattr(limits.time, "sleep", lambda *_: None)
    import notifier
    monkeypatch.setattr(notifier, "notify_limits_429_fallback", lambda *a: None)

    result = limits._get_limits_fresh()

    assert result.claude.available is True
    assert result.claude.remaining_pct == 100.0
    assert "assumed available" in result.claude.error


def test_apply_429_fallback_ignores_stale_active_snapshot(monkeypatch):
    """An active 429 provider must not keep using a base snapshot older than 1h."""
    _reset_429_state(monkeypatch)

    stale_base = limits.ProviderLimits(
        available=True,
        remaining_pct=25.0,
        resets_in_sec=1200,
        windows={"five_hour": limits.WindowData(remaining_pct=25.0, resets_in_sec=1200)},
    )
    monkeypatch.setattr(
        limits,
        "_429_snapshots",
        {"claude": (stale_base, time.monotonic() - limits._429_MAX_BASE_AGE_SEC - 1)},
    )
    monkeypatch.setattr(limits, "_429_estimated_usage", {"claude": {"five_hour": 12.0}})
    monkeypatch.setattr(limits, "_limits_cache", None)
    import notifier
    monkeypatch.setattr(notifier, "notify_limits_429_fallback", lambda *a: None)

    result = limits.AllLimits(
        claude=limits.ProviderLimits(error="HTTP 429"),
        gemini=limits.ProviderLimits(available=True, remaining_pct=90.0),
        codex=limits.ProviderLimits(available=True, remaining_pct=85.0),
    )

    adjusted = limits._apply_429_fallback(result, {"claude"})

    assert adjusted.claude.remaining_pct == 100.0
    assert "assumed available" in adjusted.claude.error
    assert limits._429_estimated_usage["claude"] == {}


def test_get_limits_fresh_uses_optimistic_on_cold_start_429(monkeypatch):
    """When 429 occurs without cache, assume provider is available."""
    _reset_bg_state(monkeypatch)
    _reset_429_state(monkeypatch)

    def fake_run_cclimits():
        return {
            "claude": {"error": "HTTP 429"},
            "gemini": {"status": "ok", "models": {"flash": {"remaining": "99%", "resets_in": "1h"}}},
            "codex": {"status": "ok", "primary_window": {"remaining": "90%", "resets_in": "2h"}},
        }

    monkeypatch.setattr(limits, "_fresh_limits_lock", threading.Lock())
    monkeypatch.setattr(limits, "_limits_cache", None)
    monkeypatch.setattr(limits, "_run_cclimits", fake_run_cclimits)
    monkeypatch.setattr(limits.time, "sleep", lambda *_: None)
    import notifier
    monkeypatch.setattr(notifier, "notify_limits_429_fallback", lambda *a: None)

    result = limits._get_limits_fresh()

    assert result.claude.available is True
    assert result.claude.remaining_pct == 100.0
    assert "assumed available" in result.claude.error


def test_get_limits_fresh_accumulates_estimated_usage(monkeypatch):
    """Estimated usage is subtracted from cached data across multiple calls."""
    _reset_bg_state(monkeypatch)
    _reset_429_state(monkeypatch)

    def fake_run_cclimits():
        return {
            "claude": {"error": "HTTP 429"},
            "gemini": {"status": "ok", "models": {"flash": {"remaining": "99%", "resets_in": "1h"}}},
            "codex": {"status": "ok", "primary_window": {"remaining": "90%", "resets_in": "2h"}},
        }

    good_cache = limits.AllLimits(
        claude=limits.ProviderLimits(
            available=True, remaining_pct=50.0, resets_in_sec=1800,
            windows={"five_hour": limits.WindowData(remaining_pct=50.0, resets_in_sec=1800)},
        ),
        gemini=limits.ProviderLimits(available=True, remaining_pct=99.0),
        codex=limits.ProviderLimits(available=True, remaining_pct=90.0),
    )

    monkeypatch.setattr(limits, "_fresh_limits_lock", threading.Lock())
    monkeypatch.setattr(limits, "_limits_cache", (good_cache, time.monotonic()))
    monkeypatch.setattr(limits, "_run_cclimits", fake_run_cclimits)
    monkeypatch.setattr(limits.time, "sleep", lambda *_: None)
    import notifier
    monkeypatch.setattr(notifier, "notify_limits_429_fallback", lambda *a: None)

    # First call: 429, fall back to cache (50%)
    result1 = limits._get_limits_fresh()
    assert result1.claude.remaining_pct == 50.0

    # Simulate task consuming estimated 10%
    limits.report_estimated_usage("claude", 10.0)

    # Second call: 429 still, should show 50% - 10% = 40%
    result2 = limits._get_limits_fresh()
    assert result2.claude.remaining_pct == 40.0
    assert result2.claude.available is True

    # Simulate another task consuming 5%
    limits.report_estimated_usage("claude", 5.0)

    # Third call: should show 50% - 15% = 35%
    result3 = limits._get_limits_fresh()
    assert result3.claude.remaining_pct == 35.0


def test_get_limits_fresh_clears_429_state_when_resolved(monkeypatch):
    """When 429 clears, reset all estimation state."""
    _reset_bg_state(monkeypatch)
    _reset_429_state(monkeypatch)

    calls = {"n": 0}
    def fake_run_cclimits():
        calls["n"] += 1
        if calls["n"] <= 3:  # first 3 calls: 429 (initial + 2 retries)
            return {
                "claude": {"error": "HTTP 429"},
                "gemini": {"status": "ok", "models": {"flash": {"remaining": "99%", "resets_in": "1h"}}},
                "codex": {"status": "ok", "primary_window": {"remaining": "90%", "resets_in": "2h"}},
            }
        return {
            "claude": {"status": "ok", "five_hour": {"remaining": "70%", "resets_in": "2h"}},
            "gemini": {"status": "ok", "models": {"flash": {"remaining": "99%", "resets_in": "1h"}}},
            "codex": {"status": "ok", "primary_window": {"remaining": "90%", "resets_in": "2h"}},
        }

    good_cache = limits.AllLimits(
        claude=limits.ProviderLimits(
            available=True, remaining_pct=80.0, resets_in_sec=3600,
            windows={"five_hour": limits.WindowData(remaining_pct=80.0, resets_in_sec=3600)},
        ),
        gemini=limits.ProviderLimits(available=True, remaining_pct=99.0),
        codex=limits.ProviderLimits(available=True, remaining_pct=90.0),
    )

    monkeypatch.setattr(limits, "_fresh_limits_lock", threading.Lock())
    monkeypatch.setattr(limits, "_limits_cache", (good_cache, time.monotonic()))
    monkeypatch.setattr(limits, "_run_cclimits", fake_run_cclimits)
    monkeypatch.setattr(limits.time, "sleep", lambda *_: None)
    import notifier
    monkeypatch.setattr(notifier, "notify_limits_429_fallback", lambda *a: None)
    monkeypatch.setattr(notifier, "notify_limits_429_cleared", lambda *a: None)

    # First call: 429 with cache fallback
    result1 = limits._get_limits_fresh()
    assert "claude" in limits._429_snapshots
    limits.report_estimated_usage("claude", 10.0)

    # Second call: 429 clears (call #4+ returns real data)
    result2 = limits._get_limits_fresh()
    assert result2.claude.remaining_pct == 70.0
    assert "claude" not in limits._429_snapshots
    assert limits._429_estimated_usage == {}


def test_compute_next_poll_sec_returns_429_interval():
    """When _429_snapshots is not empty, poll interval should be _BG_POLL_429_SEC."""
    import limits as lm
    old = lm._429_snapshots
    try:
        dummy_pl = limits.ProviderLimits()
        lm._429_snapshots = {"claude": (dummy_pl, 0.0)}
        result = limits.AllLimits(
            claude=limits.ProviderLimits(available=True, remaining_pct=50.0, resets_in_sec=3600),
            gemini=limits.ProviderLimits(error="missing"),
            codex=limits.ProviderLimits(error="missing"),
        )
        assert limits._compute_next_poll_sec(result) == limits._BG_POLL_429_SEC
    finally:
        lm._429_snapshots = old


def test_notify_429_sent_only_once_per_period(monkeypatch):
    """Telegram 429 notification is sent only once per 429 period per provider."""
    _reset_bg_state(monkeypatch)
    _reset_429_state(monkeypatch)

    notify_calls = []
    def fake_notify(name, pct):
        notify_calls.append(name)

    def fake_run_cclimits():
        return {
            "claude": {"error": "HTTP 429"},
            "gemini": {"status": "ok", "models": {"flash": {"remaining": "99%", "resets_in": "1h"}}},
            "codex": {"status": "ok", "primary_window": {"remaining": "90%", "resets_in": "2h"}},
        }

    good_cache = limits.AllLimits(
        claude=limits.ProviderLimits(available=True, remaining_pct=80.0, windows={
            "five_hour": limits.WindowData(remaining_pct=80.0, resets_in_sec=3600),
        }),
    )

    monkeypatch.setattr(limits, "_fresh_limits_lock", threading.Lock())
    monkeypatch.setattr(limits, "_limits_cache", (good_cache, time.monotonic()))
    monkeypatch.setattr(limits, "_run_cclimits", fake_run_cclimits)
    monkeypatch.setattr(limits.time, "sleep", lambda *_: None)
    import notifier
    monkeypatch.setattr(notifier, "notify_limits_429_fallback", fake_notify)

    # Call twice — notification only once
    limits._get_limits_fresh()
    limits._get_limits_fresh()

    assert notify_calls == ["claude"]


def test_apply_429_fallback_re_notifies_after_provider_recovers(monkeypatch):
    """A provider that recovers during another provider's 429 period can notify again."""
    _reset_429_state(monkeypatch)

    notify_calls = []

    def fake_notify(name, pct):
        notify_calls.append(name)

    import notifier
    monkeypatch.setattr(notifier, "notify_limits_429_fallback", fake_notify)
    monkeypatch.setattr(limits, "_limits_cache", None)
    monkeypatch.setattr(limits, "_429_notified", {"claude", "gemini"})

    result_recovered = limits.AllLimits(
        claude=limits.ProviderLimits(
            available=True,
            remaining_pct=70.0,
            resets_in_sec=1800,
            windows={"five_hour": limits.WindowData(remaining_pct=70.0, resets_in_sec=1800)},
        ),
        gemini=limits.ProviderLimits(error="HTTP 429"),
        codex=limits.ProviderLimits(available=True, remaining_pct=80.0),
    )

    limits._apply_429_fallback(result_recovered, {"gemini"})
    assert limits._429_notified == {"gemini"}

    result_second_429 = limits.AllLimits(
        claude=limits.ProviderLimits(error="HTTP 429"),
        gemini=limits.ProviderLimits(error="HTTP 429"),
        codex=limits.ProviderLimits(available=True, remaining_pct=80.0),
    )

    limits._apply_429_fallback(result_second_429, {"claude", "gemini"})

    assert notify_calls == ["claude"]


# ── _get_claude_limits_from_local tests ───────────────────────────────────────

def test_get_claude_limits_from_local_returns_none_for_empty_plan():
    assert limits._get_claude_limits_from_local("") is None


def test_get_claude_limits_from_local_returns_none_for_unknown_plan():
    assert limits._get_claude_limits_from_local("unknown_xyz") is None


def test_get_claude_limits_from_local_returns_none_when_claude_monitor_missing(monkeypatch):
    """If claude_monitor is unavailable (ImportError), return None gracefully."""
    import sys
    # Block the top-level package so the internal imports raise ImportError
    monkeypatch.setitem(sys.modules, "claude_monitor", None)
    result = limits._get_claude_limits_from_local("pro")
    assert result is None


def test_get_claude_limits_from_local_handles_runtime_exception(monkeypatch):
    """Any unexpected exception inside the function is caught and returns None."""
    import sys
    # Remove any previously cached modules so the inner import is attempted
    for key in list(sys.modules):
        if key.startswith("claude_monitor"):
            monkeypatch.delitem(sys.modules, key, raising=False)

    class _BadModule:
        """Importable stub that raises on attribute access."""
        def __getattr__(self, name):
            raise RuntimeError("simulated failure")

    monkeypatch.setitem(sys.modules, "claude_monitor.core.models", _BadModule())
    result = limits._get_claude_limits_from_local("pro")
    assert result is None


def test_get_claude_limits_from_local_returns_full_capacity_when_no_block_is_active(monkeypatch):
    pytest.importorskip("claude_monitor")
    import datetime as dt
    import claude_monitor.data.analyzer as analyzer
    import claude_monitor.data.reader as reader

    class FakeAnalyzer:
        def __init__(self, session_duration_hours):
            assert session_duration_hours == 5

        def transform_to_blocks(self, entries):
            return [
                SimpleNamespace(
                    is_active=False,
                    end_time=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1),
                    token_counts=SimpleNamespace(total_tokens=19_000),
                )
            ]

    monkeypatch.setattr(reader, "load_usage_entries", lambda **_kwargs: ([object()], None))
    monkeypatch.setattr(analyzer, "SessionAnalyzer", FakeAnalyzer)

    result = limits._get_claude_limits_from_local("pro")

    assert result is not None
    assert result.available is True
    assert result.remaining_pct == 100.0
    assert result.resets_in_sec == 0
    assert result.windows["five_hour"].remaining_pct == 100.0


# ── _run_cclimits_impl cache-ttl flag tests ───────────────────────────────────

def test_run_cclimits_impl_includes_cache_ttl_when_use_cache_true(monkeypatch):
    from types import SimpleNamespace
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return SimpleNamespace(returncode=0, stdout="{}")

    monkeypatch.setattr(limits.subprocess, "run", fake_run)
    result = limits._run_cclimits_impl(5, use_cache=True)
    assert "--cache-ttl" in captured["cmd"]
    assert str(limits._CCLIMITS_CACHE_TTL_SEC) in captured["cmd"]
    assert result == {}


def test_run_cclimits_impl_omits_cache_ttl_when_use_cache_false(monkeypatch):
    from types import SimpleNamespace
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return SimpleNamespace(returncode=0, stdout="{}")

    monkeypatch.setattr(limits.subprocess, "run", fake_run)
    result = limits._run_cclimits_impl(5, use_cache=False)
    assert "--cache-ttl" not in captured["cmd"]
    assert result == {}


def test_run_cclimits_impl_retries_without_cache_ttl_when_flag_is_unsupported(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if len(calls) == 1:
            return SimpleNamespace(
                returncode=2,
                stdout="",
                stderr="error: unrecognized arguments: --cache-ttl",
            )
        return SimpleNamespace(returncode=0, stdout='{"claude": {"status": "ok"}}', stderr="")

    monkeypatch.setattr(limits.subprocess, "run", fake_run)

    result = limits._run_cclimits_impl(5, use_cache=True)

    assert result == {"claude": {"status": "ok"}}
    assert "--cache-ttl" in calls[0]
    assert "--cache-ttl" not in calls[1]


# ── Tier 0 (local JSONL) fallback path in _apply_429_fallback ─────────────────

def test_apply_429_fallback_tier0_uses_local_data(monkeypatch):
    """When local JSONL data is available it takes precedence over snapshot/cache."""
    _reset_429_state(monkeypatch)

    local_pl = limits.ProviderLimits(
        available=True,
        remaining_pct=75.0,
        resets_in_sec=3600,
        windows={"five_hour": limits.WindowData(remaining_pct=75.0, resets_in_sec=3600)},
        error="HTTP 429 (local-files)",
    )
    monkeypatch.setattr(limits, "_get_claude_limits_from_local", lambda *_: local_pl)
    monkeypatch.setattr(limits, "_limits_cache", None)

    import notifier
    monkeypatch.setattr(notifier, "notify_limits_429_fallback", lambda *a: None)

    result = limits.AllLimits(
        claude=limits.ProviderLimits(error="HTTP 429"),
        gemini=limits.ProviderLimits(available=True, remaining_pct=90.0),
        codex=limits.ProviderLimits(available=True, remaining_pct=85.0),
    )

    adjusted = limits._apply_429_fallback(result, {"claude"})

    assert adjusted.claude.remaining_pct == 75.0
    assert adjusted.claude.available is True
    assert "local-files" in adjusted.claude.error
    assert "claude" in limits._429_snapshots
    assert limits._429_estimated_usage.get("claude") == {}


def test_apply_429_fallback_tier0_resets_estimated_usage(monkeypatch):
    """Tier 0 discards accumulated estimated usage because local data is always fresh."""
    _reset_429_state(monkeypatch)

    local_pl = limits.ProviderLimits(
        available=True,
        remaining_pct=60.0,
        resets_in_sec=1800,
        windows={"five_hour": limits.WindowData(remaining_pct=60.0, resets_in_sec=1800)},
        error="HTTP 429 (local-files)",
    )
    monkeypatch.setattr(limits, "_get_claude_limits_from_local", lambda *_: local_pl)
    monkeypatch.setattr(limits, "_limits_cache", None)
    monkeypatch.setattr(limits, "_429_estimated_usage", {"claude": {"five_hour": 20.0}})
    monkeypatch.setattr(limits, "_429_snapshots", {"claude": (local_pl, 0.0)})

    import notifier
    monkeypatch.setattr(notifier, "notify_limits_429_fallback", lambda *a: None)

    result = limits.AllLimits(
        claude=limits.ProviderLimits(error="HTTP 429"),
        gemini=limits.ProviderLimits(available=True, remaining_pct=90.0),
        codex=limits.ProviderLimits(available=True, remaining_pct=85.0),
    )

    limits._apply_429_fallback(result, {"claude"})

    assert limits._429_estimated_usage["claude"] == {}


def test_get_limits_fresh_uses_tier0_local_data_on_429(monkeypatch):
    """End-to-end: 429 + local JSONL available → tier 0 result returned."""
    _reset_bg_state(monkeypatch)
    _reset_429_state(monkeypatch)

    local_pl = limits.ProviderLimits(
        available=True,
        remaining_pct=70.0,
        resets_in_sec=2400,
        windows={"five_hour": limits.WindowData(remaining_pct=70.0, resets_in_sec=2400)},
        error="HTTP 429 (local-files)",
    )
    # Override the fixture's disabled stub with real local data
    monkeypatch.setattr(limits, "_get_claude_limits_from_local", lambda *_: local_pl)

    def fake_run_cclimits():
        return {
            "claude": {"error": "HTTP 429"},
            "gemini": {"status": "ok", "models": {"flash": {"remaining": "99%", "resets_in": "1h"}}},
            "codex": {"status": "ok", "primary_window": {"remaining": "90%", "resets_in": "2h"}},
        }

    monkeypatch.setattr(limits, "_fresh_limits_lock", threading.Lock())
    monkeypatch.setattr(limits, "_limits_cache", None)
    monkeypatch.setattr(limits, "_run_cclimits", fake_run_cclimits)
    monkeypatch.setattr(limits.time, "sleep", lambda *_: None)
    import notifier
    monkeypatch.setattr(notifier, "notify_limits_429_fallback", lambda *a: None)

    result = limits._get_limits_fresh()

    assert result.claude.available is True
    assert result.claude.remaining_pct == 70.0
    assert "local-files" in result.claude.error
    assert "claude" in limits._429_snapshots
