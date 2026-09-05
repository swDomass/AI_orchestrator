"""Tests for AllLimits.opencode — the OpenRouter-budget-backed ProviderLimits.

Covers: _opencode_budget_snapshot()'s threshold/rounding/fail-closed behaviour,
_opencode_reset_epoch()'s three cadences, the AllLimits drift guard across
earliest_reset_sec()/any_available()/has_transient_token_refresh(), and that
a network failure in the new opencode budget check cannot break get_limits().
"""

import dataclasses
import threading
import time
from datetime import UTC, datetime

import pytest

import limits
import openrouter_budget

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_bg_state(monkeypatch):
    """Same helper as tests/test_limits.py — reset background-thread state."""
    monkeypatch.setattr(limits, "_limits_cache", None)
    monkeypatch.setattr(limits, "_bg_thread", None)
    monkeypatch.setattr(limits, "_bg_wake", threading.Event())
    monkeypatch.setattr(limits, "_cache_ready", threading.Event())
    monkeypatch.setattr(limits, "_refresh_failed_until", {})


@pytest.fixture(autouse=True)
def _fixed_min_remaining(monkeypatch):
    """Pin the threshold so these tests don't depend on config.py's default
    (or lack thereof — see final report re: getattr fallback)."""
    monkeypatch.setattr(limits.config, "OPENCODE_MIN_REMAINING_USD", 0.25, raising=False)
    yield


@pytest.fixture(autouse=True)
def _opencode_registered(monkeypatch):
    """Pin opencode as REGISTERED for the budget tests.

    _opencode_budget_snapshot() short-circuits to "not_registered" when the
    dispatcher has no opencode provider, so without this fixture every budget
    test below would silently measure the developer's machine instead of the
    branch it names: green where opencode is installed, green for the WRONG
    reason where it is not. Same hermeticity pattern as conftest's
    _isolate_openrouter_api_key / _isolate_gemini_api_key.

    The snapshot resolves `get_provider_by_name` lazily on each call (module
    import cycle, see its docstring), so patching the dispatcher attribute is
    what the production code will actually read.
    """
    import dispatcher

    monkeypatch.setattr(
        dispatcher, "get_provider_by_name",
        lambda name: object() if name == "opencode" else None,
    )
    yield


def test_snapshot_reports_not_registered_without_the_binary(monkeypatch):
    """No opencode binary -> no run -> the key's balance is irrelevant.

    Guards the aggregate meaning: AllLimits.opencode.available feeds
    any_available()/earliest_reset_sec(), which mean "work can run here". A
    machine without opencode must not report capacity just because the shared
    OpenRouter key still holds money — and must not pay an HTTPS round-trip
    per refresh to find that out.
    """
    import dispatcher

    monkeypatch.setattr(dispatcher, "get_provider_by_name", lambda name: None)

    def _must_not_be_called():
        raise AssertionError("budget must not be fetched when opencode is unregistered")

    monkeypatch.setattr(openrouter_budget, "fetch_budget", _must_not_be_called)

    snap = limits._opencode_budget_snapshot()

    assert snap.available is False
    assert snap.error == "not_registered"


# ---------------------------------------------------------------------------
# _opencode_budget_snapshot() — threshold, exactly the ">" not ">=" boundary
# ---------------------------------------------------------------------------


def test_snapshot_available_when_remaining_just_above_threshold(monkeypatch):
    monkeypatch.setattr(openrouter_budget, "fetch_budget", lambda timeout=10.0: (5.0, 0.26, "daily"))
    snap = limits._opencode_budget_snapshot()
    assert snap.available is True


def test_snapshot_unavailable_when_remaining_exactly_at_threshold(monkeypatch):
    """The comparison is strictly '>', not '>=' — exactly at the threshold
    counts as NOT available."""
    monkeypatch.setattr(openrouter_budget, "fetch_budget", lambda timeout=10.0: (5.0, 0.25, "daily"))
    snap = limits._opencode_budget_snapshot()
    assert snap.available is False


def test_snapshot_unavailable_when_remaining_just_below_threshold(monkeypatch):
    monkeypatch.setattr(openrouter_budget, "fetch_budget", lambda timeout=10.0: (5.0, 0.24, "daily"))
    snap = limits._opencode_budget_snapshot()
    assert snap.available is False


# ---------------------------------------------------------------------------
# remaining_pct computation
# ---------------------------------------------------------------------------


