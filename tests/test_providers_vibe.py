"""Tests for the Mistral Vibe CLI provider.

The CLI facts asserted here were verified against vibe 2.22.0 (2026-07-23);
each one is load-bearing and fails loudly rather than silently degrading:
`-p` without a value (prompt via stdin), `--trust` (else the run hangs on the
trust prompt), the read-only tool policy, and `VIBE_ACTIVE_MODEL` as the only
way to select a model.
"""

from types import SimpleNamespace

import pytest

import config
from providers.vibe import VibeProvider


def _fake_watchdog(calls, *, returncode=0, stdout="OK", stderr="", stdin_error=None):
    def _run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr, stdin_error=stdin_error
        )
    return _run


# ── command construction ──────────────────────────────────────────────────────

def test_prompt_goes_through_stdin_not_argv(monkeypatch):
    """`-p` carries no value: a ~100 KB prompt as an argv element would exceed
    the Windows command-line limit, so it MUST travel via stdin."""
    calls = []
    monkeypatch.setattr("providers.vibe.run_with_watchdog", _fake_watchdog(calls))

    task = "x" * 50_000
    VibeProvider().run(task)

    cmd, kwargs = calls[0]
    assert "-p" in cmd
    # nothing after -p may look like a prompt value
    assert cmd[cmd.index("-p") + 1].startswith("--")
    assert kwargs["input_text"] == task
    assert not any(task in part for part in cmd)


def test_trust_flag_always_present(monkeypatch):
    """Without --trust the CLI blocks on the trust prompt and the run hangs."""
    calls = []
    monkeypatch.setattr("providers.vibe.run_with_watchdog", _fake_watchdog(calls))

    VibeProvider().run("review this")

    assert "--trust" in calls[0][0]


def test_read_only_disables_all_tools(monkeypatch):
    calls = []
    monkeypatch.setattr("providers.vibe.run_with_watchdog", _fake_watchdog(calls))

    VibeProvider().run("review this", read_only=True)

    cmd = calls[0][0]
    assert "--disabled-tools" in cmd
    assert cmd[cmd.index("--disabled-tools") + 1] == "*"
    assert "--enabled-tools" not in cmd
    assert cmd[cmd.index("--max-turns") + 1] == str(config.VIBE_READONLY_MAX_TURNS)


def test_write_mode_grants_read_tools_only(monkeypatch):
    """Vibe is a reviewer in this system, never an executor: even outside
    read_only it gets read_file/grep and never bash/edit/write_file."""
    calls = []
    monkeypatch.setattr("providers.vibe.run_with_watchdog", _fake_watchdog(calls))

    VibeProvider().run("review this", read_only=False)

    cmd = calls[0][0]
    enabled = {cmd[i + 1] for i, part in enumerate(cmd) if part == "--enabled-tools"}
    assert enabled == {"read_file", "grep"}
    for forbidden in ("bash", "edit", "write_file"):
        assert forbidden not in enabled
    assert "--auto-approve" not in cmd and "--yolo" not in cmd


def test_price_cap_and_workdir_passed(monkeypatch):
    calls = []
    monkeypatch.setattr("providers.vibe.run_with_watchdog", _fake_watchdog(calls))

    VibeProvider().run("review this", cwd="D:/repo")

    cmd, kwargs = calls[0]
    assert cmd[cmd.index("--max-price") + 1] == str(config.VIBE_MAX_PRICE_USD)
    assert cmd[cmd.index("--workdir") + 1] == "D:/repo"
    assert kwargs["cwd"] == "D:/repo"
    # Shell use follows the resolved binary suffix: a .exe shim (uv's install
    # route) starts directly, a .cmd/.bat wrapper cannot without a shell.
    from providers import vibe as vibe_mod
    assert kwargs["shell"] is vibe_mod._VIBE_NEEDS_SHELL


def test_price_cap_is_a_validated_positive_number(monkeypatch):
    """A bad env value must not travel to the CLI as a cost argument."""
    import importlib
    import config as config_mod

    monkeypatch.setenv("VIBE_MAX_PRICE_USD", "not-a-number")
    assert config_mod._parse_positive_float_env("VIBE_MAX_PRICE_USD", 0.5) == 0.5
    monkeypatch.setenv("VIBE_MAX_PRICE_USD", "-3")
    assert config_mod._parse_positive_float_env("VIBE_MAX_PRICE_USD", 0.5) == 0.5
    monkeypatch.setenv("VIBE_MAX_PRICE_USD", "1.25")
    assert config_mod._parse_positive_float_env("VIBE_MAX_PRICE_USD", 0.5) == 1.25


# ── model selection (env, not a flag) ─────────────────────────────────────────

def test_forced_model_travels_via_env(monkeypatch):
    """There is no --model flag; VIBE_ACTIVE_MODEL is the only lever."""
    calls = []
    monkeypatch.setattr("providers.vibe.run_with_watchdog", _fake_watchdog(calls))

    provider = VibeProvider()
    provider._forced_model = "devstral-small"
    try:
        provider.run("review this")
    finally:
        provider._forced_model = None

    cmd, kwargs = calls[0]
    assert "--model" not in cmd
    assert kwargs["env"]["VIBE_ACTIVE_MODEL"] == "devstral-small"


