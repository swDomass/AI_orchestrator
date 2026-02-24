"""
Tests for telegram_listener.py

Covers:
  - Security: wrong chat_id is silently ignored
  - Commands: /help, /status, /limits, /pause, /resume
  - AI chat: success, no-provider, run-failure, truncation
  - Semaphore: second chat call while first is running gets busy reply
  - start() is a no-op when TELEGRAM_ENABLED is False
"""

import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest

from telegram_listener import TelegramListener
from providers.base import RunResult
from limits import AllLimits, ProviderLimits
from config import MIN_CAPACITY_PERCENT

# Chat ID used for all tests — patched onto the module so the security check passes
TEST_CHAT_ID = "99999999"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_listener() -> tuple[TelegramListener, threading.Event]:
    pause_event = threading.Event()
    return TelegramListener(pause_event), pause_event


def _msg(text: str, chat_id: str = TEST_CHAT_ID) -> dict:
    """Build a minimal Telegram message dict."""
    return {"chat": {"id": int(chat_id)}, "text": text}


def _fake_limits(
    claude_pct: float = 80.0,
    gemini_pct: float = 70.0,
    codex_pct: float = 60.0,
) -> AllLimits:
    return AllLimits(
        claude=ProviderLimits(available=claude_pct > MIN_CAPACITY_PERCENT, remaining_pct=claude_pct, resets_in_sec=3600),
        gemini=ProviderLimits(available=gemini_pct > MIN_CAPACITY_PERCENT, remaining_pct=gemini_pct, resets_in_sec=1800),
        codex=ProviderLimits(available=codex_pct > MIN_CAPACITY_PERCENT, remaining_pct=codex_pct),
    )


def _fake_provider(name: str = "claude", output: str = "Antwort", success: bool = True) -> MagicMock:
    p = MagicMock()
    p.name = name
    p.run.return_value = RunResult(success=success, output=output, error="" if success else output)
    return p


# ---------------------------------------------------------------------------
# Security: chat_id filtering
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
def test_ignores_message_from_wrong_chat_id(mock_send):
    listener, _ = _make_listener()
    listener._handle_message(_msg("hello", chat_id="11111111"))
    mock_send.assert_not_called()


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
def test_accepts_message_from_correct_chat_id(mock_send):
    listener, _ = _make_listener()
    listener._handle_message(_msg("/help"))
    mock_send.assert_called_once()


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
def test_ignores_message_with_no_text(mock_send):
    listener, _ = _make_listener()
    listener._handle_message({"chat": {"id": int(TEST_CHAT_ID)}, "text": ""})
    mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
def test_help_reply_mentions_all_commands(mock_send):
    listener, _ = _make_listener()
    listener._handle_message(_msg("/help"))

    text = mock_send.call_args[0][0]
    for cmd in ("/status", "/limits", "/pause", "/resume", "/help"):
        assert cmd in text, f"Expected '{cmd}' in help text"


# ---------------------------------------------------------------------------
# /task
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task")
def test_task_adds_single_line_task_to_queue(mock_append_task, mock_send):
    listener, _ = _make_listener()
    listener._handle_message(_msg("/task Fixe den Bug im Parser"))

    mock_append_task.assert_called_once_with("Fixe den Bug im Parser")
    sent_texts = [c[0][0] for c in mock_send.call_args_list]
    assert any("hinzugefügt" in t for t in sent_texts)


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task")
def test_task_rejects_multiline_payload(mock_append_task, mock_send):
    listener, _ = _make_listener()
    listener._handle_message(_msg("/task Erste Zeile\nZweite Zeile"))

    mock_append_task.assert_not_called()
    sent_texts = [c[0][0] for c in mock_send.call_args_list]
    assert any("einzeilig" in t for t in sent_texts)


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task")
def test_task_prefix_command_does_not_match_other_commands(mock_append_task, mock_send):
    listener, _ = _make_listener()
    listener._handle_message(_msg("/tasker something"))

    mock_append_task.assert_not_called()
    mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.read_queue", return_value=["task_a", "task_b", "task_c"])
@patch("telegram_listener.get_limits")
def test_status_shows_queue_count(mock_limits, _mock_queue, mock_send):
    mock_limits.return_value = _fake_limits()
    listener, _ = _make_listener()
    listener._handle_message(_msg("/status"))

    text = mock_send.call_args[0][0]
    assert "3" in text          # 3 open tasks


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.read_queue", return_value=[])
@patch("telegram_listener.get_limits")
def test_status_shows_all_three_providers(mock_limits, _mock_queue, mock_send):
    mock_limits.return_value = _fake_limits()
    listener, _ = _make_listener()
    listener._handle_message(_msg("/status"))

    text = mock_send.call_args[0][0]
    for provider in ("claude", "gemini", "codex"):
        assert provider in text


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.read_queue", return_value=[])
@patch("telegram_listener.get_limits")
def test_status_shows_paused_when_paused(mock_limits, _mock_queue, mock_send):
    mock_limits.return_value = _fake_limits()
    listener, pause_event = _make_listener()
    pause_event.set()
    listener._handle_message(_msg("/status"))

    text = mock_send.call_args[0][0].upper()
    assert "PAUSIERT" in text or "PAUSE" in text


