from unittest.mock import patch

import pytest


with patch("config._load_dotenv"):
    import queue_manager


@pytest.fixture
def mock_queue_file(tmp_path):
    q_file = tmp_path / "agent-queue.md"
    with patch("queue_manager.QUEUE_FILE", q_file):
        yield q_file


def test_extract_cwd_supports_spaces(tmp_path, monkeypatch):
    project_dir = tmp_path / "My Project"
    project_dir.mkdir()
    monkeypatch.setattr(queue_manager, "ALLOWED_CWD_ROOTS", [])

    task = f"Fix bug cwd:{project_dir} #tool:test-loop #timeout:5m"

    assert queue_manager.extract_cwd(task) == str(project_dir)


def test_extract_cwd_supports_quoted_spaces(tmp_path, monkeypatch):
    project_dir = tmp_path / "Quoted Project"
    project_dir.mkdir()
    monkeypatch.setattr(queue_manager, "ALLOWED_CWD_ROOTS", [])

    task = f'Fix bug cwd:"{project_dir}" #codex'

    assert queue_manager.extract_cwd(task) == str(project_dir)


def test_has_cwd_tag_detects_malformed_tag():
    assert queue_manager.has_cwd_tag("Run task cwd: #codex") is True


def test_has_cwd_tag_ignores_plain_prose():
    task = "Bitte erklaere cwd: semantics im Queue-Format"
    assert queue_manager.has_cwd_tag(task) is False
    assert queue_manager.extract_cwd(task) is None


def test_extract_cwd_stops_before_non_metadata_hashtag(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setattr(queue_manager, "ALLOWED_CWD_ROOTS", [])

    task = f"Run task cwd:{project_dir} #123"

    assert queue_manager.extract_cwd(task) == str(project_dir)


def test_malformed_timeout_suffix_is_rejected_without_partial_strip():
    task = "Run checks #timeout:10ms #codex"

    assert queue_manager.extract_timeout(task, default=17) == 17
    assert queue_manager.strip_metadata_tags(task) == "Run checks #timeout:10ms"


def test_mark_done_handles_backslashes_in_task_text(mock_queue_file):
    task = r"Fix path handling in C:\proj\file.py"
    mock_queue_file.write_text(f"## Queue\n- [ ] {task}\n", encoding="utf-8")

    assert queue_manager.mark_done(task, "codex") is True

    content = mock_queue_file.read_text(encoding="utf-8")
    assert "- [x]" in content
    assert task in content


def test_mark_retry_handles_backslashes_in_task_text(mock_queue_file):
    task = r"Retry task for C:\proj\file.py"
    mock_queue_file.write_text(f"## Queue\n- [ ] {task}\n", encoding="utf-8")

    assert queue_manager.mark_retry(task, "12:34") is True

    content = mock_queue_file.read_text(encoding="utf-8")
    assert f"- [ ] {task} <!-- retry: 12:34 -->" in content


def test_append_log_uses_real_log_section_when_result_contains_log_heading(mock_queue_file):
    mock_queue_file.write_text(
        "## Queue\n"
        "- [ ] Task A\n\n"
        "## Ergebnisse\n"
        "Provider output line\n"
        "## Log\n"
        "still provider output\n\n"
        "## Log\n",
        encoding="utf-8",
    )

    queue_manager.append_log("test-entry")

    content = mock_queue_file.read_text(encoding="utf-8")
    assert "Provider output line\n## Log\nstill provider output" in content
    tail = content.split("\n## Log\n")[-1]
    assert "<!-- " in tail
    assert "test-entry" in tail


def test_append_task_fallback_inserts_before_real_results_heading_only(mock_queue_file):
    mock_queue_file.write_text(
        "Intro mentions ## Ergebnisse inline but is not a heading.\n\n"
        "## Ergebnisse\n"
        "existing result\n\n"
        "## Log\n",
        encoding="utf-8",
    )

    assert queue_manager.append_task("Neue Aufgabe") is True

    content = mock_queue_file.read_text(encoding="utf-8")
    assert "Intro mentions ## Ergebnisse inline but is not a heading." in content
    assert "## Queue\n- [ ] Neue Aufgabe\n\n## Ergebnisse" in content


def test_mark_done_uses_line_identity_for_duplicate_task_texts(mock_queue_file, monkeypatch):
    task = "Duplicate task"
    mock_queue_file.write_text(
        "## Queue\n"
        f"- [ ] {task} <!-- retry: 23:59 -->\n"
        f"- [ ] {task}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(queue_manager, "_retry_is_due", lambda *_args, **_kwargs: False)

    items = queue_manager.read_queue_items()

    assert len(items) == 1
    assert items[0].task_text == task
    assert items[0].line_no == 3
    assert queue_manager.mark_done(task, "codex", line_no=items[0].line_no) is True

    content = mock_queue_file.read_text(encoding="utf-8")
    assert f"- [ ] {task} <!-- retry: 23:59 -->" in content
    assert content.count(f"- [x] {task} ✅") == 1


def test_mark_done_resyncs_when_line_number_shifts_after_prepend(mock_queue_file):
    mock_queue_file.write_text(
        "## Queue\n"
        "- [ ] First task\n"
        "- [ ] Target task\n\n"
        "## Ergebnisse\n"
        "## Log\n",
        encoding="utf-8",
    )

    items = queue_manager.read_queue_items()
    target = next(item for item in items if item.task_text == "Target task")
    assert target.line_no == 3

    # Simulate concurrent Telegram /task prepend shifting all existing queue lines down by one.
    assert queue_manager.append_task("Concurrent task") is True

    assert queue_manager.mark_done("Target task", "codex", line_no=target.line_no) is True

    content = mock_queue_file.read_text(encoding="utf-8")
    assert "- [ ] Concurrent task" in content
    assert content.count("- [x] Target task ✅") == 1


def test_apply_update_retries_after_transient_lock_contention(mock_queue_file, monkeypatch):
    attempts = {"count": 0}

    def flaky_lock(_file_obj):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise BlockingIOError("busy")

    monkeypatch.setattr(queue_manager, "_lock_file", flaky_lock)
    monkeypatch.setattr(queue_manager, "_unlock_file", lambda _file_obj: None)
    monkeypatch.setattr(queue_manager, "_QUEUE_UPDATE_LOCK_RETRY_DELAY_SEC", 0)

    updated = queue_manager._apply_update(lambda _content: "## Queue\n- [ ] Retry-safe update\n")

    assert updated is True
    assert attempts["count"] == 2
    assert "Retry-safe update" in mock_queue_file.read_text(encoding="utf-8")


def test_resolve_note_revalidates_rglob_fallback_match(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "nested").mkdir(parents=True)
    fallback = vault / "nested" / "Target Note.md"
    fallback.write_text("secret", encoding="utf-8")

    monkeypatch.setattr(queue_manager, "VAULT_PATH", vault)
    monkeypatch.setattr(queue_manager, "_is_within_vault", lambda _path: False)

    assert queue_manager._resolve_note("some/missing/path/Target Note") is None
