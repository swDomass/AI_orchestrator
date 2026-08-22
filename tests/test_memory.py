"""Tests for the three-layer memory system (memory.py).

Layer 1: Curated MEMORY.md
Layer 2: Daily append-only logs
Layer 3: TF-IDF search (existing, tested indirectly)
"""

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

with patch("config._load_dotenv"):
    import config
    from tools.base_tool import _build_system_prompt


@pytest.fixture()
def memory_root(tmp_path):
    """Create a fresh memory module pointing at tmp_path/vault."""
    vault = tmp_path / "vault"
    vault.mkdir()
    # Patch config before importing memory
    with patch("config._load_dotenv"):
        old_vault = config.VAULT_PATH
        config.VAULT_PATH = vault

        import memory as mem
        # `memory` is a singleton for the whole pytest session, so these five globals have
        # to be restored as well. Left pointing at a deleted tmp path, a later test that
        # asserts "no daily context" would pass vacuously.
        saved = {
            name: getattr(mem, name)
            for name in (
                "_MEMORY_ROOT", "_TASK_RESULTS_DIR", "_ARCHIVE_DIR",
                "_DAILY_DIR", "_CURATED_MEMORY_FILE",
            )
        }
        # Patch module-level paths
        mem._MEMORY_ROOT = vault / "99_System" / "AI" / "memory"
        mem._TASK_RESULTS_DIR = mem._MEMORY_ROOT / "task_results"
        mem._ARCHIVE_DIR = mem._MEMORY_ROOT / "archive"
        mem._DAILY_DIR = mem._MEMORY_ROOT / "daily"
        mem._CURATED_MEMORY_FILE = mem._MEMORY_ROOT / "MEMORY.md"

        yield mem

        for name, value in saved.items():
            setattr(mem, name, value)
        config.VAULT_PATH = old_vault


# ── Layer 1: Curated MEMORY.md ──────────────────────────────────────────────


class TestCuratedMemory:
    def test_no_file_returns_empty(self, memory_root):
        assert memory_root.get_curated_memory() == ""

    def test_reads_file_content(self, memory_root):
        memory_root._CURATED_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        memory_root._CURATED_MEMORY_FILE.write_text(
            "# Long-term patterns\n\n- Always use pytest\n- Windows-first\n",
            encoding="utf-8",
        )
        result = memory_root.get_curated_memory()
        assert "Always use pytest" in result
        assert "Windows-first" in result

    def test_truncates_long_content(self, memory_root):
        memory_root._CURATED_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        long_content = "x" * 10_000
        memory_root._CURATED_MEMORY_FILE.write_text(long_content, encoding="utf-8")
        result = memory_root.get_curated_memory(max_chars=500)
        assert len(result) <= 510  # 500 + "..." + newline
        assert result.endswith("...")

    def test_custom_max_chars(self, memory_root):
        memory_root._CURATED_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        memory_root._CURATED_MEMORY_FILE.write_text("a" * 200, encoding="utf-8")
        result = memory_root.get_curated_memory(max_chars=100)
        assert len(result) <= 110


# ── Layer 2: Daily Logs ─────────────────────────────────────────────────────


class TestDailyLog:
    def test_append_creates_file(self, memory_root):
        ok = memory_root.append_daily_log(
            "Fix auth bug",
            "Fixed 3 issues in auth.py",
            "claude",
            45.0,
            cwd="/d/project",
            success=True,
        )
        assert ok is True

        today = date.today()
        path = memory_root._daily_log_path(today)
        assert path.exists()

        content = path.read_text(encoding="utf-8")
        assert f"# Memory {today.isoformat()}" in content
        assert "Fix auth bug" in content
        assert "claude" in content
        assert "success" in content

    def test_append_multiple_entries(self, memory_root):
        memory_root.append_daily_log("Task 1", "Result 1", "claude", 10.0)
        memory_root.append_daily_log("Task 2", "Result 2", "gemini", 20.0)

        path = memory_root._daily_log_path(date.today())
        content = path.read_text(encoding="utf-8")
        assert "Task 1" in content
        assert "Task 2" in content
        assert content.count(f"# Memory {date.today().isoformat()}") == 1
        assert content.count("## ") == 2  # Two time-stamped sections

    def test_parallel_appends_write_header_once(self, memory_root):
        def _append(i: int) -> bool:
            return memory_root.append_daily_log(f"Task {i}", f"Result {i}", "claude", 5.0)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(_append, (1, 2)))

        assert all(results)
        path = memory_root._daily_log_path(date.today())
        content = path.read_text(encoding="utf-8")
        assert content.count(f"# Memory {date.today().isoformat()}") == 1

    def test_append_failed_task(self, memory_root):
        memory_root.append_daily_log("Failing task", "Error msg", "claude", 5.0, success=False)

        path = memory_root._daily_log_path(date.today())
        content = path.read_text(encoding="utf-8")
        assert "failed" in content

    def test_truncates_long_result(self, memory_root):
        long_result = "x" * 1000
        memory_root.append_daily_log("Task", long_result, "claude", 5.0)

        path = memory_root._daily_log_path(date.today())
        content = path.read_text(encoding="utf-8")
        assert "…" in content
        # Should not contain full 1000 chars (now truncated to 80 chars)
        assert len(content) < 400

    def test_daily_log_path_format(self, memory_root):
        d = date(2026, 3, 9)
        path = memory_root._daily_log_path(d)
        assert path.name == "Memory 2026-03-09.md"
        assert path.parent == memory_root._DAILY_DIR


