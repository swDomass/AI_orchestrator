"""Tests for the /posten_<Kennung>[_<plattform>] approval command (ADR-007).

No real process and no real Telegram message: subprocess.run and send_message are mocked.
"""

import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config import SAFETY_RULES, SYSTEM_PROMPTS
from telegram_listener import TelegramListener

TEST_CHAT_ID = "99999999"
KENNUNG = "ab12cd34ef56"


def _msg(text: str, *, chat_id: str = TEST_CHAT_ID, message_id: int = 4711) -> dict:
    return {"chat": {"id": int(chat_id)}, "text": text, "message_id": message_id}


def _done(returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)


def _wait_idle(listener: TelegramListener, timeout: float = 5.0) -> None:
    """The handler takes the lock synchronously, the worker frees it: wait for that."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if listener._posten_lock.acquire(blocking=False):
            listener._posten_lock.release()
            return
        time.sleep(0.01)
    raise AssertionError("worker did not release the lock")


def _texts(mock_send) -> list[str]:
    return [c.args[0] for c in mock_send.call_args_list]


@pytest.fixture
def posten_env(tmp_path, monkeypatch):
    """Configured command (three existing paths), chat id patched in, send_message mocked."""
    python = tmp_path / "python.exe"
    befehl = tmp_path / "posten.py"
    cwd = tmp_path / "wl"
    python.write_text(""), befehl.write_text(""), cwd.mkdir()
    monkeypatch.setattr("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
    monkeypatch.setattr("telegram_listener.FREIGABE_POSTEN_PYTHON", str(python))
    monkeypatch.setattr("telegram_listener.FREIGABE_POSTEN_BEFEHL", str(befehl))
    monkeypatch.setattr("telegram_listener.FREIGABE_POSTEN_CWD", str(cwd))
    with patch("telegram_listener.send_message") as send:
        yield SimpleNamespace(python=str(python), befehl=str(befehl), cwd=str(cwd), send=send,
                              listener=TelegramListener(threading.Event()))


# ---------------------------------------------------------------------------
# Start with the exact argument list
# ---------------------------------------------------------------------------

def test_valid_id_starts_exactly_one_process_with_expected_argv(posten_env):
    with patch("telegram_listener.subprocess.run", return_value=_done(0)) as run:
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        _wait_idle(posten_env.listener)
    run.assert_called_once()
    assert run.call_args.args[0] == [posten_env.python, "-X", "utf8", posten_env.befehl, "freigabe",
                                     KENNUNG, "--nachricht-id", "4711"]
    kw = run.call_args.kwargs
    assert kw["shell"] is False
    assert kw["cwd"] == posten_env.cwd
    assert kw["timeout"] == 1800
    assert kw["capture_output"] is True and kw["encoding"] == "utf-8"


def test_child_process_gets_utf8_environment_and_keeps_the_rest(posten_env, monkeypatch):
    """Kern-K1: ohne UTF-8 schreibt das Kind cp1252 und scheitert an Emoji im Bericht."""
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("ORCH_TEST_BEHALTEN", "ja")
    with patch("telegram_listener.subprocess.run", return_value=_done(0)) as run:
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        _wait_idle(posten_env.listener)
    env = run.call_args.kwargs["env"]
    assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8"
    assert env["ORCH_TEST_BEHALTEN"] == "ja" and "PATH" in {k.upper() for k in env}


def test_acknowledgement_is_sent_and_does_not_start_with_slash(posten_env):
    with patch("telegram_listener.subprocess.run", return_value=_done(0)):
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        _wait_idle(posten_env.listener)
    texts = _texts(posten_env.send)
    assert len(texts) == 1
    assert KENNUNG in texts[0] and "Upload startet" in texts[0]
    assert not texts[0].startswith("/")


@pytest.mark.parametrize("plattform", ["google", "facebook", "youtube", "pinterest", "instagram"])
def test_platform_suffix_becomes_plattform_option(posten_env, plattform):
    with patch("telegram_listener.subprocess.run", return_value=_done(0)) as run:
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}_{plattform}"))
        _wait_idle(posten_env.listener)
    argv = run.call_args.args[0]
    assert argv[5:8] == [KENNUNG, "--plattform", plattform]
    assert argv[-2:] == ["--nachricht-id", "4711"]
    assert f"nur {plattform}" in _texts(posten_env.send)[0]


def test_bot_suffix_and_uppercase_are_normalised_by_the_parser(posten_env):
    with patch("telegram_listener.subprocess.run", return_value=_done(0)) as run:
        posten_env.listener._handle_message(_msg(f"/POSTEN_{KENNUNG.upper()}@wl_bot"))
        _wait_idle(posten_env.listener)
    assert run.call_args.args[0][5] == KENNUNG


# ---------------------------------------------------------------------------
# Rejections: no process
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "/posten_ab12cd34ef5",            # 11 hex characters
    "/posten_ab12cd34ef567",          # 13 hex characters
    "/posten_ab12cd34ef5g",           # not hex
    f"/posten_{KENNUNG}_tiktok",      # unknown platform
    f"/posten_{KENNUNG}_google_x",    # second suffix
    "/posten_",
    "/posten",
    "/postenfoo",
])
def test_invalid_id_starts_no_process(posten_env, text):
    with patch("telegram_listener.subprocess.run") as run:
        posten_env.listener._handle_message(_msg(text))
    run.assert_not_called()
    assert "Unbekannte Freigabe-Kennung" in _texts(posten_env.send)[-1]


def test_text_after_the_command_starts_no_process(posten_env):
    with patch("telegram_listener.subprocess.run") as run:
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG} rm -rf"))
    run.assert_not_called()
    assert "ohne Zusatz" in _texts(posten_env.send)[-1]


def test_foreign_chat_starts_no_process_and_gets_no_answer(posten_env):
    with patch("telegram_listener.subprocess.run") as run:
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}", chat_id="123"))
    run.assert_not_called()
    posten_env.send.assert_not_called()


@pytest.mark.parametrize("name", ["PYTHON", "BEFEHL", "CWD"])
def test_missing_setting_means_not_set_up(posten_env, monkeypatch, name):
    monkeypatch.setattr(f"telegram_listener.FREIGABE_POSTEN_{name}", "")
    with patch("telegram_listener.subprocess.run") as run:
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
    run.assert_not_called()
    assert "nicht eingerichtet" in _texts(posten_env.send)[-1]


@pytest.mark.parametrize("name", ["PYTHON", "BEFEHL", "CWD"])
def test_nonexistent_path_means_not_set_up(posten_env, monkeypatch, tmp_path, name):
    monkeypatch.setattr(f"telegram_listener.FREIGABE_POSTEN_{name}", str(tmp_path / "gibt-es-nicht"))
    with patch("telegram_listener.subprocess.run") as run:
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
    run.assert_not_called()
    assert "nicht eingerichtet" in _texts(posten_env.send)[-1]


# ---------------------------------------------------------------------------
# Single flight
# ---------------------------------------------------------------------------

def test_second_command_during_a_run_is_refused(posten_env):
    started, release = threading.Event(), threading.Event()

    def _blocking_run(*_a, **_k):
        started.set()
        assert release.wait(5)
        return _done(0)

    with patch("telegram_listener.subprocess.run", side_effect=_blocking_run) as run:
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        assert started.wait(5)
        posten_env.listener._handle_message(_msg("/posten_0123456789ab"))
        assert "läuft schon" in _texts(posten_env.send)[-1]
        release.set()
        _wait_idle(posten_env.listener)
    assert run.call_count == 1


# ---------------------------------------------------------------------------
# After the run
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", [0, 3, 4, 5])
def test_result_exit_codes_send_no_further_message(posten_env, code):
    with patch("telegram_listener.subprocess.run", return_value=_done(code, "Fehler: x")):
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        _wait_idle(posten_env.listener)
    assert len(_texts(posten_env.send)) == 1  # only the acknowledgement


def test_exit_1_is_a_crash_and_reports_stderr_tail(posten_env):
    """Kern-K2: Exit 1 = Absturz (Traceback), nicht 'Fehler gemeldet' - der Listener meldet."""
    with patch("telegram_listener.subprocess.run",
               return_value=_done(1, "Traceback (most recent call last)\nUnicodeEncodeError: x")):
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        _wait_idle(posten_env.listener)
    text = _texts(posten_env.send)[-1]
    assert "Exit 1" in text and "UnicodeEncodeError" in text


def test_unexpected_exit_reports_code_and_last_five_stderr_lines(posten_env):
    stderr = "\n".join(f"zeile{i}" for i in range(1, 9)) + "\n\n   \n"
    with patch("telegram_listener.subprocess.run", return_value=_done(2, stderr)):
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        _wait_idle(posten_env.listener)
    text = _texts(posten_env.send)[-1]
    assert f"Freigabe {KENNUNG}" in text and "Exit 2" in text
    assert "zeile8" in text and "zeile4" in text
    assert "zeile3" not in text


def test_stderr_tail_is_capped_at_800_characters(posten_env):
    stderr = "\n".join("x" * 400 for _ in range(5))
    with patch("telegram_listener.subprocess.run", return_value=_done(7, stderr)):
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        _wait_idle(posten_env.listener)
    text = _texts(posten_env.send)[-1]
    assert text.count("x") == 800


def test_timeout_reports_abort(posten_env):
    err = subprocess.TimeoutExpired(cmd="x", timeout=1800)
    with patch("telegram_listener.subprocess.run", side_effect=err):
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        _wait_idle(posten_env.listener)
    text = _texts(posten_env.send)[-1]
    assert "30 min abgebrochen" in text and "Stand unklar" in text


def test_start_error_reports_exception_type(posten_env):
    with patch("telegram_listener.subprocess.run", side_effect=PermissionError("secret path")):
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        _wait_idle(posten_env.listener)
    text = _texts(posten_env.send)[-1]
    assert "PermissionError" in text and "secret path" not in text


@pytest.mark.parametrize("outcome", [
    {"return_value": _done(0)},
    {"return_value": _done(2, "boom")},
    {"side_effect": subprocess.TimeoutExpired(cmd="x", timeout=1)},
    {"side_effect": OSError("nope")},
])
def test_lock_is_free_again_after_every_outcome(posten_env, outcome):
    with patch("telegram_listener.subprocess.run", **outcome) as run:
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        _wait_idle(posten_env.listener)
        posten_env.listener._handle_message(_msg("/posten_0123456789ab"))
        _wait_idle(posten_env.listener)
    assert run.call_count == 2


def test_approve_does_not_touch_the_posten_path(posten_env):
    with patch("telegram_listener.subprocess.run") as run, \
            patch.object(TelegramListener, "_cmd_approve") as approve:
        posten_env.listener._handle_message(_msg("/approve"))
    approve.assert_called_once_with("")
    run.assert_not_called()


# ---------------------------------------------------------------------------
# Runde 4 (R4-O): Herkunft, Schwaerzung, absolute Pfade, Startreihenfolge, Regeltext, /help
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,value", [
    ("forward_origin", {"type": "user"}),
    ("forward_date", 1700000000),
    ("forward_from", {"id": 1}),
    ("forward_from_chat", {"id": -1}),
    ("via_bot", {"id": 2, "is_bot": True}),
])
def test_forwarded_or_bot_relayed_command_starts_no_process(posten_env, key, value):
    msg = _msg(f"/posten_{KENNUNG}")
    msg[key] = value
    with patch("telegram_listener.subprocess.run") as run:
        posten_env.listener._handle_message(msg)
    run.assert_not_called()
    assert "nicht weiterleiten" in _texts(posten_env.send)[-1]


@pytest.mark.parametrize("secret", [
    "Bearer abc123.def-456",
    "https://x/y?access_token=EAAsecretvalue123&b=1",
    "ya29.a0AfH6SMBxyz-_123",
    "1//0gAbCdEfGhIjKlMnOpQrStUv",
    "EAAGm0PX4ZCpsBAKabcdefghij1234",
    "pina_AbCdEfGhIjKlMnOpQrSt1234",
    "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345",
])
def test_stderr_tail_is_redacted_before_sending(posten_env, secret):
    stderr = f"Fehler beim Aufruf {secret} abgelehnt"
    with patch("telegram_listener.subprocess.run", return_value=_done(2, stderr)):
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        _wait_idle(posten_env.listener)
    text = _texts(posten_env.send)[-1]
    assert "geschwärzt" in text
    token = secret.split()[-1].split("access_token=")[-1].split("&")[0]
    assert token not in text
    assert "Fehler beim Aufruf" in text


@pytest.mark.parametrize("name", ["PYTHON", "BEFEHL", "CWD"])
def test_relative_path_means_not_set_up(posten_env, monkeypatch, name):
    monkeypatch.setattr(f"telegram_listener.FREIGABE_POSTEN_{name}", "relativ/pfad")
    with patch("telegram_listener.subprocess.run") as run:
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
    run.assert_not_called()
    assert "Pfad muss absolut sein" in _texts(posten_env.send)[-1]


def test_paths_are_resolved_and_the_resolved_values_are_started(posten_env, monkeypatch):
    unresolved_python = f"{posten_env.cwd}/../python.exe"
    unresolved_cwd = f"{posten_env.cwd}/../wl"
    monkeypatch.setattr("telegram_listener.FREIGABE_POSTEN_PYTHON", unresolved_python)
    monkeypatch.setattr("telegram_listener.FREIGABE_POSTEN_CWD", unresolved_cwd)
    with patch("telegram_listener.subprocess.run", return_value=_done(0)) as run:
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        _wait_idle(posten_env.listener)
    assert ".." not in run.call_args.args[0][0] and ".." not in run.call_args.kwargs["cwd"]
    assert run.call_args.args[0][0] == posten_env.python
    assert run.call_args.kwargs["cwd"] == posten_env.cwd


def test_acknowledgement_comes_only_after_the_worker_thread_started(posten_env):
    order: list[str] = []

    class _FakeThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            order.append("start")

    posten_env.send.side_effect = lambda text: order.append("send")
    with patch("telegram_listener.threading.Thread", _FakeThread):
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
    assert order == ["start", "send"]


def test_thread_start_failure_frees_the_lock_and_reports_without_acknowledgement(posten_env):
    class _BrokenThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    with patch("telegram_listener.threading.Thread", _BrokenThread), \
            patch("telegram_listener.subprocess.run") as run:
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
    run.assert_not_called()
    texts = _texts(posten_env.send)
    assert len(texts) == 1
    assert f"Freigabe {KENNUNG}: Upload konnte nicht gestartet werden (RuntimeError)" in texts[0]
    assert "Upload startet" not in texts[0] and "start new thread" not in texts[0]
    assert posten_env.listener._posten_lock.acquire(blocking=False)
    posten_env.listener._posten_lock.release()


def test_safety_rules_for_non_claude_providers_name_the_posten_rule():
    assert "freigabe" in SAFETY_RULES and "--probelauf" in SAFETY_RULES
    assert "wl_socialmedia.posten" in SAFETY_RULES and "posten/befehl.py freigabe" in SAFETY_RULES
    assert "Telegram command" in SAFETY_RULES
    assert "--probelauf" in SYSTEM_PROMPTS["opencode"]


def test_help_lists_the_posten_command(posten_env):
    posten_env.listener._handle_message(_msg("/help"))
    text = _texts(posten_env.send)[-1]
    assert "/posten" in text and "Kennung" in text and "plattform" in text


def test_result_message_never_arrives_before_the_acknowledgement(posten_env):
    with patch("telegram_listener.subprocess.run", return_value=_done(2, "boom")):
        posten_env.listener._handle_message(_msg(f"/posten_{KENNUNG}"))
        _wait_idle(posten_env.listener)
    texts = _texts(posten_env.send)
    assert "Upload startet" in texts[0] and "Exit 2" in texts[1]
