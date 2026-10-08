"""Tests for notifier._escape_markdown() and _truncate() utilities, and notify_error()'s
delivery result."""

import pytest

import notifier
from notifier import _escape_markdown, _strip_backticks, _truncate

# ── _escape_markdown ─────────────────────────────────────────────────────────

def test_escape_markdown_backslash():
    assert _escape_markdown("a\\b") == "a\\\\b"


def test_escape_markdown_asterisk():
    assert _escape_markdown("*bold*") == "\\*bold\\*"


def test_escape_markdown_underscore():
    assert _escape_markdown("_italic_") == "\\_italic\\_"


def test_escape_markdown_backtick():
    assert _escape_markdown("`code`") == "\\`code\\`"


def test_escape_markdown_brackets():
    assert _escape_markdown("[link](url)") == "\\[link\\]\\(url\\)"


def test_escape_markdown_all_control_chars():
    text = "\\_*`[]()"
    escaped = _escape_markdown(text)
    for ch in "\\_*`[]()":
        assert f"\\{ch}" in escaped


def test_escape_markdown_plain_text_unchanged():
    assert _escape_markdown("hello world 123") == "hello world 123"


# ── _truncate ────────────────────────────────────────────────────────────────

def test_truncate_short_text_unchanged():
    assert _truncate("hello", 100) == "hello"


def test_truncate_long_text_gets_ellipsis():
    result = _truncate("a" * 200, 50)
    assert result.endswith("...")
    assert len(result.encode("utf-8")) <= 55  # 50 + "..."


def test_truncate_byte_aware_with_umlauts():
    # ä is 2 bytes in UTF-8, so 50 ä chars = 100 bytes
    text = "ä" * 50
    result = _truncate(text, 80)  # 80 bytes < 100 bytes
    assert len(result.encode("utf-8")) <= 85  # 80 + "..."


def test_truncate_byte_aware_with_emoji():
    # 🎉 is 4 bytes in UTF-8
    text = "🎉" * 20  # 80 bytes
    result = _truncate(text, 40)
    assert len(result.encode("utf-8")) <= 45  # 40 + "..."


def test_truncate_default_limit():
    text = "x" * 4000
    result = _truncate(text)
    assert len(result.encode("utf-8")) <= 3505  # 3500 + "..."


# ── _strip_backticks ─────────────────────────────────────────────────────────

def test_strip_backticks_replaces_with_single_quotes():
    assert _strip_backticks("`code`") == "'code'"


# ── Disabled Telegram ────────────────────────────────────────────────────────

def test_send_returns_false_when_disabled(monkeypatch):
    monkeypatch.setattr("notifier.TELEGRAM_ENABLED", False)
    from notifier import _send
    assert _send("test message") is False


def test_send_returns_false_without_token(monkeypatch):
    monkeypatch.setattr("notifier.TELEGRAM_ENABLED", True)
    monkeypatch.setattr("notifier.TELEGRAM_BOT_TOKEN", "")
    from notifier import _send
    assert _send("test message") is False


# ── notify_error reports delivery (2026-10-08, Korrekturrunde 1 zu PR #6) ────
# The policy-hold throttle in orchestrator.py records an alert only when this is
# True; it used to return None, so a lost alert silenced its cause for 6 h.


@pytest.mark.parametrize("enabled, send_result, expected", [
    (True, True, True),
    (True, False, False),       # network error / Telegram 5xx/429 / timeout
    (True, None, False),        # anything but True is not a delivery
    (False, True, False),       # NOTIFY_ON_ERROR off: nothing sent
], ids=["delivered", "lost", "non_bool", "notifications_off"])
def test_notify_error_returns_whether_the_message_was_delivered(
    monkeypatch, enabled, send_result, expected,
):
    sent: list[str] = []
    monkeypatch.setattr(notifier, "NOTIFY_ON_ERROR", enabled)
    monkeypatch.setattr(notifier, "_send", lambda text: sent.append(text) or send_result)

    result = notifier.notify_error("Task", "policy", "boom")

    assert result is expected
    assert len(sent) == (1 if enabled else 0)


def test_notify_error_without_telegram_configured_is_false(monkeypatch):
    """The real _send, Telegram switched off: no network, a plain False."""
    monkeypatch.setattr(notifier, "NOTIFY_ON_ERROR", True)
    monkeypatch.setattr(notifier, "TELEGRAM_ENABLED", False)

    assert notifier.notify_error("Task", "policy", "boom") is False