class TestDailyContext:
    def test_no_logs_returns_empty(self, memory_root):
        assert memory_root.get_daily_context() == ""

    def test_reads_today(self, memory_root):
        memory_root.append_daily_log("Today task", "Today result", "claude", 10.0)

        ctx = memory_root.get_daily_context()
        assert "Today task" in ctx
        assert f"# Memory {date.today().isoformat()}" in ctx

    def test_reads_today_and_yesterday(self, memory_root):
        # Write today's log
        memory_root.append_daily_log("Today task", "Today result", "claude", 10.0)

        # Manually create yesterday's log
        yesterday = date.today() - timedelta(days=1)
        ypath = memory_root._daily_log_path(yesterday)
        ypath.parent.mkdir(parents=True, exist_ok=True)
        ypath.write_text(
            f"# Memory {yesterday.isoformat()}\n\n## 23:00 — Yesterday task\n- result\n",
            encoding="utf-8",
        )

        ctx = memory_root.get_daily_context()
        assert "Today task" in ctx
        assert "Yesterday task" in ctx
        # Today should come first
        today_pos = ctx.index("Today task")
        yesterday_pos = ctx.index("Yesterday task")
        assert today_pos < yesterday_pos

    def test_ignores_older_logs(self, memory_root):
        # Create a log from 2 days ago
        old = date.today() - timedelta(days=2)
        old_path = memory_root._daily_log_path(old)
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_text("# Old content\n", encoding="utf-8")

        ctx = memory_root.get_daily_context()
        assert ctx == ""

    def test_truncates_to_max_chars(self, memory_root):
        # Write a very long daily log
        memory_root._ensure_dirs()
        path = memory_root._daily_log_path(date.today())
        path.write_text("x" * 20_000, encoding="utf-8")

        ctx = memory_root.get_daily_context(max_chars=500)
        assert len(ctx) <= 510
        assert ctx.startswith("...\n")

    def test_truncation_keeps_latest_today_entries(self, memory_root):
        memory_root._ensure_dirs()
        path = memory_root._daily_log_path(date.today())
        path.write_text(
            "# Memory today\n\n"
            "older entry\n" + ("x" * 800) + "\n"
            "latest important entry\n",
            encoding="utf-8",
        )

        ctx = memory_root.get_daily_context(max_chars=120)
        assert "latest important entry" in ctx
        assert "older entry" not in ctx


# ── store_result also writes daily log ──────────────────────────────────────


class TestStoreResultDailyIntegration:
    def test_store_result_appends_daily_log(self, memory_root):
        memory_root.store_result(
            "Test task",
            "Some output",
            "claude",
            30.0,
            cwd="/d/project",
            success=True,
        )

        # Task result file should exist
        results = list(memory_root._TASK_RESULTS_DIR.glob("*.md"))
        assert len(results) == 1

        # Daily log should also exist
        path = memory_root._daily_log_path(date.today())
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "Test task" in content


# ── TF-IDF search (existing, basic smoke test) ─────────────────────────────


