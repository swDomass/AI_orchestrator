"""I0 tests for scientific-investigation: audit-trail + classifier + bypass-counter.

Covers the deterministic safeguards:
  * Append-only JSONL audit-write semantics + entry validation.
  * externality_classifier reads the audit trail and never trusts file paths
    without matching SHA256.
  * Cross-provider bypass counter rate-limits at 3 / 30 days.
  * Tag parser recognizes all I0 tags.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from providers.base import RunResult
from tools.crosschecks import audit_trail, bypass_counter
from tools.crosschecks.audit_trail import (
    append_audit_entry,
    approvals_path,
    compute_sha256,
    load_audit_entries,
    record_telegram_message,
)
from tools.crosschecks.externality_classifier import (
    classify_crosscheck_tier,
    crosscheck_tiers_per_subtask,
)
from tools.scientific_investigation import (
    ScientificInvestigationTool,
    atomic_write_state,
    build_run_dir,
    build_state_dir,
    parse_tags,
)
from tools.sub_tool_context import build_sub_env


# ── Helpers ──────────────────────────────────────────────────────────────────


class _ScriptedProvider:
    name = "claude"
    supports_sessions = False

    def __init__(self):
        self.calls: list[str] = []

    def run(self, task: str, **kwargs) -> RunResult:
        self.calls.append(task)
        return RunResult(success=True, output="")


def _patch_notifier(monkeypatch):
    monkeypatch.setattr(
        "tools.scientific_investigation.notify_tool_done",
        lambda *a, **kw: None,
    )


# ── audit_trail: append-only + validation ────────────────────────────────────


def test_audit_trail_jsonl_append_only_write(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    append_audit_entry(run_dir, {
        "type": "preregistration_threshold",
        "criterion_id": "F1",
        "source": "norm_reference",
        "reference": "DIN-EN-60068-2 §4.3",
    })
    append_audit_entry(run_dir, {
        "type": "preregistration_threshold",
        "criterion_id": "F2",
        "source": "telegram_approval",
        "telegram_msg_id": "12345",
    })
    path = approvals_path(run_dir)
    raw_lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(raw_lines) == 2
    first = json.loads(raw_lines[0])
    second = json.loads(raw_lines[1])
    assert first["criterion_id"] == "F1"
    assert second["criterion_id"] == "F2"
    assert "ts" in first  # timestamp auto-set


def test_audit_trail_rejects_unknown_entry_type(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(ValueError, match="unknown audit entry type"):
        append_audit_entry(run_dir, {"type": "rogue_type"})


def test_audit_trail_rejects_unknown_source(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(ValueError, match="unknown audit source"):
        append_audit_entry(run_dir, {
            "type": "preregistration_threshold",
            "criterion_id": "F1",
            "source": "rogue_source",
        })


def test_audit_trail_crosscheck_tier_requires_file_path_and_sha(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(ValueError, match="file_path"):
        append_audit_entry(run_dir, {
            "type": "crosscheck_tier",
            "assigned_tier": "T2",
            "source": "norm_reference",
        })


def test_audit_trail_crosscheck_tier_rejects_invalid_tier(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(ValueError, match="assigned_tier"):
        append_audit_entry(run_dir, {
            "type": "crosscheck_tier",
            "file_path": "tests/x.py",
            "file_sha256": "deadbeef",
            "assigned_tier": "T1",  # T1 was removed in v5
            "source": "norm_reference",
        })


def test_load_audit_entries_skips_malformed_lines(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    path = approvals_path(run_dir)
    with path.open("w", encoding="utf-8") as fh:
        fh.write('{"type": "preregistration_threshold", "criterion_id": "F1"}\n')
        fh.write("this is not JSON at all\n")
        fh.write("\n")  # blank line
        fh.write('{"type": "preregistration_warning", "msg": "ok"}\n')
    entries = load_audit_entries(run_dir)
    assert len(entries) == 2
    assert entries[0]["criterion_id"] == "F1"
    assert entries[1]["type"] == "preregistration_warning"


def test_load_audit_entries_filters_by_type(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    append_audit_entry(run_dir, {
        "type": "preregistration_threshold",
        "criterion_id": "F1",
        "source": "norm_reference",
    })
    append_audit_entry(run_dir, {
        "type": "preregistration_warning",
        "msg": "no external norm available",
    })
    only_thresholds = load_audit_entries(run_dir, entry_type="preregistration_threshold")
    assert len(only_thresholds) == 1
    assert only_thresholds[0]["criterion_id"] == "F1"


def test_record_telegram_message_writes_separate_file(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    record_telegram_message(
        run_dir,
        telegram_msg_id="42",
        purpose="prereg_F1",
        payload={"text": "Approve threshold F1?"},
    )
    mapping = (run_dir / "audit" / "telegram_messages.jsonl").read_text("utf-8")
    parsed = json.loads(mapping.strip())
    assert parsed["telegram_msg_id"] == "42"
    assert parsed["purpose"] == "prereg_F1"


# ── compute_sha256 ───────────────────────────────────────────────────────────


def test_compute_sha256_stable_for_unchanged_file(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("print('hello')\n", encoding="utf-8")
    sha1 = compute_sha256(f)
    sha2 = compute_sha256(f)
    assert sha1 == sha2 and len(sha1) == 64


def test_compute_sha256_changes_on_edit(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("print('hello')\n", encoding="utf-8")
    sha_before = compute_sha256(f)
    f.write_text("print('hello world')\n", encoding="utf-8")
    sha_after = compute_sha256(f)
    assert sha_before != sha_after


def test_compute_sha256_returns_empty_for_missing(tmp_path):
    assert compute_sha256(tmp_path / "no_such_file") == ""


# ── externality_classifier ───────────────────────────────────────────────────


def test_externality_classifier_returns_T3_when_no_audit_entry(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    crosscheck = tmp_path / "tests" / "crosscheck_x.py"
    crosscheck.parent.mkdir(parents=True)
    crosscheck.write_text("def test_x(): pass\n", encoding="utf-8")
    assert classify_crosscheck_tier(crosscheck, run_dir) == "T3"


def test_externality_classifier_reads_audit_trail_for_T2(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    crosscheck = tmp_path / "tests" / "crosscheck_x.py"
    crosscheck.parent.mkdir(parents=True)
    crosscheck.write_text("def test_x(): pass\n", encoding="utf-8")
    sha = compute_sha256(crosscheck)
    append_audit_entry(run_dir, {
        "type": "crosscheck_tier",
        "file_path": str(crosscheck),
        "file_sha256": sha,
        "assigned_tier": "T2",
        "source": "norm_reference",
        "reference": "DIN-EN-60068-2 §4.3",
    })
    assert classify_crosscheck_tier(crosscheck, run_dir) == "T2"


def test_externality_classifier_returns_T3_when_sha_mismatch(tmp_path):
    """File edited after tier assignment → audit entry invalid → T3 fallback."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    crosscheck = tmp_path / "tests" / "crosscheck_x.py"
    crosscheck.parent.mkdir(parents=True)
    crosscheck.write_text("def test_x(): pass\n", encoding="utf-8")
    sha_old = compute_sha256(crosscheck)
    append_audit_entry(run_dir, {
        "type": "crosscheck_tier",
        "file_path": str(crosscheck),
        "file_sha256": sha_old,
        "assigned_tier": "T2",
        "source": "norm_reference",
    })
    # Modify the file → SHA changes → classifier must fall back to T3.
    crosscheck.write_text("def test_x(): assert False  # changed\n", encoding="utf-8")
    assert classify_crosscheck_tier(crosscheck, run_dir) == "T3"


