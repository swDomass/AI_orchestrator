"""P2 — every quota visible, opencode included (2026-10-09).

Covers the one Python table that maps provider/window keys to group, colour and
label (``dashboard.provider_meta`` / ``provider_meta_map``), the new
``limits_now`` payload key (``analytics._limits_now``), that ``current_limits``
is byte-identical to the value the pre-change code produced, and that /api/data
ships the mapping to the page without touching the 30-s cache object.
"""

import hashlib
import json
import socket
import threading
import urllib.request
from datetime import datetime
from unittest.mock import patch

import pytest

import analytics
import dashboard

# A capacity log in the writer's real format (heartbeat._append_capacity_log):
# two snapshots 40 min apart, Gemini as the "-1.0 | false" placeholder, one
# Codex window below its threshold, and a legacy unprefixed bucket.
_CAPACITY_LOG = """\
# AI Provider Capacity Log
<!-- appended by orchestrator heartbeat -->

2026-10-09 07:46:18 | claude_five_hour | 64.0 | true
2026-10-09 07:46:18 | claude_seven_day | 41.0 | true
2026-10-09 07:46:18 | codex_primary_window | 93.0 | true
2026-10-09 07:46:18 | codex_secondary_window | 88.0 | true
2026-10-09 07:46:18 | gemini | -1.0 | false
2026-10-09 07:46:18 | opencode | 97.8 | true
2026-10-09 08:26:18 | claude_five_hour | 58.0 | true
2026-10-09 08:26:18 | claude_seven_day | 39.0 | true
2026-10-09 08:26:18 | codex_primary_window | 91.0 | true
2026-10-09 08:26:18 | codex_secondary_window | 2.0 | false
2026-10-09 08:26:18 | gemini | -1.0 | false
2026-10-09 08:26:18 | opencode | 100.0 | true
2026-10-09 07:00:00 | five_hour | 12.0 | true
"""

# What `_get_current_limits()` returned for _CAPACITY_LOG at master 4925e76
# (computed with that file's own code, key order included). P2 must not move it.
_CURRENT_LIMITS_BEFORE = {
    "five": {"available": True, "remaining_pct": 12.0, "error": ""},
    "claude": {"available": True, "remaining_pct": 39.0, "error": ""},
    "codex": {"available": False, "remaining_pct": 2.0, "error": ""},
    "gemini": {"available": False, "remaining_pct": -1.0, "error": ""},
    "opencode": {"available": True, "remaining_pct": 100.0, "error": ""},
}


@pytest.fixture
def capacity_log(tmp_path, monkeypatch):
    log = tmp_path / "capacity-log.md"
    log.write_text(_CAPACITY_LOG, encoding="utf-8")
    monkeypatch.setattr(analytics, "CAPACITY_LOG_FILE", log)
    return log


# ── the mapping table ───────────────────────────────────────────────────────


def test_opencode_has_group_colour_and_label():
    meta = dashboard.provider_meta("opencode")
    assert meta["group"] == "other"
    assert meta["group"] in dashboard.LIMIT_GROUPS
    assert meta["color"] == "#29b6f6"
    assert meta["label"] == "opencode (Tagesbudget)"
    assert meta["known"] is True


@pytest.mark.parametrize(
    ("name", "group", "label"),
    [
        ("claude_five_hour", "short", "Claude 5 h"),
        ("claude_seven_day", "long", "Claude 7 Tage"),
        ("codex_primary_window", "short", "Codex 5 h"),
        ("codex_secondary_window", "long", "Codex 7 Tage"),
        ("gemini", "other", "Gemini"),
        ("gemini_gemini_2_5_flash_", "other", "Gemini 2.5 Flash"),
    ],
)
def test_known_keys_are_grouped_and_labelled_by_window_pattern(name, group, label):
    meta = dashboard.provider_meta(name)
    assert (meta["group"], meta["label"]) == (group, label)


def test_window_label_is_derived_from_the_suffix_not_the_full_key():
    # A provider the table knows, with a window suffix it knows, but a full key
    # nobody wrote down: still labelled by its window.
    assert dashboard.provider_meta("opencode_seven_day")["label"] == "opencode 7 Tage"
    assert dashboard.provider_meta("opencode_seven_day")["group"] == "long"


