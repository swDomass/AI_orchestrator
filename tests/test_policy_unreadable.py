"""A policy.yaml that exists but cannot be read holds every task (2026-10-08).

Until then `check_task` classified with whatever rules the engine still had: none
when the file was already broken at process start (every task AUTO, `git push`
included), the last good ones when it broke while running. Only `get_tool_phase`
looked at `_load_error`. Now `check_task` raises PolicyUnreadableError on every call
while the error stands, and run_once() holds the task (see
tests/test_orchestrator_policy_fail_closed.py for that half).

A MISSING file deliberately stays "nothing configured" (AUTO) — the queue lives in
the same vault folder, and queue_linter/doctor already report it.

Everything here is a real PolicyEngine on a real policy.yaml in tmp_path; nothing in
the engine is mocked.
"""

import logging
import os

import pytest

import policy as policy_module
from policy import TIER_APPROVE, TIER_AUTO, PolicyEngine, PolicyUnreadableError

_TASK = "Deploy release and git push origin main"
_GOOD = 'approve:\n  - pattern: "git push"\n    message: "git push to remote"\n'
_UNPARSEABLE = 'approve:\n  - pattern: "git push\n    message: [oops\n'
_UNPARSEABLE_OTHER = "approve: [unclosed\n"
_TOP_LEVEL_LIST = "- git push\n- rm -rf\n"
# _parse_tool_contract raises TypeError on `stop_conditions: 1` (iterating an int) —
# a section parser failing AFTER the rules section has been applied.
_BROKEN_SECTION = _GOOD + "tool_contracts: {dev-loop: {stop_conditions: 1}}\n"


def _engine(tmp_path, text: str | None) -> PolicyEngine:
    ai = tmp_path / "vault" / "99_System" / "AI"
    ai.mkdir(parents=True, exist_ok=True)
    if text is not None:
        (ai / "policy.yaml").write_text(text, encoding="utf-8")
    return PolicyEngine(vault_path=tmp_path / "vault")


def _rewrite(path, text: str) -> None:
    """Write and move the mtime on, so the engine's mtime cache sees the edit."""
    path.write_text(text, encoding="utf-8")
    later = path.stat().st_mtime + 5
    os.utime(path, (later, later))


def _rewrite_keeping_mtime(path, text: str) -> None:
    """Write and put the OLD mtime back, to the nanosecond — what an OneDrive sync
    (which sets mtimes) or a coarse timestamp resolution can produce."""
    before = path.stat()
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_mtime == before.st_mtime


def _raises_unreadable(engine: PolicyEngine, **kwargs) -> str:
    with pytest.raises(PolicyUnreadableError) as info:
        engine.check_task(_TASK, **kwargs)
    return str(info.value)


# ── the file is there and broken: every call raises ─────────────────────────

@pytest.mark.parametrize("text", [_UNPARSEABLE, _TOP_LEVEL_LIST], ids=["unparseable", "top_level_list"])
def test_a_broken_file_at_start_raises_on_every_call(tmp_path, text):
    engine = _engine(tmp_path, text)

    messages = [_raises_unreadable(engine) for _ in range(3)]

    # Every call, not only the one that reloaded (the reload stores the mtime first).
    assert len(set(messages)) == 1
    assert messages[0].startswith("policy.yaml unreadable (")
    assert str(engine.config_path) in messages[0], "the alert has to name the file"


def test_the_message_carries_the_parse_error(tmp_path):
    engine = _engine(tmp_path, _UNPARSEABLE)

    message = _raises_unreadable(engine)

    assert engine._load_error and engine._load_error in message
    assert "Error" in engine._load_error   # ScannerError/ParserError from PyYAML


def test_a_file_that_broke_while_running_does_not_fall_back_to_the_old_rules(tmp_path):
    """The night case: the engine loaded a good file, OneDrive leaves a broken one."""
    engine = _engine(tmp_path, _GOOD)
    assert engine.check_task(_TASK)[0] == TIER_APPROVE
    _rewrite(engine.config_path, _UNPARSEABLE)

    for _ in range(2):
        _raises_unreadable(engine)