def test_snapshot_remaining_pct_computed_from_limit_and_remaining(monkeypatch):
    monkeypatch.setattr(openrouter_budget, "fetch_budget", lambda timeout=10.0: (5.0, 4.89, "daily"))
    snap = limits._opencode_budget_snapshot()
    assert snap.remaining_pct == pytest.approx(97.8, abs=0.01)


def test_snapshot_limit_zero_does_not_raise(monkeypatch):
    """limit=0.0 is a valid (non-None) float — division must be guarded."""
    monkeypatch.setattr(openrouter_budget, "fetch_budget", lambda timeout=10.0: (0.0, 0.0, "daily"))
    snap = limits._opencode_budget_snapshot()  # must not raise ZeroDivisionError
    assert snap.remaining_pct == 0.0
    assert snap.available is False


# ---------------------------------------------------------------------------
# Fail-closed branches
# ---------------------------------------------------------------------------


def test_snapshot_fail_closed_when_fetch_fails(monkeypatch):
    """fetch_budget() collapsing to (None, None, None) — network/auth/parse
    failure OR a null-limit response, indistinguishable at this point."""
    monkeypatch.setattr(openrouter_budget, "fetch_budget", lambda timeout=10.0: (None, None, None))
    snap = limits._opencode_budget_snapshot()
    assert snap.available is False
    assert snap.remaining_pct == 0.0
    assert snap.error == "budget_unavailable"


def test_snapshot_uncapped_key_when_limit_is_none_but_remaining_present(monkeypatch):
    """Distinct message for the (currently only reachable via a direct mock,
    see openrouter_budget.fetch_budget()'s docstring) "key has no cap" shape."""
    monkeypatch.setattr(openrouter_budget, "fetch_budget", lambda timeout=10.0: (None, 4.89, "daily"))
    snap = limits._opencode_budget_snapshot()
    assert snap.available is False
    assert snap.remaining_pct == 0.0
    assert snap.error == "uncapped_key"


# ---------------------------------------------------------------------------
# _opencode_reset_epoch() — three cadences + unknown/None
# ---------------------------------------------------------------------------


def test_reset_epoch_daily_is_next_utc_midnight():
    now = time.time()
    epoch = limits._opencode_reset_epoch("daily")
    assert now < epoch <= now + 86400 + 5
    dt = datetime.fromtimestamp(epoch, tz=UTC)
    assert (dt.hour, dt.minute, dt.second, dt.microsecond) == (0, 0, 0, 0)


def test_reset_epoch_weekly_is_next_utc_monday_midnight():
    now = time.time()
    epoch = limits._opencode_reset_epoch("weekly")
    assert now < epoch <= now + 7 * 86400 + 5
    dt = datetime.fromtimestamp(epoch, tz=UTC)
    assert dt.weekday() == 0  # Monday
    assert (dt.hour, dt.minute, dt.second, dt.microsecond) == (0, 0, 0, 0)


def test_reset_epoch_monthly_is_first_of_next_month_midnight():
    now = time.time()
    epoch = limits._opencode_reset_epoch("monthly")
    assert now < epoch <= now + 32 * 86400
    dt = datetime.fromtimestamp(epoch, tz=UTC)
    assert dt.day == 1
    assert (dt.hour, dt.minute, dt.second, dt.microsecond) == (0, 0, 0, 0)


def test_reset_epoch_none_for_unknown_or_missing_cadence():
    assert limits._opencode_reset_epoch(None) == 0.0
    assert limits._opencode_reset_epoch("hourly") == 0.0
    assert limits._opencode_reset_epoch("") == 0.0


def test_reset_epoch_only_affects_earliest_reset_sec_not_availability(monkeypatch):
    """Sanity check tying the ASSUMPTION comment to behaviour: an available
    snapshot stays available regardless of the reset guess."""
    monkeypatch.setattr(openrouter_budget, "fetch_budget", lambda timeout=10.0: (5.0, 4.89, "daily"))
    snap = limits._opencode_budget_snapshot()
    assert snap.available is True
    assert snap.reset_at_epoch > 0


# ---------------------------------------------------------------------------
# Drift guard — the most important test in this file.
#
# Iterates dataclasses.fields(AllLimits) and, for EACH field, sets only that
# field to a recognisable value and checks the aggregate reacts. A field left
# out of any of the three hand-written enumerations fails this test instead
# of silently being ignored (exactly the drift this whole package exists to
# guard against — see the auftrag: two of three were remembered, opencode's
# own has_transient_token_refresh() coverage was found only in this session).
# ---------------------------------------------------------------------------


