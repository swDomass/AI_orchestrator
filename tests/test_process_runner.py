"""Tests for providers/process_runner.py — the liveness/hang watchdog.

All fakes use ``sys.executable -c "..."`` with TINY timeouts; no real CLIs,
no quota. Platform-neutral cases use shell=False; the tree-kill case (d) is
platform-aware with shell=True on Windows.
"""

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from providers.process_runner import run_with_watchdog
from providers.claude import ClaudeProvider
from providers.gemini import GeminiProvider
from providers.codex import CodexProvider


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


# ---------------------------------------------------------------------------
# (a) Productive run is NOT idle-killed even past the old aggressive deadline
# ---------------------------------------------------------------------------

def test_periodic_output_not_idle_killed():
    code = (
        "import time,sys\n"
        "for i in range(15):\n"
        "    sys.stdout.write(str(i)+'\\n'); sys.stdout.flush(); time.sleep(0.2)\n"
    )
    result = run_with_watchdog(
        _py(code), input_text=None, cwd=None,
        idle_timeout=1.0, hard_timeout=30, shell=False,
    )
    assert result.returncode == 0
    assert "0" in result.stdout and "14" in result.stdout


# ---------------------------------------------------------------------------
# (b) Silent process → idle-kill
# ---------------------------------------------------------------------------

def test_silent_process_idle_killed():
    code = "import time; time.sleep(30)"
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        run_with_watchdog(
            _py(code), input_text=None, cwd=None,
            idle_timeout=1.0, hard_timeout=30, shell=False,
        )
    assert exc.value.timeout_kind == "idle"
    assert time.monotonic() - start < 5  # killed quickly, not after 30s


# ---------------------------------------------------------------------------
# (c) hard_timeout kills a process that keeps emitting
# ---------------------------------------------------------------------------

def test_hard_timeout_kills_busy_process():
    code = (
        "import time,sys\n"
        "while True:\n"
        "    sys.stdout.write('x\\n'); sys.stdout.flush(); time.sleep(0.05)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        run_with_watchdog(
            _py(code), input_text=None, cwd=None,
            idle_timeout=10, hard_timeout=1.0, shell=False,
        )
    assert exc.value.timeout_kind == "hard"


# ---------------------------------------------------------------------------
# (d) Tree-kill reaps a grandchild (platform-aware)
# ---------------------------------------------------------------------------

def test_tree_kill_reaps_grandchild(tmp_path):
    marker = tmp_path / "marker.txt"
    # Grandchild writes a monotonically growing marker to a FILE (not the
    # inherited pipe → no liveness), then loops forever.
    gc_script = tmp_path / "gc.py"
    gc_script.write_text(
        "import time\n"
        f"f=open(r'{marker}','w')\n"
        "i=0\n"
        "while True:\n"
        "    f.seek(0); f.write(str(i)); f.flush(); i+=1; time.sleep(0.1)\n"
    )
    # Parent spawns the grandchild, then sleeps silently → idle fires.
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable, r'{gc_script}'])\n"
        "time.sleep(60)\n"
    )
    parent_cmd = [sys.executable, str(parent_script)]
    if sys.platform == "win32":
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            run_with_watchdog(
                parent_cmd, input_text=None, cwd=None,
                idle_timeout=1.0, hard_timeout=30, shell=True,
            )
    else:
        try:
            import os, signal  # noqa: F401
            os.getpgid  # noqa: B018
        except (ImportError, AttributeError):
            pytest.skip("POSIX process-group primitives unavailable")
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            run_with_watchdog(
                parent_cmd, input_text=None, cwd=None,
                idle_timeout=1.0, hard_timeout=30, shell=False,
            )
    assert exc.value.timeout_kind == "idle"
    # Grandchild must be dead: marker frozen after a short wait.
    if not marker.exists():
        return  # grandchild never started writing — nothing more to assert
    first = marker.read_text() or "0"
    time.sleep(1.5)
    second = marker.read_text() or "0"
    assert first == second, "grandchild kept running after tree-kill"


# ---------------------------------------------------------------------------
# (e) NDJSON parsing pulls result + usage from multi-line stream-json
# ---------------------------------------------------------------------------

def test_ndjson_extracts_last_result_event():
    ndjson = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {}}),
        json.dumps({
            "type": "result", "subtype": "success", "result": "done",
            "usage": {
                "input_tokens": 11, "output_tokens": 22,
                "cache_creation_input_tokens": 33, "cache_read_input_tokens": 44,
            },
        }),
    ])
    evt = ClaudeProvider._extract_result_event(ndjson)
    assert evt["result"] == "done"

    output, tokens = ClaudeProvider._parse_json_response(ndjson)
    assert output == "done"
    assert tokens == {
        "input_tokens": 11, "output_tokens": 22,
        "cache_creation_input_tokens": 33, "cache_read_input_tokens": 44,
    }


