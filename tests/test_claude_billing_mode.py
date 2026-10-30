"""claude_billing: the user's api|abo switch (get_mode/set_mode/status/describe) and its CLI.

The conftest fixture ``_isolate_claude_api_credits`` redirects the switch file,
block state and ledger to tmp_path; the real logs/ is never touched.
"""

import json
import logging
import sys
import time

import pytest

import claude_billing
import config
import orchestrator

KEY = "sk-ant-test-0000"


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_KEY", KEY)
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_RETRY_HOURS", 24)
    monkeypatch.setattr("notifier.send_message", lambda *a, **kw: True)


def _write(content: str) -> None:
    config.CLAUDE_BILLING_MODE_FILE.write_text(content, encoding="utf-8")


# --- get_mode -----------------------------------------------------------------

def test_get_mode_missing_file_is_api():
    assert claude_billing.get_mode() == "api"


@pytest.mark.parametrize("mode", ["abo", "api"])
def test_get_mode_reads_valid_file(mode):
    _write(json.dumps({"mode": mode}))
    assert claude_billing.get_mode() == mode


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '{"mode": "wat"}', '{"mode": "ABO"}'])
def test_get_mode_broken_or_unknown_is_api_with_one_warning(raw, caplog):
    _write(raw)
    with caplog.at_level(logging.WARNING, logger="claude_billing"):
        assert claude_billing.get_mode() == "api"
        assert claude_billing.get_mode() == "api"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "claude-billing-mode.json" in warnings[0].getMessage()
    assert "wat" not in warnings[0].getMessage()  # never echo the raw content


def test_get_mode_warns_again_for_different_content(caplog):
    with caplog.at_level(logging.WARNING, logger="claude_billing"):
        _write("{bad")
        claude_billing.get_mode()
        _write("[]")
        claude_billing.get_mode()
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


# --- api_active ---------------------------------------------------------------

def test_api_active_with_key_follows_switch(key):
    assert claude_billing.api_active() is True  # file missing
    _write('{"mode": "api"}')
    assert claude_billing.api_active() is True
    _write('{"mode": "abo"}')
    assert claude_billing.api_active() is False


def test_api_active_without_key_is_false_in_api_mode():
    _write('{"mode": "api"}')
    assert claude_billing.api_active() is False


# --- set_mode -----------------------------------------------------------------

def test_set_mode_writes_file_with_mode_source_since():
    st = claude_billing.set_mode("abo", "cli", now=time.time())
    data = json.loads(config.CLAUDE_BILLING_MODE_FILE.read_text(encoding="utf-8"))
    assert data["mode"] == "abo" and data["source"] == "cli" and data["since"]
    assert st["mode"] == "abo" and st["source"] == "cli" and st["lifted_block"] is False


@pytest.mark.parametrize("bad", ["API", "", "secret-input"])
def test_set_mode_rejects_unknown_mode_without_echo(bad):
    with pytest.raises(ValueError) as exc:
        claude_billing.set_mode(bad, "cli")
    assert "secret-input" not in str(exc.value)
    assert not config.CLAUDE_BILLING_MODE_FILE.exists()


def test_set_mode_oserror_propagates_and_keeps_old_file(monkeypatch):
    claude_billing.set_mode("abo", "cli")
    before = config.CLAUDE_BILLING_MODE_FILE.read_text(encoding="utf-8")

    def boom(path, content):
        raise PermissionError("disk")

    monkeypatch.setattr(claude_billing, "_atomic_write", boom)
    with pytest.raises(OSError):
        claude_billing.set_mode("api", "cli")
    assert config.CLAUDE_BILLING_MODE_FILE.read_text(encoding="utf-8") == before
    assert claude_billing.get_mode() == "abo"


def test_set_api_lifts_block_in_force(key):
    claude_billing.trip("credit")
    assert claude_billing.api_active() is False
    st = claude_billing.set_mode("api", "telegram")
    assert st["lifted_block"] is True
    assert not config.CLAUDE_API_STATE_FILE.exists()
    assert claude_billing.api_active() is True


