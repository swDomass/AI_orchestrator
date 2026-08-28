from unittest.mock import patch

import pytest


with patch("config._load_dotenv"):
    import queue_healing
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


def test_extract_model_tag_matches_gemini_pro():
    assert queue_manager.extract_model_tag("Review #gemini_pro") == "gemini_pro"


def test_extract_model_tag_matches_gemini_flash():
    assert queue_manager.extract_model_tag("Iterate #gemini_flash") == "gemini_flash"


def test_extract_model_tag_matches_codex_mini():
    assert queue_manager.extract_model_tag("Run #codex_mini") == "codex_mini"


def test_extract_model_tag_provider_only_tags_return_none():
    assert queue_manager.extract_model_tag("Run #gemini now") is None
    assert queue_manager.extract_model_tag("Run #codex now") is None


def test_model_tag_re_covers_every_dispatcher_alias():
    """MODEL_TAG_RE is derived from dispatcher._TAG_MAP so the two cannot drift apart
    again (regression for the gap where it hand-covered only 6 of 20 model aliases —
    gemini_flash_lite, codex_5/_5_4, vibe_medium/_small and all nine or_* aliases routed
    to the right provider but silently ran on that provider's default model)."""
    from dispatcher import _TAG_MAP

    model_aliases = {tag[1:] for tag, provider in _TAG_MAP.items() if tag[1:] != provider}
    assert len(model_aliases) == 20  # guards against a silently shrunk _TAG_MAP too

    for alias in model_aliases:
        assert queue_manager.extract_model_tag(f"Run #{alias} now") == alias


def test_model_tag_re_disambiguates_prefix_aliases():
    """codex_5 is a literal prefix of codex_5_4 — each tag must resolve to itself,
    not get truncated to (or swallowed by) the other."""
    assert queue_manager.extract_model_tag("Run #codex_5 now") == "codex_5"
    assert queue_manager.extract_model_tag("Run #codex_5_4 now") == "codex_5_4"


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


def test_extract_second_opinion_alias_returns_lowercased_value():
    task = "Review changes #tool:review-loop #second_opinion:Or_GLM cwd:/d/proj"
    assert queue_manager.extract_second_opinion_alias(task) == "or_glm"


def test_extract_second_opinion_alias_returns_none_when_absent():
    assert queue_manager.extract_second_opinion_alias("Plain task #tool:review-loop") is None


def test_strip_metadata_tags_removes_second_opinion():
    task = "Review changes #tool:review-loop #second_opinion:or_kimi cwd:/d/proj"
    stripped = queue_manager.strip_metadata_tags(task)
    assert "#second_opinion" not in stripped
    assert "or_kimi" not in stripped
    assert "Review changes" in stripped


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


# ── mark_retry and the queue's only persistent counter ────────────────────────
# `<!-- hang: N -->` is the sole per-task counter the queue carries across polls.
# mark_retry() rebuilds the whole line, so "caller passed no count" MUST mean
# "keep what is there" — it used to mean "erase it", which zeroed the counter on
# every capacity/timeout/strict-mode park.


def test_mark_retry_preserves_an_existing_hang_counter(mock_queue_file):
    """A park without a hang_count must carry the counter forward, not drop it."""
    task = "Task #tool:dev-loop"
    mock_queue_file.write_text(
        f"## Queue\n- [ ] {task} <!-- retry: 2026-01-01 00:00 --> <!-- hang: 2 -->\n",
        encoding="utf-8",
    )

    assert queue_manager.mark_retry(task, "2026-01-01 03:00", line_no=2) is True

    content = mock_queue_file.read_text(encoding="utf-8")
    assert f"- [ ] {task} <!-- retry: 2026-01-01 03:00 --> <!-- hang: 2 -->" in content
    assert queue_manager.extract_hang_count(content) == 2