def test_externality_classifier_T3_when_source_not_external(tmp_path):
    """Even with a crosscheck_tier entry, an unsupported source must not grant T2."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    crosscheck = tmp_path / "tests" / "crosscheck_x.py"
    crosscheck.parent.mkdir(parents=True)
    crosscheck.write_text("pass\n", encoding="utf-8")
    sha = compute_sha256(crosscheck)
    # user_chat_signoff is allowed in audit_trail but NOT in T2_ELIGIBLE_SOURCES.
    append_audit_entry(run_dir, {
        "type": "crosscheck_tier",
        "file_path": str(crosscheck),
        "file_sha256": sha,
        "assigned_tier": "T2",
        "source": "user_chat_signoff",
    })
    assert classify_crosscheck_tier(crosscheck, run_dir) == "T3"


def test_externality_classifier_returns_T3_for_missing_file(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    missing = tmp_path / "tests" / "does_not_exist.py"
    assert classify_crosscheck_tier(missing, run_dir) == "T3"


def test_crosscheck_tiers_per_subtask_aggregates_correctly(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    files_dir = tmp_path / "tests"
    files_dir.mkdir()
    a = files_dir / "a.py"
    b = files_dir / "b.py"
    c = files_dir / "c.py"
    a.write_text("a\n")
    b.write_text("b\n")
    c.write_text("c\n")
    # b is granted T2 via audit, a + c stay T3.
    sha_b = compute_sha256(b)
    append_audit_entry(run_dir, {
        "type": "crosscheck_tier",
        "file_path": str(b),
        "file_sha256": sha_b,
        "assigned_tier": "T2",
        "source": "paper_reference",
    })
    result = crosscheck_tiers_per_subtask(
        {"sub-001": [a, b], "sub-002": [c]},
        run_dir,
    )
    assert result == {"sub-001": ["T3", "T2"], "sub-002": ["T3"]}


# ── bypass_counter ───────────────────────────────────────────────────────────


def test_cross_provider_bypass_counter_starts_empty(tmp_path):
    assert bypass_counter.recent_bypass_count(tmp_path) == 0
    assert not bypass_counter.is_bypass_over_limit(tmp_path)


def test_cross_provider_bypass_counter_rate_limits_after_3_in_30_days(tmp_path):
    for i in range(3):
        bypass_counter.record_bypass(tmp_path, run_id=f"run-{i}")
    assert bypass_counter.recent_bypass_count(tmp_path) == 3
    assert bypass_counter.is_bypass_over_limit(tmp_path)


def test_cross_provider_bypass_counter_drops_old_entries(tmp_path):
    """Entries older than 30 days fall out of the rolling window."""
    counter_path = bypass_counter._counter_path(tmp_path)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    recent_ts = datetime.now(timezone.utc).isoformat()
    counter_path.write_text(json.dumps({
        "bypasses": [
            {"at": old_ts, "run_id": "ancient"},
            {"at": old_ts, "run_id": "ancient2"},
            {"at": old_ts, "run_id": "ancient3"},
            {"at": recent_ts, "run_id": "fresh"},
        ],
    }), encoding="utf-8")
    assert bypass_counter.recent_bypass_count(tmp_path) == 1
    assert not bypass_counter.is_bypass_over_limit(tmp_path)


def test_cross_provider_bypass_counter_record_returns_window_count(tmp_path):
    n = bypass_counter.record_bypass(tmp_path, run_id="r1")
    assert n == 1
    n = bypass_counter.record_bypass(tmp_path, run_id="r2")
    assert n == 2


def test_bypass_counter_tolerates_corrupt_file(tmp_path):
    counter_path = bypass_counter._counter_path(tmp_path)
    counter_path.write_text("not json", encoding="utf-8")
    # Should not raise; treats as empty.
    assert bypass_counter.recent_bypass_count(tmp_path) == 0


# ── parse_tags ───────────────────────────────────────────────────────────────


def test_parse_tags_recognizes_all_i0_tags():
    tags = parse_tags(
        "investigate X #prior:abc-123 #cross-provider:none "
        "#discipline:no-norms #resume:run-7 #engineering_reviewer:or_glm"
    )
    assert tags.prior_run_id == "abc-123"
    assert tags.cross_provider_none is True
    assert tags.discipline_no_norms is True
    assert tags.resume_run_id == "run-7"
    assert tags.engineering_reviewer == "or_glm"


def test_parse_tags_empty_when_no_tags():
    tags = parse_tags("just a question")
    assert tags.prior_run_id is None
    assert tags.cross_provider_none is False
    assert tags.discipline_no_norms is False
    assert tags.resume_run_id is None
    assert tags.engineering_reviewer is None


def test_parse_tags_does_not_match_partial_word():
    """#cross-provider:none must NOT match #cross-provider:none-of-these."""
    tags = parse_tags("test #cross-provider:none-of-these")
    # Word-boundary regex prevents partial-suffix match.
    assert tags.cross_provider_none is False


