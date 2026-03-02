from unittest.mock import patch

import pytest


with patch("config._load_dotenv"):
    import queue_manager


@pytest.fixture
def mock_queue_file(tmp_path):
    q_file = tmp_path / "agent-queue.md"
    with patch("queue_manager.QUEUE_FILE", q_file):
        yield q_file


def test_extract_cwd_allows_space_after_colon(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setattr(queue_manager, "ALLOWED_CWD_ROOTS", [])

    task = f"Fix bug cwd: {project_dir} #tool:test-loop"

    assert queue_manager.extract_cwd(task) == str(project_dir)


def test_extract_cwd_converts_git_bash_path(tmp_path, monkeypatch):
    """On Windows, /d/foo/bar style paths should be converted to D:\\foo\\bar."""
    import sys
    if sys.platform != "win32":
        pytest.skip("Git Bash path conversion only applies on Windows")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    # Build the Git Bash equivalent of project_dir
    win_path = str(project_dir)
    drive_letter = win_path[0].lower()
    bash_path = "/" + drive_letter + win_path[2:].replace("\\", "/")

    monkeypatch.setattr(queue_manager, "ALLOWED_CWD_ROOTS", [])
    task = f"Review code cwd:{bash_path} #tool:review-loop"

    assert queue_manager.extract_cwd(task) == str(project_dir.resolve())


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
    # "cwd:" followed by a word mid-sentence is detected by the regex
    # but extract_cwd returns None since it's not a real directory.
    # has_cwd_tag is conservative — it flags ambiguous cases so the
    # orchestrator can reject rather than run in the wrong directory.
    task = "Bitte erklaere cwd: semantics im Queue-Format"
    assert queue_manager.extract_cwd(task) is None
    # Pure prose without any path-like token after cwd: is still not a tag
    task2 = "Erklaere was cwd bedeutet"
    assert queue_manager.has_cwd_tag(task2) is False


def test_extract_model_tag_accepts_trailing_punctuation():
    assert queue_manager.extract_model_tag("Fix bug #claude_haiku.") == "claude_haiku"


def test_extract_model_tag_rejects_suffix_word_characters():
    assert queue_manager.extract_model_tag("Fix bug #claude_haiku_extra") is None


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


def test_filepath_re_captures_quoted_spaced_filename():
    """FILEPATH_RE must extract quoted multi-word filenames like '"Bremsenquitschen Suite.md"'."""
    text = 'Analysiere "Bremsenquitschen Suite.md" und erstelle einen Report.'
    matches = [(m.group(2) or m.group(3)).strip() for m in queue_manager.FILEPATH_RE.finditer(text)]
    assert "Bremsenquitschen Suite.md" in matches


def test_filepath_re_still_matches_simple_filename():
    """FILEPATH_RE must still work for single-word filenames without spaces."""
    text = "Lese README.md bitte."
    matches = [(m.group(2) or m.group(3)).strip() for m in queue_manager.FILEPATH_RE.finditer(text)]
    assert "README.md" in matches


def test_resolve_note_no_double_md_extension(tmp_path, monkeypatch):
    """_resolve_note must find 'Foo.md' via rglob even when ref already ends in .md."""
    vault = tmp_path / "vault"
    (vault / "nested").mkdir(parents=True)
    note = vault / "nested" / "Bremsenquitschen Suite.md"
    note.write_text("Inhalt", encoding="utf-8")

    monkeypatch.setattr(queue_manager, "VAULT_PATH", vault)
    monkeypatch.setattr(queue_manager, "_is_within_vault", lambda _path: True)

    result = queue_manager._resolve_note("Bremsenquitschen Suite.md")
    assert result == note


def test_inject_file_context_finds_quoted_spaced_filename(tmp_path, monkeypatch):
    """inject_file_context must resolve quoted multi-word filenames like '"Foo Bar.md"'."""
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    note = vault / "notes" / "Bremsenquitschen Suite.md"
    note.write_text("# Quitschen\nDetails hier.", encoding="utf-8")

    monkeypatch.setattr(queue_manager, "VAULT_PATH", vault)
    monkeypatch.setattr(queue_manager, "_is_within_vault", lambda _path: True)

    task = 'Analysiere "Bremsenquitschen Suite.md" und erstelle einen Report.'
    result = queue_manager.inject_file_context(task)
    assert "Quitschen" in result
    assert "Details hier." in result


def test_inject_file_context_finds_wikilink_with_md_extension(tmp_path, monkeypatch):
    """inject_file_context must resolve [[Note.md]] wikilinks that already include .md."""
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    note = vault / "notes" / "Bremsenquitschen Suite.md"
    note.write_text("# Quitschen\nDetails hier.", encoding="utf-8")

    monkeypatch.setattr(queue_manager, "VAULT_PATH", vault)
    monkeypatch.setattr(queue_manager, "_is_within_vault", lambda _path: True)

    task = "Analysiere [[Bremsenquitschen Suite.md]] und erstelle einen Report."
    result = queue_manager.inject_file_context(task)
    assert "Quitschen" in result
    assert "Details hier." in result
