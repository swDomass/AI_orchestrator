"""Tests for the opencode CLI provider (Stufe 2: tag-activated, not yet in the
dispatcher's fallback chain — see providers/opencode.py's module docstring).

Every switching branch gets its own gate: argv construction (prompt before -f,
--variant only with a forced effort, agent choice by read_only), the 50 KB
prompt cap in both modes, model resolution order (tag > picker > default,
including a non-zero picker exit and garbage picker output), each error-
classification keyword branch (policy_block explicitly NOT transient), ANSI
stripping, is_available() against a missing/malformed opencode.json (never
raising), and temp-file cleanup.
"""

import json
import os
import subprocess
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
    assert cmd[f_index + 1] not in ("--model", "--agent", "--dir", "--variant")
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
    assert cmd[7:9] == ["--dir", "D:/repo"]
    assert cmd[9] == "-f"


def test_variant_flag_absent_without_forced_effort(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    OpencodeProvider().run("review this")

    assert "--variant" not in calls[0][0]


def test_variant_flag_present_with_forced_effort_raw_value(monkeypatch):
    """#effort: values pass through raw — opencode tolerates undocumented
    values like 'xhigh' with exit 0 (measured), so there is no mapping table."""
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    provider = OpencodeProvider()
    provider._forced_effort = "xhigh"
    try:
        provider.run("review this")
    finally:
        provider._forced_effort = None

    cmd = calls[0][0]
    assert cmd[cmd.index("--variant") + 1] == "xhigh"


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


def test_dir_flag_omitted_without_cwd(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))

    OpencodeProvider().run("review this", cwd=None)

    assert "--dir" not in calls[0][0]


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


def test_picker_success_wins_over_default(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setattr(config, "OPENCODE_MODEL_PICKER", "oc_pick_model.py")
    monkeypatch.setattr(config, "OPENCODE_PICKER_PROFILE", "zdr")

    picker_calls = []

    def _fake_run(cmd, **kwargs):
        picker_calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="openrouter/zdr-review-long\n")
    monkeypatch.setattr(opencode_mod.subprocess, "run", _fake_run)

    OpencodeProvider().run("review this")

    assert picker_calls
    picker_cmd, picker_kwargs = picker_calls[0]
    assert "--profile" in picker_cmd
    assert picker_cmd[picker_cmd.index("--profile") + 1] == "zdr"
    assert picker_kwargs["timeout"] == 90
    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == "openrouter/zdr-review-long"


def test_picker_nonzero_exit_falls_back_to_default(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setattr(config, "OPENCODE_MODEL_PICKER", "oc_pick_model.py")
    monkeypatch.setattr(
        opencode_mod.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=4, stdout=""),
    )

    OpencodeProvider().run("review this")

    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == config.OPENCODE_DEFAULT_MODEL


def test_picker_garbage_output_falls_back_to_default(monkeypatch):
    """Multi-line / whitespace-containing stdout does not look like a single
    model-id token and must be discarded even at exit 0."""
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setattr(config, "OPENCODE_MODEL_PICKER", "oc_pick_model.py")
    monkeypatch.setattr(
        opencode_mod.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="some model\nwith noise"),
    )

    OpencodeProvider().run("review this")

    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == config.OPENCODE_DEFAULT_MODEL


def test_picker_raising_falls_back_to_default(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_mod, "run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setattr(config, "OPENCODE_MODEL_PICKER", "missing_script.py")

    def _raise(*a, **kw):
        raise FileNotFoundError()
    monkeypatch.setattr(opencode_mod.subprocess, "run", _raise)

    OpencodeProvider().run("review this")

    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == config.OPENCODE_DEFAULT_MODEL


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
    <parent>/node_modules/opencode-ai/bin/opencode.exe."""
    npm_dir = tmp_path / "npm"
    npm_dir.mkdir()
    shim = npm_dir / "opencode.CMD"
    shim.write_text("@echo off", encoding="utf-8")
    real_exe_dir = npm_dir / "node_modules" / "opencode-ai" / "bin"
    real_exe_dir.mkdir(parents=True)
    real_exe = real_exe_dir / "opencode.exe"
    real_exe.write_bytes(b"MZ")

    monkeypatch.setattr(opencode_mod.shutil, "which", lambda name: str(shim))

    assert opencode_mod._resolve_exe() == str(real_exe)


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
