"""Credit-run bookkeeping outside the live gating (Scheibe 2b).

* Calibration (``quota_calibration._load_entries``) and the local 429 fallback
  (``limits._get_claude_limits_from_local``) must not see credit-billed runs
  (ledger ``CLAUDE_API_RUNS_FILE``): they do not count against the subscription.
* ``runs.jsonl`` records carry ``billing`` / ``cost_usd``; old lines without the
  fields still load.
* ``_RunSpan`` fills both: single run from the ``RunResult``, tool run from the
  billing path at start (Claude only, no cost).
"""

import datetime as dt
import json
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from test_orchestrator_tool_tasks import _no_worktree_gate, _stub_single_shot_env
from test_quota_calibration import _FakeEntry, _make_claude_limits, _patch_load_entries

import claude_billing
import config
import limits
import orchestrator
import quota_calibration
import replay


def _entry(message_id, tokens=0):
    return SimpleNamespace(message_id=message_id, tokens=tokens)


def _write_ledger(path, *message_ids):
    path.write_text(
        json.dumps({"ts": time.time(), "session_id": "s", "message_ids": list(message_ids)}) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "claude-api-runs.jsonl"
    monkeypatch.setattr(config, "CLAUDE_API_RUNS_FILE", path)
    return path


@pytest.fixture
def fake_monitor(monkeypatch):
    """Importable claude_monitor stand-in (independent of the installed package)."""
    models = ModuleType("claude_monitor.core.models")
    models.CostMode = SimpleNamespace(AUTO="auto")
    reader = ModuleType("claude_monitor.data.reader")
    analyzer = ModuleType("claude_monitor.data.analyzer")
    pkgs = {}
    for name in ("claude_monitor", "claude_monitor.core", "claude_monitor.data"):
        pkgs[name] = ModuleType(name)
        pkgs[name].__path__ = []
    for name, mod in {
        **pkgs,
        "claude_monitor.core.models": models,
        "claude_monitor.data.reader": reader,
        "claude_monitor.data.analyzer": analyzer,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return SimpleNamespace(reader=reader, analyzer=analyzer)


# ------------------------------------------------------- quota_calibration


def test_load_entries_drops_ledger_ids_and_keeps_others(fake_monitor, ledger):
    _write_ledger(ledger, "api-1", "api-2")
    entries = [_entry("abo-1"), _entry("api-1"), _entry("abo-2"), _entry("api-2"), _entry(None)]
    fake_monitor.reader.load_usage_entries = lambda **kw: (entries, None)

    result = quota_calibration._load_entries(24)

    assert [e.message_id for e in result] == ["abo-1", "abo-2", None]


def test_load_entries_empty_ledger_leaves_entries_unchanged(fake_monitor, ledger):
    ledger.write_text("", encoding="utf-8")
    entries = [_entry("a"), _entry("b")]
    fake_monitor.reader.load_usage_entries = lambda **kw: (entries, None)

    assert quota_calibration._load_entries(24) == entries


def test_load_entries_none_when_monitor_missing(monkeypatch, ledger):
    for key in list(sys.modules):
        if key == "claude_monitor" or key.startswith("claude_monitor."):
            monkeypatch.delitem(sys.modules, key, raising=False)
    monkeypatch.setitem(sys.modules, "claude_monitor", None)

    assert quota_calibration._load_entries(24) is None


# ----------------------------------------------------- local 429 fallback


def _local_limits(fake_monitor, entries):
    """Run the fallback with a fake analyzer whose active block sums the entries."""
    fake_monitor.reader.load_usage_entries = lambda **kw: (entries, None)

    class FakeAnalyzer:
        def __init__(self, session_duration_hours):
            pass

        def transform_to_blocks(self, ents):
            total = sum(e.tokens for e in ents)
            return [SimpleNamespace(
                is_active=True,
                end_time=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
                token_counts=SimpleNamespace(
                    input_tokens=total, output_tokens=0,
                    cache_creation_tokens=0, cache_read_tokens=0,
                ),
            )]

    fake_monitor.analyzer.SessionAnalyzer = FakeAnalyzer
    return limits._get_claude_limits_from_local("pro")


def test_local_fallback_ignores_credit_entries(fake_monitor, ledger):
    limit = limits._CLAUDE_LOCAL_PLAN_LIMITS["pro"]
    entries = [_entry("abo-1", tokens=limit // 10), _entry("api-1", tokens=limit // 2)]

    without_ledger = _local_limits(fake_monitor, entries)
    _write_ledger(ledger, "api-1")
    with_ledger = _local_limits(fake_monitor, entries)

    assert without_ledger.remaining_pct == pytest.approx(40.0, abs=0.01)
    assert with_ledger.remaining_pct == pytest.approx(90.0, abs=0.01)


def test_local_fallback_skipped_while_api_run_in_flight(fake_monitor, ledger):
    entries = [_entry("abo-1", tokens=1000)]
    # Fully wired fake first: outside a credit run the fallback yields a result ...
    assert _local_limits(fake_monitor, entries) is not None
    # ... and the very same call returns None while one is in flight.
    with claude_billing.api_run_in_flight():
        assert limits._get_claude_limits_from_local("pro") is None
    assert limits._get_claude_limits_from_local("pro") is not None


def test_local_fallback_only_credit_entries_gives_no_data(fake_monitor, ledger):
    _write_ledger(ledger, "api-1")

    assert _local_limits(fake_monitor, [_entry("api-1", tokens=1000)]) is None


# --------------------------------------------------------------- replay


def _record(**kw):
    return replay.build_record(
        run_id="r1", ts_start=dt.datetime.now() - dt.timedelta(seconds=5),
        task_text="t", provider="claude", **kw,
    )


def test_build_record_carries_billing_and_cost():
    replay.append_run(_record(billing="api", cost_usd=0.42))

    (run,) = replay.read_runs()
    assert run["billing"] == "api"
    assert run["cost_usd"] == 0.42


def test_build_record_defaults_are_none():
    replay.append_run(_record())

    (run,) = replay.read_runs()
    assert run["billing"] is None
    assert run["cost_usd"] is None
    assert _record(billing="").billing is None


def test_old_line_without_billing_fields_still_loads():
    old = {
        "run_id": "old", "ts_start": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "ts_end": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "task_text": "t",
        "provider": "claude", "exit_status": "ok", "tokens": {"input": 1, "output": 2},
    }
    with open(replay.get_store_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(old) + "\n")
    replay.append_run(_record(billing="abo"))

    runs = {r["run_id"]: r for r in replay.read_runs()}
    assert "billing" not in runs["old"]
    assert runs["r1"]["billing"] == "abo"


# ----------------------------------------------------------------- span


def test_span_emits_billing_and_cost():
    span = orchestrator._RunSpan(run_id="s1", ts_start=dt.datetime.now(), task_text="t")
    span.ok(billing="api", cost_usd=0.5)
    span.emit()

    (run,) = replay.read_runs()
    assert (run["billing"], run["cost_usd"]) == ("api", 0.5)


def _run_single_shot(monkeypatch, provider, result):
    _stub_single_shot_env(monkeypatch, raw_line="- [ ] Plain claude task", provider=provider)
    monkeypatch.setattr(orchestrator, "_run_with_retry", lambda *a, **kw: (result, False))
    # Success path: no queue file / memory / notifier I/O.
    monkeypatch.setattr(orchestrator, "_finalize_task_with_result_checked", lambda *a, **kw: True)
    monkeypatch.setattr(
        orchestrator, "_verify_task_result",
        lambda *a, **kw: SimpleNamespace(ok=True, note="", missing=False),
    )
    monkeypatch.setattr(orchestrator.memory_module, "store_result", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "notify_task_done", lambda *a, **kw: None)
    orchestrator.run_once()
    return replay.read_runs()


def test_single_run_success_writes_billing_and_cost(monkeypatch):
    provider = SimpleNamespace(name="claude", set_cooldown=Mock())
    result = orchestrator.RunResult(success=True, output="ok", billing="api", cost_usd=0.5)

    (run,) = _run_single_shot(monkeypatch, provider, result)

    assert run["exit_status"] == "ok"
    assert (run["billing"], run["cost_usd"]) == ("api", 0.5)


def test_single_run_failure_keeps_billing_facts(monkeypatch):
    provider = SimpleNamespace(name="claude", set_cooldown=Mock())
    result = orchestrator.RunResult(success=False, error="hang", billing="abo", cost_usd=0.1)
    monkeypatch.setattr(orchestrator, "mark_retry", Mock(return_value=True))

    (run,) = _run_single_shot(monkeypatch, provider, result)

    assert run["exit_status"] != "ok"
    assert (run["billing"], run["cost_usd"]) == ("abo", 0.1)


def test_single_run_other_provider_has_no_billing(monkeypatch):
    provider = SimpleNamespace(name="codex", set_cooldown=Mock())

    (run,) = _run_single_shot(monkeypatch, provider, orchestrator.RunResult(success=True, output="ok"))

    assert run["billing"] is None
    assert run["cost_usd"] is None


def _run_tool_task(monkeypatch, provider_name, api_active, rotation=None):
    # rotation: [(provider_name, outcome), ...] -> unforced task, select_provider walks the list.
    provider = SimpleNamespace(name=provider_name, set_cooldown=Mock())
    monkeypatch.setattr(claude_billing, "api_active", lambda *a, **kw: api_active)
    _no_worktree_gate(monkeypatch)
    outcome = orchestrator.ToolTaskExecutionOutcome(success=True, finalized=True, output="done")
    task = f"Task #tool:test-loop #{provider_name}"
    monkeypatch.setattr(
        orchestrator, "read_queue_items",
        lambda: [SimpleNamespace(task_text=task, line_no=1, raw_line=f"- [ ] {task}")],
    )
    monkeypatch.setattr(orchestrator, "read_queue", lambda: [task])
    monkeypatch.setattr(orchestrator, "extract_cwd", lambda _t: None)
    monkeypatch.setattr(orchestrator, "extract_timeout", lambda _t, default=0: default)
    monkeypatch.setattr(orchestrator, "extract_tool_tag", lambda _t: "test-loop")
    monkeypatch.setattr(orchestrator, "has_explicit_provider_tag", lambda _t: rotation is None)
    monkeypatch.setattr(orchestrator, "get_limits", lambda force_refresh=False: SimpleNamespace())
    monkeypatch.setattr(orchestrator, "select_provider", lambda *a, **kw: provider)
    monkeypatch.setattr(orchestrator, "_execute_tool_task", Mock(return_value=outcome))
    if rotation:
        steps = iter(rotation)
        current = {}

        def _select(*a, **kw):
            name, current["outcome"] = next(steps)
            return SimpleNamespace(name=name, set_cooldown=Mock())

        monkeypatch.setattr(orchestrator, "select_provider", _select)
        monkeypatch.setattr(
            orchestrator, "_execute_tool_task", lambda *a, **kw: current["outcome"]
        )
    for name in ("append_log", "notify_error", "notify_task_started",
                 "notify_providers_exhausted", "notify_queue_complete"):
        monkeypatch.setattr(orchestrator, name, lambda *a, **kw: None)
    orchestrator.run_once()
    return replay.read_runs()


@pytest.mark.parametrize(("api_active", "expected"), [(True, "api"), (False, "abo")])
def test_tool_run_claude_records_billing_path_without_cost(monkeypatch, api_active, expected):
    (run,) = _run_tool_task(monkeypatch, "claude", api_active)

    assert run["billing"] == expected
    assert run["cost_usd"] is None


def test_tool_run_other_provider_has_no_billing(monkeypatch):
    (run,) = _run_tool_task(monkeypatch, "codex", True)

    assert run["billing"] is None
    assert run["cost_usd"] is None


def test_tool_run_rotated_from_claude_to_codex_has_no_billing(monkeypatch):
    # The billing path of the failed Claude attempt must not linger on the Codex run.
    failed = orchestrator.ToolTaskExecutionOutcome(
        success=False, finalized=False, retryable=True, error_code="rate_limit")
    ok = orchestrator.ToolTaskExecutionOutcome(success=True, finalized=True, output="done")

    (run,) = _run_tool_task(
        monkeypatch, "claude", True, rotation=[("claude", failed), ("codex", ok)])

    assert run["provider"] == "codex"
    assert run["billing"] is None


# ── calibration sample skipped while a credit run is in flight ───────────────

def test_calibration_sample_skipped_while_api_run_in_flight(tmp_path, monkeypatch):
    _patch_load_entries(monkeypatch, [_FakeEntry(100, 50)])
    csv_path = tmp_path / "calib.csv"
    with claude_billing.api_run_in_flight():
        quota_calibration.log_calibration_sample(_make_claude_limits(), csv_path)
    assert not csv_path.exists()
    quota_calibration.log_calibration_sample(_make_claude_limits(), csv_path)
    assert csv_path.exists() and len(csv_path.read_text(encoding="utf-8").splitlines()) >= 2
