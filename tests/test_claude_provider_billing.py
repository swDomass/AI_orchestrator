"""ClaudeProvider billing path: env injection, stream parsing, fallback to subscription.

run_with_watchdog is faked with synthetic stream-json (shapes taken from the probes in
docs/adr/005-claude-api-credits-abrechnungsweg.md). config constants and files are
redirected to tmp_path.
"""

import json
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import claude_billing
import config
from providers.claude import ClaudeProvider

KEY = "sk-ant-test-0000"
CREDIT_TEXT = "Credit balance too low · Add funds: https://platform.claude.com/settings/billing"


def _line(evt: dict) -> str:
    return json.dumps(evt, separators=(",", ":"))


def _init(source: str, sid: str = "sess-1") -> str:
    return _line({"type": "system", "subtype": "init", "apiKeySource": source, "session_id": sid})


def _assistant(mid: str, text: str = "ok", error: str | None = None, sub: bool = False) -> str:
    evt = {"type": "assistant", "message": {"id": mid, "content": [{"type": "text", "text": text}]}}
    if error:
        evt["error"] = error
    if sub:
        evt["parent_tool_use_id"] = "toolu_1"
    return _line(evt)


def _result(text: str = "ok", *, is_error: bool = False, status: int | None = None,
            cost: float = 0.0123, sid: str = "sess-1") -> str:
    return _line({
        "type": "result", "subtype": "success", "is_error": is_error, "result": text,
        "api_error_status": status, "total_cost_usd": cost, "session_id": sid,
        "usage": {"input_tokens": 5, "output_tokens": 7},
    })


def _proc(*lines: str, rc: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=rc, stdout="\n".join(lines), stderr=stderr)


def _success(source: str) -> SimpleNamespace:
    return _proc(_init(source), _assistant("msg_A"), _assistant("msg_SUB", sub=True), _result())