class TestTfIdfSearch:
    def test_search_empty_returns_empty(self, memory_root):
        assert memory_root.search_memory("anything") == []

    def test_search_finds_stored_result(self, memory_root):
        memory_root.store_result(
            "Fix auth bug in login module",
            "Fixed authentication bypass in login.py",
            "claude",
            30.0,
        )
        results = memory_root.search_memory("auth login bug")
        assert len(results) > 0
        assert "auth" in results[0]["task"].lower() or "login" in results[0]["summary"].lower()

    def test_get_context_for_task(self, memory_root):
        memory_root.store_result("Setup pytest", "Configured pytest for project", "gemini", 10.0)
        ctx = memory_root.get_context_for_task("run pytest tests")
        assert ctx  # Should return something (either TF-IDF match or recent fallback)

    def test_cwd_preference_keeps_same_cwd_when_enough(self, memory_root):
        """When ≥2 same-CWD results above threshold, cross-CWD results are dropped."""
        from datetime import datetime

        target = "/project/a"
        other = "/project/b"
        base = {
            "provider": "claude",
            "success": True,
            "timestamp": datetime.now(),
        }
        mock_results = [
            {**base, "task": "cross task", "summary": "x", "score": 0.9, "cwd": other},
            {**base, "task": "local task 1", "summary": "y", "score": 0.5, "cwd": target},
            {**base, "task": "local task 2", "summary": "z", "score": 0.4, "cwd": target},
        ]
        with patch.object(memory_root, "search_memory", return_value=mock_results):
            ctx = memory_root.get_context_for_task("test", cwd=target)
        assert "local task 1" in ctx
        assert "local task 2" in ctx
        assert "cross task" not in ctx

    def test_cwd_preference_mixes_when_one_same_cwd(self, memory_root):
        """When only 1 same-CWD result, pad with cross-CWD up to MEMORY_TOP_K."""
        from datetime import datetime

        target = "/project/a"
        other = "/project/b"
        base = {
            "provider": "claude",
            "success": True,
            "timestamp": datetime.now(),
        }
        mock_results = [
            {**base, "task": "cross high", "summary": "x", "score": 0.9, "cwd": other},
            {**base, "task": "local only", "summary": "y", "score": 0.5, "cwd": target},
            {**base, "task": "cross low", "summary": "z", "score": 0.3, "cwd": other},
        ]
        with patch.object(memory_root, "search_memory", return_value=mock_results):
            ctx = memory_root.get_context_for_task("test", cwd=target)
        assert "local only" in ctx
        assert "cross high" in ctx  # padded from cross-CWD


class TestToolPromptMemoryLayers:
    def test_tool_prompt_includes_curated_and_daily_layers(self):
        with (
            patch("tools.base_tool.get_system_prompt", return_value="CORE"),
            patch("memory.get_curated_memory", return_value="CURATED"),
            patch("memory.get_daily_context", return_value="DAILY"),
        ):
            prompt = _build_system_prompt("claude", "MATCHES")

        assert "CORE" in prompt
        assert "## Langzeit-Kontext\nCURATED" in prompt
        assert "## Heutiger Verlauf\nDAILY" in prompt
        assert f"{config.MEMORY_HISTORY_HEADING}\nMATCHES" in prompt

    def test_tool_prompt_keeps_daily_when_curated_lookup_fails(self, caplog):
        with (
            patch("tools.base_tool.get_system_prompt", return_value="CORE"),
            patch("memory.get_curated_memory", side_effect=OSError("boom")),
            patch("memory.get_daily_context", return_value="DAILY"),
            caplog.at_level(logging.WARNING),
        ):
            prompt = _build_system_prompt("claude", "MATCHES")

        assert "CORE" in prompt
        assert "## Langzeit-Kontext" not in prompt
        assert "## Heutiger Verlauf\nDAILY" in prompt
        assert f"{config.MEMORY_HISTORY_HEADING}\nMATCHES" in prompt
        assert "Tool prompt curated memory load failed" in caplog.text


# ── No-op filter (regression: silent queue failures 11./14./17./19.08.2026) ────────
#
# A run that answered "there is no task in this message" was stored with success: true and
# then re-injected as the most relevant memory for the next identical task. On 2026-08-19
# three of the five injected examples were that non-answer — a few-shot prompt for doing
# nothing. These verbatim texts come from memory/task_results/ of the failed runs.

