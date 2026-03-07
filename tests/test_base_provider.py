"""
Tests for providers/base.py and providers/claude.py.

Covers:
  - Basic cooldown lifecycle (set, check, expire)
  - cooldown_remaining_str() format
  - Per-instance isolation (no shared class-level state)
  - Concurrent reads and writes don't raise or corrupt state
  - Claude JSON output parsing with token extraction
"""

import threading
import time

import pytest

from providers.base import BaseProvider, RunResult
from providers.claude import ClaudeProvider


# ---------------------------------------------------------------------------
# Minimal concrete provider (BaseProvider is abstract)
# ---------------------------------------------------------------------------

class DummyProvider(BaseProvider):
    name = "dummy"

    def run(self, task: str, cwd=None, timeout=300) -> RunResult:
        return RunResult(success=True, output="ok")


# ---------------------------------------------------------------------------
# Basic cooldown behaviour
# ---------------------------------------------------------------------------

def test_initially_not_cooling_down():
    p = DummyProvider()
    assert p.is_cooling_down() is False


def test_set_cooldown_marks_as_cooling():
    p = DummyProvider()
    p.set_cooldown(seconds=120)
    assert p.is_cooling_down() is True


def test_cooldown_with_past_expiry_is_not_active():
    """Directly back-dating _cooldown_until simulates an expired cooldown."""
    p = DummyProvider()
    p._cooldown_until = time.time() - 1
    assert p.is_cooling_down() is False


def test_set_cooldown_zero_is_not_cooling():
    """set_cooldown(0) sets expiry to now — should not report as cooling."""
    p = DummyProvider()
    p.set_cooldown(seconds=0)
    # time.time() may equal _cooldown_until exactly; the check is `< not <=`,
    # so a cooldown of 0 might fire True for a nanosecond — just check it
    # expires quickly rather than asserting a specific value.
    time.sleep(0.01)
    assert p.is_cooling_down() is False


# ---------------------------------------------------------------------------
# cooldown_remaining_str
# ---------------------------------------------------------------------------

def test_remaining_str_zero_when_not_cooling():
    p = DummyProvider()
    assert p.cooldown_remaining_str() == "0m 0s"


def test_remaining_str_format_has_minutes_and_seconds():
    p = DummyProvider()
    p.set_cooldown(seconds=90)
    result = p.cooldown_remaining_str()
    assert "m" in result
    assert "s" in result


def test_remaining_str_value_close_to_set_duration():
    p = DummyProvider()
    p.set_cooldown(seconds=3600)
    # Should be roughly 60m — at least 59 minutes remaining immediately after set
    result = p.cooldown_remaining_str()
    minutes = int(result.split("m")[0])
    assert minutes >= 59


# ---------------------------------------------------------------------------
# Per-instance isolation
# ---------------------------------------------------------------------------

def test_two_instances_have_independent_cooldowns():
    p1 = DummyProvider()
    p2 = DummyProvider()
    p1.set_cooldown(seconds=3600)
    assert p1.is_cooling_down() is True
    assert p2.is_cooling_down() is False


def test_two_instances_have_different_locks():
    p1 = DummyProvider()
    p2 = DummyProvider()
    assert p1._lock is not p2._lock


def test_cooldown_on_one_instance_does_not_affect_run_result():
    p = DummyProvider()
    p.set_cooldown(seconds=3600)
    result = p.run("task")
    assert result.success is True


# ---------------------------------------------------------------------------
# Thread safety — stress tests
# ---------------------------------------------------------------------------

def test_concurrent_set_and_check_raises_no_errors():
    """
    10 threads each performing 100 interleaved set_cooldown + is_cooling_down
    + cooldown_remaining_str calls must never raise.
    """
    p = DummyProvider()
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            for _ in range(100):
                p.set_cooldown(seconds=(i % 60) + 1)
                _ = p.is_cooling_down()
                _ = p.cooldown_remaining_str()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Thread errors: {errors}"


def test_cooldown_set_by_one_thread_visible_to_another():
    """
    A thread that calls set_cooldown must have its result visible to other
    threads querying is_cooling_down immediately afterwards.
    """
    p = DummyProvider()
    results: list[bool] = []

    def setter() -> None:
        p.set_cooldown(seconds=3600)

    def checker() -> None:
        time.sleep(0.005)          # ensure setter ran first
        results.append(p.is_cooling_down())

    setter_thread = threading.Thread(target=setter)
    checker_threads = [threading.Thread(target=checker) for _ in range(5)]

    setter_thread.start()
    for t in checker_threads:
        t.start()

    setter_thread.join()
    for t in checker_threads:
        t.join()

    assert all(results), f"Some checkers missed the cooldown: {results}"


