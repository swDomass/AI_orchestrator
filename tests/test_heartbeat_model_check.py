"""Tests for the monthly model-update heartbeat check."""
from unittest.mock import MagicMock, patch

import pytest

import heartbeat
from heartbeat import (
    _check_model_updates,
    _llm_check_for_newer_models,
    _parse_heartbeat_md,
    _probe_model,
)
from providers.base import RunResult


@pytest.fixture(autouse=True)
def _disable_openrouter_tier(monkeypatch):
    """Ensure tests do not accidentally make real HTTP calls if the user's
    environment has OPENROUTER_API_KEY set. _llm_check_for_newer_models tries
    OpenRouter first when configured — neutralise it so existing assertions
    that mock dispatcher.select_provider still drive the outcome.
    """
    monkeypatch.setattr("dispatcher.get_provider_by_name", lambda name: None)


# ── _INTERVAL_RE: 'days' support ──────────────────────────────────────────────

def test_parse_heartbeat_md_accepts_every_n_days():
    md = "## Every 30 days\n- [ ] model-check: foo\n"
    items = _parse_heartbeat_md(md)
    assert len(items) == 1
    assert items[0].label.startswith("model-check")
    assert items[0].interval_min == 30 * 24 * 60  # 43200
    assert items[0].handler_key == "_check_model_updates"


def test_parse_heartbeat_md_accepts_singular_day():
    md = "## Every 1 day\n- [ ] model-check\n"
    items = _parse_heartbeat_md(md)
    assert items[0].interval_min == 1440


def test_parse_heartbeat_md_still_accepts_minutes_and_hours():
    md = (
        "## Every 5 minutes\n- [ ] log-capacity\n"
        "## Every 2 hours\n- [ ] check-limits\n"
        "## Every 7 days\n- [ ] model-check\n"
    )
    items = _parse_heartbeat_md(md)
    assert [i.interval_min for i in items] == [5, 120, 7 * 1440]


def test_parse_heartbeat_md_drops_zero_interval_items():
    """## Every 0 days must not silently fall through to daily-item semantics."""
    md = (
        "## Every 0 days\n- [ ] should not be parsed\n"
        "## Every 5 minutes\n- [ ] log-capacity\n"
    )
    items = _parse_heartbeat_md(md)
    assert len(items) == 1
    assert items[0].label == "log-capacity"


# ── _probe_model ──────────────────────────────────────────────────────────────

def _fake_provider(success: bool, error: str = "", cooling_down: bool = False) -> MagicMock:
    p = MagicMock()
    p._forced_model = None
    p.is_cooling_down.return_value = cooling_down
    p.cooldown_remaining_str.return_value = "0m 0s"
    p.run.return_value = RunResult(success=success, output="pong" if success else "", error=error)
    return p


def test_probe_model_returns_alive_on_success(monkeypatch):
    p = _fake_provider(success=True)
    monkeypatch.setattr("dispatcher.get_provider_by_name", lambda _: p)

    alive, detail = _probe_model("claude", "claude-opus-4-7")

    assert alive is True
    assert detail == ""
    # Provider was probed with the requested model and reset afterwards
    assert p.run.call_count == 1


def test_probe_model_flags_dead_id_on_model_rejection(monkeypatch):
    p = _fake_provider(success=False, error="Error 404: model not found")
    monkeypatch.setattr("dispatcher.get_provider_by_name", lambda _: p)

    alive, detail = _probe_model("claude", "claude-deprecated-1")

    assert alive is False
    assert "model not found" in detail.lower()


def test_probe_model_skips_when_provider_in_cooldown(monkeypatch):
    p = _fake_provider(success=True, cooling_down=True)
    p.cooldown_remaining_str.return_value = "5m 0s"
    monkeypatch.setattr("dispatcher.get_provider_by_name", lambda _: p)

    alive, detail = _probe_model("claude", "claude-opus-4-7")

    assert alive is True
    assert "cooldown" in detail
    assert p.run.call_count == 0  # no probe was sent


def test_probe_model_treats_auth_expired_as_alive(monkeypatch):
    """Expired OAuth must NOT mark a model as dead — it's a separate issue."""
    p = _fake_provider(success=False, error="authentication expired, login required")
    monkeypatch.setattr("dispatcher.get_provider_by_name", lambda _: p)

    alive, detail = _probe_model("claude", "claude-opus-4-7")

    assert alive is True
    assert "transient" in detail