def test_ndjson_last_result_event_wins():
    # Defensive guard; the CLI emits empirically exactly 1 result event as the
    # last line (verified 2026-05-30). If two appear, the LAST must win.
    ndjson = "\n".join([
        json.dumps({"type": "result", "result": "first"}),
        json.dumps({"type": "result", "result": "second"}),
    ])
    evt = ClaudeProvider._extract_result_event(ndjson)
    assert evt["result"] == "second"


# ---------------------------------------------------------------------------
# (f) TimeoutExpired → error mapping (idle→hang, hard→timeout)
# ---------------------------------------------------------------------------

def _raise_timeout(kind):
    def _fake(*a, **kw):
        exc = subprocess.TimeoutExpired(cmd="x", timeout=1)
        if kind is not None:
            exc.timeout_kind = kind
        raise exc
    return _fake


def test_claude_idle_maps_to_hang(monkeypatch):
    monkeypatch.setattr("providers.claude.run_with_watchdog", _raise_timeout("idle"))
    assert ClaudeProvider().run("t").error == "hang"


def test_claude_hard_maps_to_timeout(monkeypatch):
    monkeypatch.setattr("providers.claude.run_with_watchdog", _raise_timeout("hard"))
    assert ClaudeProvider().run("t").error == "timeout"


def test_claude_no_kind_defaults_to_timeout(monkeypatch):
    monkeypatch.setattr("providers.claude.run_with_watchdog", _raise_timeout(None))
    assert ClaudeProvider().run("t").error == "timeout"


def test_gemini_idle_maps_to_hang(monkeypatch):
    monkeypatch.setattr("providers.gemini.run_with_watchdog", _raise_timeout("idle"))
    assert GeminiProvider().run("t").error == "hang"


def test_codex_idle_maps_to_hang(monkeypatch):
    monkeypatch.setattr("providers.codex.run_with_watchdog", _raise_timeout("idle"))
    assert CodexProvider().run("t").error == "hang"


def test_codex_hard_maps_to_timeout(monkeypatch):
    monkeypatch.setattr("providers.codex.run_with_watchdog", _raise_timeout("hard"))
    assert CodexProvider().run("t").error == "timeout"


# ---------------------------------------------------------------------------
# (g) Pipe-deadlock regression: >64KB on BOTH streams, clean exit
# ---------------------------------------------------------------------------

def test_large_dual_stream_no_deadlock():
    code = (
        "import sys\n"
        "blob='A'*100000\n"
        "sys.stdout.write(blob); sys.stdout.flush()\n"
        "sys.stderr.write('B'*100000); sys.stderr.flush()\n"
    )
    result = run_with_watchdog(
        _py(code), input_text=None, cwd=None,
        idle_timeout=10, hard_timeout=30, shell=False,
    )
    assert result.returncode == 0
    assert len(result.stdout) >= 100000
    assert len(result.stderr) >= 100000


# ---------------------------------------------------------------------------
# (h) Tool-aware liveness: a tool_use event pauses the idle timer
# ---------------------------------------------------------------------------

# Real claude 2.1.158 stream-json schema: tool activity lives in the content
# blocks of assistant/user events, NOT in a top-level {"type":"tool_use"} event.
_ASSISTANT_TOOL_USE = json.dumps({
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "t1"}]},
})
_USER_TOOL_RESULT = json.dumps({
    "type": "user",
    "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]},
})
_RESULT_EVT = json.dumps({"type": "result", "subtype": "success", "result": "ok"})


def test_tool_use_event_pauses_idle_timer():
    # Emit an assistant event carrying a tool_use content block (real schema),
    # then go silent longer than idle_timeout (simulating a running tool), then
    # emit the tool_result + result. With liveness_lines=True the silent phase
    # must NOT trigger an idle-kill.
    code = (
        "import time,sys\n"
        f"sys.stdout.write({_ASSISTANT_TOOL_USE!r}+'\\n'); sys.stdout.flush()\n"
        "time.sleep(2.5)\n"  # > idle_timeout, simulates a running tool
        f"sys.stdout.write({_USER_TOOL_RESULT!r}+'\\n'); sys.stdout.flush()\n"
        f"sys.stdout.write({_RESULT_EVT!r}+'\\n'); sys.stdout.flush()\n"
    )
    result = run_with_watchdog(
        _py(code), input_text=None, cwd=None,
        idle_timeout=1.0, hard_timeout=30, shell=False,
        liveness_lines=True,
    )
    assert result.returncode == 0
    assert "result" in result.stdout