def test_set_api_lifts_memory_only_block(key):
    claude_billing._mem_state[0] = {"blocked_until": time.time() + 3600, "reason": "credit"}
    assert claude_billing.api_active() is False
    st = claude_billing.set_mode("api", "cli")
    assert st["lifted_block"] is True
    assert claude_billing._mem_state[0] is None
    assert claude_billing.api_active() is True


def test_set_api_without_block_reports_no_lift_but_drops_expired_file(key):
    config.CLAUDE_API_STATE_FILE.write_text(json.dumps({"blocked_until": 1.0, "reason": "credit"}))
    st = claude_billing.set_mode("api", "cli")
    assert st["lifted_block"] is False
    assert not config.CLAUDE_API_STATE_FILE.exists()


def test_set_abo_leaves_block_untouched(key):
    claude_billing.trip("credit")
    st = claude_billing.set_mode("abo", "cli")
    assert st["lifted_block"] is False
    assert config.CLAUDE_API_STATE_FILE.exists()


def _block_file_with_unlink_failures(monkeypatch, failures):
    """Block in force; Path.unlink on the state file raises PermissionError `failures` times
    (None = always), then works."""
    claude_billing.trip("credit")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    real_unlink = type(config.CLAUDE_API_STATE_FILE).unlink
    calls = [0]

    def flaky(self, *a, **kw):
        if self == config.CLAUDE_API_STATE_FILE:
            calls[0] += 1
            if failures is None or calls[0] <= failures:
                raise PermissionError("held")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(type(config.CLAUDE_API_STATE_FILE), "unlink", flaky)
    return calls


def test_set_api_retries_a_briefly_locked_block_file(key, monkeypatch):
    calls = _block_file_with_unlink_failures(monkeypatch, 2)
    st = claude_billing.set_mode("api", "cli")
    assert calls[0] == 3
    assert st["lifted_block"] is True
    assert not config.CLAUDE_API_STATE_FILE.exists()
    assert claude_billing.get_mode() == "api"


def test_set_api_with_undeletable_block_file_raises_and_keeps_old_mode(key, monkeypatch):
    claude_billing.set_mode("abo", "cli")
    before = config.CLAUDE_BILLING_MODE_FILE.read_text(encoding="utf-8")
    _block_file_with_unlink_failures(monkeypatch, None)
    with pytest.raises(OSError):
        claude_billing.set_mode("api", "cli")
    assert config.CLAUDE_API_STATE_FILE.exists()
    assert config.CLAUDE_BILLING_MODE_FILE.read_text(encoding="utf-8") == before
    assert claude_billing.get_mode() == "abo"


def test_set_api_with_undeletable_expired_leftover_does_not_raise(key, monkeypatch):
    _block_file_with_unlink_failures(monkeypatch, None)
    config.CLAUDE_API_STATE_FILE.write_text(json.dumps({"blocked_until": 1.0, "reason": "credit"}))
    st = claude_billing.set_mode("api", "cli")
    assert st["mode"] == "api" and st["lifted_block"] is False
    assert claude_billing.api_active() is True


def test_failed_lift_keeps_the_memory_mirror(key, monkeypatch):
    mirror = {"blocked_until": time.time() + 3600, "reason": "credit"}
    _block_file_with_unlink_failures(monkeypatch, None)
    claude_billing._mem_state[0] = mirror
    with pytest.raises(OSError):
        claude_billing.set_mode("api", "cli")
    assert claude_billing._mem_state[0] is mirror
    assert claude_billing.api_active() is False


def test_invalid_utf8_switch_file_is_api_with_one_warning(key, caplog):
    config.CLAUDE_BILLING_MODE_FILE.write_bytes(b'{"mode": "abo", "source": "\xe4"}')
    with caplog.at_level(logging.WARNING, logger="claude_billing"):
        assert claude_billing.get_mode() == "api"
        assert claude_billing.api_active() is True
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert claude_billing.status()["mode"] == "api"


def test_set_api_logs_lift_only_when_a_block_was_lifted(key, caplog):
    with caplog.at_level(logging.INFO, logger="claude_billing"):
        claude_billing.set_mode("api", "cli")
        assert "block lifted" not in caplog.text
        claude_billing.trip("credit")
        claude_billing.set_mode("api", "cli")
        assert "block lifted" in caplog.text