def test_probe_model_treats_raw_rate_limit_string_as_transient(monkeypatch):
    """Raw stderr 'rate limit' (with space) must match, not just typed 'rate_limit'."""
    p = _fake_provider(success=False, error="API rate limit exceeded; retry later")
    monkeypatch.setattr("dispatcher.get_provider_by_name", lambda _: p)

    alive, detail = _probe_model("claude", "claude-opus-4-7")

    assert alive is True
    assert "transient" in detail


def test_probe_model_does_not_flag_unsupported_encoding_as_dead(monkeypatch):
    """The old broad 'unsupported' keyword false-positively matched non-model errors."""
    p = _fake_provider(success=False, error="unsupported encoding in input file")
    monkeypatch.setattr("dispatcher.get_provider_by_name", lambda _: p)

    alive, detail = _probe_model("claude", "claude-opus-4-7")

    # Should NOT be flagged as dead — narrower keywords avoid this false positive
    assert alive is True


def test_probe_model_treats_rate_limit_as_alive(monkeypatch):
    p = _fake_provider(success=False, error="rate_limit")
    monkeypatch.setattr("dispatcher.get_provider_by_name", lambda _: p)

    alive, detail = _probe_model("claude", "claude-opus-4-7")

    assert alive is True
    assert "transient" in detail


def test_probe_model_treats_timeout_as_alive(monkeypatch):
    p = _fake_provider(success=False, error="timeout")
    monkeypatch.setattr("dispatcher.get_provider_by_name", lambda _: p)

    alive, detail = _probe_model("gemini", "gemini-3.1-pro-preview")

    assert alive is True
    assert "transient" in detail


def test_probe_model_returns_alive_when_provider_missing(monkeypatch):
    monkeypatch.setattr("dispatcher.get_provider_by_name", lambda _: None)

    alive, detail = _probe_model("claude", "x")

    assert alive is True
    assert "not initialised" in detail


def test_probe_model_restores_forced_model_on_exception(monkeypatch):
    p = _fake_provider(success=True)
    p._forced_model = "previous-model"
    p.run.side_effect = RuntimeError("boom")
    monkeypatch.setattr("dispatcher.get_provider_by_name", lambda _: p)

    alive, detail = _probe_model("claude", "tmp")

    assert alive is True
    assert "transient" in detail
    # _forced_model must be restored even though run() raised
    assert p._forced_model == "previous-model"


# ── _llm_check_for_newer_models ───────────────────────────────────────────────

def test_llm_check_returns_empty_when_no_provider(monkeypatch):
    monkeypatch.setattr("dispatcher.select_provider", lambda *a, **kw: None)
    monkeypatch.setattr("limits.get_limits", lambda: MagicMock())

    assert _llm_check_for_newer_models() == ""


def test_llm_check_returns_empty_on_ok_response(monkeypatch):
    p = _fake_provider(success=True)
    p.run.return_value = RunResult(success=True, output="OK")
    monkeypatch.setattr("dispatcher.select_provider", lambda *a, **kw: p)
    monkeypatch.setattr("limits.get_limits", lambda: MagicMock())

    assert _llm_check_for_newer_models() == ""


def test_llm_check_returns_text_on_findings(monkeypatch):
    p = _fake_provider(success=True)
    p.run.return_value = RunResult(
        success=True,
        output="claude/claude_opus: claude-opus-4-7 → claude-opus-4-8 (released 2026-06)",
    )
    monkeypatch.setattr("dispatcher.select_provider", lambda *a, **kw: p)
    monkeypatch.setattr("limits.get_limits", lambda: MagicMock())

    out = _llm_check_for_newer_models()
    assert "claude-opus-4-8" in out


def test_llm_check_strict_ok_match_rejects_okay_prefix(monkeypatch):
    """'Okay, here are the updates: ...' must NOT count as 'OK'."""
    p = _fake_provider(success=True)
    p.run.return_value = RunResult(
        success=True,
        output="Okay, here are some updates you should consider:\nclaude/claude_opus: outdated",
    )
    monkeypatch.setattr("dispatcher.select_provider", lambda *a, **kw: p)
    monkeypatch.setattr("limits.get_limits", lambda: MagicMock())

    out = _llm_check_for_newer_models()
    assert "Okay" in out  # full text returned, NOT empty


def test_llm_check_returns_warning_on_provider_failure(monkeypatch):
    """LLM-call failure must be visible in the output, not silently swallowed."""
    p = _fake_provider(success=False, error="rate_limit")
    monkeypatch.setattr("dispatcher.select_provider", lambda *a, **kw: p)
    monkeypatch.setattr("limits.get_limits", lambda: MagicMock())

    out = _llm_check_for_newer_models()
    assert out.startswith("⚠️")
    assert "rate_limit" in out