def test_full_ndjson_sequence_with_long_tool_not_idle_killed():
    # Regression for the toter-Code finding: the FULL real event sequence
    # (init → rate_limit_event → assistant[thinking] → assistant[tool_use] →
    # SILENCE > idle_timeout → user[tool_result] → assistant[text] → result),
    # with liveness_lines=True, must run to completion without an idle-kill.
    init = json.dumps({"type": "system", "subtype": "init"})
    rate = json.dumps({"type": "rate_limit_event", "message": "ok"})
    thinking = json.dumps({"type": "assistant",
                           "message": {"content": [{"type": "thinking"}]}})
    text = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text", "text": "done"}]}})
    code = (
        "import time,sys\n"
        f"sys.stdout.write({init!r}+'\\n'); sys.stdout.flush()\n"
        f"sys.stdout.write({rate!r}+'\\n'); sys.stdout.flush()\n"
        f"sys.stdout.write({thinking!r}+'\\n'); sys.stdout.flush()\n"
        f"sys.stdout.write({_ASSISTANT_TOOL_USE!r}+'\\n'); sys.stdout.flush()\n"
        "time.sleep(2.5)\n"  # tool running, stdout silent > idle_timeout
        f"sys.stdout.write({_USER_TOOL_RESULT!r}+'\\n'); sys.stdout.flush()\n"
        f"sys.stdout.write({text!r}+'\\n'); sys.stdout.flush()\n"
        f"sys.stdout.write({_RESULT_EVT!r}+'\\n'); sys.stdout.flush()\n"
    )
    result = run_with_watchdog(
        _py(code), input_text=None, cwd=None,
        idle_timeout=1.0, hard_timeout=30, shell=False,
        liveness_lines=True,
    )
    assert result.returncode == 0
    assert "result" in result.stdout


def test_parallel_tool_use_not_idle_killed_until_all_return():
    # P1 regression: Claude emits parallel tool_use blocks in ONE assistant
    # message. With the old single-boolean _tool_active, the FIRST tool_result
    # flipped the idle timer back on while the sibling tool was still running →
    # false idle-kill of a productive run. With the set-of-open-ids model the
    # run must survive the silent window after only t1 returns (t2 still open).
    two_tools = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "id": "t1"},
            {"type": "tool_use", "name": "Bash", "id": "t2"},
        ]},
    })
    result_t1 = json.dumps({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]},
    })
    result_t2 = json.dumps({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t2"}]},
    })
    code = (
        "import time,sys\n"
        f"sys.stdout.write({two_tools!r}+'\\n'); sys.stdout.flush()\n"
        "time.sleep(0.3)\n"
        f"sys.stdout.write({result_t1!r}+'\\n'); sys.stdout.flush()\n"
        "time.sleep(2.5)\n"  # t2 still running, stdout silent > idle_timeout
        f"sys.stdout.write({result_t2!r}+'\\n'); sys.stdout.flush()\n"
        f"sys.stdout.write({_RESULT_EVT!r}+'\\n'); sys.stdout.flush()\n"
    )
    result = run_with_watchdog(
        _py(code), input_text=None, cwd=None,
        idle_timeout=1.0, hard_timeout=30, shell=False,
        liveness_lines=True,
    )
    assert result.returncode == 0
    assert "result" in result.stdout


def test_silent_phase_killed_without_liveness_lines():
    # Counter-proof: same silent phase, byte-only mode → idle-kill.
    code = (
        "import time,sys\n"
        f"sys.stdout.write({_ASSISTANT_TOOL_USE!r}+'\\n'); sys.stdout.flush()\n"
        "time.sleep(10)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        run_with_watchdog(
            _py(code), input_text=None, cwd=None,
            idle_timeout=1.0, hard_timeout=30, shell=False,
            liveness_lines=False,
        )
    assert exc.value.timeout_kind == "idle"


# ---------------------------------------------------------------------------
# (i) rate_limit detection runs on RAW stdout (separate rate_limit_event line)
# ---------------------------------------------------------------------------

def test_rate_limit_detected_on_raw_stdout(monkeypatch):
    # A separate rate_limit_event line carries the wording; the result event has
    # no usage-limit wording. Detection on raw stdout must still flag rate_limit.
    ndjson = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "rate_limit_event", "message": "usage limit reached"}),
        json.dumps({"type": "result", "subtype": "error", "result": "stopped"}),
    ])
    monkeypatch.setattr(
        "providers.claude.run_with_watchdog",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout=ndjson, stderr=""),
    )
    assert ClaudeProvider().run("t").error == "rate_limit"