NOOP_2026_08_19 = (
    "Ich sehe den vollständigen Kontext (Vault, Skills, Memory), aber keine konkrete "
    "Frage oder Aufgabe von dir in dieser Nachricht.\n\n"
    "Woran soll ich arbeiten? Ein paar naheliegende Optionen angesichts der aktiven Projekte:"
)
NOOP_2026_08_11 = (
    "Ich sehe in dieser Nachricht keine konkrete Frage oder Aufgabe von dir — nur "
    "System-Kontext (CLAUDE.md, Memory, verfügbare Skills/Agents).\n\n"
    "Der letzte inhaltliche Block im Memory-Kontext ist ein **historischer** "
    "Vault-Gardener-Lauf."
)
NOOP_2026_08_17 = (
    "Ich sehe aktuell keine konkrete Frage oder Aufgabe von dir in dieser Nachricht — nur "
    "Kontext (globale Einstellungen, Vault-CLAUDE.md, Memory-Index).\n\nWoran soll ich arbeiten?"
)
NOOP_ENGLISH = "I see your configuration but no concrete task in it. What would you like me to do?"


class TestNoninformativeResults:
    def test_real_noop_answers_are_detected(self, memory_root):
        for text in (NOOP_2026_08_19, NOOP_2026_08_11, NOOP_2026_08_17, NOOP_ENGLISH):
            assert memory_root._is_noninformative(text, output_tokens=384), text[:50]

    def test_short_legitimate_results_survive(self, memory_root):
        """The counter-probe that decides whether the filter is precise or just eager.

        Every one of these is a real, informative answer that contains an absence phrase.
        daily-task-status legitimately answers in one short line, and that line contains
        "keine"/"Aufgaben" — or in English "no tasks". Matching on the absence phrase alone
        eats all of them; the addressee marker is what separates a refusal from a finding.

        The English rows are the ones that mattered: the first version of this filter left
        the English branches unanchored, and four of these six were discarded — which is
        precisely the objection ADR-002 raises against fingerprinting the answer text.
        """
        legit = [
            "Telegram gesendet (ok: true). Keine Aufgaben überfällig, 3 heute offen.",
            "I see no tasks overdue today. 3 open, 2 done.",
            "Telegram sent. I find no task-hygiene issues in 01_Tasks/.",
            "Scan complete — no specific task violations found.",
            "Review clean. No concrete task remains for this iteration.",
            "Keine offenen Aufgaben im Projekt — alle 9 Tasks storniert.",
            # Both fire the "in it" addressee marker without a word boundary:
            "Scan finished: no concrete instruction in itself, but 12 findings written.",
            "The queue file has no tasks in it that are overdue. Everything is scheduled.",
        ]
        for text in legit:
            assert not memory_root._is_noninformative(text, output_tokens=200), text

    def test_substantive_stored_result_survives_despite_phrase(self, memory_root):
        """Goes through store_result, so it asserts on a body the store can really hold.

        The earlier version padded a string past a character threshold that store_result
        can never produce (every body is truncated to MEMORY_SUMMARY_MAX_CHARS) — it
        measured confidence, not behaviour.
        """
        task = "Run the vault-gardener skill in tasks and validate modes"
        report = (
            "Vault-Gardener Tasks + Validate fertig. Report geschrieben, 19 Findings.\n\n"
            "Nebenbefund: der Lauf vom 11.08. meldete 'keine konkrete Aufgabe von dir' "
            "und hat nichts getan."
        )
        memory_root.store_result(task, report, "claude", 900.0, output_tokens=40140)

        stored = memory_root._parse_memory_file(
            next(iter(memory_root._TASK_RESULTS_DIR.glob("*.md")))
        )
        assert not stored["noninformative"]
        assert "Hygiene" in memory_root.get_context_for_task(task) or "Findings" in memory_root.get_context_for_task(task)

    def test_unknown_token_count_is_not_judged(self, memory_root):
        """output_tokens == 0 means "unknown", and unknown must not mean "filter it".

        The remaining zero-token case is a provider that reports no counts — `codex` and
        `vibe` never do. A character-length fallback cannot stand in for the missing count:
        every stored body is capped at MEMORY_SUMMARY_MAX_CHARS (700), so any threshold
        above that is unconditionally true and is no second condition at all.
        """
        assert not memory_root._is_noninformative(NOOP_2026_08_19, output_tokens=0)
        assert memory_root._is_noninformative(NOOP_2026_08_19, output_tokens=384)

    def test_parallel_aggregate_carries_token_count(self, memory_root):
        """The third injection route: #parallel used to store aggregates without tokens.

        With output_tokens missing, _is_noninformative declines to judge, so a no-op
        aggregate stayed both stored-as-success and re-injectable. The subtasks carry the
        numbers; this asserts the sum actually reaches the store, since that is what makes
        the filter applicable at all.
        """
        from parallel_runner import SubTaskResult

        results = [
            SubTaskResult(text="a", provider_name="claude", success=True, output="x", output_tokens=120),
            SubTaskResult(text="b", provider_name="claude", success=True, output="y", output_tokens=264),
        ]
        assert sum(r.output_tokens for r in results) == 384

        memory_root.store_result(
            "Parallel-Task", NOOP_2026_08_19, "parallel", 0.0,
            output_tokens=sum(r.output_tokens for r in results),
        )
        stored = memory_root._parse_memory_file(
            next(iter(memory_root._TASK_RESULTS_DIR.glob("*.md")))
        )
        assert stored["output_tokens"] == 384
        assert stored["noninformative"]

    def test_parallel_aggregate_is_judged_per_subtask(self, memory_root):
        """One lazy subtask must not discard another's real work.

        The aggregate is one string, so a refusal in subtask 2 still lands inside the
        400-char scan window. Judging the whole text would drop subtask 1's result from
        memory as well — and the token sum makes such an aggregate small enough to reach
        the check at all.
        """
        mixed = (
            "**Subtask 1** (claude): PASS — HA-Snapshot\n"
            "Snapshot geschrieben, 7 Tage nachgeholt, 0 Lücken.\n\n"
            "**Subtask 2** (claude): PASS — Vault-Gardener\n" + NOOP_2026_08_19
        )
        assert not memory_root._is_noninformative(mixed, output_tokens=713)

        all_refusals = (
            "**Subtask 1** (claude): PASS — a\n" + NOOP_2026_08_11 + "\n\n"
            "**Subtask 2** (claude): PASS — b\n" + NOOP_2026_08_19
        )
        assert memory_root._is_noninformative(all_refusals, output_tokens=713)

    def test_empty_subtask_blocks_are_not_a_noop(self, memory_root):
        """`all()` over an empty sequence is True — an aggregate of nothing must not count."""
        assert not memory_root._is_noninformative("**Subtask 1**\n**Subtask 2**", 100)

    @pytest.mark.parametrize("n_subtasks", [1, 2])
    def test_subtask_header_preview_does_not_decide(self, memory_root, n_subtasks):
        """The block header carries 60 chars of the SUBTASK TEXT, not of the answer.

        In this repo a queue line may quote the refusal wording itself — the very task that
        built this filter would. Parametrised because the per-block path used to start at
        TWO markers, so a single-subtask aggregate was still judged as one whole text and
        the header preview decided after all.
        """
        aggregate = (
            "**Subtask 1** (claude): PASS — keine konkrete Aufgabe in dieser Nachricht\n"
            "Report geschrieben, 19 Findings."
        )
        if n_subtasks == 2:
            aggregate += (
                "\n\n**Subtask 2** (claude): PASS — Zweite Aufgabe\n"
                "Snapshot geschrieben, 0 Lücken."
            )
        assert not memory_root._is_noninformative(aggregate, 700)

    def test_alternative_refusal_phrasing_is_covered(self, memory_root):
        """Found by an independent sweep, not by the pattern that was supposed to prove it."""
        real = (
            "Es fehlt eine konkrete Aufgabe in deiner Nachricht — ich sehe nur den "
            "Systemkontext und die Skill-Liste. Was soll ich tun?"
        )
        assert memory_root._is_noninformative(real, output_tokens=493)

    def test_failed_run_is_not_labelled_done(self, memory_root):
        """A fixed "(erledigt)" behind a ❌ read as "failed (done)"."""
        memory_root.store_result("Task A", "Fehler aufgetreten.", "claude", 5.0, success=False)

        ctx = memory_root.get_context_for_task("Task A")

        assert "(fehlgeschlagen)" in ctx
        assert "(erledigt)" not in ctx

    def test_large_output_is_never_flagged(self, memory_root):
        above = config.MEMORY_NOOP_MAX_OUTPUT_TOKENS + 1
        assert not memory_root._is_noninformative(NOOP_2026_08_19, output_tokens=above)

    def test_empty_summary_is_not_flagged(self, memory_root):
        assert not memory_root._is_noninformative("", output_tokens=384)

    def _write_memory_file(self, memory_root, name, task, body, output_tokens):
        memory_root._ensure_dirs()
        path = memory_root._TASK_RESULTS_DIR / name
        path.write_text(
            "---\n"
            f'task: "{task}"\n'
            "provider: claude\n"
            "cwd: \n"
            "duration_sec: 10.5\n"
            f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
            "success: true\n"
            "input_tokens: 2\n"
            f"output_tokens: {output_tokens}\n"
            "---\n\n" + body,
            encoding="utf-8",
        )
        return path

    def test_noop_is_dropped_from_scored_search(self, memory_root):
        """The TF-IDF path: a no-op scores highest because it quotes the same task."""
        task = "Run the vault-gardener skill in tasks and validate modes"
        self._write_memory_file(memory_root, "2026-08-19_vg_claude.md", task, NOOP_2026_08_19, 384)
        self._write_memory_file(
            memory_root, "2026-08-04_vg_claude.md", task,
            "Report geschrieben, 41 Hygiene-Issues gefunden.", 40140,
        )

        results = memory_root.search_memory(task)

        assert results, "the substantive run must still be found"
        assert all("keine konkrete" not in r["summary"] for r in results)
        assert any("Hygiene-Issues" in r["summary"] for r in results)

    def test_noop_is_dropped_from_recent_fallback(self, memory_root):
        """The fallback path, taken when nothing scores above MEMORY_MIN_SCORE.

        This is where a fresh no-op would otherwise win on recency alone.
        """
        self._write_memory_file(
            memory_root, "2026-08-19_vg_claude.md",
            "Run the vault-gardener skill", NOOP_2026_08_19, 384,
        )

        recent = memory_root._get_recent_memories()

        assert recent == []

    def test_injected_block_omits_the_noop(self, memory_root):
        task = "Run the vault-gardener skill in tasks and validate modes"
        self._write_memory_file(memory_root, "2026-08-19_vg_claude.md", task, NOOP_2026_08_19, 384)
        self._write_memory_file(
            memory_root, "2026-08-04_vg_claude.md", task,
            "Report geschrieben, 41 Hygiene-Issues gefunden.", 40140,
        )

        ctx = memory_root.get_context_for_task(task)

        assert "Hygiene-Issues" in ctx
        assert "keine konkrete" not in ctx