# ── atomic_write_state ───────────────────────────────────────────────────────


def test_atomic_write_state_creates_file(tmp_path):
    target = tmp_path / "deep" / "state.json"
    atomic_write_state(target, {"k": "v", "n": 1})
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == {"k": "v", "n": 1}


def test_atomic_write_state_overwrites_atomically(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_state(target, {"v": 1})
    atomic_write_state(target, {"v": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}
    # Tmp file must be cleaned up.
    assert not (tmp_path / "state.json.tmp").exists()


# ── build_sub_env (PYTHONPATH helper) ───────────────────────────────────────


def test_build_sub_env_prepends_root_to_pythonpath(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    env = build_sub_env(root, base_env={"PYTHONPATH": "/already/here"})
    parts = env["PYTHONPATH"].split(__import__("os").pathsep)
    assert parts[0] == str(root)
    assert "/already/here" in parts


def test_build_sub_env_idempotent(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    env1 = build_sub_env(root, base_env={"PYTHONPATH": ""})
    env2 = build_sub_env(root, base_env=env1)
    assert env1["PYTHONPATH"] == env2["PYTHONPATH"]


def test_build_sub_env_uses_os_environ_when_no_base(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONPATH", "/from/env")
    root = tmp_path / "project"
    root.mkdir()
    env = build_sub_env(root)
    parts = env["PYTHONPATH"].split(__import__("os").pathsep)
    assert parts[0] == str(root)
    assert "/from/env" in parts


# ── Tool integration: I0 scaffold ───────────────────────────────────────────


def test_tool_i0_scaffold_creates_layout(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider()
    result = tool.run("investigate diffusion bias", provider, cwd=str(tmp_path))
    assert result.success is True
    assert result.error_code == "i0_scaffold_only"
    docs = list((tmp_path / "docs").glob("scientific-investigation-*"))
    assert len(docs) == 1
    run_dir = docs[0]
    assert (run_dir / "draft").is_dir()
    assert (run_dir / "traces").is_dir()
    assert (run_dir / "audit").is_dir()
    assert (run_dir / "audit" / "manifest.json").is_file()
    state_dirs = list((tmp_path / ".scientific-investigation").iterdir())
    assert any((d / "sub-tasks").is_dir() for d in state_dirs)


def test_tool_i0_manifest_contains_provenance(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider()
    tool.run("question #cross-provider:none", provider, cwd=str(tmp_path))
    run_dir = next((tmp_path / "docs").glob("scientific-investigation-*"))
    manifest = json.loads((run_dir / "audit" / "manifest.json").read_text("utf-8"))
    assert manifest["provider"] == "claude"
    assert manifest["tool_version"].startswith("scientific-investigation/v5/I0")
    assert manifest["tags"]["cross_provider_none"] is True
    assert manifest["embedding_model"]  # populated from config


def test_tool_i0_records_bypass_in_audit(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider()
    tool.run("investigate #cross-provider:none", provider, cwd=str(tmp_path))
    run_dir = next((tmp_path / "docs").glob("scientific-investigation-*"))
    entries = load_audit_entries(run_dir, entry_type="cross_provider_bypass")
    assert len(entries) == 1
    assert entries[0]["bypass_count_in_window"] == 1


def test_tool_i0_blocks_when_bypass_over_limit(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    # Pre-fill the counter with 3 recent bypasses.
    for i in range(3):
        bypass_counter.record_bypass(tmp_path, run_id=f"prior-{i}")
    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider()
    result = tool.run("investigate #cross-provider:none", provider, cwd=str(tmp_path))
    assert result.success is False
    assert result.error_code == "bypass_over_limit"


def test_tool_i0_resume_reuses_run_id(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider()
    # Pre-create a state dir for the resume scenario; run should reuse the id.
    target_run_id = "resumed-run-id"
    (tmp_path / ".scientific-investigation" / target_run_id / "sub-tasks").mkdir(
        parents=True,
    )
    tool.run(f"continue work #resume:{target_run_id}", provider, cwd=str(tmp_path))
    # The state dir for the resumed run must exist (reused, not recreated under new uuid).
    assert (tmp_path / ".scientific-investigation" / target_run_id).is_dir()


# ── build_run_dir / build_state_dir ────────────────────────────────────────


def test_build_run_dir_creates_all_subdirs(tmp_path):
    rd = build_run_dir(tmp_path, "20260516-120000")
    assert rd.name == "scientific-investigation-20260516-120000"
    assert (rd / "draft").is_dir()
    assert (rd / "traces").is_dir()
    assert (rd / "audit").is_dir()


def test_build_state_dir_creates_subtasks_dir(tmp_path):
    sd = build_state_dir(tmp_path, "run-xyz")
    assert sd.name == "run-xyz"
    assert sd.parent.name == ".scientific-investigation"
    assert (sd / "sub-tasks").is_dir()


# ── Registry ────────────────────────────────────────────────────────────────


def test_tool_registered_under_canonical_tag():
    from tools.registry import get_tool
    t = get_tool("scientific-investigation")
    assert t is not None
    assert t.name == "scientific-investigation"