def test_quota_word_in_success_answer_not_flagged_rate_limit(monkeypatch):
    # A SUCCESS result whose answer text mentions "quota"/"rate limit" must NOT
    # be misread as a rate_limit cooldown (the scan excludes success prose).
    ndjson = json.dumps({
        "type": "result", "subtype": "success",
        "result": "Here is how to reduce quota usage and avoid rate limit errors.",
        "usage": {},
    })
    monkeypatch.setattr(
        "providers.claude.run_with_watchdog",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=ndjson, stderr=""),
    )
    res = ClaudeProvider().run("t")
    assert res.success is True
    assert res.error != "rate_limit"


# ---------------------------------------------------------------------------
# (j) Byte-granular liveness: a newline-less progress stream is NOT idle-killed
# ---------------------------------------------------------------------------

def test_byte_stream_without_newlines_not_idle_killed():
    # Emits chars WITHOUT a newline, faster than idle_timeout. A line-iterating
    # reader would see no newline → register no activity → false idle-kill; the
    # chunked read1() reader registers each flush as activity.
    code = (
        "import time,sys\n"
        "for i in range(8):\n"
        "    sys.stdout.write('.'); sys.stdout.flush(); time.sleep(0.3)\n"
        "sys.stdout.write('\\n')\n"
    )
    result = run_with_watchdog(
        _py(code), input_text=None, cwd=None,
        idle_timeout=1.0, hard_timeout=30, shell=False,
    )
    assert result.returncode == 0
    assert result.stdout.count(".") == 8


# ---------------------------------------------------------------------------
# (k) Anonymous (id-less) tool_use must not pause the idle timer forever
# ---------------------------------------------------------------------------

def test_anonymous_tool_use_idle_resumes_after_result():
    from providers.process_runner import _Liveness
    lv = _Liveness(0.0)
    # id-less tool_use → anonymous pause (can't be correlated by id)
    lv.on_event(
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        now=1.0,
    )
    assert lv.idle_for(5.0) == 0.0  # paused while the anonymous tool is open
    # an (unmatched) tool_result pairs off the anonymous tool → idle resumes
    lv.on_event(
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x"}]}},
        now=2.0,
    )
    assert lv.idle_for(5.0) > 0.0


# ---------------------------------------------------------------------------
# (l) stdin prompt delivery — a lost prompt TAIL must not look like success
#
# Regression for 2026-07-20: _feed_stdin swallowed the failing tail flush, the
# CLI answered the context-only remainder ("what would you like me to do?"),
# exited 0 with subtype=="success", and the queue task was finalized as done.
# Cost: 3 days of vault health data.
# ---------------------------------------------------------------------------

def test_stdin_full_delivery_reports_no_error():
    """Happy path: child consumes everything → stdin_error stays None."""
    code = "import sys; data = sys.stdin.read(); sys.stdout.write(str(len(data)))"
    prompt = "x" * 100_000
    result = run_with_watchdog(
        _py(code), input_text=prompt, cwd=None,
        idle_timeout=10.0, hard_timeout=30, shell=False,
    )
    assert result.returncode == 0
    assert result.stdin_error is None
    assert result.stdout.strip() == str(len(prompt))


def test_stdin_partial_delivery_is_reported():
    """Child reads one line then exits → tail is lost → must be reported.

    This is the exact shape of the incident: the write/flush fails midway, so
    the child never sees the prompt tail (which carries the task text).
    """
    code = "import sys; sys.stdin.readline(); sys.exit(0)"
    prompt = "context line\n" + ("filler " * 700_000) + "\nTHE ACTUAL TASK"
    result = run_with_watchdog(
        _py(code), input_text=prompt, cwd=None,
        idle_timeout=10.0, hard_timeout=30, shell=False,
    )
    # The child itself is perfectly healthy — that is the whole trap.
    assert result.returncode == 0
    assert result.stdin_error is not None
    assert str(len(prompt)) in result.stdin_error


def test_stdin_none_reports_no_error():
    """input_text=None (no prompt at all) is not a delivery failure."""
    result = run_with_watchdog(
        _py("print('ok')"), input_text=None, cwd=None,
        idle_timeout=5.0, hard_timeout=20, shell=False,
    )
    assert result.returncode == 0
    assert result.stdin_error is None


@pytest.mark.parametrize("provider_cls,module", [
    (ClaudeProvider, "providers.claude.run_with_watchdog"),
    (CodexProvider, "providers.codex.run_with_watchdog"),
    (GeminiProvider, "providers.gemini.run_with_watchdog"),
])
def test_incomplete_stdin_overrides_a_clean_looking_run(provider_cls, module, monkeypatch):
    """rc==0 + valid success payload must still FAIL when the prompt was cut."""
    json_out = json.dumps({
        "type": "result", "subtype": "success",
        "result": "Was möchtest du heute tun?",
        "usage": {"input_tokens": 10, "output_tokens": 1079},
    })
    monkeypatch.setattr(module, lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json_out, stderr="",
        stdin_error="flush failed for 51234 chars: OSError: [Errno 22] Invalid argument",
    ))
    result = provider_cls().run("test task")
    assert result.success is False
    assert result.error == "stdin_incomplete"  # bare code, exact match (R7)


