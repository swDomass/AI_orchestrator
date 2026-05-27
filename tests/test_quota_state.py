"""Tests for the Phase-1 SoTH quota state writer/reader (quota_state.py)."""

import json

import limits
import quota_state


def _sample_all_limits():
    return limits.AllLimits(
        claude=limits.ProviderLimits(
            available=True, remaining_pct=42.5, resets_in_sec=3600,
            windows={
                "five_hour": limits.WindowData(remaining_pct=35.0, resets_in_sec=1200),
                "seven_day": limits.WindowData(remaining_pct=42.5, resets_in_sec=3600),
            },
        ),
        gemini=limits.ProviderLimits(available=True, remaining_pct=90.0),
        codex=limits.ProviderLimits(available=False, remaining_pct=0.0, error="auth expired"),
    )


def test_build_state_structure():
    state = quota_state.build_state(_sample_all_limits(), now=1_700_000_000.0)

    assert state["schema_version"] == quota_state.SCHEMA_VERSION
    assert state["fetched_at_unix"] == 1_700_000_000.0
    assert state["fetched_at_utc"].endswith("+00:00")

    claude = state["providers"]["claude"]
    assert claude["available"] is True
    assert claude["remaining_pct"] == 42.5
    assert claude["used_pct"] == 57.5
    fh = claude["windows"]["five_hour"]
    assert fh["remaining_pct"] == 35.0
    assert fh["used_pct"] == 65.0
    assert fh["resets_in_sec"] == 1200
    assert fh["reset_at_epoch"] == 1_700_000_000.0 + 1200

    # All three providers serialised
    assert set(state["providers"]) == {"claude", "gemini", "codex"}
    assert state["providers"]["codex"]["error"] == "auth expired"


def test_build_state_embeds_calibration():
    state = quota_state.build_state(_sample_all_limits(), now=1_700_000_000.0)
    cal = state["calibration"]
    assert cal["model"] == "io_only"
    assert cal["tokens_per_pct"]["claude"] == dict(
        limits.ESTIMATE_TOKENS_PER_PCT_CLAUDE_WINDOWS,
    )


def test_write_and_read_roundtrip(tmp_path):
    path = tmp_path / "logs" / "cc_quota_state.json"
    assert quota_state.write_quota_state(_sample_all_limits(), path) is True
    assert path.exists()

    state = quota_state.read_quota_state(path)
    assert state is not None
    assert state["providers"]["claude"]["windows"]["seven_day"]["resets_in_sec"] == 3600

    # File is valid JSON on disk and no temp file left behind.
    json.loads(path.read_text(encoding="utf-8"))
    leftovers = [p for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == []


def test_read_missing_returns_none(tmp_path):
    assert quota_state.read_quota_state(tmp_path / "nope.json") is None


def test_read_empty_returns_none(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text("", encoding="utf-8")
    assert quota_state.read_quota_state(p) is None


def test_read_corrupt_returns_none(tmp_path):
    p = tmp_path / "corrupt.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert quota_state.read_quota_state(p) is None


def test_read_non_object_returns_none(tmp_path):
    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert quota_state.read_quota_state(p) is None


def test_write_never_raises_on_bad_target(tmp_path):
    # Point at a path whose parent is a file → mkdir/replace fails, but the
    # writer must swallow it and return False rather than propagate.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    target = blocker / "sub" / "state.json"
    assert quota_state.write_quota_state(_sample_all_limits(), target) is False


def test_state_age_sec():
    state = {"fetched_at_unix": 1000.0}
    assert quota_state.state_age_sec(state, now=1300.0) == 300.0
    # Clock skew (file newer than now) clamps to 0, never negative.
    assert quota_state.state_age_sec(state, now=500.0) == 0.0
    assert quota_state.state_age_sec({}, now=1300.0) is None