def test_a_profile_cannot_classify_past_an_unreadable_file(tmp_path):
    """The check comes before the profile layering: a profile rule that would
    decide the task (here AUTO for exactly this text) must not carry it through."""
    engine = _engine(tmp_path, _UNPARSEABLE)

    _raises_unreadable(engine, profile_rules={"auto": ["git push"]})


def test_a_failing_section_parser_raises_the_same_error_on_the_first_call_too(tmp_path):
    """The reload that hits the section parser gets its raw TypeError; check_task turns
    it into the same PolicyUnreadableError every later call raises. Two different
    texts for one fault would be two alerts from run_once()'s throttle."""
    engine = _engine(tmp_path, _GOOD)
    _rewrite(engine.config_path, _BROKEN_SECTION)

    messages = [_raises_unreadable(engine) for _ in range(3)]

    assert len(set(messages)) == 1
    assert "TypeError" in messages[0]


# ── nothing configured: missing or empty file classifies as before ──────────

@pytest.mark.parametrize("text", [None, "", "null\n", "{}\n"],
                         ids=["missing", "empty", "yaml_null", "empty_mapping"])
def test_missing_or_empty_file_is_nothing_configured(tmp_path, text):
    engine = _engine(tmp_path, text)

    assert engine.check_task(_TASK) == (TIER_AUTO, [])
    assert engine.check_task(_TASK) == (TIER_AUTO, [])


# ── repair: the engine reads the file again ─────────────────────────────────

def test_a_repaired_file_classifies_again(tmp_path):
    engine = _engine(tmp_path, _UNPARSEABLE)
    _raises_unreadable(engine)

    _rewrite(engine.config_path, _GOOD)

    assert engine.check_task(_TASK) == (TIER_APPROVE, ["git push to remote"])
    assert engine._load_error is None


@pytest.mark.parametrize("broken", [_UNPARSEABLE, _TOP_LEVEL_LIST, _BROKEN_SECTION],
                         ids=["unparseable", "top_level_list", "section_parser"])
def test_a_file_repaired_with_the_same_mtime_is_read_again(tmp_path, broken):
    """Pflicht-Probe: the reload stores the mtime before parsing, so with the plain
    mtime shortcut a repair that keeps the mtime was never read — the orchestrator
    would hold every task until it is restarted."""
    engine = _engine(tmp_path, _GOOD)
    _rewrite(engine.config_path, broken)
    with pytest.raises((PolicyUnreadableError, TypeError)):
        engine.check_task(_TASK)

    _rewrite_keeping_mtime(engine.config_path, _GOOD)

    assert engine.check_task(_TASK) == (TIER_APPROVE, ["git push to remote"])


def test_an_unchanged_broken_file_warns_once_not_per_call(tmp_path, caplog):
    """The retry re-parses on every call while the error stands; it must not log a
    warning on every policy lookup."""
    with caplog.at_level(logging.DEBUG, logger="policy"):
        engine = _engine(tmp_path, _UNPARSEABLE)
        for _ in range(5):
            with pytest.raises(PolicyUnreadableError):
                engine.check_task(_TASK)

    warnings = [r for r in caplog.records if r.name == "policy" and r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


def test_a_different_breakage_warns_again(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="policy"):
        engine = _engine(tmp_path, _UNPARSEABLE)
        _raises_unreadable(engine)
        _rewrite(engine.config_path, _UNPARSEABLE_OTHER)
        _raises_unreadable(engine)

    assert len([r for r in caplog.records if r.name == "policy"]) == 2


def test_the_singleton_raises_too(monkeypatch, tmp_path):
    """run_once() asks get_engine() — the installed singleton is the same engine."""
    monkeypatch.setattr(policy_module, "_engine", _engine(tmp_path, _UNPARSEABLE))

    with pytest.raises(PolicyUnreadableError):
        policy_module.get_engine().check_task(_TASK)


def test_unreadable_is_still_a_value_error():
    """get_tool_phase() raised ValueError before; callers catching that keep working."""
    assert issubclass(PolicyUnreadableError, ValueError)