def test_claude_reports_tokens_even_when_stdin_incomplete(monkeypatch):
    """The truncated run still burned tokens — quota accounting must see them."""
    json_out = json.dumps({
        "type": "result", "subtype": "success", "result": "Was soll ich tun?",
        "usage": {"input_tokens": 3, "output_tokens": 189,
                  "cache_creation_input_tokens": 25988,
                  "cache_read_input_tokens": 24507},
    })
    monkeypatch.setattr(
        "providers.claude.run_with_watchdog",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json_out, stderr="",
                                         stdin_error="close failed for 51234 chars: BrokenPipeError: "),
    )
    result = ClaudeProvider().run("test task")
    assert result.success is False
    assert result.output_tokens == 189
    assert result.cache_creation_input_tokens == 25988


@pytest.mark.parametrize("provider_cls,module,stdout", [
    (ClaudeProvider, "providers.claude.run_with_watchdog",
     json.dumps({"type": "result", "subtype": "success", "result": "19 Felder geschrieben",
                 "usage": {"input_tokens": 18, "output_tokens": 989}})),
    (CodexProvider, "providers.codex.run_with_watchdog", "19 Felder geschrieben"),
    (GeminiProvider, "providers.gemini.run_with_watchdog", "19 Felder geschrieben"),
])
def test_legacy_result_without_stdin_field_still_succeeds(provider_cls, module, stdout, monkeypatch):
    """A three-field SimpleNamespace (no stdin_error) must not raise/regress.

    Guards the getattr() contract AND the no-threshold rule: 989 output tokens
    is a REAL success for the health-snapshot task — any output-size heuristic
    would wrongly fail it (2026-07-16/17 vs 07-15/20, see .dev-loop plan §4).
    """
    monkeypatch.setattr(module, lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=stdout, stderr="",
    ))
    result = provider_cls().run("test task")
    assert result.success is True


@pytest.fixture(scope="module")
def broken_pipe_rate_limit_result():
    """A REAL run where both signals are present: rc!=0 with a rate-limit
    message AND a genuinely broken stdin pipe (prompt > OS pipe buffer)."""
    # "rate limit" is the one phrase all three providers classify (claude also
    # knows "usage limit", codex "429"/"too many", gemini "resource exhausted").
    code = (
        "import sys; "
        "sys.stderr.write('Error: rate limit exceeded. Resets at 3pm.'); "
        "sys.exit(1)"
    )
    result = run_with_watchdog(
        _py(code), input_text="x" * 160_000, cwd=None,
        idle_timeout=10.0, hard_timeout=30, shell=False,
    )
    assert result.returncode == 1
    assert result.stdin_error is not None, "test premise: the pipe must break"
    return result


@pytest.mark.parametrize("provider_cls,module", [
    (ClaudeProvider, "providers.claude.run_with_watchdog"),
    (CodexProvider, "providers.codex.run_with_watchdog"),
    (GeminiProvider, "providers.gemini.run_with_watchdog"),
])
def test_stdin_error_does_not_mask_rate_limit(
    provider_cls, module, broken_pipe_rate_limit_result, monkeypatch,
):
    """A better classification must win over stdin_incomplete.

    Regression for the review finding: a child that dies early (rate limit,
    missing session) ALSO breaks the stdin pipe, so the broken pipe is a
    symptom. An up-front stdin check masked rate_limit — which skips the quota
    cooldown — and session_missing — which skips session recovery in every
    dev-loop/review-loop phase.
    """
    r = broken_pipe_rate_limit_result
    monkeypatch.setattr(module, lambda *a, **kw: SimpleNamespace(
        returncode=r.returncode, stdout=r.stdout,
        stderr=r.stderr, stdin_error=r.stdin_error,
    ))
    res = provider_cls().run("test task")
    assert res.success is False
    assert res.error == "rate_limit", f"{res.error!r} masked the rate limit"


def test_stdin_incomplete_wins_at_rc0_when_nothing_better_matches(monkeypatch):
    """rc == 0 with no other signal → the truncated prompt IS the cause."""
    monkeypatch.setattr(
        "providers.codex.run_with_watchdog",
        lambda *a, **kw: SimpleNamespace(
            returncode=0, stdout="", stderr="",
            stdin_error="write() failed; prompt was 160000 chars",
        ),
    )
    res = CodexProvider().run("test task")
    assert res.error == "stdin_incomplete"