def test_multiple_providers_concurrent_no_cross_contamination():
    """
    Three providers running concurrently must not share cooldown state.
    Provider 0 gets a long cooldown; providers 1 & 2 must remain available.
    """
    providers = [DummyProvider() for _ in range(3)]
    errors: list[str] = []

    def set_only_first() -> None:
        providers[0].set_cooldown(seconds=3600)

    def check_others() -> None:
        time.sleep(0.01)
        for idx in (1, 2):
            if providers[idx].is_cooling_down():
                errors.append(f"Provider {idx} incorrectly shows cooling")

    t1 = threading.Thread(target=set_only_first)
    t2 = threading.Thread(target=check_others)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], "\n".join(errors)


# ---------------------------------------------------------------------------
# Claude JSON output parsing
# ---------------------------------------------------------------------------

def test_parse_json_response_extracts_tokens():
    """Valid JSON with usage data → output text + token counts."""
    import json
    data = {
        "type": "result",
        "subtype": "success",
        "result": "Hello world",
        "usage": {"input_tokens": 5000, "output_tokens": 1200},
    }
    output, inp, out = ClaudeProvider._parse_json_response(json.dumps(data))
    assert output == "Hello world"
    assert inp == 5000
    assert out == 1200


def test_parse_json_response_missing_usage():
    """JSON without usage field → tokens default to 0."""
    import json
    data = {"type": "result", "result": "ok"}
    output, inp, out = ClaudeProvider._parse_json_response(json.dumps(data))
    assert output == "ok"
    assert inp == 0
    assert out == 0


def test_parse_json_response_plain_text_fallback():
    """Non-JSON stdout → returned as-is with 0 tokens."""
    output, inp, out = ClaudeProvider._parse_json_response("plain text output")
    assert output == "plain text output"
    assert inp == 0
    assert out == 0


def test_parse_json_response_empty_string():
    output, inp, out = ClaudeProvider._parse_json_response("")
    assert output == ""
    assert inp == 0
    assert out == 0


def test_parse_json_response_malformed_json():
    """Broken JSON → falls back to raw string."""
    output, inp, out = ClaudeProvider._parse_json_response('{"broken')
    assert output == '{"broken'
    assert inp == 0
    assert out == 0


def test_claude_run_returns_tokens_from_json(monkeypatch):
    """Full integration: ClaudeProvider.run() extracts tokens from JSON output."""
    import json
    from types import SimpleNamespace

    json_out = json.dumps({
        "type": "result",
        "result": "Task done successfully",
        "usage": {"input_tokens": 8000, "output_tokens": 3000},
    })

    monkeypatch.setattr(
        "providers.claude.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json_out, stderr=""),
    )

    p = ClaudeProvider()
    result = p.run("test task")
    assert result.success is True
    assert result.output == "Task done successfully"
    assert result.input_tokens == 8000
    assert result.output_tokens == 3000


def test_claude_run_rejects_non_success_json_without_result(monkeypatch):
    """Structured JSON abort/error payloads must not be treated as success."""
    import json
    from types import SimpleNamespace

    json_out = json.dumps({
        "type": "result",
        "subtype": "error_max_turns",
        "usage": {"input_tokens": 120, "output_tokens": 45},
    })

    monkeypatch.setattr(
        "providers.claude.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json_out, stderr=""),
    )

    result = ClaudeProvider().run("test task")
    assert result.success is False
    assert '"subtype": "error_max_turns"' in result.error
    assert result.input_tokens == 120
    assert result.output_tokens == 45


def test_claude_run_rejects_non_success_json_even_with_result(monkeypatch):
    """A non-success subtype must override a textual result field."""
    import json
    from types import SimpleNamespace

    json_out = json.dumps({
        "type": "result",
        "subtype": "error_during_execution",
        "result": "Task aborted",
        "usage": {"input_tokens": 300, "output_tokens": 50},
    })

    monkeypatch.setattr(
        "providers.claude.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json_out, stderr=""),
    )

    result = ClaudeProvider().run("test task")
    assert result.success is False
    assert result.error == "Task aborted"
    assert result.input_tokens == 300
    assert result.output_tokens == 50
