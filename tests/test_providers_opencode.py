"""Tests for the opencode CLI provider (Stufe 2: tag-activated, not yet in the
dispatcher's fallback chain — see providers/opencode.py's module docstring).

Every switching branch gets its own gate: argv construction (prompt before -f,
`#effort` on the model only with a forced effort, agent choice by read_only), the 50 KB
prompt cap in both modes, model resolution order (tag > picker > default,
including a non-zero picker exit and garbage picker output), each error-
classification keyword branch (policy_block explicitly NOT transient), ANSI
stripping, is_available() against a missing/malformed opencode.json (never
raising), and temp-file cleanup.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from providers import opencode as opencode_mod
from providers.base import TRANSIENT_ERRORS, is_transient
from providers.opencode import (
    OpencodeProvider,
    _looks_auth_failed,
    _looks_rate_limited,
)

_FAKE_EXE = "C:/fake/opencode.exe"
_REAL_SOURCES = opencode_mod._external_instruction_sources  # before the autouse stub


def _fake_watchdog(calls, *, returncode=0, stdout="OK", stderr=""):
    def _run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return _run


@pytest.fixture(autouse=True)
def _fake_exe(monkeypatch):
    """Every test runs against a stable fake exe path — the real machine's
    opencode install (or lack of it) must not decide test outcomes."""
    monkeypatch.setattr(opencode_mod, "_OPENCODE_EXE", _FAKE_EXE)


@pytest.fixture(autouse=True)
def _no_picker_by_default(monkeypatch):
    """OPENCODE_MODEL_PICKER defaults to "" in production; pin it per-test so
    a developer's own .env override can't leak into these tests."""
    monkeypatch.setattr(config, "OPENCODE_MODEL_PICKER", "")


@pytest.fixture(autouse=True)
def _no_external_instructions_by_default(monkeypatch):
    """The real machine's global opencode sources must not decide test outcomes."""
    monkeypatch.setattr(opencode_mod, "_external_instruction_sources", lambda: [])


# ── command construction ──────────────────────────────────────────────────────

def test_prompt_message_sits_before_f_flag(monkeypatch):
    """-f is a yargs array flag: the short instruction message must never
    appear after it or it gets swallowed as a second filename."""
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    OpencodeProvider().run("review this", cwd="D:/repo")

    cmd, _ = calls[0]
    f_index = cmd.index("-f")
    assert cmd[2] == opencode_mod._SHORT_MESSAGE
    assert cmd.index(opencode_mod._SHORT_MESSAGE) < f_index
    # nothing prompt-shaped trails -f except its own value
    assert cmd[f_index + 1] not in ("--model", "--agent", "--standalone")
    assert len(cmd) == f_index + 2