CREDIT_FAIL = _proc(
    _init("ANTHROPIC_API_KEY"), _assistant("m_f", CREDIT_TEXT, error="billing_error"),
    _result(CREDIT_TEXT, is_error=True, status=400, cost=0), rc=1,
)
AUTH_FAIL = _proc(
    _init("ANTHROPIC_API_KEY"),
    _assistant("e56be78f-a2f1", "Invalid API key · Fix external API key", error="authentication_failed"),
    _result("Invalid API key · Fix external API key", is_error=True, status=401, cost=0), rc=1,
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_KEY", KEY)
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_RETRY_HOURS", 24)
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_RATE_RETRY_MIN", 60)
    monkeypatch.setattr(config, "CLAUDE_API_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(config, "CLAUDE_API_RUNS_FILE", tmp_path / "runs.jsonl")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    send = MagicMock(return_value=True)
    monkeypatch.setattr("notifier.send_message", send)
    calls: list[dict] = []
    queue: list[SimpleNamespace] = []

    def fake(cmd, **kwargs):
        calls.append(kwargs)
        return queue.pop(0)

    monkeypatch.setattr("providers.claude.run_with_watchdog", fake)
    return SimpleNamespace(
        calls=calls, queue=queue, send=send, state=tmp_path / "state.json",
        runs=tmp_path / "runs.jsonl", monkeypatch=monkeypatch,
    )


def _no_key(env):
    env.monkeypatch.setattr(config, "ORCH_CLAUDE_API_KEY", "")


def _state(env) -> dict:
    state: dict = json.loads(env.state.read_text(encoding="utf-8"))
    return state


# ── Injection + parsing ──────────────────────────────────────────────────────

def test_without_key_env_is_none_and_run_is_abo(env):
    _no_key(env)
    env.queue.append(_success("none"))
    res = ClaudeProvider().run("task")
    assert env.calls[0]["env"] is None
    assert res.success and res.billing == "abo"
    assert res.cost_usd == pytest.approx(0.0123)
    assert not env.runs.exists() and not env.state.exists()


def test_with_key_env_carries_anthropic_key_and_success_records_ledger(env):
    env.state.write_text(json.dumps({"blocked_until": 1.0, "reason": "credit", "notified": True}))
    env.queue.append(_success("ANTHROPIC_API_KEY"))
    res = ClaudeProvider().run("task")
    assert env.calls[0]["env"]["ANTHROPIC_API_KEY"] == KEY
    assert res.success and res.billing == "api"
    assert not env.state.exists()  # clear() after a successful API run
    assert claude_billing.api_message_ids() == {"msg_A", "msg_SUB"}  # incl. subagent message
    row = json.loads(env.runs.read_text(encoding="utf-8").splitlines()[0])
    assert row["session_id"] == "sess-1"


def test_without_init_event_billing_follows_intent(env):
    env.queue.append(_proc(_assistant("m1"), _result()))
    assert ClaudeProvider().run("task").billing == "api"
    _no_key(env)
    env.queue.append(_proc(_assistant("m2"), _result()))
    assert ClaudeProvider().run("task").billing == "abo"


def test_non_billing_failure_on_api_records_ledger_but_keeps_state(env):
    state = {"blocked_until": 1.0, "reason": "credit", "notified": True}
    env.state.write_text(json.dumps(state))
    env.queue.append(_proc(
        _init("ANTHROPIC_API_KEY"), _assistant("m_x", "boom", error="server_error"),
        _result("boom", is_error=True, status=500), rc=1,
    ))
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 1
    assert not res.success and res.billing == "api"
    assert env.state.exists()  # no clear
    assert "m_x" in claude_billing.api_message_ids()


# ── Fallback to subscription ─────────────────────────────────────────────────

def test_credit_error_repeats_once_on_abo_and_blocks(env):
    env.queue.extend([CREDIT_FAIL, _success("none")])
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 2
    assert env.calls[0]["env"]["ANTHROPIC_API_KEY"] == KEY
    assert env.calls[1]["env"] is None
    assert res.success and res.billing == "abo"
    assert _state(env)["reason"] == "credit"
    assert claude_billing.api_active() is False
    assert env.send.call_count == 1
    # only the failed API attempt is booked, never the abo repeat
    assert not claude_billing.api_message_ids() & {"msg_A", "msg_SUB"}


def test_invalid_key_probe_form_falls_back_with_reason_auth(env):
    env.queue.extend([AUTH_FAIL, _success("none")])
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 2 and env.calls[1]["env"] is None
    assert res.billing == "abo"
    assert _state(env)["reason"] == "auth"


def test_status_402_alone_counts_as_credit(env):
    env.queue.extend([
        _proc(_init("ANTHROPIC_API_KEY"), _result("nope", is_error=True, status=402), rc=1),
        _success("none"),
    ])
    ClaudeProvider().run("task")
    assert len(env.calls) == 2
    assert _state(env)["reason"] == "credit"


def test_text_match_in_stderr_on_failed_api_run_counts_as_credit(env):
    env.queue.extend([
        _proc(_init("ANTHROPIC_API_KEY"), rc=1,
              stderr="Error: your credit balance is too low"),
        _success("none"),
    ])
    ClaudeProvider().run("task")
    assert len(env.calls) == 2


# Official texts (platform.claude.com/docs/en/about-claude/api-credits-for-subscribers and
# /docs/en/api/rate-limits, fetched 2026-10-27): text only, no billing code, status 400.
@pytest.mark.parametrize("text", [
    "Your credit balance is too low to access the Anthropic API. "
    "Please go to Plans & Billing to upgrade or purchase credits.",
    "Credit balance too low · Add funds: https://platform.claude.com/settings/billing",
    "You have reached your specified workspace API usage limits. "
    "You will regain access on 2026-11-01 at 00:00 UTC.",
], ids=["api-credit-text", "cli-credit-text", "workspace-limit-text"])
def test_official_credit_and_workspace_limit_texts_count_as_credit(env, text):
    env.queue.extend([
        _proc(_init("ANTHROPIC_API_KEY"), _assistant("m_t", text),
              _result(text, is_error=True, status=400, cost=0), rc=1),
        _success("none"),
    ])
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 2 and res.billing == "abo"
    assert _state(env)["reason"] == "credit"


def test_only_one_repeat_even_if_abo_run_fails_too(env):
    env.queue.extend([CREDIT_FAIL, _proc(_init("none"), _result("x", is_error=True), rc=1)])
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 2
    assert not res.success and res.billing == "abo"


def test_two_failing_episodes_send_one_notice(env):
    env.monkeypatch.setattr(config, "ORCH_CLAUDE_API_RETRY_HOURS", 0)  # block expires at once
    env.queue.extend([CREDIT_FAIL, _success("none"), CREDIT_FAIL, _success("none")])
    ClaudeProvider().run("task")
    ClaudeProvider().run("task")
    assert len(env.calls) == 4
    assert env.send.call_count == 1


def test_blocked_state_skips_api_until_expiry(env):
    env.queue.extend([CREDIT_FAIL, _success("none"), _success("none")])
    ClaudeProvider().run("task")
    ClaudeProvider().run("task")
    assert env.calls[2]["env"] is None  # second run goes straight to the subscription
    assert len(env.calls) == 3


# ── What must NOT trip ───────────────────────────────────────────────────────

def _rate_fail(text: str = "rate limit exceeded, billing tier") -> SimpleNamespace:
    return _proc(
        _init("ANTHROPIC_API_KEY"), _assistant("m_r", text, error="rate_limit"),
        _result("rate limit exceeded", is_error=True, status=429), rc=1,
    )


def test_rate_limit_429_in_api_mode_repeats_once_on_abo_with_short_block(env):
    # 429 in API mode: one repeat on the subscription plus a short block (ADR-005).
    env.queue.extend([_rate_fail(), _success("none")])
    before = time.time()
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 2
    assert env.calls[0]["env"]["ANTHROPIC_API_KEY"] == KEY
    assert env.calls[1]["env"] is None
    assert res.success and res.billing == "abo"
    state = _state(env)
    assert state["reason"] == "rate"
    assert before + 59 * 60 <= state["blocked_until"] <= time.time() + 61 * 60  # not 24 h
    assert claude_billing.api_active() is False
    assert env.send.call_count == 1


def _overload_fail(status: int | None, text: str, stderr: str = "") -> SimpleNamespace:
    return _proc(
        _init("ANTHROPIC_API_KEY"), _assistant("m_o", "x"),
        _result(text, is_error=True, status=status), rc=1, stderr=stderr,
    )


def test_overload_529_in_api_mode_is_no_rate_and_does_not_trip(env):
    text = "API Error: 529 overloaded_error Overloaded"
    env.queue.append(_overload_fail(529, text, stderr=text))
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 1
    assert not res.success and res.error == "rate_limit" and res.billing == "api"
    assert not env.state.exists()
    env.send.assert_not_called()


def test_overload_stderr_without_status_is_no_rate_and_does_not_trip(env):
    env.queue.append(_overload_fail(None, "", stderr="Overloaded"))
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 1
    assert not res.success and res.error == "rate_limit" and res.billing == "api"
    assert not env.state.exists()


def test_explicit_non_429_status_wins_over_rate_text(env):
    # An explicit server status (529) beats rate-limit wording elsewhere in the text.
    env.queue.extend([_overload_fail(529, "API Error: 529 Overloaded", stderr="rate limit"), _success("none")])
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 1
    assert not res.success and res.error == "rate_limit" and res.billing == "api"
    assert not env.state.exists()


def test_rate_limit_code_with_explicit_529_status_is_no_rate(env):
    # The assistant `rate_limit` code follows the same status rule as the keywords.
    env.queue.extend([_proc(
        _init("ANTHROPIC_API_KEY"), _assistant("m_o", "x", error="rate_limit"),
        _result("overloaded", is_error=True, status=529), rc=1,
    ), _success("none")])
    ClaudeProvider().run("task")
    assert len(env.calls) == 1
    assert not env.state.exists()


def test_rate_limit_text_without_status_still_counts_as_rate(env):
    env.queue.extend([_overload_fail(None, "", stderr="Rate limit reached for requests"), _success("none")])
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 2 and res.success and res.billing == "abo"
    assert _state(env)["reason"] == "rate"


def test_rate_fallback_records_failed_api_attempt_but_not_the_abo_repeat(env):
    # A 429 mid-run: messages of the failed API attempt already ran on credits and
    # must stay out of the subscription calibration; the abo repeat must not.
    env.queue.extend([_rate_fail(), _success("none")])
    res = ClaudeProvider().run("task")
    assert res.success and res.billing == "abo"
    assert claude_billing.api_message_ids() == {"m_r"}  # not msg_A / msg_SUB of the repeat


def test_rate_limit_429_in_abo_mode_does_not_trip_or_repeat(env):
    _no_key(env)
    env.queue.append(_proc(
        _init("none"), _assistant("m_r", "rate limit exceeded", error="rate_limit"),
        _result("rate limit exceeded", is_error=True, status=429), rc=1,
    ))
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 1
    assert not res.success and res.billing == "abo"
    assert not env.state.exists()
    env.send.assert_not_called()


def test_timeout_on_api_path_does_not_fall_back(env):
    def boom(cmd, **kwargs):
        env.calls.append(kwargs)
        raise subprocess.TimeoutExpired(cmd, 1)

    env.monkeypatch.setattr("providers.claude.run_with_watchdog", boom)
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 1 and not res.success
    assert not env.state.exists()


def test_rate_then_credit_episode_sends_one_message_in_total(env):
    env.monkeypatch.setattr(config, "ORCH_CLAUDE_API_RATE_RETRY_MIN", 0)  # block expires at once
    env.queue.extend([_rate_fail(), _success("none"), CREDIT_FAIL, _success("none")])
    ClaudeProvider().run("task")
    ClaudeProvider().run("task")
    assert len(env.calls) == 4
    assert _state(env)["reason"] == "credit"
    assert env.send.call_count == 1


def test_fallback_logs_warning_without_key(env, caplog):
    caplog.set_level("INFO")
    env.queue.extend([_rate_fail(f"limit for {KEY}"), _success("none")])
    ClaudeProvider().run("task")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("rate" in r.getMessage() for r in warnings)
    assert all(KEY not in r.getMessage() for r in caplog.records)


def test_credit_text_in_abo_mode_does_not_trip(env):
    _no_key(env)
    env.queue.append(_proc(
        _init("none"), _assistant("m_a", CREDIT_TEXT),
        _result(CREDIT_TEXT, is_error=True), rc=1,
    ))
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 1
    assert res.billing == "abo"
    assert not env.state.exists()
    env.send.assert_not_called()


def test_successful_answer_mentioning_billing_does_not_trip(env):
    env.queue.append(_proc(
        _init("ANTHROPIC_API_KEY"), _assistant("m_b", "billing report done"),
        _result("billing report: credit balance fine"),
    ))
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 1 and res.success
    assert not env.state.exists()


# ── Connector notice, timeout with partial stdout, session id on repeat ──────

NOTICE = ("⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY or another auth "
          "source is set and takes precedence over your claude.ai login · Unset it")


def test_connector_notice_alone_is_not_the_error(env):
    env.queue.append(_proc(
        _init("ANTHROPIC_API_KEY"), _assistant("m_x", "model not found"),
        _result("model not found", is_error=True, status=404, cost=0),
        rc=1, stderr=NOTICE,
    ))
    res = ClaudeProvider().run("task")
    assert not res.success and len(env.calls) == 1
    assert "model not found" in res.error
    assert "connectors" not in res.error and "auth" not in res.error.lower()


def test_connector_notice_is_stripped_but_real_stderr_stays(env):
    env.queue.append(_proc(rc=1, stderr=f"{NOTICE}\nboom: disk exploded\n"))
    res = ClaudeProvider().run("task")
    assert res.error == "boom: disk exploded"


def test_timeout_with_partial_stdout_books_ids_in_ledger_without_fallback(env):
    partial = "\n".join([_init("ANTHROPIC_API_KEY", sid="sess-T"), _assistant("m_T1")])

    def boom(cmd, **kwargs):
        env.calls.append(kwargs)
        # bytes on purpose: exercises the str/bytes normalisation
        raise subprocess.TimeoutExpired(cmd, 1, output=partial.encode(), stderr=None)

    env.monkeypatch.setattr("providers.claude.run_with_watchdog", boom)
    res = ClaudeProvider().run("task")
    assert len(env.calls) == 1 and res.error == "timeout" and res.billing == "api"
    assert claude_billing.api_message_ids() == {"m_T1"}
    assert not env.state.exists()


def test_abo_repeat_resumes_instead_of_reusing_session_id(env):
    env.monkeypatch.setattr(config, "CLAUDE_SESSION_ENABLED", True)
    cmds: list[list[str]] = []
    queue = [CREDIT_FAIL, _success("none")]

    def fake(cmd, **kwargs):
        cmds.append(list(cmd))
        env.calls.append(kwargs)
        return queue.pop(0)

    env.monkeypatch.setattr("providers.claude.run_with_watchdog", fake)
    res = ClaudeProvider().run("task", session_id="abc")
    assert res.success and len(cmds) == 2
    assert cmds[0][cmds[0].index("--session-id") + 1] == "abc"
    assert "--session-id" not in cmds[1]
    assert cmds[1][cmds[1].index("--resume") + 1] == "abc"


def test_transcript_subagent_ids_join_stream_ids_in_ledger(env, tmp_path):
    sid = "0a1b2c3d-1111-2222-3333-444455556666"
    sub = tmp_path / "claude-projects" / "proj" / sid / "subagents" / "agent-x.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text(json.dumps({"type": "assistant", "message": {"id": "msg_SUB_T"}}) + "\n", encoding="utf-8")
    env.queue.append(_proc(
        _init("ANTHROPIC_API_KEY", sid), _assistant("msg_A"), _result(sid=sid),
    ))
    res = ClaudeProvider().run("task")
    assert res.success and res.billing == "api"
    assert claude_billing.api_message_ids() == {"msg_A", "msg_SUB_T"}


def test_resumed_run_books_only_new_transcript_ids(env, tmp_path):
    sid = "0a1b2c3d-1111-2222-3333-444455556666"
    tr = tmp_path / "claude-projects" / "proj" / f"{sid}.jsonl"
    tr.parent.mkdir(parents=True)
    tr.write_text(json.dumps({"type": "assistant", "message": {"id": "msg_OLD"}}) + "\n", encoding="utf-8")
    env.queue.append(_proc(_init("ANTHROPIC_API_KEY", sid), _assistant("m_new"), _result(sid=sid)))
    res = ClaudeProvider().run("task", session_id=sid, resume=True)
    assert res.success and res.billing == "api"
    assert claude_billing.api_message_ids() == {"m_new"}  # msg_OLD predates the resume


def test_api_run_in_flight_only_during_api_attempt(env):
    seen: list[int] = []

    def fake(cmd, **kwargs):
        seen.append(claude_billing.api_runs_in_flight())
        return env.queue.pop(0)

    env.monkeypatch.setattr("providers.claude.run_with_watchdog", fake)
    env.queue.extend([CREDIT_FAIL, _success("none")])
    ClaudeProvider().run("task")
    assert seen == [1, 0]  # API attempt counted, subscription repeat not
    assert claude_billing.api_runs_in_flight() == 0