@pytest.mark.parametrize("module,provider_cls", [
    ("providers.codex.run_with_watchdog", CodexProvider),
    ("providers.gemini.run_with_watchdog", GeminiProvider),
    ("providers.claude.run_with_watchdog", ClaudeProvider),
])
def test_nonzero_rc_keeps_real_error_even_without_keyword_match(
    module, provider_cls, monkeypatch,
):
    """A CLI crash with no matching keyword must NOT become stdin_incomplete.

    Any child dying early breaks the feeder too (the prompt dwarfs the OS pipe
    buffer), so at rc != 0 the broken pipe is a symptom. Booking these as
    stdin_incomplete would bury causes like "not logged in" / "model not found"
    and route ordinary crashes into the stdin retry path.
    """
    monkeypatch.setattr(module, lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr="Error: not logged in",
        stdin_error="write() failed; prompt was 160000 chars",
    ))
    res = provider_cls().run("test task")
    assert res.success is False
    assert res.error != "stdin_incomplete"
    assert "not logged in" in res.error
    # ...but the delivery diagnosis must not be lost either.
    assert "stdin" in (res.output or "").lower()


def test_stdin_incomplete_is_a_bare_error_code(monkeypatch):
    """Error codes are matched by exact equality across the orchestrator.

    A suffixed string ("stdin_incomplete: flush() failed; 51234 chars…") would
    miss every classification branch AND make each incident a unique
    error_code in analytics. Detail belongs in output, not in the code.
    """
    json_out = json.dumps({
        "type": "result", "subtype": "success", "result": "Was soll ich tun?",
        "usage": {"input_tokens": 3, "output_tokens": 189},
    })
    monkeypatch.setattr(
        "providers.claude.run_with_watchdog",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json_out, stderr="",
                                         stdin_error="flush() failed; prompt was 51234 chars"),
    )
    result = ClaudeProvider().run("test task")
    assert result.error == "stdin_incomplete"          # exact, not prefixed
    assert "51234" in (result.output or "")            # detail preserved


def test_stdin_incomplete_is_registered_in_taxonomy():
    """An unregistered error code silently books as 'unknown' in the dashboard."""
    import taxonomy
    assert taxonomy._ERROR_CODE_MAP.get("stdin_incomplete") == taxonomy.CAT_STDIN
    assert taxonomy.CAT_STDIN in taxonomy.ALL_CATEGORIES


def test_stdin_delivery_defaults_to_not_delivered():
    """Fail-closed contract: only an explicit success may set delivered=True.

    Guards against re-introducing the fail-open variant, where an uncaught
    exception class (MemoryError on a multi-MB prompt, RuntimeError at
    interpreter shutdown) left 'finished=True, error=None' → silent success.
    """
    from providers.process_runner import _StdinDelivery
    assert _StdinDelivery().delivered is False
    assert _StdinDelivery().error is None


def test_timeout_carries_stdin_diagnosis():
    """A child waiting for a prompt that never arrived idles out as 'hang'.

    Without the attached diagnosis that surfaces as a bare hang and burns
    MAX_HANG_RETRIES before blocking, with no hint at the real cause.
    """
    code = "import sys; sys.stdin.readline(); import time; time.sleep(30)"
    prompt = "line one\n" + "y" * 200_000
    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        run_with_watchdog(
            _py(code), input_text=prompt, cwd=None,
            idle_timeout=1.0, hard_timeout=20, shell=False,
        )
    # hasattr() alone would be vacuous — the attribute is assigned on every
    # timeout, including with value None. Assert the DIAGNOSIS is present.
    assert exc_info.value.stdin_error is not None
    assert str(len(prompt)) in exc_info.value.stdin_error


# ---------------------------------------------------------------------------
# (stdin_via_file) The fix: deliver the prompt via a temp file so the TAIL of a
# large prompt survives — the silent-failure mode the feeder-pipe path allows.
# ---------------------------------------------------------------------------

def test_stdin_via_file_delivers_full_prompt_including_tail():
    """A >64KB prompt reaches the child COMPLETE — the task text at the very end
    (the part lost intermittently by the piped feeder) must arrive."""
    code = (
        "import sys\n"
        "data = sys.stdin.buffer.read().decode('utf-8')\n"
        "last = data.rstrip('\\n').splitlines()[-1] if data else ''\n"
        "sys.stdout.write('LEN:%d LAST:%s\\n' % (len(data), last))\n"
    )
    tail = "TAILMARKER_XYZ"
    prompt = ("Fuelltext-Zeile mit Umlauten äöü\n" * 8000) + tail  # ~250 KB, >> pipe buffer
    result = run_with_watchdog(
        _py(code), input_text=prompt, cwd=None,
        idle_timeout=10.0, hard_timeout=30, shell=False, stdin_via_file=True,
    )
    assert result.returncode == 0
    assert f"LEN:{len(prompt)}" in result.stdout   # every char delivered, no truncation
    assert f"LAST:{tail}" in result.stdout          # the tail specifically survived
    assert result.stdin_error is None               # file delivery is complete by construction