def test_llm_check_prefers_openrouter_when_available(monkeypatch):
    """Tier 1: when OpenRouter is configured, it is used first; native chain is skipped."""
    openrouter = _fake_provider(success=True)
    openrouter.run.return_value = RunResult(success=True, output="OK")
    openrouter.name = "openrouter"
    openrouter.is_cooling_down = lambda: False

    # Override the autouse fixture for this test only
    monkeypatch.setattr("dispatcher.get_provider_by_name",
                        lambda name: openrouter if name == "openrouter" else None)

    # If the code falls through to tier 2, select_provider would be called.
    # Track it to verify we did NOT fall through.
    select_calls = []
    monkeypatch.setattr("dispatcher.select_provider",
                        lambda *a, **kw: select_calls.append(kw) or None)
    monkeypatch.setattr("limits.get_limits", lambda: MagicMock())

    assert _llm_check_for_newer_models() == ""
    openrouter.run.assert_called_once()
    assert select_calls == [], "select_provider should not be called when OpenRouter succeeds"


def test_llm_check_falls_back_when_openrouter_fails(monkeypatch):
    """Tier 2: OpenRouter failure falls through to the default chain."""
    openrouter = _fake_provider(success=False, error="rate_limit")
    openrouter.name = "openrouter"
    openrouter.is_cooling_down = lambda: False

    fallback = _fake_provider(success=True)
    fallback.run.return_value = RunResult(success=True, output="OK")

    monkeypatch.setattr("dispatcher.get_provider_by_name",
                        lambda name: openrouter if name == "openrouter" else None)
    monkeypatch.setattr("dispatcher.select_provider", lambda *a, **kw: fallback)
    monkeypatch.setattr("limits.get_limits", lambda: MagicMock())

    assert _llm_check_for_newer_models() == ""
    openrouter.run.assert_called_once()
    fallback.run.assert_called_once()


def test_llm_check_skips_openrouter_when_cooling_down(monkeypatch):
    """A cooling-down OpenRouter is bypassed without a call attempt."""
    openrouter = _fake_provider(success=True)
    openrouter.name = "openrouter"
    openrouter.is_cooling_down = lambda: True  # cooling down

    fallback = _fake_provider(success=True)
    fallback.run.return_value = RunResult(success=True, output="OK")

    monkeypatch.setattr("dispatcher.get_provider_by_name",
                        lambda name: openrouter if name == "openrouter" else None)
    monkeypatch.setattr("dispatcher.select_provider", lambda *a, **kw: fallback)
    monkeypatch.setattr("limits.get_limits", lambda: MagicMock())

    assert _llm_check_for_newer_models() == ""
    openrouter.run.assert_not_called()
    fallback.run.assert_called_once()


def test_llm_check_prompt_includes_openrouter_aliases(monkeypatch):
    """Drift-detection: the prompt must enumerate OpenRouter aliases too."""
    p = _fake_provider(success=True)
    captured: dict = {}

    def _capture_run(prompt, **kwargs):
        captured["prompt"] = prompt
        return RunResult(success=True, output="OK")

    p.run.side_effect = _capture_run
    monkeypatch.setattr("dispatcher.select_provider", lambda *a, **kw: p)
    monkeypatch.setattr("limits.get_limits", lambda: MagicMock())

    _llm_check_for_newer_models()
    assert "OpenRouter/or_minimax_free" in captured["prompt"]
    assert "OpenRouter/or_glm" in captured["prompt"]


def test_llm_check_includes_date_anchor_in_prompt(monkeypatch):
    """The prompt must include today's date so the LLM doesn't anchor on training cutoff."""
    p = _fake_provider(success=True)
    p.run.return_value = RunResult(success=True, output="OK")
    captured: dict = {}

    def _capture_run(prompt, **kwargs):
        captured["prompt"] = prompt
        return RunResult(success=True, output="OK")

    p.run.side_effect = _capture_run
    monkeypatch.setattr("dispatcher.select_provider", lambda *a, **kw: p)
    monkeypatch.setattr("limits.get_limits", lambda: MagicMock())

    _llm_check_for_newer_models()
    assert "Stand:" in captured["prompt"]
    assert "20" in captured["prompt"]  # year is included


# ── _check_model_updates (integration) ────────────────────────────────────────

