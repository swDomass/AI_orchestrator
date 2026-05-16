"""Tests for the slash command catalog in telegram_listener.py (#32)."""

from unittest.mock import patch
import threading

import pytest

import idempotency
from telegram_listener import TelegramListener

TEST_CHAT_ID = "99999999"


@pytest.fixture(autouse=True)
def isolated_idempotency_store(tmp_path):
    idempotency.set_store_path(tmp_path / "idempotency.jsonl")
    idempotency.reset_for_tests()
    yield
    idempotency.reset_for_tests()


def _make_listener() -> TelegramListener:
    return TelegramListener(threading.Event())


def _msg(text: str, *, chat_id: str = TEST_CHAT_ID, message_id: int = 1) -> dict:
    return {"chat": {"id": int(chat_id)}, "text": text, "message_id": message_id}


# ---------------------------------------------------------------------------
# /review — path is the cwd
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_review_with_explicit_path_queues_review_loop(
    mock_append, mock_send, tmp_path, monkeypatch
):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])

    listener = _make_listener()
    listener._handle_message(_msg(f"/review {project}"))

    mock_append.assert_called_once()
    line = mock_append.call_args[0][0]
    assert "#tool:review-loop" in line
    assert f"cwd:{project.resolve()}" in line


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_review_invalid_path_rejected(mock_append, mock_send, tmp_path, monkeypatch):
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])
    listener = _make_listener()
    listener._handle_message(_msg(f"/review {tmp_path / 'does_not_exist'}"))

    mock_append.assert_not_called()
    text = mock_send.call_args[0][0]
    assert "ungültig" in text or "ALLOWED" in text


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_review_without_args_and_no_last_cwd_rejects(mock_append, mock_send):
    listener = _make_listener()
    listener._handle_message(_msg("/review"))
    mock_append.assert_not_called()
    text = mock_send.call_args[0][0]
    assert "last-cwd" in text or "pfad" in text.lower()


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_review_then_security_uses_last_cwd(
    mock_append, mock_send, tmp_path, monkeypatch
):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])

    listener = _make_listener()
    listener._handle_message(_msg(f"/review {project}", message_id=10))
    # Second command without arg — should reuse last cwd
    listener._handle_message(_msg("/security", message_id=11))

    assert mock_append.call_count == 2
    second_line = mock_append.call_args_list[1][0][0]
    assert "#tool:security-audit" in second_line
    assert f"cwd:{project.resolve()}" in second_line


# ---------------------------------------------------------------------------
# /dev — requires explicit cwd: or last-cwd
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_dev_with_explicit_cwd_queues_dev_loop(
    mock_append, mock_send, tmp_path, monkeypatch
):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])

    listener = _make_listener()
    listener._handle_message(_msg(f"/dev refactor parser cwd:{project}"))

    mock_append.assert_called_once()
    line = mock_append.call_args[0][0]
    assert "#tool:dev-loop" in line
    assert "refactor parser" in line
    assert f"cwd:{project.resolve()}" in line


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_dev_without_cwd_and_no_last_cwd_rejects(mock_append, mock_send):
    listener = _make_listener()
    listener._handle_message(_msg("/dev refactor parser"))
    mock_append.assert_not_called()


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_dev_empty_description_rejects(mock_append, mock_send, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])

    listener = _make_listener()
    listener._handle_message(_msg(f"/dev cwd:{project}"))
    # No description → reject
    mock_append.assert_not_called()


# ---------------------------------------------------------------------------
# /critique — plan file
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_critique_with_existing_plan_uses_parent_dir(
    mock_append, mock_send, tmp_path, monkeypatch
):
    project = tmp_path / "proj"
    project.mkdir()
    plan = project / "plan.md"
    plan.write_text("# Plan", encoding="utf-8")
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])

    listener = _make_listener()
    listener._handle_message(_msg(f"/critique {plan}"))

    mock_append.assert_called_once()
    line = mock_append.call_args[0][0]
    assert "#tool:critical-review" in line
    assert str(plan) in line
    assert f"cwd:{project.resolve()}" in line


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_critique_with_missing_plan_rejects(mock_append, mock_send, tmp_path):
    listener = _make_listener()
    listener._handle_message(_msg(f"/critique {tmp_path / 'no-plan.md'}"))
    mock_append.assert_not_called()


# ---------------------------------------------------------------------------
# /brainstorm — uses last cwd
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_brainstorm_uses_last_cwd(mock_append, mock_send, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])

    listener = _make_listener()
    listener._handle_message(_msg(f"/review {project}", message_id=20))
    listener._handle_message(_msg("/brainstorm Logging Konzept", message_id=21))

    assert mock_append.call_count == 2
    line = mock_append.call_args_list[1][0][0]
    assert "#tool:brainstorm" in line
    assert "Logging Konzept" in line


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_brainstorm_without_last_cwd_rejects(mock_append, mock_send):
    listener = _make_listener()
    listener._handle_message(_msg("/brainstorm Topic"))
    mock_append.assert_not_called()


# ---------------------------------------------------------------------------
# Idempotency at append-time
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_same_message_id_dedupes_slash_command(
    mock_append, mock_send, tmp_path, monkeypatch
):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])

    listener = _make_listener()
    listener._handle_message(_msg(f"/review {project}", message_id=42))
    listener._handle_message(_msg(f"/review {project}", message_id=42))

    # Only the first invocation appends; the second is a duplicate.
    assert mock_append.call_count == 1


@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_different_message_ids_not_deduped(mock_append, mock_send, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])

    listener = _make_listener()
    listener._handle_message(_msg(f"/review {project}", message_id=1))
    listener._handle_message(_msg(f"/review {project}", message_id=2))

    assert mock_append.call_count == 2


# ---------------------------------------------------------------------------
# Help mentions the new commands
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
def test_help_lists_new_slash_commands(mock_send):
    listener = _make_listener()
    listener._handle_message(_msg("/help"))
    text = mock_send.call_args[0][0]
    for cmd in ("/review", "/dev", "/security", "/audit", "/critique", "/brainstorm"):
        assert cmd in text, f"Help text missing {cmd}"


# ---------------------------------------------------------------------------
# Append failure surfaced to user
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=False)
def test_append_failure_reports_to_user(mock_append, mock_send, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])

    listener = _make_listener()
    listener._handle_message(_msg(f"/review {project}"))
    mock_append.assert_called_once()
    text = mock_send.call_args[0][0]
    assert "konnte nicht" in text.lower() or "schreibfehler" in text.lower()


# ---------------------------------------------------------------------------
# Rate limit — task_limiter applies
# ---------------------------------------------------------------------------

@patch("telegram_listener.TELEGRAM_CHAT_ID", TEST_CHAT_ID)
@patch("telegram_listener.send_message")
@patch("telegram_listener.append_task", return_value=True)
def test_slash_command_uses_task_rate_limiter(mock_append, mock_send, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr("queue_manager.ALLOWED_CWD_ROOTS", [])

    listener = _make_listener()
    # exhaust the task limiter by filling its internal deque
    for _ in range(10):
        listener._task_limiter.allow()
    # next call should now be denied
    listener._handle_message(_msg(f"/review {project}", message_id=99))

    mock_append.assert_not_called()
    text = mock_send.call_args[0][0]
    assert "warten" in text.lower() or "zu viele" in text.lower()