def test_mark_retry_preserves_the_counter_on_the_lineless_fallback_path(mock_queue_file):
    """Same guarantee without line_no — that path rebuilds the line via regex sub."""
    task = "Task #tool:dev-loop"
    mock_queue_file.write_text(
        f"## Queue\n- [ ] {task} <!-- retry: 2026-01-01 00:00 --> <!-- hang: 1 -->\n",
        encoding="utf-8",
    )

    assert queue_manager.mark_retry(task, "2026-01-01 03:00") is True

    content = mock_queue_file.read_text(encoding="utf-8")
    assert queue_manager.extract_hang_count(content) == 1
    assert "<!-- retry: 2026-01-01 03:00 -->" in content


def test_mark_retry_reads_the_counter_off_the_resolved_line_not_the_stale_one(mock_queue_file):
    """Line numbers shift while a task runs; the counter must follow the task.

    Reading the old count in the CALLER would read it off whatever now sits at the
    remembered line number. Resolution and read have to happen together, which is
    why mark_retry hands _replace_open_task_line a builder instead of a string.
    """
    task = "Target task"
    mock_queue_file.write_text(
        f"## Queue\n- [ ] {task} <!-- retry: 2026-01-01 00:00 --> <!-- hang: 2 -->\n",
        encoding="utf-8",
    )
    items = queue_manager.read_queue_items()
    target = next(i for i in items if i.task_text == task)
    assert target.line_no == 2

    # Concurrent Telegram /task prepend pushes the target down one line; the line
    # the caller remembers now holds a task with NO counter.
    assert queue_manager.append_task("Concurrent task") is True

    assert queue_manager.mark_retry(task, "2026-01-01 03:00", line_no=target.line_no) is True

    content = mock_queue_file.read_text(encoding="utf-8")
    assert f"- [ ] {task} <!-- retry: 2026-01-01 03:00 --> <!-- hang: 2 -->" in content
    assert "- [ ] Concurrent task\n" in content  # untouched, still counter-free


def test_mark_retry_sets_the_counter_when_one_is_passed(mock_queue_file):
    """The hang / format_error paths pass previous+1 and that value must win."""
    task = "Task #tool:dev-loop"
    mock_queue_file.write_text(
        f"## Queue\n- [ ] {task} <!-- retry: 2026-01-01 00:00 --> <!-- hang: 1 -->\n",
        encoding="utf-8",
    )

    assert queue_manager.mark_retry(task, "2026-01-01 03:00", line_no=2, hang_count=2) is True

    content = mock_queue_file.read_text(encoding="utf-8")
    assert queue_manager.extract_hang_count(content) == 2
    assert content.count("<!-- hang:") == 1  # replaced, not appended twice


def test_mark_retry_with_hang_count_zero_clears_the_counter(mock_queue_file):
    """0 stays an explicit reset — 'preserve' is None, not falsy-int."""
    task = "Task #tool:dev-loop"
    mock_queue_file.write_text(
        f"## Queue\n- [ ] {task} <!-- retry: 2026-01-01 00:00 --> <!-- hang: 2 -->\n",
        encoding="utf-8",
    )

    assert queue_manager.mark_retry(task, "2026-01-01 03:00", line_no=2, hang_count=0) is True

    content = mock_queue_file.read_text(encoding="utf-8")
    assert "<!-- hang:" not in content
    assert f"- [ ] {task} <!-- retry: 2026-01-01 03:00 -->" in content


def test_mark_retry_adds_no_counter_when_the_line_never_had_one(mock_queue_file):
    """Preservation must not invent a marker on an untouched task line."""
    task = "Task #tool:dev-loop"
    mock_queue_file.write_text(f"## Queue\n- [ ] {task}\n", encoding="utf-8")

    assert queue_manager.mark_retry(task, "2026-01-01 03:00", line_no=2) is True

    content = mock_queue_file.read_text(encoding="utf-8")
    assert "<!-- hang:" not in content