def test_unknown_provider_gets_rest_colour_group_and_label():
    meta = dashboard.provider_meta("mistral_tagesbudget")
    assert meta["group"] == "other"
    assert meta["color"] in dashboard._REST_PALETTE
    assert meta["label"] == "mistral tagesbudget"
    assert meta["known"] is False
    # stable per name — same colour on every call, independent of hash() salting
    assert dashboard.provider_meta("mistral_tagesbudget")["color"] == meta["color"]
    idx = int(hashlib.sha256(b"mistral_tagesbudget").hexdigest()[:8], 16) % len(dashboard._REST_PALETTE)
    assert meta["color"] == dashboard._REST_PALETTE[idx]


def test_rest_palette_never_reuses_a_base_colour():
    base_colours = {c for (_, c, _) in dashboard._PROVIDER_BASES.values()}
    base_colours |= {c for (_, _, c) in dashboard._PROVIDER_BASES.values()}
    assert not base_colours & set(dashboard._REST_PALETTE)
    assert len(dashboard._REST_PALETTE) > len(dashboard._PROVIDER_BASES)


def test_meta_map_gives_distinct_colours_and_never_runs_out():
    few = dashboard.provider_meta_map(["claude", "codex", "opencode", "foo", "bar", "baz"])
    colours = [m["color"] for m in few.values()]
    assert len(set(colours)) == len(colours)
    many_names = [f"x{i}" for i in range(len(dashboard._REST_PALETTE) + 5)]
    many = dashboard.provider_meta_map(many_names)
    assert set(many) == set(many_names)
    assert all(m["color"] in dashboard._REST_PALETTE for m in many.values())
    # deterministic: the same set gives the same colours
    assert dashboard.provider_meta_map(many_names) == many


def test_meta_map_skips_none_and_accepts_any_iterable():
    meta = dashboard.provider_meta_map(iter(["opencode", None]))
    assert list(meta) == ["opencode"]


def test_doughnut_names_are_covered():
    """A sixth provider in provider_distribution gets a colour of its own."""
    names = ["claude", "codex", "gemini", "opencode", "unknown", "openrouter"]
    meta = dashboard.provider_meta_map(names)
    assert set(meta) == set(names)
    assert len({m["color"] for m in meta.values()}) == len(names)


# ── analytics.limits_now ────────────────────────────────────────────────────


def test_limits_now_takes_newest_value_per_exact_key(capacity_log):
    snaps = analytics._parse_capacity_log(capacity_log)
    now = datetime(2026, 10, 9, 8, 33, 18)  # 7 min after the newest snapshot
    out = analytics._limits_now(snaps, now=now)
    assert set(out) == {
        "claude_five_hour", "claude_seven_day",
        "codex_primary_window", "codex_secondary_window",
        "opencode", "gemini",
    }  # the legacy unprefixed "five_hour" is dropped
    assert out["claude_five_hour"]["remaining_pct"] == 58.0
    assert out["claude_seven_day"]["remaining_pct"] == 39.0
    assert out["codex_primary_window"]["remaining_pct"] == 91.0
    assert out["opencode"] == {
        "ts": "2026-10-09T08:26:18", "age_sec": 420, "remaining_pct": 100.0,
        "available": True, "stale": False, "state": "ok",
    }


def test_limits_now_minus_one_false_is_not_available(capacity_log):
    out = analytics._limits_now(analytics._parse_capacity_log(capacity_log),
                                now=datetime(2026, 10, 9, 8, 30))
    assert out["gemini"]["available"] is False
    assert out["gemini"]["remaining_pct"] is None
    assert out["gemini"]["state"] == "unavailable"
    # below threshold: not available, but the real value is kept
    assert out["codex_secondary_window"]["available"] is False
    assert out["codex_secondary_window"]["remaining_pct"] == 2.0
    assert out["codex_secondary_window"]["state"] == "unavailable"