# ---------------------------------------------------------------------------
# /limits
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.get_limits")
def test_limits_shows_percentage_for_available_providers(mock_limits, mock_send):
    mock_limits.return_value = AllLimits(
        claude=ProviderLimits(available=True, remaining_pct=73.5, resets_in_sec=3600),
        gemini=ProviderLimits(available=True, remaining_pct=55.0),
        codex=ProviderLimits(available=False, error="exhausted"),
    )
    listener, _ = _make_listener()
    listener._handle_message(_msg("/limits"))

    text = mock_send.call_args[0][0]
    assert "73.5" in text
    assert "55.0" in text
    assert "exhausted" in text


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.get_limits")
def test_limits_shows_all_providers(mock_limits, mock_send):
    mock_limits.return_value = _fake_limits()
    listener, _ = _make_listener()
    listener._handle_message(_msg("/limits"))

    text = mock_send.call_args[0][0]
    for provider in ("claude", "gemini", "codex"):
        assert provider in text


# ---------------------------------------------------------------------------
# /pause  &  /resume
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
def test_pause_sets_pause_event(mock_send):
    listener, pause_event = _make_listener()
    assert not pause_event.is_set()

    listener._handle_message(_msg("/pause"))

    assert pause_event.is_set()


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
def test_pause_reply_contains_confirmation(mock_send):
    listener, _ = _make_listener()
    listener._handle_message(_msg("/pause"))

    text = mock_send.call_args[0][0].lower()
    assert "pausiert" in text or "pause" in text


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
def test_resume_clears_pause_event(mock_send):
    listener, pause_event = _make_listener()
    pause_event.set()

    listener._handle_message(_msg("/resume"))

    assert not pause_event.is_set()


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
def test_resume_reply_contains_confirmation(mock_send):
    listener, pause_event = _make_listener()
    pause_event.set()
    listener._handle_message(_msg("/resume"))

    text = mock_send.call_args[0][0].lower()
    assert "wieder" in text or "läuft" in text or "resume" in text


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
def test_pause_resume_cycle_toggles_event_correctly(mock_send):
    listener, pause_event = _make_listener()

    listener._handle_message(_msg("/pause"))
    assert pause_event.is_set()

    listener._handle_message(_msg("/resume"))
    assert not pause_event.is_set()


# ---------------------------------------------------------------------------
# AI chat — happy path
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.get_limits")
@patch("telegram_listener.select_provider")
def test_chat_calls_provider_run_with_user_text(mock_select, mock_limits, mock_send):
    provider = _fake_provider(output="42")
    mock_select.return_value = provider
    mock_limits.return_value = _fake_limits()

    listener, _ = _make_listener()
    listener._handle_chat("Was ist 6 mal 7?")

    provider.run.assert_called_once()
    call_args = provider.run.call_args
    assert "6 mal 7" in call_args[0][0] or "6 mal 7" in str(call_args)


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.get_limits")
@patch("telegram_listener.select_provider")
def test_chat_success_sends_provider_output(mock_select, mock_limits, mock_send):
    provider = _fake_provider(name="gemini", output="Die Antwort ist 42")
    mock_select.return_value = provider
    mock_limits.return_value = _fake_limits()

    listener, _ = _make_listener()
    listener._handle_chat("irgendwas")

    # The final send_message call must contain the provider name and the output
    last_text = mock_send.call_args[0][0]
    assert "42" in last_text
    assert "gemini" in last_text


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.get_limits")
@patch("telegram_listener.select_provider")
def test_chat_uses_telegram_chat_timeout(mock_select, mock_limits, mock_send):
    """provider.run must be called with the configured chat timeout, not the queue timeout."""
    from config import TELEGRAM_CHAT_TIMEOUT_SEC

    provider = _fake_provider()
    mock_select.return_value = provider
    mock_limits.return_value = _fake_limits()

    listener, _ = _make_listener()
    listener._handle_chat("test")

    _, kwargs = provider.run.call_args
    assert kwargs.get("timeout") == TELEGRAM_CHAT_TIMEOUT_SEC


