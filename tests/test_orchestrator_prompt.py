"""Tests for orchestrator._build_prompt() and run_once() basics."""

from types import SimpleNamespace
from unittest.mock import patch


def _make_prompt(
    task="Test task",
    provider_name="claude",
    skill_name=None,
    memory_context="",
    file_context="",
):
    """Import and call _build_prompt with mocks to avoid vault/memory side effects."""
    mock_memory = SimpleNamespace(
        get_curated_memory=lambda: "",
        get_daily_context=lambda: "",
    )
    with patch("orchestrator.memory_module", mock_memory), \
         patch("orchestrator.collect_file_context", return_value=file_context):
        from orchestrator import _build_prompt
        return _build_prompt(task, provider_name, skill_name=skill_name, memory_context=memory_context)


def test_build_prompt_includes_system_prompt(monkeypatch):
    monkeypatch.setattr("config.load_soul", lambda: {"base": "I am a helpful assistant."})
    prompt = _make_prompt()
    assert "I am a helpful assistant" in prompt


def test_build_prompt_includes_memory_context():
    prompt = _make_prompt(memory_context="Previous task: fixed auth bug in login.py")
    assert "Previous task: fixed auth bug" in prompt


def test_build_prompt_returns_string():
    result = _make_prompt()
    assert isinstance(result, str)


def test_build_prompt_with_skill_name(monkeypatch):
    mock_skill = SimpleNamespace(prompt="Review all code changes carefully.", name="review-loop")
    monkeypatch.setattr("skills.load_skill", lambda name, vault_path=None: mock_skill)
    prompt = _make_prompt(skill_name="review-loop")
    assert "Review all code" in prompt


# --- Task placement (regression: silent morning-brief failures 20./24./25.07.2026) ---
#
# The task text used to ride inside the file-context block, which put it at ~62 % of
# the prompt and ended the prompt with whatever files the task referenced. The model
# then answered "no concrete task in your message" on an otherwise clean run.

def test_build_prompt_ends_with_task_section():
    prompt = _make_prompt(task="Schreibe den Morgenbrief", file_context="")
    assert prompt.rstrip().endswith("## Aufgabe\nSchreibe den Morgenbrief")


def test_build_prompt_keeps_task_last_despite_large_file_context():
    """The whole point: injected file content must never come after the instruction."""
    bulk = "\n".join(f"--- Inhalt von 'config{i}.md' ---\nsome config" for i in range(50))
    prompt = _make_prompt(task="Schreibe den Morgenbrief", file_context=bulk)

    assert prompt.index("## Aufgabe") > prompt.index("some config")
    assert prompt.rstrip().endswith("Schreibe den Morgenbrief")


def test_build_prompt_task_sits_in_final_stretch_of_prompt():
    """Guards the position, not just the order — the failure was one of burial."""
    bulk = "x" * 20000
    task = "Schreibe den Morgenbrief"
    prompt = _make_prompt(task=task, memory_context=bulk, file_context=bulk)

    assert prompt.index(task) / len(prompt) > 0.9


def test_build_prompt_labels_file_context():
    prompt = _make_prompt(file_context="--- Inhalt von 'x.md' ---\nbody")
    assert "## Referenzierte Dateien" in prompt


# --- Context/task boundary (regression: silent failures 11./14./17./19.08.2026) ---
#
# Putting the task last was not enough. The memory block renders past runs as a numbered
# list that quotes their task text, so the trailing "## Aufgabe" carrying the same text
# read as the next entry of that list. Four runs answered "I see the full context but no
# concrete task" on a clean exit 0. The boundary has to be stated, not implied by order.

def _delimiter():
    from orchestrator import PROMPT_TASK_DELIMITER
    return PROMPT_TASK_DELIMITER


def test_build_prompt_delimits_context_from_task():
    prompt = _make_prompt(task="Schreibe den Morgenbrief", memory_context="alte Läufe")
    assert _delimiter() in prompt


def test_delimiter_sits_directly_before_the_task_section():
    """Nothing may slip between the announcement and what it announces."""
    prompt = _make_prompt(task="Schreibe den Morgenbrief", memory_context="alte Läufe")
    # rindex, not index: the injected context legitimately contains `##` headings that
    # quote past runs, and if one of them ever read "## Aufgabe" the first-match version
    # would compare an empty slice and pass vacuously.
    between = prompt[prompt.index(_delimiter()) + len(_delimiter()):prompt.rindex("## Aufgabe")]
    assert between.strip() == ""