def test_realign_stale_freshonly_keeps_the_hang_counter(mock_queue_file):
    """The other queue-line rewriter must not drop the marker either.

    `realign_stale_freshonly` moves a missed `#freshonly` slot forward. It edits the
    retry marker in place (`_set_retry_marker`) rather than rebuilding the line, so
    the counter survives — pinned here because "it happens to rebuild differently"
    is not a guarantee, and this is the queue's only persistent state.
    """
    task = "Briefing #tool:dev-loop #at:08:00 #every:24h #freshonly"
    mock_queue_file.write_text(
        f"## Queue\n- [ ] {task} <!-- retry: 2020-01-01 08:00 --> <!-- hang: 2 -->\n",
        encoding="utf-8",
    )

    assert queue_manager.realign_stale_freshonly() == 1

    content = mock_queue_file.read_text(encoding="utf-8")
    assert queue_manager.extract_hang_count(content) == 2, content
    assert "2020-01-01" not in content  # the slot really did move


def test_queue_healing_drop_keeps_the_line_comments(mock_queue_file):
    """queue_healing edits only the status box, so markers ride along."""
    task = "Task #tool:dev-loop #id:t1"
    mock_queue_file.write_text(
        f"## Queue\n- [ ] {task} <!-- retry: 2026-01-01 00:00 --> <!-- hang: 2 -->\n",
        encoding="utf-8",
    )

    ok, _msg = queue_healing.apply_drop("t1")

    content = mock_queue_file.read_text(encoding="utf-8")
    assert ok is True
    assert "- [-]" in content            # dropped...
    assert "<!-- hang: 2 -->" in content  # ...without losing the counter


def test_finalize_resets_the_counter_for_a_recurring_task(mock_queue_file):
    """A successful run clears the counter — the requeue of an #every: task is a
    fresh start, not a continuation of earlier dead attempts."""
    task = "Briefing #tool:dev-loop #every:24h"
    mock_queue_file.write_text(
        f"## Queue\n- [ ] {task} <!-- retry: 2026-01-01 00:00 --> <!-- hang: 2 -->\n",
        encoding="utf-8",
    )

    assert queue_manager.finalize_task_with_result(task, "ok", "claude", line_no=2) is True

    content = mock_queue_file.read_text(encoding="utf-8")
    assert "<!-- hang:" not in content
    assert f"- [ ] {task} <!-- retry:" in content  # #every: keeps it open


def test_append_log_writes_to_events_log_not_queue_md(mock_queue_file, tmp_path, monkeypatch):
    """append_log now writes to queue-events.log, not the queue MD file."""
    events_log = tmp_path / "queue-events.log"
    monkeypatch.setattr("queue_manager.QUEUE_EVENTS_LOG_FILE", events_log)
    queue_manager._events_log_cleanup_last_date = None

    original_content = (
        "## Queue\n"
        "- [ ] Task A\n\n"
        "## Ergebnisse\n"
        "Provider output line\n"
        "## Log\n"
        "still provider output\n\n"
        "## Log\n"
    )
    mock_queue_file.write_text(original_content, encoding="utf-8")

    queue_manager.append_log("test-entry")

    # Queue MD must be unchanged
    assert mock_queue_file.read_text(encoding="utf-8") == original_content
    # Event logged to events file
    assert "test-entry" in events_log.read_text(encoding="utf-8")


def test_append_task_fallback_appends_queue_section_at_end(mock_queue_file):
    """When no ## Queue section exists, append_task creates one at end of file."""
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
    assert "## Queue\n- [ ] Neue Aufgabe" in content


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


def test_collect_file_context_finds_quoted_spaced_filename(tmp_path, monkeypatch):
    """collect_file_context must resolve quoted multi-word filenames like '"Foo Bar.md"'."""
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    note = vault / "notes" / "Bremsenquitschen Suite.md"
    note.write_text("# Quitschen\nDetails hier.", encoding="utf-8")

    monkeypatch.setattr(queue_manager, "VAULT_PATH", vault)
    monkeypatch.setattr(queue_manager, "_is_within_vault", lambda _path: True)

    task = 'Analysiere "Bremsenquitschen Suite.md" und erstelle einen Report.'
    result = queue_manager.collect_file_context(task)
    assert "Quitschen" in result
    assert "Details hier." in result