def test_check_model_updates_returns_none_when_all_alive(monkeypatch):
    monkeypatch.setattr(heartbeat, "_probe_model", lambda *_a, **_kw: (True, ""))
    monkeypatch.setattr(heartbeat, "_llm_check_for_newer_models", lambda: "")

    assert _check_model_updates() is None


def test_check_model_updates_reports_dead_ids(monkeypatch):
    def fake_probe(provider, model_id, **kwargs):
        if model_id == "claude-haiku-4-5-20251001":
            return False, "model not found"
        return True, ""

    monkeypatch.setattr(heartbeat, "_probe_model", fake_probe)
    monkeypatch.setattr(heartbeat, "_llm_check_for_newer_models", lambda: "")

    msg = _check_model_updates()

    assert msg is not None
    assert "Tote Model-IDs" in msg
    assert "claude-haiku-4-5-20251001" in msg


def test_check_model_updates_includes_llm_suggestions(monkeypatch):
    monkeypatch.setattr(heartbeat, "_probe_model", lambda *_a, **_kw: (True, ""))
    monkeypatch.setattr(heartbeat, "_llm_check_for_newer_models", lambda: "claude_opus → 4.8")

    msg = _check_model_updates()

    assert msg is not None
    assert "claude_opus" in msg
    assert "Tote" not in msg  # no dead IDs section


def test_check_model_updates_reports_flaky_ids(monkeypatch):
    def fake_probe(provider, model_id, **kwargs):
        if model_id == "gpt-5.5":
            return True, "unclear (some odd error)"
        return True, ""

    monkeypatch.setattr(heartbeat, "_probe_model", fake_probe)
    monkeypatch.setattr(heartbeat, "_llm_check_for_newer_models", lambda: "")

    msg = _check_model_updates()

    assert msg is not None
    assert "Auffällige" in msg
    assert "gpt-5.5" in msg


# ── Persistent state: last_run survives restart for long-interval items ──────

def test_persistent_state_round_trip(tmp_path, monkeypatch):
    """Save/load symmetry: long-interval items keep their last_run across restarts."""
    import json
    from datetime import datetime as _dt
    from heartbeat import (
        HeartbeatItem,
        HeartbeatRunner,
        _PERSIST_INTERVAL_THRESHOLD_MIN,
        _save_heartbeat_state,
        _load_heartbeat_state,
    )

    state_file = tmp_path / "heartbeat-state.json"
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_STATE_FILE", state_file)

    fixed = _dt(2026, 5, 8, 12, 0, 0)
    state = {
        "model-check: foo":  {"last_run": fixed.isoformat(), "last_run_date": fixed.date().isoformat()},
        "log-capacity":      {"last_run": fixed.isoformat()},  # short interval — should be ignored on restore
    }
    _save_heartbeat_state(state)

    loaded = _load_heartbeat_state()
    assert loaded == state


def test_runner_restores_last_run_for_long_interval_items(tmp_path, monkeypatch):
    """Items with interval >= 1 day must hydrate from disk on init."""
    from datetime import datetime as _dt
    from unittest.mock import patch
    from heartbeat import HeartbeatItem, HeartbeatRunner

    state_file = tmp_path / "heartbeat-state.json"
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_STATE_FILE", state_file)

    fixed = _dt(2026, 5, 1, 12, 0, 0)
    state_file.write_text(
        '{"model-check: foo": {"last_run": "%s"}}' % fixed.isoformat(),
        encoding="utf-8",
    )

    monthly_item = HeartbeatItem(
        label="model-check: foo",
        interval_min=30 * 1440,
        handler_key="_check_model_updates",
    )

    with patch.object(HeartbeatRunner, "_reload_if_changed", return_value=None):
        runner = HeartbeatRunner()
        runner._items = [monthly_item]
        runner._restore_persistent_state()

    assert runner._items[0].last_run == fixed


def test_runner_does_not_persist_short_interval_items(tmp_path, monkeypatch):
    """5-minute items must NOT clutter the state file."""
    from datetime import datetime as _dt
    from unittest.mock import patch
    from heartbeat import HeartbeatItem, HeartbeatRunner

    state_file = tmp_path / "heartbeat-state.json"
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_STATE_FILE", state_file)

    short_item = HeartbeatItem(label="log-capacity", interval_min=5)
    short_item.last_run = _dt(2026, 5, 8, 12, 0, 0)

    with patch.object(HeartbeatRunner, "_reload_if_changed", return_value=None):
        runner = HeartbeatRunner()
        runner._items = [short_item]
        runner._persist_state()

    # No persistent items → no file written
    assert not state_file.exists()