def test_stdin_via_file_cleans_up_temp_file(monkeypatch):
    """The temp prompt file must never be left behind."""
    import providers.process_runner as pr
    created: list[str] = []
    real = pr._write_temp_prompt

    def spy(text, enc):
        path = real(text, enc)
        created.append(path)
        return path

    monkeypatch.setattr(pr, "_write_temp_prompt", spy)
    run_with_watchdog(
        _py("import sys; sys.stdin.buffer.read()"), input_text="hello", cwd=None,
        idle_timeout=5.0, hard_timeout=20, shell=False, stdin_via_file=True,
    )
    assert created, "the file path was supposed to go through _write_temp_prompt"
    assert all(not os.path.exists(p) for p in created), "temp prompt file leaked"


def test_stdin_via_file_cleans_up_after_timeout_kill(monkeypatch):
    """Acceptance: the temp file is removed even when the child is killed."""
    import providers.process_runner as pr
    created: list[str] = []
    real = pr._write_temp_prompt

    def spy(text, enc):
        path = real(text, enc)
        created.append(path)
        return path

    monkeypatch.setattr(pr, "_write_temp_prompt", spy)
    # Child never reads stdin and never exits → idle-killed while the temp file
    # is still its stdin. The finally must still close + unlink it.
    with pytest.raises(subprocess.TimeoutExpired):
        run_with_watchdog(
            _py("import time; time.sleep(30)"), input_text="x" * 100_000, cwd=None,
            idle_timeout=1.0, hard_timeout=20, shell=False, stdin_via_file=True,
        )
    assert created, "the file path was supposed to go through _write_temp_prompt"
    assert all(not os.path.exists(p) for p in created), "temp file leaked after kill"


# ---------------------------------------------------------------------------
# (stdin_via_file) Regression 2026-07-25: the file path used to assert
# `delivery.delivered = True` unconditionally — "a real file has a deterministic
# EOF, therefore delivery is complete". A deterministic EOF only guarantees the
# child SEES an end, not that the prompt was written or that input_text held
# anything. That assertion pinned stdin_error to None for every stdin_via_file
# caller and turned the detection in claude.py into dead code: a safety net that
# could not fire. These tests keep the net alive.
# ---------------------------------------------------------------------------

def test_verify_prompt_file_accepts_intact_file(tmp_path):
    import providers.process_runner as pr
    text = "Aufgabe mit Umlauten äöü\n"
    path = tmp_path / "p.txt"
    path.write_text(text, encoding="utf-8", newline="")
    assert pr._verify_prompt_file(str(path), text, "utf-8") is None


def test_verify_prompt_file_detects_truncation(tmp_path):
    import providers.process_runner as pr
    text = "x" * 5000
    path = tmp_path / "p.txt"
    path.write_text(text[:100], encoding="utf-8", newline="")
    err = pr._verify_prompt_file(str(path), text, "utf-8")
    assert err is not None
    assert "100" in err and "5000" in err


def test_verify_prompt_file_rejects_empty_prompt(tmp_path):
    """An empty instruction is never a legitimate payload — fail loudly."""
    import providers.process_runner as pr
    path = tmp_path / "p.txt"
    path.write_text("   \n", encoding="utf-8", newline="")
    assert "empty" in pr._verify_prompt_file(str(path), "   \n", "utf-8")


def test_verify_prompt_file_fails_closed_on_missing_file(tmp_path):
    import providers.process_runner as pr
    err = pr._verify_prompt_file(str(tmp_path / "gone.txt"), "content", "utf-8")
    assert err is not None
    assert "unreadable" in err