def test_all_limits_drift_guard_covers_every_field():
    field_names = [f.name for f in dataclasses.fields(limits.AllLimits)]
    assert field_names, "AllLimits has no fields — test setup is broken"

    for name in field_names:
        avail_limits = limits.AllLimits(**{name: limits.ProviderLimits(available=True, remaining_pct=100.0)})
        assert avail_limits.any_available() is True, (
            f"any_available() does not react to AllLimits.{name} — drift"
        )

        future_epoch = time.time() + 999_999
        reset_limits = limits.AllLimits(**{name: limits.ProviderLimits(reset_at_epoch=future_epoch)})
        assert reset_limits.earliest_reset_sec() > 900_000, (
            f"earliest_reset_sec() does not react to AllLimits.{name} — drift"
        )

        token_limits = limits.AllLimits(
            **{name: limits.ProviderLimits(available=False, error="token expired")}
        )
        assert token_limits.has_transient_token_refresh() is True, (
            f"has_transient_token_refresh() does not react to AllLimits.{name} — drift"
        )


def test_all_limits_drift_guard_would_catch_a_field_missing_from_any_available():
    """Meta-test: prove the guard technique actually fails when a field is
    skipped, using a local stand-in class with a deliberately incomplete
    any_available() — otherwise the guard above could be vacuously true."""

    @dataclasses.dataclass
    class _Incomplete:
        claude: limits.ProviderLimits = dataclasses.field(default_factory=limits.ProviderLimits)
        forgotten: limits.ProviderLimits = dataclasses.field(default_factory=limits.ProviderLimits)

        def any_available(self) -> bool:
            return self.claude.available  # "forgotten" not counted — the bug

    field_names = [f.name for f in dataclasses.fields(_Incomplete)]
    failures = []
    for name in field_names:
        inst = _Incomplete(**{name: limits.ProviderLimits(available=True)})
        if not inst.any_available():
            failures.append(name)
    assert failures == ["forgotten"]


# ---------------------------------------------------------------------------
# The new network dependency must not break the refresh path.
# ---------------------------------------------------------------------------


def test_get_limits_survives_opencode_urlopen_raising(monkeypatch):
    """urlopen() blowing up inside the opencode budget check must not prevent
    get_limits() from returning a result for the other providers."""
    _reset_bg_state(monkeypatch)
    monkeypatch.setattr(limits.config, "OPENROUTER_API_KEY", "sk-test-key", raising=False)

    raw = {
        "claude": {"status": "ok", "five_hour": {"remaining": "80%", "resets_in": "2h"},
                   "seven_day": {"remaining": "80%", "resets_in": "3d"}},
        "gemini": {"status": "missing"},
        "codex": {"status": "missing"},
    }
    monkeypatch.setattr(limits, "_run_cclimits", lambda: raw)
    monkeypatch.setattr(limits, "_refresh_token", lambda _provider: False)

    def blow_up(req, timeout):
        raise OSError("network is down")

    monkeypatch.setattr(openrouter_budget.urllib.request, "urlopen", blow_up)

    result = limits.get_limits(force_refresh=True)  # must not raise

    assert result.claude.available is True
    assert result.opencode.available is False
    assert result.opencode.error == "budget_unavailable"


def test_get_limits_fresh_wires_opencode_override_on_success(monkeypatch):
    """Integration check: a healthy opencode budget response actually lands
    in AllLimits.opencode via _get_limits_fresh(), not just the standalone
    snapshot function."""
    monkeypatch.setattr(limits, "_fresh_limits_lock", threading.Lock())
    monkeypatch.setattr(limits, "_refresh_token", lambda _provider: False)
    monkeypatch.setattr(
        limits, "_run_cclimits",
        lambda: {"claude": {"status": "missing"}, "gemini": {"status": "missing"}, "codex": {"status": "missing"}},
    )
    monkeypatch.setattr(openrouter_budget, "fetch_budget", lambda timeout=10.0: (5.0, 4.89, "daily"))

    result = limits._get_limits_fresh()

    assert result.opencode.available is True
    assert result.opencode.remaining_pct == pytest.approx(97.8, abs=0.01)