def test_full_argv_order_matches_spec(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setattr(config, "OPENCODE_DEFAULT_MODEL", "openrouter/zdr-review")

    OpencodeProvider().run("review this", cwd="D:/repo", read_only=True)

    cmd, _ = calls[0]
    assert cmd[0] == _FAKE_EXE
    assert cmd[1] == "run"
    assert cmd[2] == opencode_mod._SHORT_MESSAGE
    assert cmd[3:5] == ["--model", "openrouter/zdr-review"]
    assert cmd[5:7] == ["--agent", "extern-review"]
    assert cmd[7] == "--standalone"
    assert cmd[8] == "-f"
    assert len(cmd) == 10
    assert "--dir" not in cmd and "--variant" not in cmd


def test_no_variant_suffix_without_forced_effort(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setattr(config, "OPENCODE_DEFAULT_MODEL", "openrouter/m")

    OpencodeProvider().run("review this")

    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == "openrouter/m"
    assert "--variant" not in cmd


def test_effort_rides_on_model_id_as_hash_suffix(monkeypatch):
    """2.2 has no --variant; the forced effort becomes `<model>#<effort>`."""
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setattr(config, "OPENCODE_DEFAULT_MODEL", "openrouter/m")

    provider = OpencodeProvider()
    provider._forced_effort = "high"
    try:
        provider.run("review this")
    finally:
        provider._forced_effort = None

    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == "openrouter/m#high"
    assert "--variant" not in cmd
    assert cmd[-2] == "-f"


def test_standalone_flag_precedes_f(monkeypatch):
    """Without --standalone the work runs in the shared background service,
    which a process kill does not reach."""
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    OpencodeProvider().run("review this")

    cmd = calls[0][0]
    assert cmd.index("--standalone") < cmd.index("-f")


def test_agent_choice_read_only_uses_extern_review(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    OpencodeProvider().run("review this", read_only=True)

    cmd = calls[0][0]
    assert cmd[cmd.index("--agent") + 1] == "extern-review"


def test_agent_choice_write_mode_uses_extern_dev(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    OpencodeProvider().run("review this", read_only=False)

    cmd = calls[0][0]
    assert cmd[cmd.index("--agent") + 1] == "extern-dev"


def test_no_dir_flag_but_cwd_and_pwd_set_with_cwd(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setenv("PWD", "D:/foreign")

    OpencodeProvider().run("review this", cwd="D:/repo")

    cmd, kwargs = calls[0]
    assert "--dir" not in cmd
    assert kwargs["cwd"] == os.path.abspath("D:/repo")
    assert kwargs["env"]["PWD"] == os.path.abspath("D:/repo")


def test_inherited_config_overrides_stripped_from_child_env(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    for name in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR",
                 "OPENCODE_PERMISSION", "opencode_permission"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("OPENCODE_CONFIG_PATH", "keep")

    OpencodeProvider().run("review this", cwd="D:/repo")

    env = calls[0][1]["env"]
    assert not [k for k in env if k.upper() in opencode_mod._STRIPPED_ENV]
    assert env["OPENCODE_CONFIG_PATH"] == "keep"


@pytest.mark.parametrize("cwd", ["D:/repo", None])
def test_project_config_and_claude_switches_forced_over_inherited_zero(monkeypatch, cwd):
    """opencode 2.2 reads CONFIG_PROJECT_DISABLE ?? DISABLE_PROJECT_CONFIG: an
    inherited =0 on either name (any case) must not survive."""
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setenv("OPENCODE_CONFIG_PROJECT_DISABLE", "0")
    # NB: Windows upper-cases os.environ names, so this lower-case name is not
    # distinguishable from the upper-case one there; it only matters on POSIX.
    monkeypatch.setenv("opencode_disable_project_config", "false")
    monkeypatch.setenv("OPENCODE_DISABLE_CLAUDE_CODE", "0")

    OpencodeProvider().run("review this", cwd=cwd)

    env = calls[0][1]["env"]
    for name in ("OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_CONFIG_PROJECT_DISABLE",
                 "OPENCODE_DISABLE_CLAUDE_CODE"):
        assert env[name] == "1"
    assert [k.upper() for k in env].count("OPENCODE_DISABLE_PROJECT_CONFIG") == 1


def test_global_instruction_source_blocks_start(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setattr(opencode_mod, "_external_instruction_sources",
                        lambda: ["C:/h/.config/opencode/AGENTS.md"])

    res = OpencodeProvider().run("review this", cwd="D:/repo")

    assert not res.success
    assert res.error == "external_instructions: C:/h/.config/opencode/AGENTS.md"
    assert calls == []


def test_no_global_instruction_source_starts(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    assert OpencodeProvider().run("review this", cwd="D:/repo").success
    assert len(calls) == 1


def _sandbox(monkeypatch, tmp_path):
    """Point every source location of the real function into tmp_path."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))
    folder = tmp_path / "home" / ".config" / "opencode"
    folder.mkdir(parents=True)
    return folder


def test_external_instruction_sources_finds_each_source(monkeypatch, tmp_path):
    folder = _sandbox(monkeypatch, tmp_path)
    assert _REAL_SOURCES() == []

    (folder / "opencode.json").write_text('{"instructions": []}', encoding="utf-8")
    (tmp_path / "pd" / "opencode").mkdir(parents=True)
    agents = folder / "AGENTS.md"
    agents.write_text("x", encoding="utf-8")
    found = _REAL_SOURCES()
    assert len(found) == 3 and str(agents) in found


def test_jsonc_config_counts_as_source(monkeypatch, tmp_path):
    folder = _sandbox(monkeypatch, tmp_path)
    (folder / "opencode.jsonc").write_text("{}", encoding="utf-8")
    assert _REAL_SOURCES() == [str(folder / "opencode.jsonc")]


def test_unreadable_config_fails_closed_and_blocks_start(monkeypatch, tmp_path):
    folder = _sandbox(monkeypatch, tmp_path)
    (folder / "opencode.json").write_text("{kaputt", encoding="utf-8")
    found = _REAL_SOURCES()
    assert found == [f"{folder / 'opencode.json'}#unreadable"]
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setattr(opencode_mod, "_external_instruction_sources", lambda: found)
    res = OpencodeProvider().run("review this", cwd="D:/repo")
    assert not res.success and res.error.endswith("#unreadable") and calls == []


def test_xdg_config_home_is_stripped_from_child_env(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "rel")
    assert "XDG_CONFIG_HOME" not in OpencodeProvider._build_env("D:/repo")


def test_xdg_config_home_is_not_followed(monkeypatch, tmp_path):
    folder = _sandbox(monkeypatch, tmp_path)
    xdg_folder = tmp_path / "xdg" / "opencode"
    xdg_folder.mkdir(parents=True)
    (xdg_folder / "AGENTS.md").write_text("x", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert _REAL_SOURCES() == []
    (folder / "AGENTS.md").write_text("x", encoding="utf-8")
    assert _REAL_SOURCES() == [str(folder / "AGENTS.md")]


def test_relative_programdata_is_a_finding(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("PROGRAMDATA", "pd")
    assert _REAL_SOURCES() == ["pd#relative"]


def test_empty_programdata_falls_back_to_default_not_relative(monkeypatch, tmp_path):
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("PROGRAMDATA", "")
    seen = []
    monkeypatch.setattr(Path, "is_dir", lambda self: seen.append(self) or False)
    assert _REAL_SOURCES() == []
    assert Path(r"C:\ProgramData") / "opencode" in seen
    assert all(p.is_absolute() or p.parts[0] != "opencode" for p in seen)


def test_default_config_dir_without_xdg_uses_home_dot_config(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    folder = tmp_path / "home" / ".config" / "opencode"
    folder.mkdir(parents=True)
    (folder / "AGENTS.md").write_text("x", encoding="utf-8")
    assert _REAL_SOURCES() == [str(folder / "AGENTS.md")]


def test_non_object_config_is_unreadable(monkeypatch, tmp_path):
    folder = _sandbox(monkeypatch, tmp_path)
    (folder / "opencode.json").write_text("[]", encoding="utf-8")
    assert _REAL_SOURCES() == [f"{folder / 'opencode.json'}#unreadable"]


def test_relative_cwd_becomes_absolute_for_cwd_and_pwd(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    OpencodeProvider().run("review this", cwd="some/rel")

    kwargs = calls[0][1]
    assert os.path.isabs(kwargs["cwd"]) and kwargs["cwd"] == os.path.abspath("some/rel")
    assert kwargs["env"]["PWD"] == kwargs["cwd"]


def test_pwd_removed_without_cwd_even_if_parent_exports_one(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setenv("PWD", "D:/foreign")

    OpencodeProvider().run("review this", cwd=None)

    kwargs = calls[0][1]
    assert kwargs["cwd"] is None
    assert "PWD" not in kwargs["env"]
    assert os.environ["PWD"] == "D:/foreign"  # the parent env is untouched


def _variant_provider(effort):
    provider = OpencodeProvider()
    provider._forced_effort = effort
    return provider


def test_variant_unavailable_repeats_exactly_once_without_variant(monkeypatch):
    calls = []
    outcomes = iter([
        SimpleNamespace(returncode=1, stdout="",
                        stderr="Error: Variant unavailable for openrouter/m: xhigh"),
        SimpleNamespace(returncode=0, stdout="REVIEW", stderr=""),
    ])

    def _run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return next(outcomes)

    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _run)
    monkeypatch.setattr(config, "OPENCODE_DEFAULT_MODEL", "openrouter/m")

    result = _variant_provider("xhigh").run("review this", cwd="D:/repo")

    assert len(calls) == 2
    first, second = calls[0][0], calls[1][0]
    assert first[first.index("--model") + 1] == "openrouter/m#xhigh"
    assert second[second.index("--model") + 1] == "openrouter/m"
    assert second[-2] == "-f" and second[-1] == first[-1]
    repo = os.path.abspath("D:/repo")
    assert calls[1][1]["env"]["PWD"] == repo and calls[1][1]["cwd"] == repo
    assert result.success is True and result.output == "REVIEW"


def test_variant_unavailable_second_failure_is_returned_not_retried_again(monkeypatch):
    calls = []
    monkeypatch.setattr(
        opencode_mod, "run_with_watchdog",
        _fake_watchdog(calls, returncode=1, stdout="", stderr="Error: Variant unavailable: x"),
    )

    result = _variant_provider("x").run("review this")

    assert len(calls) == 2
    assert result.success is False


def test_variant_unavailable_text_without_effort_does_not_repeat(monkeypatch):
    calls = []
    monkeypatch.setattr(
        opencode_mod, "run_with_watchdog",
        _fake_watchdog(calls, returncode=1, stdout="", stderr="Variant unavailable"),
    )

    OpencodeProvider().run("review this")

    assert len(calls) == 1


def test_variant_phrase_in_successful_answer_does_not_repeat(monkeypatch):
    calls = []
    monkeypatch.setattr(
        opencode_mod, "run_with_watchdog",
        _fake_watchdog(calls, returncode=0, stdout="the text 'Variant unavailable' is quoted"),
    )

    result = _variant_provider("high").run("review this")

    assert len(calls) == 1 and result.success is True


def test_variant_phrase_only_in_stdout_with_answer_does_not_repeat(monkeypatch):
    """Exit 1 after a delivered answer that quotes the phrase: one paid run only."""
    calls = []
    monkeypatch.setattr(
        opencode_mod, "run_with_watchdog",
        _fake_watchdog(calls, returncode=1, stdout="review: 'Variant unavailable' is handled", stderr=""),
    )

    result = _variant_provider("high").run("review this")

    assert len(calls) == 1
    assert "review:" in result.output


def test_variant_phrase_in_stderr_with_nonempty_stdout_does_not_repeat(monkeypatch):
    calls = []
    monkeypatch.setattr(
        opencode_mod, "run_with_watchdog",
        _fake_watchdog(calls, returncode=1, stdout="partial answer", stderr="Variant unavailable"),
    )

    _variant_provider("high").run("review this")

    assert len(calls) == 1


def test_other_failure_with_effort_does_not_repeat(monkeypatch):
    calls = []
    monkeypatch.setattr(
        opencode_mod, "run_with_watchdog",
        _fake_watchdog(calls, returncode=1, stdout="", stderr="boom"),
    )

    _variant_provider("high").run("review this")

    assert len(calls) == 1


def test_shell_is_never_used(monkeypatch):
    """Only a real resolved .exe is ever spawned (see module docstring) —
    shell=True would reopen the quoting-escape hole this provider exists to
    avoid."""
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    OpencodeProvider().run("review this")

    assert calls[0][1]["shell"] is False


def test_prompt_travels_via_file_not_stdin(monkeypatch):
    """input_text=None structurally removes the stdin_incomplete contract —
    the prompt is delivered entirely through the -f attachment."""
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    OpencodeProvider().run("review this")

    assert calls[0][1]["input_text"] is None


def test_idle_and_hard_timeouts_passed_through(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    OpencodeProvider().run("review this", timeout=1234)

    kwargs = calls[0][1]
    assert kwargs["idle_timeout"] == config.OPENCODE_IDLE_TIMEOUT_SEC
    assert kwargs["hard_timeout"] == 1234


def test_env_is_a_copy_not_os_environ_itself(monkeypatch):
    """Providers are shared singletons run from parallel threads — os.environ
    itself must never be handed over or mutated."""
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    OpencodeProvider().run("review this")

    env = calls[0][1]["env"]
    assert env is not os.environ
    assert "PATH" in env or "Path" in env


# ── prompt size cap ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("read_only", [True, False])
def test_prompt_over_cap_rejected_without_spawning(monkeypatch, read_only):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setattr(config, "OPENCODE_MAX_PROMPT_BYTES", 100)

    result = OpencodeProvider().run("x" * 101, read_only=read_only)

    assert result.success is False
    assert result.error == "prompt_too_large"
    assert not calls  # no subprocess spawn at all — no silent truncation


def test_prompt_at_exactly_the_cap_is_accepted(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setattr(config, "OPENCODE_MAX_PROMPT_BYTES", 100)

    result = OpencodeProvider().run("x" * 100)

    assert result.success is True
    assert calls


# ── model resolution ────────────────────────────────────────────────────────────

def test_forced_model_tag_wins_over_picker_and_default(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setattr(config, "OPENCODE_MODEL_PICKER", "some_picker.py")

    def _picker_should_not_run(*a, **kw):
        raise AssertionError("picker must not run when a model tag is forced")
    monkeypatch.setattr(opencode_mod.subprocess, "run", _picker_should_not_run)

    provider = OpencodeProvider()
    provider._forced_model = "openrouter/zdr-review-alt"
    try:
        provider.run("review this")
    finally:
        provider._forced_model = None

    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == "openrouter/zdr-review-alt"


def test_no_tag_no_picker_uses_static_default(monkeypatch):
    """OPENCODE_MODEL_PICKER == "" is the NORMAL case (fresh machine) — the
    picker must not even be attempted."""
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    def _picker_should_not_run(*a, **kw):
        raise AssertionError("picker must not run when unconfigured")
    monkeypatch.setattr(opencode_mod.subprocess, "run", _picker_should_not_run)

    OpencodeProvider().run("review this")

    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == config.OPENCODE_DEFAULT_MODEL


def _picker_on(monkeypatch, profile="zdr"):
    monkeypatch.setattr(config, "OPENCODE_MODEL_PICKER", "oc_pick_model.py")
    monkeypatch.setattr(config, "OPENCODE_PICKER_PROFILE", profile)


def _scripted_picker(monkeypatch, answers):
    """Fake picker: ``answers`` maps a rung to ``(exit_code, stdout)`` or an
    exception to raise; a rung not listed answers exit 5. Returns the call log."""
    calls = []

    def _run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        answer = answers.get(cmd[cmd.index("--stufe") + 1], (5, ""))
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(returncode=answer[0], stdout=answer[1])
    monkeypatch.setattr(opencode_mod.subprocess, "run", _run)
    return calls


def _scripted_watchdog(monkeypatch, outcomes, on_call=None):
    """Fake opencode runs: one entry of ``outcomes`` per start (an exception is
    raised). Returns the call log; a start beyond the script fails the test."""
    calls = []

    def _run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if on_call:
            on_call()
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _run)
    return calls


def _ok(text="OK"):
    return SimpleNamespace(returncode=0, stdout=text, stderr="")


def _fail(stdout="", stderr="", returncode=1):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _idle_kill():
    exc = subprocess.TimeoutExpired("opencode", 1)
    exc.timeout_kind = "idle"
    return exc


def _model_of(call):
    cmd = call[0]
    return cmd[cmd.index("--model") + 1]


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_picker_success_wins_over_default(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    _picker_on(monkeypatch)
    picker_calls = _scripted_picker(monkeypatch, {"1": (0, "openrouter/zdr-review-long\n")})

    OpencodeProvider().run("review this")

    picker_cmd, picker_kwargs = picker_calls[0]
    assert picker_cmd == [
        sys.executable, "-X", "utf8", "oc_pick_model.py", "--profile", "zdr",
        "--aufgabe", "umsetzung", "--stufe", "1", "--dir", os.getcwd(),
    ]
    assert picker_kwargs["timeout"] == config.OPENCODE_PICKER_TIMEOUT_SEC
    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == "openrouter/zdr-review-long"


def test_picker_timeout_default_is_120_and_idle_default_is_600():
    assert config.OPENCODE_PICKER_TIMEOUT_SEC == 120
    assert config.OPENCODE_IDLE_TIMEOUT_SEC == 600


@pytest.mark.parametrize("picker_answer,error", [
    ((4, ""), "policy_block"),                       # was: fell back to the default
    ((0, "some model\nwith noise"), "unreachable"),  # unusable stdout
    (FileNotFoundError(), "unreachable"),            # picker script missing
])
def test_picker_failure_aborts_without_default_or_start(monkeypatch, picker_answer, error):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    _picker_on(monkeypatch)
    _scripted_picker(monkeypatch, {"1": picker_answer})

    result = OpencodeProvider().run("review this")

    assert calls == []
    assert result.success is False
    assert result.error == error


# ── picker ladder (S7) ─────────────────────────────────────────────────────────

_PICKER_AT_IMPORT = config.OPENCODE_MODEL_PICKER  # before any test fixture patches it


def test_tests_never_start_the_real_picker():
    """Group K: tests/conftest.py pins the picker empty before ``import config``;
    without it a picker in .env makes every ``with_opencode`` test start the real script."""
    assert _PICKER_AT_IMPORT == ""
    assert os.environ.get("OPENCODE_MODEL_PICKER") == ""


@pytest.mark.parametrize("read_only,aufgabe", [(True, "review"), (False, "umsetzung")])
def test_task_kind_and_call_shape_follow_read_only(monkeypatch, tmp_path, read_only, aufgabe):
    """Group A: read_only picks the task kind; call shape with an absolute --dir."""
    _picker_on(monkeypatch, "open")
    picker_calls = _scripted_picker(monkeypatch, {"1": (0, "openrouter/m1")})
    _scripted_watchdog(monkeypatch, [_ok()])

    OpencodeProvider().run("x", cwd=str(tmp_path), read_only=read_only)

    cmd = picker_calls[0][0]
    assert _arg(cmd, "--aufgabe") == aufgabe
    assert _arg(cmd, "--profile") == "open"
    assert _arg(cmd, "--stufe") == "1"
    assert _arg(cmd, "--dir") == str(tmp_path)


def test_picker_dir_is_made_absolute_and_falls_back_to_getcwd(monkeypatch):
    _picker_on(monkeypatch)
    picker_calls = _scripted_picker(monkeypatch, {"1": (0, "openrouter/m1")})
    _scripted_watchdog(monkeypatch, [_ok(), _ok()])

    OpencodeProvider().run("x", cwd="rel/dir")
    OpencodeProvider().run("x")

    assert _arg(picker_calls[0][0], "--dir") == os.path.abspath("rel/dir")
    assert _arg(picker_calls[1][0], "--dir") == os.getcwd()


@pytest.mark.parametrize("read_only,expected", [(True, True), (False, False)])
def test_without_exploration_switch_only_for_review(monkeypatch, read_only, expected):
    """Group L: review passes write no ledger line, so they must never get a challenger."""
    _picker_on(monkeypatch)
    picker_calls = _scripted_picker(monkeypatch, {"1": (5, ""), "2": (0, "openrouter/m2")})
    _scripted_watchdog(monkeypatch, [_ok()])

    OpencodeProvider().run("x", read_only=read_only)

    assert len(picker_calls) == 2
    for cmd, _ in picker_calls:
        assert ("--ohne-erkundung" in cmd) is expected


def test_rungs_are_asked_only_when_needed(monkeypatch):
    """Group B: a rung-1 success never asks rung 2; a failed run asks it afterwards."""
    _picker_on(monkeypatch)
    picker_calls = _scripted_picker(
        monkeypatch, {"1": (0, "openrouter/m1"), "2": (0, "openrouter/m2")}
    )
    run_calls = _scripted_watchdog(monkeypatch, [_ok()])
    OpencodeProvider().run("x")
    assert [_arg(c[0], "--stufe") for c in picker_calls] == ["1"]
    assert len(run_calls) == 1

    picker_calls.clear()
    run_calls = _scripted_watchdog(monkeypatch, [_idle_kill(), _ok("second")])
    result = OpencodeProvider().run("x")
    assert [_arg(c[0], "--stufe") for c in picker_calls] == ["1", "2"]
    assert [_model_of(c) for c in run_calls] == ["openrouter/m1", "openrouter/m2"]
    assert result.success is True and result.output == "second"


@pytest.mark.parametrize("profile", ["zdr", "open"])
def test_exit_5_is_not_an_error_but_the_next_rung(monkeypatch, profile):
    """Group C: an unoccupied rung hands over to the next one under both profiles."""
    _picker_on(monkeypatch, profile)
    picker_calls = _scripted_picker(monkeypatch, {"1": (5, ""), "2": (0, "openrouter/m2")})
    run_calls = _scripted_watchdog(monkeypatch, [_ok()])

    result = OpencodeProvider().run("x")

    assert result.success is True
    assert [_arg(c[0], "--stufe") for c in picker_calls] == ["1", "2"]
    assert _model_of(run_calls[0]) == "openrouter/m2"


@pytest.mark.parametrize("answer,error", [
    ((4, ""), "policy_block"),
    ((3, ""), "unreachable"),
    ((6, ""), "unreachable"),
    ((2, ""), "unreachable"),
    ((1, ""), "unreachable"),
    ((0, "two tokens"), "unreachable"),
    (subprocess.TimeoutExpired("picker", 120), "unreachable"),
    (OSError("gone"), "unreachable"),
])
def test_zdr_is_fail_closed_on_every_picker_failure(monkeypatch, answer, error):
    """Group D: under zdr nothing but exit 5 may lead on; no start, no default, no rung 2."""
    _picker_on(monkeypatch, "zdr")
    picker_calls = _scripted_picker(
        monkeypatch, {"1": answer, "2": (0, "openrouter/m2")}
    )
    run_calls = _scripted_watchdog(monkeypatch, [])

    result = OpencodeProvider().run("x")

    assert run_calls == []
    assert len(picker_calls) == 1
    assert (result.success, result.error) == (False, error)
    assert config.OPENCODE_DEFAULT_MODEL not in result.output


@pytest.mark.parametrize("profile", ["zdr", "open"])
def test_exit_12_stale_prices_aborts_under_both_profiles(monkeypatch, profile):
    """Group E: unlike exit 5, exit 12 does not move on to rung 2 -- not even under open."""
    _picker_on(monkeypatch, profile)
    picker_calls = _scripted_picker(monkeypatch, {"1": (12, ""), "2": (0, "openrouter/m2")})
    run_calls = _scripted_watchdog(monkeypatch, [])

    result = OpencodeProvider().run("x")

    assert run_calls == []
    assert len(picker_calls) == 1
    assert (result.success, result.error) == (False, "unreachable")


@pytest.mark.parametrize("profile", ["zdr", "open"])
def test_all_rungs_unoccupied_is_unreachable_never_the_default(monkeypatch, profile):
    """Group F."""
    _picker_on(monkeypatch, profile)
    picker_calls = _scripted_picker(monkeypatch, {})
    run_calls = _scripted_watchdog(monkeypatch, [])

    result = OpencodeProvider().run("x")

    assert run_calls == []
    assert [_arg(c[0], "--stufe") for c in picker_calls] == ["1", "2", "ausweich"]
    assert (result.success, result.error) == (False, "unreachable")
    assert "keine Stufe belegt" in result.output


@pytest.mark.parametrize("answer", [
    (3, ""), (6, ""), (4, ""), (1, ""), (0, "two tokens"), subprocess.TimeoutExpired("picker", 120),
])
def test_open_profile_moves_on_after_a_picker_failure(monkeypatch, answer):
    """Group G: under open a failing rung is skipped (the data-protection gate is zdr's)."""
    _picker_on(monkeypatch, "open")
    picker_calls = _scripted_picker(monkeypatch, {"1": answer, "2": (0, "openrouter/m2")})
    run_calls = _scripted_watchdog(monkeypatch, [_ok()])

    result = OpencodeProvider().run("x")

    assert result.success is True
    assert [_arg(c[0], "--stufe") for c in picker_calls] == ["1", "2"]
    assert _model_of(run_calls[0]) == "openrouter/m2"


def test_open_profile_still_fails_closed_when_every_rung_fails(monkeypatch):
    _picker_on(monkeypatch, "open")
    _scripted_picker(monkeypatch, {"1": (3, ""), "2": (6, ""), "ausweich": (3, "")})
    run_calls = _scripted_watchdog(monkeypatch, [])

    result = OpencodeProvider().run("x")

    assert run_calls == []
    assert (result.success, result.error) == (False, "unreachable")


@pytest.mark.parametrize("outcome,error", [
    (_fail(stderr="429 too many requests"), "rate_limit"),
    (_fail(stderr="401 unauthorized"), "auth_error"),
    (subprocess.TimeoutExpired("opencode", 1), "timeout"),
    (FileNotFoundError(), "opencode CLI not found"),
    (OSError("cannot start"), "cannot start"),
])
def test_no_next_rung_after_rate_limit_auth_timeout_or_missing_cli(monkeypatch, outcome, error):
    """Group H (stay): the next model would meet the same wall or the deadline is spent."""
    _picker_on(monkeypatch)
    picker_calls = _scripted_picker(
        monkeypatch, {"1": (0, "openrouter/m1"), "2": (0, "openrouter/m2")}
    )
    run_calls = _scripted_watchdog(monkeypatch, [outcome, _ok()])

    result = OpencodeProvider().run("x")

    assert (result.success, result.error) == (False, error)
    assert len(run_calls) == 1
    assert len(picker_calls) == 1


@pytest.mark.parametrize("outcome", [
    _idle_kill(),                                     # hang
    _fail(returncode=0, stdout=""),                   # empty output
    _fail(stderr="UnknownError: boom"),               # unclassified
    _fail(stderr="Zero data retention required"),     # policy_block from opencode
])
def test_next_rung_after_hang_empty_output_unclassified_or_policy_block(monkeypatch, outcome):
    """Group H (go on)."""
    _picker_on(monkeypatch)
    _scripted_picker(monkeypatch, {"1": (0, "openrouter/m1"), "2": (0, "openrouter/m2")})
    run_calls = _scripted_watchdog(monkeypatch, [outcome, _ok("fine")])

    result = OpencodeProvider().run("x")

    assert result.success is True and result.output == "fine"
    assert [_model_of(c) for c in run_calls] == ["openrouter/m1", "openrouter/m2"]


def test_failure_of_the_last_started_run_is_returned_when_no_rung_is_left(monkeypatch):
    _picker_on(monkeypatch)
    _scripted_picker(monkeypatch, {"1": (0, "openrouter/m1")})  # 2 and ausweich unoccupied
    _scripted_watchdog(monkeypatch, [_idle_kill()])

    result = OpencodeProvider().run("x")

    assert (result.success, result.error) == (False, "hang")


def test_repeated_model_on_a_later_rung_is_skipped_and_the_fallback_is_asked(monkeypatch):
    """ADR-007: rung 2 answers the model rung 1 already ran -> never started twice."""
    _picker_on(monkeypatch)
    picker_calls = _scripted_picker(monkeypatch, {
        "1": (0, "openrouter/m1"), "2": (0, "openrouter/m1"), "ausweich": (0, "openrouter/m3"),
    })
    run_calls = _scripted_watchdog(monkeypatch, [_idle_kill(), _ok("fine")])

    result = OpencodeProvider().run("x")

    assert result.success is True and result.output == "fine"
    assert [_model_of(c) for c in run_calls] == ["openrouter/m1", "openrouter/m3"]
    assert [_arg(c[0], "--stufe") for c in picker_calls] == ["1", "2", "ausweich"]


def test_every_remaining_rung_repeated_returns_the_last_result(monkeypatch):
    """All later rungs repeat -> like 'no rung occupied': the failed run is the answer."""
    _picker_on(monkeypatch)
    picker_calls = _scripted_picker(monkeypatch, {
        "1": (0, "openrouter/m1"), "2": (0, "openrouter/m1"), "ausweich": (0, "openrouter/m1"),
    })
    run_calls = _scripted_watchdog(monkeypatch, [_idle_kill()])

    result = OpencodeProvider().run("x")

    assert (result.success, result.error) == (False, "hang")
    assert len(run_calls) == 1
    assert len(picker_calls) == 3


def test_forced_tag_skips_the_picker_and_never_moves_on(monkeypatch):
    _picker_on(monkeypatch)
    picker_calls = _scripted_picker(monkeypatch, {"1": (0, "openrouter/m1")})
    run_calls = _scripted_watchdog(monkeypatch, [_idle_kill(), _ok()])
    provider = OpencodeProvider()
    provider._forced_model = "openrouter/forced"
    try:
        result = provider.run("x")
    finally:
        provider._forced_model = None

    assert picker_calls == []
    assert [_model_of(c) for c in run_calls] == ["openrouter/forced"]
    assert result.error == "hang"


def _fake_clock(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(
        opencode_mod, "time",
        SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: None),
    )
    return clock


def test_ladder_shares_one_deadline_between_rungs(monkeypatch):
    """Group I: rung 2 only gets what rung 1 left over."""
    clock = _fake_clock(monkeypatch)
    _picker_on(monkeypatch)
    _scripted_picker(monkeypatch, {"1": (0, "openrouter/m1"), "2": (0, "openrouter/m2")})
    run_calls = _scripted_watchdog(
        monkeypatch, [_idle_kill(), _ok()], on_call=lambda: clock.__setitem__(0, clock[0] + 100)
    )

    OpencodeProvider().run("x", timeout=500)

    assert [c[1]["hard_timeout"] for c in run_calls] == [500, 400]


def test_no_further_rung_below_60_seconds_remaining(monkeypatch):
    clock = _fake_clock(monkeypatch)
    _picker_on(monkeypatch)
    picker_calls = _scripted_picker(
        monkeypatch, {"1": (0, "openrouter/m1"), "2": (0, "openrouter/m2")}
    )
    run_calls = _scripted_watchdog(
        monkeypatch, [_idle_kill(), _ok()], on_call=lambda: clock.__setitem__(0, clock[0] + 100)
    )

    result = OpencodeProvider().run("x", timeout=150)  # 50 s left after rung 1

    assert len(run_calls) == 1
    assert len(picker_calls) == 1  # not even asked
    assert (result.success, result.error) == (False, "hang")


def test_ladder_logs_each_rung_and_warns_on_picker_exit(monkeypatch, caplog):
    """Group J."""
    _picker_on(monkeypatch, "open")
    _scripted_picker(monkeypatch, {"1": (3, ""), "2": (0, "openrouter/m2")})
    _scripted_watchdog(monkeypatch, [_ok()])

    with caplog.at_level("INFO", logger="providers.opencode"):
        OpencodeProvider().run("x", read_only=True)

    infos = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert "[opencode] Wähler review/open Stufe 2 → openrouter/m2" in infos
    assert any("Stufe 1" in m and "Exit 3" in m for m in warnings)
    assert any("extern-review/openrouter/m2" in m for m in infos)


# ── error classification ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "stdout,stderr,expected",
    [
        ("", "data policy violation", "policy_block"),
        ("", "Zero data retention required for this key", "policy_block"),
        ("", "rate limit exceeded", "rate_limit"),
        ("", "HTTP 429 Too Many Requests", "rate_limit"),
        ("", "this model requires more credits", "rate_limit"),
        ("", "402 Payment Required", "rate_limit"),
        ("", "401 unauthorized", "auth_error"),
        ("", "403 Forbidden", "auth_error"),
        ("", "invalid API key", "auth_error"),
    ],
)
def test_error_keyword_classification(monkeypatch, stdout, stderr, expected):
    monkeypatch.setattr(
        opencode_mod, "run_with_watchdog",
        _fake_watchdog([], returncode=1, stdout=stdout, stderr=stderr),
    )
    result = OpencodeProvider().run("review this")
    assert result.success is False
    assert result.error == expected


def test_policy_block_is_not_transient():
    """A ZDR/data-policy refusal is permanent — retrying cannot change it."""
    assert "policy_block" not in TRANSIENT_ERRORS
    assert is_transient("policy_block") is False


def test_rate_limit_is_transient():
    assert is_transient("rate_limit") is True


def test_unclassified_nonzero_exit_carries_raw_stderr(monkeypatch):
    monkeypatch.setattr(
        opencode_mod, "run_with_watchdog",
        _fake_watchdog([], returncode=1, stdout="", stderr="UnknownError: boom"),
    )
    result = OpencodeProvider().run("review this")
    assert result.success is False
    assert result.error == "UnknownError: boom"


def test_success_zero_exit_with_empty_output_is_not_a_success(monkeypatch):
    monkeypatch.setattr(
        opencode_mod, "run_with_watchdog",
        _fake_watchdog([], returncode=0, stdout="", stderr=""),
    )
    result = OpencodeProvider().run("review this")
    assert result.success is False
    assert result.error == "empty output"


# ── ANSI stripping ──────────────────────────────────────────────────────────────

def test_ansi_escapes_stripped_from_success_output(monkeypatch):
    monkeypatch.setattr(
        opencode_mod, "run_with_watchdog",
        _fake_watchdog([], returncode=0, stdout="\x1b[91mP1\x1b[0m: bug here"),
    )
    result = OpencodeProvider().run("review this")
    assert result.success is True
    assert "\x1b" not in result.output
    assert result.output == "P1: bug here"


def test_ansi_escapes_stripped_before_keyword_classification(monkeypatch):
    monkeypatch.setattr(
        opencode_mod, "run_with_watchdog",
        _fake_watchdog([], returncode=1, stdout="", stderr="\x1b[31mrate limit\x1b[0m hit"),
    )
    result = OpencodeProvider().run("review this")
    assert result.error == "rate_limit"


# ── token counts ─────────────────────────────────────────────────────────────────

def test_no_token_counts_reported(monkeypatch):
    """opencode reports no usage/cost per run (measured cost=0 in 142/142 log
    lines) — the orchestrator's char/duration estimate takes over."""
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog([]))
    result = OpencodeProvider().run("review this")
    assert (result.input_tokens, result.output_tokens) == (0, 0)


# ── timeouts / missing binary ────────────────────────────────────────────────────

def test_timeout_kinds_map_to_hang_and_timeout(monkeypatch):
    def _raise(kind):
        def _run(cmd, **kwargs):
            exc = subprocess.TimeoutExpired(cmd, 1)
            exc.timeout_kind = kind
            raise exc
        return _run

    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _raise("idle"))
    assert OpencodeProvider().run("x").error == "hang"

    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _raise("hard"))
    assert OpencodeProvider().run("x").error == "timeout"


def test_missing_exe_is_a_clean_error_no_spawn(monkeypatch):
    monkeypatch.setattr(opencode_mod, "_OPENCODE_EXE", None)
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    result = OpencodeProvider().run("x")

    assert result.success is False
    assert "not found" in result.error
    assert not calls


def test_missing_binary_filenotfound_from_watchdog_is_a_clean_error(monkeypatch):
    def _raise(cmd, **kwargs):
        raise FileNotFoundError()
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _raise)

    result = OpencodeProvider().run("x")

    assert result.success is False
    assert "not found" in result.error


def test_sessions_unsupported():
    assert OpencodeProvider.supports_sessions is False
    # accepts session args and ignores them (BaseProvider contract)
    OpencodeProvider()  # constructible; run() signature checked by the tests above


# ── temp prompt file lifecycle ──────────────────────────────────────────────────

def test_prompt_temp_file_created_and_cleaned_up_on_success(monkeypatch):
    captured_path = {}

    def _run(cmd, **kwargs):
        path = cmd[cmd.index("-f") + 1]
        captured_path["path"] = path
        assert os.path.isfile(path)
        with open(path, encoding="utf-8") as fh:
            assert fh.read() == "review this task"
        return SimpleNamespace(returncode=0, stdout="OK", stderr="")

    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _run)

    OpencodeProvider().run("review this task")

    assert captured_path["path"]
    assert not os.path.exists(captured_path["path"])


def test_prompt_temp_file_cleaned_up_even_on_timeout(monkeypatch):
    captured_path = {}

    def _run(cmd, **kwargs):
        path = cmd[cmd.index("-f") + 1]
        captured_path["path"] = path
        exc = subprocess.TimeoutExpired(cmd, 1)
        exc.timeout_kind = "idle"
        raise exc

    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _run)

    result = OpencodeProvider().run("review this task")

    assert result.error == "hang"
    assert captured_path["path"]
    assert not os.path.exists(captured_path["path"])


def test_prompt_temp_file_cleaned_up_on_unexpected_oserror(monkeypatch):
    captured_path = {}

    def _run(cmd, **kwargs):
        path = cmd[cmd.index("-f") + 1]
        captured_path["path"] = path
        raise OSError("boom")

    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _run)

    result = OpencodeProvider().run("review this task")

    assert result.success is False
    assert "boom" in result.error
    assert not os.path.exists(captured_path["path"])


# ── is_available() ──────────────────────────────────────────────────────────────

def test_is_available_true_with_exe_and_both_agents(monkeypatch, tmp_path):
    cfg_path = tmp_path / "opencode.json"
    cfg_path.write_text(
        json.dumps({"agent": {"extern-review": {}, "extern-dev": {}}}), encoding="utf-8"
    )
    monkeypatch.setattr(config, "OPENCODE_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(opencode_mod, "_resolve_exe", lambda: _FAKE_EXE)

    assert OpencodeProvider.is_available() is True


def test_is_available_false_missing_config_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OPENCODE_CONFIG_PATH", tmp_path / "does_not_exist.json")
    monkeypatch.setattr(opencode_mod, "_resolve_exe", lambda: _FAKE_EXE)

    assert OpencodeProvider.is_available() is False


def test_is_available_false_malformed_json_never_raises(monkeypatch, tmp_path):
    cfg_path = tmp_path / "opencode.json"
    cfg_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(config, "OPENCODE_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(opencode_mod, "_resolve_exe", lambda: _FAKE_EXE)

    assert OpencodeProvider.is_available() is False


def test_is_available_false_missing_required_agent(monkeypatch, tmp_path):
    cfg_path = tmp_path / "opencode.json"
    cfg_path.write_text(json.dumps({"agent": {"extern-review": {}}}), encoding="utf-8")
    monkeypatch.setattr(config, "OPENCODE_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(opencode_mod, "_resolve_exe", lambda: _FAKE_EXE)

    assert OpencodeProvider.is_available() is False


def test_is_available_false_when_only_cmd_shim_resolvable(monkeypatch, tmp_path):
    """No .exe found next to the shim (subprocess can only start a .cmd via
    shell=True, which breaks argument quoting — see module docstring)."""
    cfg_path = tmp_path / "opencode.json"
    cfg_path.write_text(
        json.dumps({"agent": {"extern-review": {}, "extern-dev": {}}}), encoding="utf-8"
    )
    monkeypatch.setattr(config, "OPENCODE_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(opencode_mod, "_resolve_exe", lambda: None)

    assert OpencodeProvider.is_available() is False


def test_is_available_never_raises_on_unexpected_exception(monkeypatch):
    """Runs at dispatcher-import time — must collapse any exception to False."""
    def _boom():
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(opencode_mod, "_resolve_exe", _boom)

    assert OpencodeProvider.is_available() is False


def test_resolve_exe_prefers_real_exe_next_to_npm_shim(monkeypatch, tmp_path):
    """The shutil.which() hit for a bare `opencode` on this machine is always
    the .CMD/.ps1 npm shim; the real binary sits at
    <parent>/node_modules/@opencode/cli/bin/opencode.exe (legacy fallback:
    <parent>/node_modules/opencode-ai/bin/opencode.exe)."""
    npm_dir = tmp_path / "npm"
    npm_dir.mkdir()
    shim = npm_dir / "opencode.CMD"
    shim.write_text("@echo off", encoding="utf-8")
    real_exe_dir = npm_dir / "node_modules" / "@opencode" / "cli" / "bin"
    real_exe_dir.mkdir(parents=True)
    real_exe = real_exe_dir / "opencode.exe"
    real_exe.write_bytes(b"MZ")

    monkeypatch.setattr(opencode_mod.shutil, "which", lambda name: str(shim))

    assert opencode_mod._resolve_exe() == str(real_exe)


def _npm_with_exe(tmp_path, *pkg_parts):
    npm_dir = tmp_path / "npm"
    npm_dir.mkdir()
    shim = npm_dir / "opencode.CMD"
    shim.write_text("@echo off", encoding="utf-8")
    exe_dir = npm_dir.joinpath("node_modules", *pkg_parts, "bin")
    exe_dir.mkdir(parents=True)
    (exe_dir / "opencode.exe").write_bytes(b"MZ")
    return shim, exe_dir / "opencode.exe"


def test_resolve_exe_falls_back_to_legacy_opencode_ai_layout(monkeypatch, tmp_path):
    shim, legacy = _npm_with_exe(tmp_path, "opencode-ai")
    monkeypatch.setattr(opencode_mod.shutil, "which", lambda name: str(shim))

    assert opencode_mod._resolve_exe() == str(legacy)


def test_resolve_exe_prefers_new_layout_when_both_exist(monkeypatch, tmp_path):
    shim, new = _npm_with_exe(tmp_path, "@opencode", "cli")
    legacy_dir = tmp_path / "npm" / "node_modules" / "opencode-ai" / "bin"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "opencode.exe").write_bytes(b"MZ")
    monkeypatch.setattr(opencode_mod.shutil, "which", lambda name: str(shim))

    assert opencode_mod._resolve_exe() == str(new)


def test_resolve_exe_none_when_real_exe_missing(monkeypatch, tmp_path):
    npm_dir = tmp_path / "npm"
    npm_dir.mkdir()
    shim = npm_dir / "opencode.CMD"
    shim.write_text("@echo off", encoding="utf-8")
    # no node_modules/opencode-ai/bin/opencode.exe created

    monkeypatch.setattr(opencode_mod.shutil, "which", lambda name: str(shim))

    assert opencode_mod._resolve_exe() is None


def test_resolve_exe_none_when_shutil_which_finds_nothing(monkeypatch):
    monkeypatch.setattr(opencode_mod.shutil, "which", lambda name: None)

    assert opencode_mod._resolve_exe() is None


# ---------------------------------------------------------------------------
# Error classification: the two directions are deliberately NOT symmetric.
# Found by the opencode external reviewer (2026-09-04), which pointed out that
# auth_error is absent from providers.base.TRANSIENT_ERRORS — so a false positive
# there stamps a recoverable task terminally failed (❌) at 03:00, while a false
# positive on rate_limit only costs a cooldown. The old version scanned bare
# substrings for both.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "rate limit exceeded",
    "http 429 too many requests",
    "error: 429",
    "requires more credits",
])
def test_rate_limit_detected_for_real_signals(text):
    assert _looks_rate_limited(text) is True


@pytest.mark.parametrize("text", ["token count was 14290", "result 4029 items"])
def test_rate_limit_ignores_digits_embedded_in_longer_numbers(text):
    """Digit boundaries: 14290 and 4029 contain "429"/"402" as substrings."""
    assert _looks_rate_limited(text) is False


def test_rate_limit_stays_generous_on_prose_by_design():
    """"Berechne 429 mal 3" still counts as a rate limit, and that is deliberate.

    The asymmetry, re-measured 2026-09-04 (the first version of this docstring
    had the mechanism wrong): orchestrator.py:711 bails out of the in-run
    backoff for TRANSIENT_ERRORS only. So rate_limit costs exactly ONE paid
    opencode run, while every non-transient classification — auth_error and the
    generic fallback alike — runs the full MAX_RETRIES_PER_PROVIDER = 2. A
    missed real rate limit is therefore the expensive direction (2 paid runs,
    no cooldown), a false positive the cheap one. Pinned so a future "cleanup"
    does not quietly make both sides strict.
    """
    assert _looks_rate_limited("berechne 429 mal 3") is True


@pytest.mark.parametrize("text", [
    "insufficient credits",
    "not enough credits to continue",
    "error: insufficient balance",
    "402 payment required",
    "you are out of credits",
])
def test_rate_limit_covers_the_402_family(text):
    """The shared $5/day key can be drained mid-run by the HTTP provider, and
    OpenRouter's wording for that case is not pinned by any measurement we have.
    Missing it would classify a plain "wait for the reset" situation as a generic
    failure — the 2-paid-runs branch. All phrases are multi-word on purpose: a
    bare "credits"/"balance" is ordinary review vocabulary."""
    assert _looks_rate_limited(text) is True


@pytest.mark.parametrize("text", [
    "das modul vergibt credits an nutzer",
    "die balance zwischen lesbarkeit und tempo stimmt",
])
def test_rate_limit_402_phrases_do_not_fire_on_single_words(text):
    assert _looks_rate_limited(text) is False


@pytest.mark.parametrize("text", [
    "401 unauthorized",
    "status: 403",
    "invalid api key",
    "authentication failed",
])
def test_auth_error_detected_for_real_signals(text):
    assert _looks_auth_failed(text) is True


@pytest.mark.parametrize("text", [
    "der report nennt 403 zeilen code",
    "die analyse fand 401 treffer",
    "berechne 403 mal 2",
])
def test_auth_error_not_triggered_by_prose_numbers(text):
    """Bare numbers in prose are not an auth signal.

    NOTE on the consequence, corrected 2026-09-04: an earlier version of this
    docstring claimed a false auth_error left the task "terminal, no retry,
    silently lost overnight". Measured, it does not — no consumer of auth_error
    exists outside taxonomy.py's category map, and an unclassified error takes
    the same non-transient path via error_code_of() == "". What a false positive
    actually costs is a WRONG LABEL plus the 2-attempt in-run retry that every
    non-transient code gets. The label is the reason to keep this strict, not a
    lost task. See test_auth_signals_in_the_agents_answer_are_ignored below for
    the structural half of the fix."""
    assert _looks_auth_failed(text) is False


@pytest.mark.parametrize("text", [
    "p2: der endpoint liefert http 403 statt 404",
    "p1: unauthorized users can read the config file",
    "forbidden paths sind nicht gefiltert",
    "authentication failed handling is missing in login.py",
])
def test_auth_signals_in_the_agents_answer_are_ignored(monkeypatch, text):
    """A security review that *talks about* 401/403 must not be classified by
    its own words.

    This is the case the round-1 external review actually named, and keyword
    tightening alone never closed it: "http 403" carries exactly the HTTP context
    _AUTH_CODE_RE requires, and "unauthorized"/"forbidden" are among the most
    common words in a security review. The structural fix is the stream split —
    stdout is the agent's answer, stderr is opencode's own protocol (measured
    2026-09-04 over three runs), so only stderr may decide the error class.
    """
    calls = []
    monkeypatch.setattr(
        opencode_mod, "run_with_watchdog",
        _fake_watchdog(calls, returncode=1, stdout=text, stderr=""),
    )

    result = OpencodeProvider().run("review this")

    assert result.success is False
    assert result.error != "auth_error"
    # The answer is not thrown away just because the run failed.
    assert text in result.output


def test_auth_error_still_detected_on_stderr(monkeypatch):
    """The narrowing must not disarm the check: opencode's own channel still counts."""
    calls = []
    monkeypatch.setattr(
        opencode_mod, "run_with_watchdog",
        _fake_watchdog(calls, returncode=1, stdout="", stderr="Error: HTTP 401 unauthorized"),
    )

    result = OpencodeProvider().run("review this")

    assert result.error == "auth_error"


def test_policy_block_is_a_known_taxonomy_code():
    """opencode is the only provider emitting policy_block; without it in
    _KNOWN_ERROR_CODES, error_code_of() returns "" and the code evaporates in
    analytics while the (correct) terminal behaviour happens for the wrong reason."""
    from providers.base import error_code_of, is_transient
    assert error_code_of("policy_block") == "policy_block"
    assert error_code_of("policy_block: No endpoints found matching your data policy") == "policy_block"
    assert is_transient("policy_block") is False