def test_stdin_via_file_reports_incomplete_prompt_file(monkeypatch):
    """The net fires: a short temp file must surface as stdin_error, not as success."""
    import providers.process_runner as pr
    real = pr._write_temp_prompt

    def truncating(text, enc):
        # Write only a prefix — simulates a partial/failed write to the temp file.
        return real(text[: len(text) // 2], enc)

    monkeypatch.setattr(pr, "_write_temp_prompt", truncating)
    result = run_with_watchdog(
        _py("import sys; sys.stdin.buffer.read()"), input_text="y" * 4000, cwd=None,
        idle_timeout=5.0, hard_timeout=20, shell=False, stdin_via_file=True,
    )
    assert result.stdin_error is not None, "truncated prompt file was booked as delivered"
    assert "incomplete" in result.stdin_error


def _boom_spawn(*_args, **_kwargs):
    raise FileNotFoundError("simulated missing executable")


def test_stdin_via_file_cleans_up_on_spawn_failure(monkeypatch):
    """A spawn failure (e.g. a shim where shell=False can't launch) must not leak
    the temp prompt file — the early except owns cleanup before the try/finally."""
    import providers.process_runner as pr
    created: list[str] = []
    real = pr._write_temp_prompt

    def spy(text, enc):
        path = real(text, enc)
        created.append(path)
        return path

    monkeypatch.setattr(pr, "_write_temp_prompt", spy)
    monkeypatch.setattr(pr, "_spawn", _boom_spawn)
    with pytest.raises(FileNotFoundError):
        run_with_watchdog(
            _py("print('x')"), input_text="hello world", cwd=None,
            idle_timeout=5.0, hard_timeout=20, shell=False, stdin_via_file=True,
        )
    assert created, "the temp file must have been written before the spawn attempt"
    assert all(not os.path.exists(p) for p in created), "temp file leaked on spawn failure"


def test_stdin_via_file_cleans_up_on_setup_failure(monkeypatch):
    """A failure at the EARLIEST post-spawn setup step (liveness init, now inside
    the try) must still clean up the child process AND the temp prompt file via
    the finally — nothing between the spawn and the try may leak."""
    import providers.process_runner as pr
    created: list[str] = []
    real = pr._write_temp_prompt

    def spy(text, enc):
        path = real(text, enc)
        created.append(path)
        return path

    def boom_liveness(*_args, **_kwargs):
        raise RuntimeError("simulated post-spawn setup failure")

    monkeypatch.setattr(pr, "_write_temp_prompt", spy)
    monkeypatch.setattr(pr, "_Liveness", boom_liveness)
    with pytest.raises(RuntimeError):
        run_with_watchdog(
            _py("import time; time.sleep(30)"), input_text="x" * 5000, cwd=None,
            idle_timeout=5.0, hard_timeout=20, shell=False, stdin_via_file=True,
        )
    assert created, "the temp file must have been written before setup"
    assert all(not os.path.exists(p) for p in created), "temp file leaked on setup failure"


def test_sweep_removes_stale_prompt_files_but_keeps_fresh(tmp_path):
    """The self-healing sweep clears leftovers older than the threshold and — for
    concurrent-run safety — leaves recent files alone."""
    import providers.process_runner as pr
    directory = tmp_path / "orch_prompts"
    directory.mkdir()
    stale = directory / "orch_prompt_old.txt"
    fresh = directory / "orch_prompt_new.txt"
    unrelated = directory / "keepme.txt"
    for f in (stale, fresh, unrelated):
        f.write_text("x")
    old = time.time() - (pr._PROMPT_STALE_SEC + 60)
    os.utime(stale, (old, old))
    pr._sweep_stale_prompts(str(directory))
    assert not stale.exists(), "stale prompt file should be swept"
    assert fresh.exists(), "fresh prompt file must be kept (concurrent-run safety)"
    assert unrelated.exists(), "non-prompt files must never be touched"


def test_stdin_via_file_with_none_input_writes_no_file(monkeypatch):
    """No prompt → no temp file, behaves like the plain None-input path."""
    import providers.process_runner as pr
    called: list[int] = []
    monkeypatch.setattr(pr, "_write_temp_prompt", lambda *a: called.append(1) or "unused")
    result = run_with_watchdog(
        _py("print('ok')"), input_text=None, cwd=None,
        idle_timeout=5.0, hard_timeout=20, shell=False, stdin_via_file=True,
    )
    assert result.returncode == 0
    assert not called  # no file written when there is nothing to deliver


def test_claude_provider_uses_file_stdin_without_shell(monkeypatch):
    """The claude provider must opt into file delivery and drop the shell."""
    captured: dict = {}
    json_out = json.dumps({
        "type": "result", "subtype": "success", "result": "erledigt",
        "usage": {"input_tokens": 5, "output_tokens": 7},
    })

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured.update(kw)
        return SimpleNamespace(returncode=0, stdout=json_out, stderr="", stdin_error=None)

    monkeypatch.setattr("providers.claude.run_with_watchdog", fake_run)
    result = ClaudeProvider().run("do the thing")
    assert result.success is True
    assert captured["stdin_via_file"] is True
    assert captured["shell"] is False
    assert captured["input_text"] == "do the thing"