def test_delimiter_appears_exactly_once():
    """Two boundaries are no boundary — the reader cannot tell which one counts."""
    prompt = _make_prompt(
        task="Schreibe den Morgenbrief",
        memory_context="alte Läufe",
        file_context="--- Inhalt von 'x.md' ---\nbody",
    )
    assert prompt.count(_delimiter()) == 1


def test_task_stays_last_after_the_delimiter():
    """The 2026-07-25 fix must survive: nothing is appended behind the instruction."""
    prompt = _make_prompt(task="Schreibe den Morgenbrief", memory_context="alte Läufe")
    assert prompt.rstrip().endswith("## Aufgabe\nSchreibe den Morgenbrief")


def test_live_task_is_separated_from_its_own_quoted_history():
    """The actual failure, reproduced: history quoting the task verbatim.

    Without the boundary the final section is just the sixth entry of a list of five.
    The assertion is structural — the last occurrence of the task text has to lie behind
    the delimiter, so no reader can mistake it for another log line.
    """
    task = "Run the vault-gardener skill in `tasks` + `validate` modes for this vault"
    history = (
        f"1. [2026-08-04] ✅ {task}\n   Provider: claude\n   Report geschrieben.\n\n"
        f"2. [2026-07-27] ✅ {task}\n   Provider: claude\n   Report geschrieben."
    )
    prompt = _make_prompt(task=task, memory_context=history)

    assert prompt.rindex(task) > prompt.index(_delimiter())
    assert prompt.index(_delimiter()) > prompt.index("1. [2026-08-04]")


def test_empty_task_gets_the_delimiter_without_the_imperative():
    """A queue line of pure routing tags strips to nothing.

    The normal delimiter ends in "execute it now", which would order the run to carry out
    an instruction the very next line declares empty. The boundary still belongs there —
    only the command does not.
    """
    from orchestrator import PROMPT_TASK_DELIMITER_EMPTY

    prompt = _make_prompt(task="#claude_sonnet #every:24h", memory_context="alte Läufe")

    assert PROMPT_TASK_DELIMITER_EMPTY in prompt
    assert _delimiter() not in prompt
    assert prompt.rstrip().endswith("(LEER — die Queue-Zeile enthielt nur Metadaten-Tags)")


def test_no_delimiter_without_any_context_above_it():
    """A prompt that is nothing but the instruction has no boundary to draw."""
    mock_memory = SimpleNamespace(get_curated_memory=lambda: "", get_daily_context=lambda: "")
    with patch("orchestrator.memory_module", mock_memory), \
         patch("orchestrator.collect_file_context", return_value=""), \
         patch("orchestrator.get_system_prompt", return_value=""), \
         patch("skills.build_index", return_value=""):
        from orchestrator import _build_prompt
        prompt = _build_prompt("Schreibe den Morgenbrief", "claude")

    assert _delimiter() not in prompt
    assert prompt == "## Aufgabe\nSchreibe den Morgenbrief"


def test_build_prompt_strips_routing_tags_from_task():
    """Scoped to the task section — the skill index legitimately documents these tags."""
    prompt = _make_prompt(task="Mach was #claude_sonnet #every:24h")
    task_section = prompt.split("## Aufgabe\n")[-1]
    assert "#claude_sonnet" not in task_section
    assert "#every:24h" not in task_section
    assert "Mach was" in task_section


def test_build_prompt_without_file_context_has_no_empty_section():
    prompt = _make_prompt(file_context="")
    assert "## Referenzierte Dateien" not in prompt


def test_run_once_returns_true_on_empty_queue(monkeypatch):
    monkeypatch.setattr("orchestrator.read_queue_items", lambda: [])
    mock_memory = SimpleNamespace(archive_old_memories=lambda: 0)
    monkeypatch.setattr("orchestrator.memory_module", mock_memory)
    from orchestrator import run_once
    result = run_once(dry_run=True)
    assert result is True


def test_run_once_dry_run_processes_without_execution(monkeypatch):
    task = SimpleNamespace(task_text="Fix bug cwd:.", line_no=1, subtasks=())
    monkeypatch.setattr("orchestrator.read_queue_items", lambda: [task])
    mock_memory = SimpleNamespace(
        archive_old_memories=lambda: 0,
        get_context_for_task=lambda *a, **kw: "",
        get_curated_memory=lambda: "",
        get_daily_context=lambda: "",
    )
    monkeypatch.setattr("orchestrator.memory_module", mock_memory)
    monkeypatch.setattr("orchestrator.collect_file_context", lambda *a, **kw: "")

    # Dry-run should not raise even without a real provider
    from orchestrator import run_once
    # Just verify it doesn't crash in dry-run mode
    try:
        run_once(dry_run=True)
    except SystemExit:
        pass  # acceptable in dry-run