def test_collect_file_context_finds_wikilink_with_md_extension(tmp_path, monkeypatch):
    """collect_file_context must resolve [[Note.md]] wikilinks that already include .md."""
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    note = vault / "notes" / "Bremsenquitschen Suite.md"
    note.write_text("# Quitschen\nDetails hier.", encoding="utf-8")

    monkeypatch.setattr(queue_manager, "VAULT_PATH", vault)
    monkeypatch.setattr(queue_manager, "_is_within_vault", lambda _path: True)

    task = "Analysiere [[Bremsenquitschen Suite.md]] und erstelle einen Report."
    result = queue_manager.collect_file_context(task)
    assert "Quitschen" in result
    assert "Details hier." in result


# --- collect_file_context: blocks WITHOUT the task text (2026-07-25) -------
#
# The prompt builder needs the file blocks separately so it can place the
# instruction last. The former inject_file_context() wrapper ("task + blocks")
# was removed with its last caller — it was exactly the coupling that buried the
# task in the middle of the prompt.

def _vault_with_note(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    (vault / "notes" / "Thema.md").write_text("# Thema\nInhalt hier.", encoding="utf-8")
    monkeypatch.setattr(queue_manager, "VAULT_PATH", vault)
    monkeypatch.setattr(queue_manager, "_is_within_vault", lambda _path: True)


def test_collect_file_context_excludes_the_task_text(tmp_path, monkeypatch):
    _vault_with_note(tmp_path, monkeypatch)
    task = "Analysiere [[Thema]] gruendlich."
    blocks = queue_manager.collect_file_context(task)
    assert "Inhalt hier." in blocks
    assert "Analysiere" not in blocks, "task text must not ride along in the context block"


def test_collect_file_context_returns_empty_without_refs():
    assert queue_manager.collect_file_context("Ein Task ohne Referenzen.") == ""


# --- #verify: tag -----------------------------------------------------------

def test_extract_verify_tag_reads_plain_path():
    task = "Mach was #verify:scripts\\check.ps1 #claude_sonnet"
    assert queue_manager.extract_verify_tag(task) == "scripts\\check.ps1"


def test_extract_verify_tag_reads_quoted_path_with_spaces():
    task = 'Mach was #verify:"C:\\My Scripts\\check.ps1" #every:24h'
    assert queue_manager.extract_verify_tag(task) == "C:\\My Scripts\\check.ps1"


def test_extract_verify_tag_absent_returns_none():
    assert queue_manager.extract_verify_tag("Mach was #claude_sonnet") is None


def test_extract_verify_tag_stops_at_hash():
    """An unbounded (\\S+) pulled adjoining tag text into the script path.

    The path then never resolves ("Skript nicht gefunden"), and because the check is
    fail-closed that means a permanent alarm on every SUCCESSFUL run. Note the glued
    `#every:` is unusable regardless — every tag regex here has a `(?<!\\S)` lookbehind,
    so a tag without a leading space is never matched. This bound keeps the damage out
    of the path; it does not rescue the malformed tag.
    """
    assert queue_manager.extract_verify_tag("Task #verify:c.ps1#every:24h") == "c.ps1"


def test_verify_tag_leaves_properly_spaced_neighbours_intact():
    """The realistic layout: space-separated tags all survive side by side."""
    task = "Task #verify:c.ps1 #every:24h #claude_sonnet"
    assert queue_manager.extract_verify_tag(task) == "c.ps1"
    assert queue_manager.EVERY_TAG_RE.search(task) is not None
    assert queue_manager.extract_model_tag(task) == "claude_sonnet"


def test_extract_verify_tag_does_not_swallow_following_cwd():
    """Unquoted paths stop at whitespace — a trailing cwd: must stay intact."""
    task = "Mach was #verify:check.ps1 cwd:D:\\Ordner mit Leerzeichen #every:24h"
    assert queue_manager.extract_verify_tag(task) == "check.ps1"
    # CWD_RE directly — extract_cwd() would validate the path exists on disk.
    m = queue_manager.CWD_RE.search(task)
    assert (m.group(1) or m.group(2)) == "D:\\Ordner mit Leerzeichen"


def test_pathless_verify_tag_yields_none_but_is_still_stripped():
    """A typo'd `#verify:` disables the check (fail-open) — it must at least not leak
    literal tag text into the prompt. `--lint-queue` flags the fail-open part."""
    assert queue_manager.extract_verify_tag("Task #verify:") is None
    assert queue_manager.strip_metadata_tags("Task #verify:") == "Task"


def test_empty_quoted_verify_path_yields_none_without_crashing():
    """group(1) is '' here — truthiness testing would fall through to a None group(2)."""
    assert queue_manager.extract_verify_tag('Task #verify:""') is None
    assert queue_manager.strip_metadata_tags('Task #verify:""') == "Task"


def test_strip_metadata_tags_removes_verify_tag():
    task = 'Schreibe den Brief #verify:"scripts\\check.ps1" #claude_sonnet'
    stripped = queue_manager.strip_metadata_tags(task)
    assert "#verify" not in stripped
    assert "check.ps1" not in stripped
    assert stripped == "Schreibe den Brief"

# --- collect_file_context: the budget path (2026-08-28) -------------------
#
# Until now NO test passed max_chars, so every one of them ran the unlimited
# branch and the budget arithmetic was never executed. That is precisely how the
# divisor came to count unresolvable refs: measured on the live vault-gardener
# task, 4 refs of which 3 were dead left the one real file with 1875 of 7500
# chars, and 1940 of 7500 were injected at all.


def _vault(tmp_path, monkeypatch):
    """Vault whose notes/ dir is the search root, mirroring the helpers above."""
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    monkeypatch.setattr(queue_manager, "VAULT_PATH", vault)
    monkeypatch.setattr(queue_manager, "_is_within_vault", lambda _path: True)
    return vault


def _write(vault, name, chars):
    """A note of *chars* length in few long lines.

    Long lines on purpose: _extract_relevant_section keeps +/-50 lines around the
    best keyword hit, so a many-short-lines file would be swallowed whole by the
    extractor and never reach the hard-truncation branch this test targets.
    """
    zeile = "Inhalt " + "x" * 493
    text = "\n".join([zeile] * max(1, chars // 500))
    (vault / "notes" / name).write_text(text, encoding="utf-8")
    return len(text)


def test_budget_divisor_ignores_unresolvable_refs(tmp_path, monkeypatch):
    """The real file must get the whole budget, not a share held back for dead refs.

    Regression for the measured vault-gardener case: three of the four refs never
    resolve (two YYYY-MM-DD placeholders, one [[Projekt]] out of prose).
    """
    vault = _vault(tmp_path, monkeypatch)
    _write(vault, "common-fixes.md", 10_000)

    task = (
        "Report nach 99_System/reports/YYYY-MM-DD_health-validate.md schreiben, "
        "Scan aus common-fixes.md nutzen, Telegram siehe [[YYYY-MM-DD_health-validate]], "
        "Tasks ohne [[Projekt]]-Link pruefen."
    )
    result = queue_manager.collect_file_context(task, max_chars=7500)

    assert "...[truncated]" in result, "the file is larger than the budget, so it must be cut"
    # Under the old split (7500 // 4 refs) this was ~1875 chars plus frame.
    assert len(result) > 5000, (
        f"only {len(result)} chars injected - the divisor is still counting dead refs"
    )
    # Strict: frame, marker and joins are reserved inside the shares, so the
    # ceiling holds exactly. HEAD overshot it by +81/+87/+97/+111 chars at
    # n=2/5/10/20; that overhead is gone rather than merely tolerated.
    assert len(result) <= 7500, "the budget ceiling must hold exactly"


def test_unused_budget_rolls_over_to_the_next_file(tmp_path, monkeypatch):
    """A small file hands its unused share on instead of letting it expire."""
    vault = _vault(tmp_path, monkeypatch)
    _write(vault, "klein.md", 500)
    _write(vault, "gross.md", 10_000)

    result = queue_manager.collect_file_context(
        "Lies klein.md und gross.md", max_chars=4000
    )

    gross_block = result.split("--- Inhalt von 'gross.md' ---")[1]
    # A static split would have capped gross.md at 4000 // 2 = 2000.
    assert len(gross_block) > 2500, (
        f"gross.md got only {len(gross_block)} chars - the freed budget expired"
    )


def test_oversized_file_is_skipped_and_leaves_the_divisor_alone(tmp_path, monkeypatch):
    """A file above MAX_CONTEXT_FILE_SIZE must not claim a share of the budget."""
    vault = _vault(tmp_path, monkeypatch)
    (vault / "notes" / "riesig.md").write_text(
        "y" * (queue_manager.MAX_CONTEXT_FILE_SIZE + 1), encoding="utf-8"
    )
    _write(vault, "normal.md", 10_000)

    result = queue_manager.collect_file_context(
        "Lies riesig.md und normal.md", max_chars=6000
    )

    assert "riesig.md" not in result
    # Splitting across both would have left normal.md at 3000.
    assert len(result) > 4000, (
        f"normal.md got only {len(result)} chars - the oversized file still counted"
    )


def test_missing_file_leaves_the_divisor_alone(tmp_path, monkeypatch):
    """An unresolvable ref must not shrink the share of the files that do exist."""
    vault = _vault(tmp_path, monkeypatch)
    _write(vault, "da.md", 10_000)

    result = queue_manager.collect_file_context(
        "Lies da.md und gibt-es-nicht.md", max_chars=6000
    )

    assert "gibt-es-nicht" not in result
    assert len(result) > 4000, f"only {len(result)} chars - the missing ref still counted"


def test_budget_too_small_says_so_per_file(tmp_path, monkeypatch, caplog):
    """A file that cannot be served is reported by name, not silently dropped.

    Replaces the former break-path test: since the frame is reserved inside the
    share, total_injected can no longer overshoot and the loop-wide break was
    measured dead. Each unservable file now reports itself, which is strictly more
    information than one message followed by silence.
    """
    import logging

    vault = _vault(tmp_path, monkeypatch)
    for name in ("a.md", "b.md", "c.md"):
        _write(vault, name, 5_000)

    with caplog.at_level(logging.INFO, logger="queue_manager"):
        result = queue_manager.collect_file_context(
            "Lies a.md, b.md und c.md", max_chars=120
        )

    gemeldet = [r.message for r in caplog.records if "übersprungen" in r.message]
    assert len(gemeldet) == 3, f"every unservable file must report itself, got {gemeldet}"
    assert result == ""


def test_context_messages_go_to_the_logger_not_stdout(tmp_path, monkeypatch, caplog, capsys):
    """Every message must survive an unattended --watch run.

    run_orchestrator.ps1 starts the orchestrator without redirecting stdout, so a
    print() is gone in exactly the run where it matters. The truncation notice is
    the important one: it is the only signal that material did not arrive whole.
    """
    import logging

    vault = _vault(tmp_path, monkeypatch)
    _write(vault, "gross.md", 10_000)

    with caplog.at_level(logging.INFO, logger="queue_manager"):
        queue_manager.collect_file_context(
            "Lies gross.md und fehlt-nicht-da.md", max_chars=3000
        )

    meldungen = [r.message for r in caplog.records]
    assert any("gek" in m and "gross.md" in m for m in meldungen), (
        "the truncation notice must reach the log"
    )
    assert any("nicht gefunden" in m for m in meldungen)

    assert capsys.readouterr().out == "", "nothing may go to stdout any more"


def test_unused_budget_also_rolls_backwards(tmp_path, monkeypatch):
    """A large file listed FIRST must still profit from small files behind it.

    This is the morning-brief shape and the reason shares are handed out by need
    rather than in reference order: SKILL.md is the first ref there, so a
    forward-only carry left it on its 1/3 share while 1447 chars expired behind it.
    """
    vault = _vault(tmp_path, monkeypatch)
    _write(vault, "a_gross.md", 10_000)
    _write(vault, "b_klein.md", 500)
    _write(vault, "c_klein.md", 500)

    result = queue_manager.collect_file_context(
        "Lies a_gross.md, b_klein.md und c_klein.md", max_chars=6000
    )

    gross_block = result.split("--- Inhalt von 'a_gross.md' ---")[1].split("--- Ende ---")[0]
    # An equal split would have been 6000 // 3 = 2000; the two small files need
    # ~500 each, so the large one must get clearly more than its nominal third.
    assert len(gross_block) > 3000, (
        f"a_gross.md got only {len(gross_block)} chars - slack behind it expired"
    )


def test_share_budget_gives_slack_to_the_hungry():
    """_share_budget: a file under its equal share releases the rest."""
    assert queue_manager._share_budget([100, 100], 1000) == [100, 100]
    assert queue_manager._share_budget([100, 5000], 1000) == [100, 900]
    assert queue_manager._share_budget([5000, 100], 1000) == [900, 100]
    assert queue_manager._share_budget([5000, 5000], 1000) == [500, 500]
    assert sum(queue_manager._share_budget([1, 2, 9999], 100)) <= 100
    # The divisor len(needs) - rank is only ever non-zero because the loop does not
    # run on an empty list. That is an implicit invariant, so pin it: a later guard
    # clause or prefetch could bring ZeroDivisionError back with the suite still green.
    assert queue_manager._share_budget([], 7500) == []
    # Discriminating: an equal split would cap the third at 30 // 3 = 10.
    assert queue_manager._share_budget([1, 1, 3000], 30) == [1, 1, 28]


def test_all_refs_dead_with_budget_returns_empty(tmp_path, monkeypatch):
    """The one production path that reaches _share_budget([], total).

    Edge of exactly the defect being fixed: every ref unresolvable AND a budget set.
    """
    _vault(tmp_path, monkeypatch)
    result = queue_manager.collect_file_context(
        "Nur tote Refs: [[Projekt]] und fehlt.md", max_chars=7500
    )
    assert result == ""

def test_large_first_file_does_not_starve_the_small_ones(tmp_path, monkeypatch):
    """Regression: a big early file must not push later small files out entirely.

    Found by the external review, 2026-08-28. Distributing max_chars as pure content
    and appending frames afterwards let the first file's block overshoot the ceiling,
    and the loop-wide break then discarded the two small files that had already been
    granted a share. Measured at max_chars=7500 with contents 10000/1/1: HEAD kept
    all three files (2644 chars), the content-only split kept only the large one
    (7556 chars, over budget).
    """
    vault = _vault(tmp_path, monkeypatch)
    _write(vault, "gross.md", 10_000)
    (vault / "notes" / "k1.md").write_text("a", encoding="utf-8")
    (vault / "notes" / "k2.md").write_text("b", encoding="utf-8")

    result = queue_manager.collect_file_context(
        "Lies gross.md k1.md k2.md", max_chars=7500
    )

    for name in ("gross.md", "k1.md", "k2.md"):
        assert f"'{name}'" in result, f"{name} was starved out by the large file"
    assert len(result) <= 7500, f"ceiling broken: {len(result)} chars"