def test_no_forced_model_drops_inherited_env_var(monkeypatch):
    """A stray shell export must not decide the model for untagged runs —
    otherwise VIBE_MODEL_ALIASES stops being the single source of truth."""
    calls = []
    monkeypatch.setattr("providers.vibe.run_with_watchdog", _fake_watchdog(calls))
    monkeypatch.setenv("VIBE_ACTIVE_MODEL", "leaked-from-the-shell")

    VibeProvider().run("review this")

    assert "VIBE_ACTIVE_MODEL" not in calls[0][1]["env"]


def test_child_env_forces_utf8(monkeypatch):
    """vibe is a Python CLI: without PYTHONUTF8 it dies with a charmap error
    while writing non-ASCII output under a Windows cp1252 locale."""
    calls = []
    monkeypatch.setattr("providers.vibe.run_with_watchdog", _fake_watchdog(calls))

    VibeProvider().run("review this")

    env = calls[0][1]["env"]
    assert env["PYTHONUTF8"] == "1"
    # Popen replaces rather than merges: the child still needs a real PATH.
    assert "PATH" in env or "Path" in env


def test_env_is_a_copy_not_a_mutation_of_os_environ(monkeypatch):
    """Providers are shared singletons driven from parallel threads — writing
    the model into os.environ would leak one run's model into another's."""
    import os
    calls = []
    monkeypatch.setattr("providers.vibe.run_with_watchdog", _fake_watchdog(calls))

    provider = VibeProvider()
    provider._forced_model = "devstral-small"
    try:
        provider.run("review this")
    finally:
        provider._forced_model = None

    assert "VIBE_ACTIVE_MODEL" not in os.environ


# ── result classification ─────────────────────────────────────────────────────

def test_success_returns_output(monkeypatch):
    monkeypatch.setattr(
        "providers.vibe.run_with_watchdog", _fake_watchdog([], stdout="P1: bug here")
    )
    result = VibeProvider().run("review this")
    assert result.success is True
    assert result.output == "P1: bug here"


def test_no_token_counts_reported(monkeypatch):
    """vibe exposes neither usage nor cost per run — the orchestrator's
    char/duration estimation has to take over, so these must stay 0."""
    monkeypatch.setattr("providers.vibe.run_with_watchdog", _fake_watchdog([]))
    result = VibeProvider().run("review this")
    assert (result.input_tokens, result.output_tokens) == (0, 0)


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("Error: rate limit exceeded", "rate_limit"),
        ("HTTP 401 unauthorized", "auth_error"),
        ("connection reset", "unreachable"),
    ],
)
def test_error_keyword_classification(monkeypatch, stderr, expected):
    monkeypatch.setattr(
        "providers.vibe.run_with_watchdog",
        _fake_watchdog([], returncode=1, stdout="", stderr=stderr),
    )
    result = VibeProvider().run("review this")
    assert result.success is False
    assert result.error == expected


def test_stdin_truncation_in_success_branch_is_a_failure(monkeypatch):
    """rc==0 plus plausible output is exactly the shape the 2026-07-20 incident
    had: the answer belongs to a truncated prompt, not to the task."""
    monkeypatch.setattr(
        "providers.vibe.run_with_watchdog",
        _fake_watchdog([], stdout="OK", stdin_error="flush failed"),
    )
    result = VibeProvider().run("review this")
    assert result.success is False
    assert result.error == "stdin_incomplete"


def test_real_error_wins_over_stdin_diagnosis(monkeypatch):
    """At rc != 0 the broken pipe is a symptom — a CLI dying early also breaks
    the feeder. The real cause must survive; the diagnosis rides along."""
    monkeypatch.setattr(
        "providers.vibe.run_with_watchdog",
        _fake_watchdog(
            [], returncode=1, stdout="", stderr="rate limit", stdin_error="flush failed"
        ),
    )
    result = VibeProvider().run("review this")
    assert result.error == "rate_limit"


def test_timeout_kinds_map_to_hang_and_timeout(monkeypatch):
    import subprocess

    def _raise(kind):
        def _run(cmd, **kwargs):
            exc = subprocess.TimeoutExpired(cmd, 1)
            exc.timeout_kind = kind
            raise exc
        return _run

    monkeypatch.setattr("providers.vibe.run_with_watchdog", _raise("idle"))
    assert VibeProvider().run("x").error == "hang"

    monkeypatch.setattr("providers.vibe.run_with_watchdog", _raise("hard"))
    assert VibeProvider().run("x").error == "timeout"


def test_missing_binary_is_a_clean_error(monkeypatch):
    def _raise(cmd, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr("providers.vibe.run_with_watchdog", _raise)
    result = VibeProvider().run("x")
    assert result.success is False
    assert "not found" in result.error


def test_sessions_unsupported():
    """Accepts session args and ignores them (BaseProvider contract)."""
    assert VibeProvider.supports_sessions is False
