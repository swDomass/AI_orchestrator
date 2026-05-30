"""Tests for providers/process_runner.py — the liveness/hang watchdog.

All fakes use ``sys.executable -c "..."`` with TINY timeouts; no real CLIs,
no quota. Platform-neutral cases use shell=False; the tree-kill case (d) is
platform-aware with shell=True on Windows.
"""

import json
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
