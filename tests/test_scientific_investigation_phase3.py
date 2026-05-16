"""I4 tests for scientific-investigation: Phase 3 execution loop + adversarial search.

Covers:
  * Levenshtein distance + diversity check.
  * verify_tool_calls passes/fails as expected.
  * Adversarial trace JSONL written with all queries.
  * phase_execution_loop invokes the injected executor with sub-CWD + PYTHONPATH.
  * Crosscheck files in tests/ are classified per Sub-Task.
  * Sub-Task failure does NOT abort the loop.
  * Tool-level integration: I4 happy path reaches i4_phase3_done.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from providers.base import RunResult
from tools.crosschecks import audit_trail
from tools.crosschecks.adversarial_search import (
    AdversarialSearchReport,
    SearchQuery,
    SearchResult,
    check_query_diversity,
    levenshtein_distance,
    run_adversarial_search,
    verify_tool_calls,
    write_adversarial_trace,
)
from tools.scientific_investigation_phase2 import InvestigationPlan, SubTask
from tools.scientific_investigation_phase3 import (
    SubTaskResult,
    discover_crosscheck_files,
    phase_execution_loop,
    write_execution_report_md,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


class _ScriptedProvider:
    name = "claude"
    supports_sessions = False

    def __init__(self):
        self.calls: list[str] = []

    def run(self, task: str, **kwargs) -> RunResult:
        self.calls.append(task)
        return RunResult(success=True, output="")


def _make_plan(*sub_tasks: SubTask) -> InvestigationPlan:
    return InvestigationPlan(sub_tasks=list(sub_tasks), raw_yaml="")


def _make_sub_task(sub_id: str, type_: str = "data_analysis") -> SubTask:
    return SubTask(
        sub_id=sub_id,
        title=f"Title {sub_id}",
        description=f"desc {sub_id}",
        addresses_criteria=["F1"],
        type=type_,
        expected_output="o",
    )


# ── Levenshtein ──────────────────────────────────────────────────────────────


def test_levenshtein_zero_for_identical():
    assert levenshtein_distance("hello", "hello") == 0


def test_levenshtein_handles_empty():
    assert levenshtein_distance("", "abc") == 3
    assert levenshtein_distance("abc", "") == 3


def test_levenshtein_substitution():
    assert levenshtein_distance("cat", "hat") == 1


def test_levenshtein_known_values():
    # "kitten" → "sitting" requires 3 edits (k→s, e→i, +g)
    assert levenshtein_distance("kitten", "sitting") == 3


# ── Diversity check ────────────────────────────────────────────────────────


def test_diversity_pass_with_distinct_queries():
    queries = [
        SearchQuery("does X cause Y", "refute"),
        SearchQuery("alternative explanation for Y phenomenon", "alt"),
        SearchQuery("counter-evidence regarding the X theory", "counter"),
        SearchQuery("why X is not necessary for Y outcome", "neg"),
    ]
    check = check_query_diversity(queries)
    assert check.pass_ is True


def test_diversity_fails_with_too_few_queries():
    queries = [
        SearchQuery("a", "x"),
        SearchQuery("b", "x"),
        SearchQuery("c", "x"),  # only 3, min is 4
    ]
    check = check_query_diversity(queries)
    assert check.pass_ is False
    assert any("min 4" in f for f in check.failures)


def test_diversity_fails_when_two_queries_too_similar():
    queries = [
        SearchQuery("does X cause Y", "refute"),
        SearchQuery("does X cause Z", "refute"),  # only one char different
        SearchQuery("alternative explanation for Y phenomenon", "alt"),
        SearchQuery("counter-evidence regarding the X theory", "counter"),
    ]
    check = check_query_diversity(queries)
    assert check.pass_ is False
    assert any("too similar" in f for f in check.failures)


def test_diversity_threshold_overridable():
    queries = [SearchQuery(f"q{i}", "x") for i in range(4)]
    # min_distance=1 would pass; default 8 fails
    assert check_query_diversity(queries, min_distance=1).pass_ is True
    assert check_query_diversity(queries).pass_ is False


# ── verify_tool_calls ──────────────────────────────────────────────────────


def test_verify_tool_calls_passes_when_actual_ge_claimed():
    res = verify_tool_calls(claimed_queries=4, actual_calls=4)
    assert res.pass_ is True


def test_verify_tool_calls_fails_when_actual_lt_claimed():
    res = verify_tool_calls(claimed_queries=4, actual_calls=1)
    assert res.pass_ is False
    assert "1" in res.note and "4" in res.note


# ── write_adversarial_trace ────────────────────────────────────────────────


def test_write_adversarial_trace_appends_jsonl(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    queries = [SearchQuery(f"query {i}", "intent") for i in range(2)]
    results = [SearchResult(q, summary="result text") for q in queries]
    report = AdversarialSearchReport(
        claim_id="c1",
        queries=queries,
        results=results,
        diversity=check_query_diversity(queries),
        tool_calls=verify_tool_calls(2, 2),
    )
    path = write_adversarial_trace(rd, claim_id="c1", report=report)
    assert path.is_file()
    lines = [json.loads(line) for line in path.read_text("utf-8").strip().split("\n")]
    assert len(lines) == 2
    assert lines[0]["claim_id"] == "c1"
    assert lines[0]["query"] == "query 0"


# ── run_adversarial_search end-to-end ──────────────────────────────────────


_DIVERSE_QUERIES = [
    SearchQuery("does diffusion mechanism produce false negative", "refute"),
    SearchQuery("alternative explanation for bias under high temperature", "alt"),
    SearchQuery("counter-evidence regarding tolerance threshold standards", "counter"),
    SearchQuery("why convection theory contradicts measured pattern", "neg"),
]


def test_run_adversarial_search_writes_trace_and_returns_report(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    queries = list(_DIVERSE_QUERIES)

    def fake_executor(q):
        return SearchResult(q, summary=f"result for {q.text}")

    report = run_adversarial_search(
        queries, claim_id="c1", run_dir=rd, search_executor=fake_executor,
    )
    assert report.diversity.pass_ is True
    assert report.tool_calls.pass_ is True
    assert report.trace_path is not None
    assert report.trace_path.is_file()


# ── discover_crosscheck_files ──────────────────────────────────────────────


def test_discover_crosscheck_files_finds_pattern(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "crosscheck_a.py").write_text("def test(): pass\n")
    (tests_dir / "crosscheck_b.py").write_text("def test(): pass\n")
    (tests_dir / "regular_test.py").write_text("def test(): pass\n")
    files = discover_crosscheck_files(tmp_path, sub_id="S1", run_dir=rd)
    paths = {f.path.name for f in files}
    assert paths == {"crosscheck_a.py", "crosscheck_b.py"}
    # Default tier without an audit entry is T3.
    assert all(f.tier == "T3" for f in files)


def test_discover_crosscheck_files_empty_when_no_tests_dir(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    files = discover_crosscheck_files(tmp_path, sub_id="S1", run_dir=rd)
    assert files == []


# ── phase_execution_loop ──────────────────────────────────────────────────


def test_phase_execution_invokes_executor_per_sub_task(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    plan = _make_plan(_make_sub_task("S1"), _make_sub_task("S2"))

    invocations: list[str] = []

    def executor(sub_task, *, sub_state_cwd, env, provider, timeout):
        invocations.append(sub_task.sub_id)
        return SubTaskResult(sub_task=sub_task, success=True, output="ok")

    phase3 = phase_execution_loop(
        plan, _ScriptedProvider(),
        run_dir=rd, root_cwd=tmp_path, run_id="r1",
        sub_task_executor=executor,
    )
    assert invocations == ["S1", "S2"]
    assert phase3.all_successful() is True


def test_phase_execution_passes_pythonpath_to_executor(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    plan = _make_plan(_make_sub_task("S1"))
    captured: dict = {}

    def executor(sub_task, *, sub_state_cwd, env, provider, timeout):
        captured["pythonpath"] = env.get("PYTHONPATH", "")
        captured["sub_cwd"] = sub_state_cwd
        return SubTaskResult(sub_task=sub_task, success=True, output="")

    phase_execution_loop(
        plan, _ScriptedProvider(),
        run_dir=rd, root_cwd=tmp_path, run_id="r1",
        sub_task_executor=executor,
    )
    assert str(tmp_path) in captured["pythonpath"].split(__import__("os").pathsep)
    assert "S1" in str(captured["sub_cwd"])


def test_phase_execution_continues_after_sub_task_failure(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    plan = _make_plan(_make_sub_task("S1"), _make_sub_task("S2"))

    def executor(sub_task, *, sub_state_cwd, env, provider, timeout):
        if sub_task.sub_id == "S1":
            return SubTaskResult(
                sub_task=sub_task, success=False, output="",
                error="boom", error_code="failed",
            )
        return SubTaskResult(sub_task=sub_task, success=True, output="ok")

    phase3 = phase_execution_loop(
        plan, _ScriptedProvider(),
        run_dir=rd, root_cwd=tmp_path, run_id="r1",
        sub_task_executor=executor,
    )
    assert phase3.all_successful() is False
    assert phase3.sub_task_results[0].error_code == "failed"
    assert phase3.sub_task_results[1].success is True


def test_phase_execution_discovers_and_classifies_crosschecks(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    # Pre-create crosscheck files in tests/
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "crosscheck_x.py").write_text("pass\n")
    plan = _make_plan(_make_sub_task("S1"))

    def executor(sub_task, *, sub_state_cwd, env, provider, timeout):
        return SubTaskResult(sub_task=sub_task, success=True, output="")

    phase3 = phase_execution_loop(
        plan, _ScriptedProvider(),
        run_dir=rd, root_cwd=tmp_path, run_id="r1",
        sub_task_executor=executor,
    )
    assert phase3.crosscheck_tiers_per_subtask == {"S1": ["T3"]}
    # at_least_one_t2_per_subtask should be False (only T3)
    assert phase3.at_least_one_t2_per_subtask() is False


def test_phase_execution_runs_adversarial_search_for_literature_subtasks(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    plan = _make_plan(_make_sub_task("S1", type_="literature_search"))

    def executor(sub_task, *, sub_state_cwd, env, provider, timeout):
        return SubTaskResult(sub_task=sub_task, success=True, output="")

    def query_gen(sub_task):
        return list(_DIVERSE_QUERIES)

    def search_exec(q):
        return SearchResult(q, summary="found")

    phase3 = phase_execution_loop(
        plan, _ScriptedProvider(),
        run_dir=rd, root_cwd=tmp_path, run_id="r1",
        sub_task_executor=executor,
        adversarial_query_generator=query_gen,
        adversarial_search_executor=search_exec,
    )
    assert phase3.sub_task_results[0].adversarial is not None
    assert phase3.sub_task_results[0].adversarial.overall_pass() is True


def test_phase_execution_skips_adversarial_for_non_literature_subtasks(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    plan = _make_plan(_make_sub_task("S1", type_="data_analysis"))

    def executor(sub_task, *, sub_state_cwd, env, provider, timeout):
        return SubTaskResult(sub_task=sub_task, success=True, output="")

    phase3 = phase_execution_loop(
        plan, _ScriptedProvider(),
        run_dir=rd, root_cwd=tmp_path, run_id="r1",
        sub_task_executor=executor,
        adversarial_query_generator=lambda _: [SearchQuery("q", "x")],
        adversarial_search_executor=lambda q: SearchResult(q, summary="s"),
    )
    assert phase3.sub_task_results[0].adversarial is None


def test_phase_execution_writes_audit_entry_per_sub_task(tmp_path):
    rd = tmp_path / "run"
    (rd / "audit").mkdir(parents=True)
    plan = _make_plan(_make_sub_task("S1"))

    def executor(sub_task, *, sub_state_cwd, env, provider, timeout):
        return SubTaskResult(sub_task=sub_task, success=True, output="")

    phase_execution_loop(
        plan, _ScriptedProvider(),
        run_dir=rd, root_cwd=tmp_path, run_id="r1",
        sub_task_executor=executor,
    )
    entries = audit_trail.load_audit_entries(rd, entry_type="execution_sub_task")
    assert len(entries) == 1
    assert entries[0]["summary"]["sub_id"] == "S1"


# ── write_execution_report_md ──────────────────────────────────────────────


def test_write_execution_report_md_renders_status(tmp_path):
    from tools.scientific_investigation_phase3 import Phase3Result, CrosscheckFile
    results = [
        SubTaskResult(
            sub_task=_make_sub_task("S1"),
            success=True, output="ok", duration_sec=2.5,
            crosscheck_files=[
                CrosscheckFile(path=tmp_path / "x.py", sub_id="S1", tier="T3"),
            ],
        )
    ]
    phase3 = Phase3Result(
        sub_task_results=results,
        total_duration_sec=2.5,
        crosscheck_tiers_per_subtask={"S1": ["T3"]},
    )
    text = write_execution_report_md(tmp_path, phase3=phase3).read_text("utf-8")
    assert "Phase 3 — Execution Report" in text
    assert "S1: Title S1" in text
    assert "T3" in text
    assert "❌" in text  # at_least_one_t2 is False


# ── Tool-level integration: I4 happy path ──────────────────────────────────


def test_tool_run_i4_reaches_phase3_done(monkeypatch, tmp_path):
    """Imports inside the test to use the audit-test fixture's stub executor."""
    import tests.test_scientific_investigation_audit as audit_test_module

    audit_test_module._patch_notifier(monkeypatch)
    tool = audit_test_module.ScientificInvestigationTool()
    provider = audit_test_module._ScriptedProvider()
    result = tool.run("investigate diffusion", provider, cwd=str(tmp_path))
    assert result.success is True
    assert result.error_code == "i4_phase3_done"
    assert result.iterations == 4
    run_dir = next((tmp_path / "docs").glob("scientific-investigation-*"))
    assert (run_dir / "execution_report.md").is_file()
    state = json.loads(
        next((tmp_path / ".scientific-investigation").glob("*/state.json"))
        .read_text("utf-8")
    )
    assert state["phase"] == "phase3_execution_done"
    assert state["phase3"]["sub_tasks_run"] == 1