def test_get_mode_retries_a_briefly_unreadable_file(monkeypatch, caplog):
    _write(json.dumps({"mode": "abo"}))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    real_read = type(config.CLAUDE_BILLING_MODE_FILE).read_text
    calls = [0]

    def flaky(self, *a, **kw):
        if self == config.CLAUDE_BILLING_MODE_FILE:
            calls[0] += 1
            if calls[0] == 1:
                raise PermissionError("replace in flight")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(type(config.CLAUDE_BILLING_MODE_FILE), "read_text", flaky)
    with caplog.at_level(logging.WARNING, logger="claude_billing"):
        assert claude_billing.get_mode() == "abo"
    assert calls[0] == 2 and not caplog.records


# --- status / describe --------------------------------------------------------

def test_status_has_exactly_the_documented_keys(key):
    assert set(claude_billing.status()) == {
        "mode", "source", "since", "key_configured", "api_active", "blocked_until", "block_reason",
    }


def test_status_reports_block_only_while_in_force(key):
    now = time.time()
    claude_billing.trip("credit", now=now)
    st = claude_billing.status(now)
    assert st["blocked_until"] and st["block_reason"] == "credit" and st["api_active"] is False
    later = claude_billing.status(now + 25 * 3600)
    assert later["blocked_until"] is None and later["block_reason"] is None


def test_describe_four_cases(key, monkeypatch):
    assert claude_billing.describe(claude_billing.status()) == "Claude-Abrechnung: API-Guthaben"
    claude_billing.set_mode("abo", "cli")
    assert claude_billing.describe(claude_billing.status()) == "Claude-Abrechnung: Abo (Schalter)"
    claude_billing.set_mode("api", "cli")
    claude_billing.trip("credit")
    text = claude_billing.describe(claude_billing.status())
    assert text.startswith("Claude-Abrechnung: Abo — API gesperrt bis ")
    assert "Guthaben leer" in text
    claude_billing.set_mode("api", "cli")
    monkeypatch.setattr(config, "ORCH_CLAUDE_API_KEY", "")
    assert "kein ORCH_CLAUDE_API_KEY in .env" in claude_billing.describe(claude_billing.status())


def test_status_exception_branch_reports_unknown_mode(key, monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(claude_billing, "_read_mode_file", boom)
    st = claude_billing.status()
    assert st["mode"] is None and st["api_active"] is False
    assert claude_billing.describe(st) == "Claude-Abrechnung: nicht ermittelbar"


# --- CLI: python orchestrator.py --claude-billing ... --------------------------

def _cli(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", *args])
    monkeypatch.setattr(orchestrator, "setup_logging", lambda: None)
    monkeypatch.setattr(orchestrator, "install_thread_excepthook", lambda: None)
    orchestrator.main()


def test_cli_abo_then_status_then_api(monkeypatch, capsys, key):
    _cli(monkeypatch, "--claude-billing", "abo")
    out = capsys.readouterr().out
    assert "Abo" in out and "Gilt ab dem nächsten Claude-Lauf." in out
    assert json.loads(config.CLAUDE_BILLING_MODE_FILE.read_text(encoding="utf-8"))["source"] == "cli"
    _cli(monkeypatch, "--claude-billing", "status")
    out = capsys.readouterr().out
    assert "Abo" in out and "Gilt ab" not in out
    claude_billing.trip("credit")
    _cli(monkeypatch, "--claude-billing", "api")
    out = capsys.readouterr().out
    assert "API-Guthaben" in out and "Sperre aufgehoben" in out
    assert not config.CLAUDE_API_STATE_FILE.exists()
    assert KEY not in out


def test_cli_oserror_exits_1_without_leaking_key(monkeypatch, capsys, key):
    def boom(path, content):
        raise PermissionError("disk")

    monkeypatch.setattr(claude_billing, "_atomic_write", boom)
    with pytest.raises(SystemExit) as exc:
        _cli(monkeypatch, "--claude-billing", "abo")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Schalter nicht gespeichert: PermissionError" in err and KEY not in err