# ---------------------------------------------------------------------------
# AI chat — error paths
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.get_limits")
@patch("telegram_listener.select_provider", return_value=None)
def test_chat_no_provider_sends_error_reply(mock_select, mock_limits, mock_send):
    mock_limits.return_value = _fake_limits(claude_pct=0, gemini_pct=0, codex_pct=0)

    listener, _ = _make_listener()
    listener._handle_chat("Irgendetwas")

    text = mock_send.call_args[0][0]
    assert "❌" in text


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.get_limits")
@patch("telegram_listener.select_provider")
def test_chat_provider_run_failure_sends_error_reply(mock_select, mock_limits, mock_send):
    provider = _fake_provider(success=False, output="rate_limit")
    mock_select.side_effect = [provider, None]
    mock_limits.return_value = _fake_limits()

    listener, _ = _make_listener()
    listener._handle_chat("test")

    last_text = mock_send.call_args[0][0]
    assert "❌" in last_text


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.get_limits")
@patch("telegram_listener.select_provider")
def test_chat_defensive_guard_stops_repeated_provider_selection(mock_select, mock_limits, mock_send):
    """
    If select_provider ignores the exclude set and returns the same provider again,
    _handle_chat must abort instead of looping forever.
    """
    provider = _fake_provider(name="claude", success=False, output="boom")
    mock_select.side_effect = [provider, provider]
    mock_limits.return_value = _fake_limits()

    listener, _ = _make_listener()
    listener._handle_chat("test")

    texts = [c[0][0] for c in mock_send.call_args_list]
    assert any("wiederholt" in t for t in texts), f"Expected defensive guard message, got: {texts}"


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.get_limits")
@patch("telegram_listener.select_provider")
def test_chat_exception_in_provider_sends_error_reply(mock_select, mock_limits, mock_send):
    provider = MagicMock()
    provider.name = "claude"
    provider.run.side_effect = RuntimeError("unexpected crash")
    mock_select.return_value = provider
    mock_limits.return_value = _fake_limits()

    listener, _ = _make_listener()
    listener._handle_chat("test")

    last_text = mock_send.call_args[0][0]
    assert "❌" in last_text


# ---------------------------------------------------------------------------
# AI chat — output truncation
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.get_limits")
@patch("telegram_listener.select_provider")
def test_chat_long_response_is_truncated_to_4000_chars(mock_select, mock_limits, mock_send):
    long_output = "x" * 5000
    provider = _fake_provider(output=long_output)
    mock_select.return_value = provider
    mock_limits.return_value = _fake_limits()

    listener, _ = _make_listener()
    listener._handle_chat("Schreib etwas sehr Langes")

    last_text = mock_send.call_args[0][0]
    # 4000-char output + short prefix stays well within Telegram's 4096 limit
    assert len(last_text) <= 4096


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.get_limits")
@patch("telegram_listener.select_provider")
def test_chat_short_response_is_not_truncated(mock_select, mock_limits, mock_send):
    short_output = "kurze Antwort"
    provider = _fake_provider(output=short_output)
    mock_select.return_value = provider
    mock_limits.return_value = _fake_limits()

    listener, _ = _make_listener()
    listener._handle_chat("was?")

    last_text = mock_send.call_args[0][0]
    assert short_output in last_text


# ---------------------------------------------------------------------------
# Semaphore: concurrent chat requests
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
def test_second_chat_while_first_running_gets_busy_reply(mock_send):
    """
    Hold the semaphore before calling _handle_chat to simulate a running call.
    The second call must immediately reply with a 'busy' message and return.
    """
    listener, _ = _make_listener()

    acquired = listener._chat_sem.acquire(blocking=False)
    assert acquired, "Semaphore should be free initially"

    try:
        listener._handle_chat("zweite Nachricht während erste läuft")
    finally:
        listener._chat_sem.release()

    texts = [c[0][0] for c in mock_send.call_args_list]
    assert any(
        "läuft" in t or "warten" in t.lower() for t in texts
    ), f"Expected busy reply, got: {texts}"


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
def test_semaphore_is_released_after_successful_chat(mock_send):
    """After _handle_chat completes, the semaphore must be available again."""
    with (
        patch("telegram_listener.select_provider", return_value=_fake_provider()),
        patch("telegram_listener.get_limits", return_value=_fake_limits()),
    ):
        listener, _ = _make_listener()
        listener._handle_chat("test")

    # Semaphore should be free now
    acquired = listener._chat_sem.acquire(blocking=False)
    assert acquired, "Semaphore was not released after successful chat"
    listener._chat_sem.release()


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
def test_semaphore_is_released_after_failed_chat(mock_send):
    """Semaphore must also be released when the chat call hits an error."""
    with (
        patch("telegram_listener.select_provider", return_value=None),
        patch("telegram_listener.get_limits", return_value=_fake_limits()),
    ):
        listener, _ = _make_listener()
        listener._handle_chat("test")

    acquired = listener._chat_sem.acquire(blocking=False)
    assert acquired, "Semaphore was not released after failed chat"
    listener._chat_sem.release()


# ---------------------------------------------------------------------------
# Plain-text dispatch via _handle_message (integration of routing logic)
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.get_limits")
@patch("telegram_listener.select_provider")
def test_plain_text_routes_to_ai_and_replies(mock_select, mock_limits, mock_send):
    """A non-command message sent through _handle_message must reach the provider."""
    provider = _fake_provider(output="Ja, das stimmt!")
    mock_select.return_value = provider
    mock_limits.return_value = _fake_limits()

    listener, _ = _make_listener()
    listener._handle_message(_msg("Ist Python eine Programmiersprache?"))

    # The worker thread is a daemon; give it a moment to finish
    time.sleep(0.3)

    provider.run.assert_called_once()
    texts = [c[0][0] for c in mock_send.call_args_list]
    assert any("stimmt" in t for t in texts)


# ---------------------------------------------------------------------------
# start() behaviour when Telegram is disabled
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_ENABLED", False)
def test_start_is_noop_when_telegram_disabled():
    listener, _ = _make_listener()
    listener.start()
    assert not listener._thread.is_alive()