def test_limits_now_marks_values_older_than_45_min_stale(capacity_log):
    snaps = analytics._parse_capacity_log(capacity_log)
    fresh = analytics._limits_now(snaps, now=datetime(2026, 10, 9, 9, 11, 18))  # 45 min
    old = analytics._limits_now(snaps, now=datetime(2026, 10, 9, 9, 11, 19))    # 45 min + 1 s
    assert fresh["claude_five_hour"]["stale"] is False
    assert fresh["claude_five_hour"]["state"] == "ok"
    assert old["claude_five_hour"]["stale"] is True
    assert old["claude_five_hour"]["state"] == "stale"
    assert old["claude_five_hour"]["age_sec"] == 45 * 60 + 1
    # unavailable stays unavailable, but still says it is old
    assert old["gemini"]["state"] == "unavailable"
    assert old["gemini"]["stale"] is True


def test_limits_now_empty_log_is_empty(tmp_path):
    assert analytics._limits_now(analytics._parse_capacity_log(tmp_path / "nope.md")) == {}


def _patched_dashboard_data(tmp_path, capacity_log):
    with patch("analytics.VAULT_PATH", tmp_path), \
         patch("analytics.LOG_FILE", tmp_path / "logs" / "orchestrator.log"), \
         patch("analytics.QUEUE_EVENTS_LOG_FILE", tmp_path / "queue-events.log"), \
         patch("analytics.CAPACITY_LOG_FILE", capacity_log), \
         patch("analytics.ALLOWED_CWD_ROOTS", []):
        analytics._cache["data"] = None
        try:
            return analytics.get_dashboard_data(days=7)
        finally:
            analytics._cache["data"] = None


def test_dashboard_data_carries_limits_now_and_unchanged_current_limits(tmp_path, capacity_log):
    data = _patched_dashboard_data(tmp_path, capacity_log)
    assert set(data["limits_now"]) >= {
        "claude_five_hour", "claude_seven_day", "codex_primary_window",
        "codex_secondary_window", "opencode",
    }
    assert all("age_sec" in v for v in data["limits_now"].values())
    # current_limits: byte-identical to the pre-change value, key order included
    assert json.dumps(data["current_limits"]) == json.dumps(_CURRENT_LIMITS_BEFORE)
    assert json.dumps(analytics._get_current_limits()) == json.dumps(_CURRENT_LIMITS_BEFORE)


def test_dashboard_data_keeps_every_existing_key(tmp_path, capacity_log):
    data = _patched_dashboard_data(tmp_path, capacity_log)
    before = {
        "generated_at", "total_tasks", "success_rate", "avg_duration_sec",
        "active_providers", "tasks_per_day", "provider_distribution",
        "limits_timeline", "current_limits", "recent_events",
        "usage_suggest_today", "session", "billing_recent", "billing_total",
        "cache_hit_rate_recent", "cache_hit_rate_total", "tool_trace_stats",
        "failure_counts", "failure_timeline", "active_runs",
    }
    assert set(data) == before | {"limits_now"}
    # opencode is in the timeline the charts draw from
    assert "opencode" in data["limits_timeline"]


# ── /api/data ships the mapping ─────────────────────────────────────────────


def test_api_data_ships_provider_meta_without_touching_the_cache(monkeypatch):
    cached = {
        "limits_timeline": {"opencode": [], "claude_five_hour": [], "zeta_window": []},
        "limits_now": {"codex_secondary_window": {}},
        "provider_distribution": {"labels": ["claude", "neuer_provider"], "values": [3, 1]},
    }
    monkeypatch.setattr(dashboard, "get_dashboard_data", lambda days=7: cached)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server, port = dashboard._bind_server(port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/data", timeout=5) as resp:
            payload = json.loads(resp.read())
    finally:
        server.shutdown()
        server.server_close()
    meta = payload["provider_meta"]
    assert set(meta) == {
        "opencode", "claude_five_hour", "zeta_window",
        "codex_secondary_window", "claude", "neuer_provider",
    }
    assert meta["opencode"]["label"] == "opencode (Tagesbudget)"
    assert meta["zeta_window"]["group"] == "other"
    assert "provider_meta" not in cached  # the 30-s cache object stays as it was
