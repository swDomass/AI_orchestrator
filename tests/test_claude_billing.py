"""claude_billing: billing state, notification-once, ledger, child env.

Paths and config constants are redirected to tmp_path; the real logs/ is never touched.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import claude_billing
import config

KEY = "sk-ant-test-0000"
HOUR = 3600


@pytest.fixture
def billing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_KEY", KEY)
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_RETRY_HOURS", 24)
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_RATE_RETRY_MIN", 60)
    monkeypatch.setattr(config, "CLAUDE_API_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(config, "CLAUDE_API_RUNS_FILE", tmp_path / "runs.jsonl")
    send = MagicMock(return_value=True)
    monkeypatch.setattr("notifier.send_message", send)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return SimpleNamespace(send=send, state=tmp_path / "state.json", runs=tmp_path / "runs.jsonl")


# ── config: the key is read once and removed from os.environ ──────────────────

def test_config_pops_key_from_environ_and_keeps_anthropic_key_unset():
    repo = Path(config.__file__).parent
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["ORCH_CLAUDE_API_KEY"] = KEY
    code = (
        "import json, os, config;"
        "print(json.dumps({'cfg': config.ORCH_CLAUDE_API_KEY,"
        " 'orch_in_env': 'ORCH_CLAUDE_API_KEY' in os.environ,"
        " 'anthropic_in_env': 'ANTHROPIC_API_KEY' in os.environ}))"
    )
    out = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=repo, env=env, capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip().splitlines()[-1]
    data = json.loads(out)
    assert data == {"cfg": KEY, "orch_in_env": False, "anthropic_in_env": False}


# ── child_env / key_configured ───────────────────────────────────────────────

def test_child_env_adds_key_without_touching_os_environ(billing):
    env = claude_billing.child_env()
    assert env["ANTHROPIC_API_KEY"] == KEY
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert env.get("PATH") == os.environ.get("PATH")  # full copy, not a delta


def test_child_env_without_key_raises_value_error(billing, monkeypatch):
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_KEY", "")
    assert claude_billing.key_configured() is False
    with pytest.raises(ValueError) as exc:
        claude_billing.child_env()
    assert KEY not in str(exc.value)


# ── api_active / trip / clear ────────────────────────────────────────────────

def test_api_active_without_key_is_false(billing, monkeypatch):
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_KEY", "")
    assert claude_billing.api_active() is False


def test_api_active_with_key_and_no_state_is_true(billing):
    assert claude_billing.api_active() is True


def test_trip_blocks_until_retry_window_over_then_clear_frees(billing):
    t0 = 1_800_000_000.0
    claude_billing.trip("credit", now=t0)
    assert claude_billing.api_active(now=t0 + 1) is False
    assert claude_billing.api_active(now=t0 + 23 * HOUR) is False
    assert claude_billing.api_active(now=t0 + 25 * HOUR) is True
    claude_billing.trip("credit", now=t0 + 30 * HOUR)
    assert claude_billing.api_active(now=t0 + 31 * HOUR) is False
    claude_billing.clear(now=t0 + 55 * HOUR)  # block expired -> episode over
    assert claude_billing.api_active(now=t0 + 31 * HOUR) is True
    assert not billing.state.exists()


def test_trip_writes_state_with_reason_and_never_the_key(billing):
    claude_billing.trip("auth", now=1_800_000_000.0)
    raw = billing.state.read_text(encoding="utf-8")
    state = json.loads(raw)
    assert state["reason"] == "auth"
    assert state["blocked_until"] == 1_800_000_000.0 + 24 * HOUR
    assert state["notified"] is True
    assert KEY not in raw
    sent = billing.send.call_args[0][0]
    assert KEY not in sent
    assert "API-Schlüssel ungültig" in sent


def test_trip_twice_notifies_exactly_once_and_again_after_clear(billing):
    claude_billing.trip("credit", now=1_800_000_000.0)
    claude_billing.trip("credit", now=1_800_000_000.0 + 25 * HOUR)
    assert billing.send.call_count == 1
    claude_billing.clear(now=1_800_000_000.0 + 50 * HOUR)
    claude_billing.trip("credit", now=1_800_000_000.0 + 50 * HOUR)
    assert billing.send.call_count == 2


def test_block_survives_failed_state_write_in_memory(billing, monkeypatch, caplog):
    def boom(path, content):
        raise OSError("disk full")

    monkeypatch.setattr(claude_billing, "_atomic_write", boom)
    t0 = 1_800_000_000.0
    claude_billing.trip("credit", now=t0)
    claude_billing.trip("credit", now=t0 + 1)
    assert not billing.state.exists()
    assert claude_billing.api_active(now=t0 + 2) is False
    assert billing.send.call_count == 1  # notified marker kept in memory
    assert "OSError" in caplog.text and KEY not in caplog.text
    claude_billing.clear(now=t0 + 25 * HOUR)
    assert claude_billing.api_active(now=t0 + 25 * HOUR) is True
    # clear() also drops the in-memory marker: a new episode notifies again.
    claude_billing.trip("credit", now=t0 + 26 * HOUR)
    assert billing.send.call_count == 2


def test_failed_write_beats_stale_expired_state_file(billing, monkeypatch):
    # Disk full: an old, expired episode file stays on disk and the new block
    # cannot be written. The in-memory block must win over the stale file.
    t0 = 1_800_000_000.0
    billing.state.write_text(json.dumps(
        {"blocked_until": t0 - HOUR, "reason": "credit", "notified": True}), encoding="utf-8")

    def boom(path, content):
        raise OSError("disk full")

    monkeypatch.setattr(claude_billing, "_atomic_write", boom)
    claude_billing.trip("credit", now=t0)
    assert claude_billing.api_active(now=t0 + 1) is False


def test_trip_marks_notified_even_when_telegram_fails(billing):
    billing.send.return_value = False
    claude_billing.trip("credit", now=1_800_000_000.0)
    claude_billing.trip("credit", now=1_800_000_000.0 + 25 * HOUR)
    assert billing.send.call_count == 1
    assert json.loads(billing.state.read_text(encoding="utf-8"))["notified"] is True


def test_trip_rate_blocks_only_rate_retry_minutes_with_rate_text(billing):
    # Spec: docs/adr/005-claude-api-credits-abrechnungsweg.md (429 -> short block)
    t0 = 1_800_000_000.0
    claude_billing.trip("rate", now=t0)
    assert json.loads(billing.state.read_text(encoding="utf-8"))["reason"] == "rate"
    assert claude_billing.api_active(now=t0 + 59 * 60) is False
    assert claude_billing.api_active(now=t0 + 61 * 60) is True  # not 24 h
    assert "API-Ratenlimit der Console-Organisation erreicht" in billing.send.call_args[0][0]


def test_rate_then_credit_episode_without_clear_sends_one_message(billing):
    t0 = 1_800_000_000.0
    claude_billing.trip("rate", now=t0)
    claude_billing.trip("credit", now=t0 + 2 * HOUR)
    assert billing.send.call_count == 1
    assert json.loads(billing.state.read_text(encoding="utf-8"))["reason"] == "credit"


def test_trip_and_clear_are_logged_clear_only_when_block_existed(billing, caplog):
    caplog.set_level("INFO", logger="claude_billing")
    claude_billing.clear()  # nothing blocked -> no line
    assert not caplog.records
    claude_billing.trip("rate", now=1_800_000_000.0)
    claude_billing.clear(now=1_800_000_000.0 + 2 * HOUR)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("reason=rate" in m and "until" in m for m in msgs)
    assert any("cleared" in m for m in msgs)
    assert all(KEY not in m for m in msgs)


def test_corrupt_state_file_counts_as_no_state(billing):
    billing.state.write_text("{not json", encoding="utf-8")
    assert claude_billing.api_active() is True


# ── block race: trip never shortens, clear never deletes a block in force ─────

def test_rate_trip_does_not_shorten_running_credit_block(billing):
    t0 = 1_800_000_000.0
    claude_billing.trip("credit", now=t0)
    claude_billing.trip("rate", now=t0 + HOUR)
    state = json.loads(billing.state.read_text(encoding="utf-8"))
    assert state["blocked_until"] == t0 + 24 * HOUR
    assert state["reason"] == "credit"
    assert claude_billing.api_active(now=t0 + 3 * HOUR) is False


def test_clear_during_block_keeps_state_and_no_second_notice(billing):
    t0 = 1_800_000_000.0
    claude_billing.trip("credit", now=t0)
    claude_billing.clear(now=t0 + HOUR)  # parallel run ended successfully
    assert billing.state.exists()
    claude_billing.trip("credit", now=t0 + 2 * HOUR)
    assert billing.send.call_count == 1


def test_clear_after_expiry_removes_state(billing):
    t0 = 1_800_000_000.0
    claude_billing.trip("credit", now=t0)
    claude_billing.clear(now=t0 + 25 * HOUR)
    assert not billing.state.exists()


def test_parallel_trip_and_clear_send_at_most_one_notice(billing):
    # Real clock: trip() blocks for 24 h from now, so clear() never wins.
    def worker_trip():
        for _ in range(20):
            claude_billing.trip("credit")

    def worker_clear():
        for _ in range(20):
            claude_billing.clear()

    threads = [threading.Thread(target=f) for f in (worker_trip, worker_clear, worker_clear, worker_trip)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert billing.send.call_count <= 1
    assert billing.state.exists()


# ── in-flight counter ────────────────────────────────────────────────────────

def test_in_flight_counter_up_and_down_also_on_exception(billing):
    assert claude_billing.api_runs_in_flight() == 0
    with claude_billing.api_run_in_flight():
        assert claude_billing.api_runs_in_flight() == 1
        with claude_billing.api_run_in_flight():
            assert claude_billing.api_runs_in_flight() == 2
    assert claude_billing.api_runs_in_flight() == 0
    with pytest.raises(RuntimeError), claude_billing.api_run_in_flight():
        raise RuntimeError("boom")
    assert claude_billing.api_runs_in_flight() == 0


# ── ledger ───────────────────────────────────────────────────────────────────

def test_ledger_record_ids(billing):
    claude_billing.record_api_run("s1", ["m1", "m2"], now=1_000_000.0)
    claude_billing.record_api_run("s2", ["m3"], now=1_000_100.0)
    assert claude_billing.api_message_ids() == {"m1", "m2", "m3"}


def test_ledger_drops_rows_older_than_window(billing, monkeypatch):
    monkeypatch.setattr(config, "QUOTA_RECALIBRATE_WINDOW_DAYS", 60)
    now = 10_000_000.0
    claude_billing.record_api_run("old", ["m_old"], now=now - 61 * 86400)
    claude_billing.record_api_run("new", ["m_new"], now=now)
    assert claude_billing.api_message_ids() == {"m_new"}
    assert len(billing.runs.read_text(encoding="utf-8").splitlines()) == 1


def test_ledger_missing_or_corrupt_is_empty(billing):
    assert claude_billing.api_message_ids() == set()
    billing.runs.write_text("garbage\n{\"ts\": \"x\"}\n", encoding="utf-8")
    assert claude_billing.api_message_ids() == set()


def test_filter_api_entries_removes_ledger_ids_only(billing):
    claude_billing.record_api_run("s", ["m_api"], now=1_000_000.0)
    entries = [
        SimpleNamespace(message_id="m_api"),
        SimpleNamespace(message_id="m_abo"),
        SimpleNamespace(message_id=None),
        SimpleNamespace(),
    ]
    kept = claude_billing.filter_api_entries(entries)
    assert [getattr(e, "message_id", "-") for e in kept] == ["m_abo", None, "-"]


def test_filter_api_entries_empty_ledger_returns_input_unchanged(billing):
    entries = [SimpleNamespace(message_id="m1")]
    assert claude_billing.filter_api_entries(entries) is entries


# ── transcript message IDs (incl. subagents) ─────────────────────────────────

SID = "0a1b2c3d-1111-2222-3333-444455556666"


def _write_jsonl(path, *events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n{broken\n", encoding="utf-8")


def _assistant_evt(mid):
    return {"type": "assistant", "message": {"id": mid, "role": "assistant"}}


def test_transcript_message_ids_main_and_subagent_files(billing, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setattr(config, "CLAUDE_PROJECTS_DIR", projects)
    proj = projects / "D--some-project"
    _write_jsonl(proj / f"{SID}.jsonl", _assistant_evt("msg_1"), _assistant_evt("msg_2"),
                 {"type": "user", "message": {"role": "user", "content": "hi"}})
    _write_jsonl(proj / SID / "subagents" / "agent-x.jsonl", _assistant_evt("msg_SUB"))
    _write_jsonl(proj / "ffffffff-0000-0000-0000-000000000000.jsonl", _assistant_evt("msg_OTHER"))
    assert sorted(claude_billing.transcript_message_ids(SID)) == ["msg_1", "msg_2", "msg_SUB"]


@pytest.mark.parametrize("sid", ["", "*", "../x", "sess-1", None, "*0123abcd"])
def test_transcript_message_ids_invalid_session_id_is_empty(billing, tmp_path, monkeypatch, sid):
    projects = tmp_path / "projects"
    monkeypatch.setattr(config, "CLAUDE_PROJECTS_DIR", projects)
    _write_jsonl(projects / "p" / "sess-1.jsonl", _assistant_evt("msg_x"))
    # Matches the glob "*0123abcd" — only a full-match check keeps it out.
    _write_jsonl(projects / "p" / "x0123abcd.jsonl", _assistant_evt("msg_y"))
    assert claude_billing.transcript_message_ids(sid) == []


def test_transcript_message_ids_missing_dir_is_empty(billing, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_PROJECTS_DIR", tmp_path / "nope")
    assert claude_billing.transcript_message_ids(SID) == []
