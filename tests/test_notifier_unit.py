"""Tests for notifier._escape_markdown() and _truncate() utilities."""

import pytest

from notifier import _escape_markdown, _truncate, _strip_backticks


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