class TestDailyLogNoopFilter:
    """Layer 2 — the second injection path, which the first version of the fix missed.

    `append_daily_log` writes an entry per run and `get_daily_context` feeds today's and
    yesterday's back into every prompt. A no-op therefore poisons the next run through
    this path even when layer 3 is clean, and it did: the entry of 2026-08-19 08:58 was
    live, labelled `Status: success`, when this was written.
    """

    # Exactly what append_daily_log stores: first line, hard 80-char cut, then an ellipsis.
    # The addressee marker the strict pattern keys on falls off in that cut, so the strict
    # rule alone cannot see this line — measured before writing the truncation pattern.
    TRUNCATED = "Ich sehe den vollständigen Kontext (Vault, Skills, Memory), aber keine konkrete …"

    def test_truncated_noop_line_is_recognised(self, memory_root):
        assert memory_root._is_noop_log_entry(self.TRUNCATED)

    def test_strict_pattern_alone_would_miss_the_truncated_line(self, memory_root):
        """Guards the reason the looser layer-2 rule exists at all."""
        assert not memory_root._looks_like_no_task_answer(self.TRUNCATED)

    def test_legitimate_log_lines_survive(self, memory_root):
        for line in [
            "`\"ok\":true` — Telegram gesendet, UTF-8 sauber.",
            "Keine Aufgaben überfällig, 3 heute offen.",
            "✅ HA-Health-Snapshot (7-Tage-Catch-up abgeschlossen):",
            "Report geschrieben, 19 Hygiene-Findings, keine konkrete Zahl offen.",
        ]:
            assert not memory_root._is_noop_log_entry(line), line

    def test_noop_is_not_written_to_the_daily_log(self, memory_root):
        memory_root.append_daily_log(
            "Run the vault-gardener skill", NOOP_2026_08_19, "claude", 10.5,
        )
        memory_root.append_daily_log(
            "Morning brief", "`ok:true` — Telegram gesendet.", "claude", 225.0,
        )

        ctx = memory_root.get_daily_context()

        assert "Telegram gesendet" in ctx
        assert "keine konkrete" not in ctx

    def test_already_written_noop_is_filtered_on_read(self, memory_root):
        """Covers the two-day backlog: entries written before the write-side skip existed.

        Reproduces the real file byte-for-byte in shape, including the truncated summary.
        """
        memory_root._ensure_dirs()
        path = memory_root._daily_log_path(date.today())
        path.write_text(
            "# Memory 2026-08-19\n"
            "\n## 08:58 — Run the vault-gardener skill in `tasks` + `validate` modes\n"
            "- **Provider:** claude\n"
            "- **Duration:** 11s\n"
            "- **Status:** success\n"
            f"- {self.TRUNCATED}\n"
            "\n## 09:01 — Lies zuerst morning-brief/SKILL.md\n"
            "- **Provider:** claude\n"
            "- **Duration:** 225s\n"
            "- **Status:** success\n"
            "- `\"ok\":true` — Telegram gesendet, UTF-8 sauber.\n",
            encoding="utf-8",
        )

        ctx = memory_root.get_daily_context()

        assert "Telegram gesendet" in ctx
        assert "vault-gardener" not in ctx
        assert "keine konkrete" not in ctx
        assert ctx.startswith("# Memory")

    def test_write_side_judges_the_full_result(self, memory_root):
        """The write side is the primary defence and gets the input with the most signal.

        Measured against the eight real no-ops: judging the 80-char summary catches 3,
        judging the full text catches 8. Symmetry between write and read looked like the
        safer property and was not — asymmetry in this direction is harmless, since the
        looser read rule can only remove entries, never resurrect one.
        """
        # 2026-08-14 is one of the five whose truncated summary the read rule misses —
        # the write side has to catch it, or nothing does.
        missed_by_read = (
            "Ich sehe den vollständigen Kontext (Vault-Struktur, Skills, Memory), aber "
            "keine konkrete Frage oder Aufgabe von dir in dieser Nachricht."
        )
        first = missed_by_read.partition("\n")[0].strip()
        summary = (first[:80] + "…") if len(first) > 80 else first
        assert not memory_root._is_noop_log_entry(summary), "Vorbedingung: Leseregel greift hier nicht"

        memory_root.append_daily_log(
            "Run the vault-gardener skill", missed_by_read, "claude", 10.5,
            output_tokens=724,
        )

        assert "Leerlauf" in memory_root._daily_log_path(date.today()).read_text(encoding="utf-8")
        assert memory_root.get_daily_context() == ""

    def test_loose_rule_does_not_override_a_known_token_count(self, memory_root):
        """The 80-char rule exists for providers without token counts, not to overrule one.

        As a plain `or` it did: a 40 000-token report whose first line quotes the refusal
        wording was dropped entirely — not just from the prompt, but from the hand-readable
        vault note, while the strict rule had correctly said no.
        """
        report = (
            "Der Filter erkennt Antworten wie 'keine konkrete Aufgabe in dieser Nachricht' "
            "zuverlässig und läuft jetzt in beiden Layern.\n\n" + ("Detailbefund. " * 100)
        )

        memory_root.append_daily_log("Task", report, "claude", 900.0, output_tokens=40140)

        assert "Der Filter erkennt" in memory_root.get_daily_context()

    def test_noop_above_the_token_ceiling_is_still_caught(self, memory_root):
        """The strict rule stops at the ceiling; the truncation stump must not.

        With only "strict if tokens, loose if none", a no-op just above 900 tokens passed
        BOTH layers — written, marked as success, and injected. Measured regression.
        """
        memory_root.append_daily_log("Task", NOOP_2026_08_19, "claude", 12.0, output_tokens=1000)

        assert not memory_root._is_noninformative(NOOP_2026_08_19, 1000), "Vorbedingung"
        assert "keine konkrete" not in memory_root.get_daily_context()

    def test_noop_stays_visible_to_a_human(self, memory_root):
        """Dropping the entry made the failure class LESS visible than before the fix.

        Three recurring queue lines carry no `#verify:` tag; for those the daily-note entry
        is the only symptom a person ever sees.
        """
        memory_root.append_daily_log(
            "Run the vault-gardener skill", NOOP_2026_08_19, "claude", 10.5, output_tokens=384,
        )
        raw = memory_root._daily_log_path(date.today()).read_text(encoding="utf-8")

        assert "Leerlauf" in raw, "der Mensch muss den Leerlauf sehen"
        assert "keine konkrete" not in memory_root.get_daily_context(), "der Prompt nicht"

    def test_missing_token_count_falls_back_to_the_loose_rule(self, memory_root):
        """codex and vibe report no counts at all — there the 80-char rule is all there is."""
        memory_root.append_daily_log("Task", NOOP_2026_08_11, "codex", 12.0, output_tokens=0)

        assert memory_root.get_daily_context() == ""

    def test_report_that_merely_mentions_an_absence_is_kept(self, memory_root):
        """"keine Aufgabe von dir" is a finding about the user, not a refusal to act.

        This wording used to be treated as an addressee anchor and cost seven realistic
        results their memory entry — including a short dev-loop report about this very
        filter, which would have deleted itself.
        """
        report = (
            "Vault-Gardener fertig. Es gab keine Aufgabe von dir, die gegen die "
            "Leitplanken verstößt.\n\nDetailbefund mit Inhalt."
        )

        memory_root.append_daily_log("Vault-Gardener", report, "claude", 900.0, output_tokens=800)

        assert "Vault-Gardener fertig" in memory_root.get_daily_context()
        assert not memory_root._is_noninformative(report, output_tokens=800)

    def test_entry_without_header_is_still_filtered(self, memory_root):
        """`## ` at the very start of the file used to escape into the untouched head."""
        memory_root._ensure_dirs()
        path = memory_root._daily_log_path(date.today())
        path.write_text(
            "## 08:58 — Run the vault-gardener skill\n"
            "- **Provider:** claude\n"
            "- **Status:** success\n"
            f"- {self.TRUNCATED}\n"
            "\n## 09:01 — Morning brief\n"
            "- **Provider:** claude\n"
            "- **Status:** success\n"
            "- `\"ok\":true` — Telegram gesendet.\n",
            encoding="utf-8",
        )

        ctx = memory_root.get_daily_context()

        assert "Telegram gesendet" in ctx
        assert "vault-gardener" not in ctx

    def test_log_of_nothing_but_noops_yields_no_section(self, memory_root):
        """Otherwise `if daily:` stays true and the prompt carries an empty section."""
        memory_root._ensure_dirs()
        path = memory_root._daily_log_path(date.today())
        path.write_text(
            "# Memory 2026-08-19\n"
            "\n## 08:58 — Run the vault-gardener skill\n"
            "- **Provider:** claude\n"
            "- **Status:** success\n"
            f"- {self.TRUNCATED}\n",
            encoding="utf-8",
        )

        assert memory_root.get_daily_context() == ""

    def test_clean_log_round_trips_unchanged(self, memory_root):
        """Filtering must not reformat a log in which nothing is filtered."""
        memory_root._ensure_dirs()
        original = (
            "# Memory 2026-08-19\n"
            "\n## 09:01 — Morning brief\n"
            "- **Provider:** claude\n"
            "- **Status:** success\n"
            "- `\"ok\":true` — Telegram gesendet.\n"
        )
        memory_root._daily_log_path(date.today()).write_text(original, encoding="utf-8")

        assert memory_root.get_daily_context() == original.strip()

    def test_log_without_entries_is_returned_unchanged(self, memory_root):
        memory_root._ensure_dirs()
        path = memory_root._daily_log_path(date.today())
        path.write_text("# Memory 2026-08-19\n", encoding="utf-8")
        assert memory_root.get_daily_context().strip() == "# Memory 2026-08-19"

    def test_daily_log_quotes_the_task_at_label_length(self, memory_root):
        """Layer 2 used 120 chars — more of the live queue line than layer 3's label."""
        long_task = (
            "Run the vault-gardener skill in `tasks` + `validate` modes for this vault. "
            "Scan ALL task files under 01_Tasks/ recursively"
        )
        memory_root.append_daily_log(long_task, "Report geschrieben.", "claude", 900.0)

        ctx = memory_root.get_daily_context()

        assert long_task[: config.MEMORY_TASK_LABEL_CHARS] in ctx
        assert long_task[: config.MEMORY_TASK_LABEL_CHARS + 15] not in ctx


class TestHistoryBlockRendering:
    def test_quoted_task_is_shortened_to_a_label(self, memory_root):
        """The verbatim 80-char quote reproduced the opening of the live queue line."""
        long_task = (
            "Run the vault-gardener skill in `tasks` + `validate` modes for this vault. "
            "Scan ALL task files under 01_Tasks/ recursively"
        )
        memory_root.store_result(long_task, "Report geschrieben.", "claude", 900.0)

        ctx = memory_root.get_context_for_task(long_task)

        assert long_task[: config.MEMORY_TASK_LABEL_CHARS] in ctx
        assert long_task[: config.MEMORY_TASK_LABEL_CHARS + 15] not in ctx
        assert "(erledigt)" in ctx
