"""I1 tests for scientific-investigation: Phase 0 framing + Phase 0.5 prereg.

Covers:
  * Framing: structured YAML parsed, similarity index updated, hits flagged.
  * Pre-Reg: norm/paper sources create audit entries; telegram path blocks
    on the manager and records msg_id; disziplin-warning gate fires when no
    threshold has external source; hash-lock changes when content changes.
  * Tool integration: run() now executes Phase 0+0.5 and persists state.
  * Telegram listener: /approve <run_id> [criterion_id] routes to manager.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from providers.base import RunResult
from tools.crosschecks import audit_trail, similarity_index
from tools.scientific_investigation import ScientificInvestigationTool
from tools.scientific_investigation_approvals import (
    INVESTIGATION_CRITERION,
    PreRegApprovalManager,
    get_manager,
    reset_manager_for_tests,
)
from tools.scientific_investigation_phases import (
    Threshold,
    _parse_yaml_minimal,
    _validate_reference,
    compute_prereg_hash,
    phase_framing,
    phase_prereg,
    write_plan_md,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_manager():
    reset_manager_for_tests()
    yield
    reset_manager_for_tests()


class _ScriptedProvider:
    """Returns pre-scripted outputs in order."""
    name = "claude"
    supports_sessions = False

    def __init__(self, outputs: list[str]):
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    def run(self, task: str, **kwargs) -> RunResult:
        self.prompts.append(task)
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))


def _patch_notifier(monkeypatch):
    from tools.scientific_investigation_phase3 import SubTaskResult

    monkeypatch.setattr(
        "tools.scientific_investigation.notify_tool_done",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr("dispatcher.get_provider_by_name", lambda name: None)

    def _stub(sub_task, *, sub_state_cwd, env, provider, timeout):
        return SubTaskResult(
            sub_task=sub_task, success=True,
            output=f"stub: {sub_task.sub_id}", duration_sec=0.0,
        )

    monkeypatch.setattr(
        "tools.scientific_investigation_phase3.default_devloop_executor", _stub,
    )


def _good_framing_yaml(question="Is X true?", framing_text="Engineering eval of X"):
    return f"""```yaml
question: {question}
hypothesis: X is true under condition Y
bias_statement: Author wants X to be true to validate prior design choice
discipline: engineering
framing_text: {framing_text}
```"""


def _good_prereg_yaml(thresholds_block: str | None = None):
    if thresholds_block is None:
        thresholds_block = """\
  - criterion_id: F1
    description: Toleranz-Test bei 800K
    threshold_value: 5%
    source: norm_reference
    reference: DIN-EN-60068-2 §4.3 Toleranz 5% bei 800K
"""
    return f"""```yaml
thresholds:
{thresholds_block}```"""


# ── _parse_yaml_minimal ──────────────────────────────────────────────────────


def test_yaml_minimal_parses_top_level_scalars():
    text = "question: precise\nhypothesis: vague"
    out = _parse_yaml_minimal(text)
    assert out["question"] == "precise"
    assert out["hypothesis"] == "vague"


def test_yaml_minimal_parses_threshold_list():
    text = """\
thresholds:
  - criterion_id: F1
    source: norm_reference
    reference: DIN 5
  - criterion_id: F2
    source: telegram_approval
"""
    out = _parse_yaml_minimal(text)
    assert isinstance(out["thresholds"], list)
    assert len(out["thresholds"]) == 2
    assert out["thresholds"][0]["criterion_id"] == "F1"
    assert out["thresholds"][1]["source"] == "telegram_approval"


def test_yaml_minimal_strips_quotes():
    text = 'question: "with quotes"'
    assert _parse_yaml_minimal(text)["question"] == "with quotes"


# ── _validate_reference ──────────────────────────────────────────────────────


def test_validate_reference_accepts_norm_with_digit():
    _validate_reference("F1", "norm_reference", "DIN-EN-60068-2 §4.3 Toleranz 5%")


def test_validate_reference_rejects_too_short():
    with pytest.raises(ValueError, match="too short"):
        _validate_reference("F1", "norm_reference", "DIN 5")


def test_validate_reference_rejects_paper_without_doi():
    with pytest.raises(ValueError, match="DOI"):
        _validate_reference("F1", "paper_reference", "Smith et al 2024 page 12 some text")


def test_validate_reference_accepts_paper_with_doi():
    _validate_reference(
        "F1",
        "paper_reference",
        "Smith 2024 doi:10.1234/abcd page 7 reports 5% threshold",
    )


def test_validate_reference_rejects_norm_without_digit():
    with pytest.raises(ValueError, match="digit"):
        _validate_reference("F1", "norm_reference", "Some text without any numbers here")


def test_validate_reference_skips_telegram():
    _validate_reference("F1", "telegram_approval", "")  # no constraint


# ── compute_prereg_hash ─────────────────────────────────────────────────────


def test_compute_prereg_hash_deterministic():
    a = Threshold("F1", "desc", "5%", "norm_reference", "DIN-X §1 5%")
    b = Threshold("F2", "desc", "1mm", "telegram_approval", "")
    h1 = compute_prereg_hash([a, b])
    h2 = compute_prereg_hash([b, a])  # order-independent
    assert h1 == h2 and len(h1) == 64


def test_compute_prereg_hash_changes_when_value_changes():
    a = Threshold("F1", "desc", "5%", "norm_reference", "DIN-X §1 5%")
    h1 = compute_prereg_hash([a])
    a.threshold_value = "10%"
    h2 = compute_prereg_hash([a])
    assert h1 != h2


# ── similarity_index ────────────────────────────────────────────────────────


def test_similarity_index_appends_and_finds(tmp_path):
    similarity_index.append_investigation(
        tmp_path,
        run_id="run-1",
        framing_text="diffusion bias engineering analysis 800K toleranz",
        embedding_model="mini",
    )
    hits = similarity_index.find_similar_investigations(
        tmp_path,
        framing_text="diffusion bias engineering analysis 800K toleranz",
        threshold=0.5,
    )
    assert len(hits) == 1 and hits[0]["run_id"] == "run-1"


def test_similarity_index_excludes_self(tmp_path):
    similarity_index.append_investigation(
        tmp_path, run_id="run-1", framing_text="alpha beta gamma delta", embedding_model="mini",
    )
    hits = similarity_index.find_similar_investigations(
        tmp_path,
        framing_text="alpha beta gamma delta",
        threshold=0.1,
        exclude_run_id="run-1",
    )
    assert hits == []


def test_similarity_index_idempotent_on_run_id(tmp_path):
    similarity_index.append_investigation(
        tmp_path, run_id="run-1", framing_text="text one", embedding_model="m",
    )
    similarity_index.append_investigation(
        tmp_path, run_id="run-1", framing_text="different text", embedding_model="m",
    )
    raw = similarity_index._load(tmp_path)
    assert len(raw) == 1


def test_similarity_cosine_above_threshold_for_near_duplicates(tmp_path):
    # Identical framing should score 1.0
    text = "engineering investigation about diffusion bias under 800 kelvin"
    similarity_index.append_investigation(
        tmp_path, run_id="prior", framing_text=text, embedding_model="m",
    )
    hits = similarity_index.find_similar_investigations(
        tmp_path, framing_text=text, threshold=0.9,
    )
    assert len(hits) == 1
    assert hits[0]["score"] >= 0.9


# ── PreRegApprovalManager ───────────────────────────────────────────────────


def test_manager_request_blocks_until_response():
    mgr = PreRegApprovalManager()

    def responder():
        # Wait until the request thread has registered the pending entry
        for _ in range(200):
            if mgr.has_pending("run-1", "F1"):
                break
            time.sleep(0.01)
        mgr.respond(
            run_id="run-1", criterion_id="F1",
            response="approved", telegram_msg_id="42", approver="user",
        )

    threading.Thread(target=responder, daemon=True).start()
    response, msg_id, approver, _ = mgr.request_threshold_approval(
        run_id="run-1", criterion_id="F1", timeout_sec=2.0,
    )
    assert response == "approved"
    assert msg_id == "42"
    assert approver == "user"


def test_manager_returns_timeout_when_no_response():
    mgr = PreRegApprovalManager()
    response, *_ = mgr.request_threshold_approval(
        run_id="x", criterion_id="F1", timeout_sec=0.05,
    )
    assert response == "timeout"


def test_manager_respond_returns_false_when_no_waiter():
    mgr = PreRegApprovalManager()
    delivered = mgr.respond(
        run_id="ghost", criterion_id="F1", response="approved",
    )
    assert delivered is False


def test_manager_rejects_unknown_response_kind():
    mgr = PreRegApprovalManager()
    delivered = mgr.respond(
        run_id="r", criterion_id="F1", response="weird",  # type: ignore[arg-type]
    )
    assert delivered is False


def test_manager_cancel_all_wakes_waiters():
    mgr = PreRegApprovalManager()
    threading.Thread(
        target=lambda: (time.sleep(0.05), mgr.cancel_all()), daemon=True,
    ).start()
    response, *_ = mgr.request_threshold_approval(
        run_id="r", criterion_id="F1", timeout_sec=2.0,
    )
    assert response == "skipped"


# ── phase_framing ───────────────────────────────────────────────────────────


def test_phase_framing_parses_and_indexes(tmp_path):
    run_dir = tmp_path / "docs" / "scientific-investigation-x"
    run_dir.mkdir(parents=True)
    (run_dir / "audit").mkdir()
    provider = _ScriptedProvider([_good_framing_yaml()])
    framing = phase_framing(
        "investigate diffusion", provider,
        run_dir=run_dir, root_cwd=tmp_path, run_id="run-1",
        timeout_sec=600,
    )
    assert framing.question == "Is X true?"
    assert framing.discipline == "engineering"
    assert framing.framing_text  # non-empty
    # Indexed
    raw = similarity_index._load(tmp_path)
    assert len(raw) == 1 and raw[0]["run_id"] == "run-1"


def test_phase_framing_flags_similarity_hits(tmp_path):
    run_dir = tmp_path / "docs" / "scientific-investigation-x"
    run_dir.mkdir(parents=True)
    (run_dir / "audit").mkdir()
    # Pre-seed with an identical framing under a different run_id
    similarity_index.append_investigation(
        tmp_path, run_id="prior",
        framing_text="Engineering eval of X", embedding_model="m",
    )
    provider = _ScriptedProvider([_good_framing_yaml(framing_text="Engineering eval of X")])
    framing = phase_framing(
        "investigate", provider,
        run_dir=run_dir, root_cwd=tmp_path, run_id="run-2",
        timeout_sec=600,
    )
    assert any(h["run_id"] == "prior" for h in framing.similarity_hits)


def test_phase_framing_raises_on_empty_framing_text(tmp_path):
    run_dir = tmp_path / "docs" / "x"
    run_dir.mkdir(parents=True)
    (run_dir / "audit").mkdir()
    bad_yaml = """```yaml
question: Q
hypothesis: H
bias_statement: B
discipline: engineering
framing_text:
```"""
    provider = _ScriptedProvider([bad_yaml])
    with pytest.raises(ValueError, match="framing_text empty"):
        phase_framing(
            "x", provider,
            run_dir=run_dir, root_cwd=tmp_path, run_id="r",
            timeout_sec=60,
        )


# ── phase_prereg ────────────────────────────────────────────────────────────


def _make_run_dir(tmp_path: Path) -> Path:
    rd = tmp_path / "docs" / "scientific-investigation-x"
    (rd / "audit").mkdir(parents=True)
    return rd


def test_phase_prereg_norm_creates_audit_entry(tmp_path):
    rd = _make_run_dir(tmp_path)
    framing = _make_framing()
    provider = _ScriptedProvider([_good_prereg_yaml()])
    prereg = phase_prereg(
        framing, provider,
        run_dir=rd, run_id="run-1",
        timeout_sec=60, telegram_timeout_sec=1,
    )
    assert len(prereg.thresholds) == 1
    assert prereg.thresholds[0].source == "norm_reference"
    assert not prereg.discipline_warning
    entries = audit_trail.load_audit_entries(rd, entry_type="preregistration_threshold")
    assert len(entries) == 1
    assert entries[0]["criterion_id"] == "F1"
    assert entries[0]["reference"].startswith("DIN-EN-60068-2")


def test_phase_prereg_telegram_path_records_msg_id(tmp_path, monkeypatch):
    """A telegram-approval threshold gets its msg_id from the manager response.

    Note: when ALL thresholds use telegram_approval the disziplin-warning
    gate also fires — we approve it here so the test focuses on the msg_id.
    """
    rd = _make_run_dir(tmp_path)
    framing = _make_framing()
    yaml_block = """\
  - criterion_id: F1
    description: t
    threshold_value: 5%
    source: telegram_approval
    reference: ""
"""
    provider = _ScriptedProvider([_good_prereg_yaml(yaml_block)])
    mgr = get_manager()

    def responder():
        # Approve F1, then approve the discipline-warning gate.
        for criterion, msg_id in (("F1", "msg-77"), ("__discipline_warning__", "msg-warn")):
            for _ in range(200):
                if mgr.has_pending("run-1", criterion):
                    break
                time.sleep(0.01)
            mgr.respond(
                run_id="run-1", criterion_id=criterion,
                response="approved", telegram_msg_id=msg_id, approver="dominik",
            )

    threading.Thread(target=responder, daemon=True).start()
    notify_calls = []

    def fake_notify(run_id, criterion_id, t):
        notify_calls.append((run_id, criterion_id))
        return "msg-77"

    prereg = phase_prereg(
        framing, provider,
        run_dir=rd, run_id="run-1",
        timeout_sec=60, telegram_timeout_sec=2,
        notify_callable=fake_notify,
    )
    assert prereg.thresholds[0].telegram_msg_id == "msg-77"
    assert notify_calls == [("run-1", "F1")]
    assert prereg.discipline_warning_approved is True


def test_phase_prereg_discipline_warning_when_all_telegram(tmp_path, monkeypatch):
    rd = _make_run_dir(tmp_path)
    framing = _make_framing()
    yaml_block = """\
  - criterion_id: F1
    description: t
    threshold_value: 5%
    source: telegram_approval
    reference: ""
"""
    provider = _ScriptedProvider([_good_prereg_yaml(yaml_block)])
    mgr = get_manager()

    def responder():
        # First respond F1, then __discipline_warning__
        for criterion in ("F1", "__discipline_warning__"):
            for _ in range(200):
                if mgr.has_pending("run-1", criterion):
                    break
                time.sleep(0.01)
            mgr.respond(
                run_id="run-1", criterion_id=criterion,
                response="approved", telegram_msg_id=f"msg-{criterion}",
                approver="user",
            )

    threading.Thread(target=responder, daemon=True).start()
    prereg = phase_prereg(
        framing, provider,
        run_dir=rd, run_id="run-1",
        timeout_sec=60, telegram_timeout_sec=2,
    )
    assert prereg.discipline_warning is True
    assert prereg.discipline_warning_approved is True
    warnings = audit_trail.load_audit_entries(rd, entry_type="preregistration_warning")
    assert len(warnings) == 1
    assert warnings[0]["user_response"] == "approved"


def test_phase_prereg_discipline_warning_rejected_aborts(tmp_path):
    rd = _make_run_dir(tmp_path)
    framing = _make_framing()
    yaml_block = """\
  - criterion_id: F1
    description: t
    threshold_value: 5%
    source: telegram_approval
    reference: ""
"""
    provider = _ScriptedProvider([_good_prereg_yaml(yaml_block)])
    mgr = get_manager()

    def responder():
        # Approve threshold but reject discipline-warning
        for _ in range(200):
            if mgr.has_pending("run-1", "F1"):
                break
            time.sleep(0.01)
        mgr.respond(run_id="run-1", criterion_id="F1", response="approved")
        for _ in range(200):
            if mgr.has_pending("run-1", "__discipline_warning__"):
                break
            time.sleep(0.01)
        mgr.respond(run_id="run-1", criterion_id="__discipline_warning__", response="rejected")

    threading.Thread(target=responder, daemon=True).start()
    with pytest.raises(RuntimeError, match="Disziplin-Warnung"):
        phase_prereg(
            framing, provider,
            run_dir=rd, run_id="run-1",
            timeout_sec=60, telegram_timeout_sec=2,
        )


def test_phase_prereg_rejects_too_many_thresholds(tmp_path):
    rd = _make_run_dir(tmp_path)
    block = "".join(
        f"""  - criterion_id: F{i}
    description: t{i}
    threshold_value: {i}
    source: norm_reference
    reference: DIN-Standard §{i} value {i}%
"""
        for i in range(1, 7)
    )
    provider = _ScriptedProvider([_good_prereg_yaml(block)])
    with pytest.raises(ValueError, match="max 5"):
        phase_prereg(
            _make_framing(), provider,
            run_dir=rd, run_id="r",
            timeout_sec=60, telegram_timeout_sec=1,
        )


def test_phase_prereg_rejects_non_sequential_criterion_ids(tmp_path):
    rd = _make_run_dir(tmp_path)
    block = """  - criterion_id: F1
    description: t
    threshold_value: 5
    source: norm_reference
    reference: DIN-EN-60068-2 §1 Toleranz 5% bei 800K
  - criterion_id: F3
    description: t
    threshold_value: 7
    source: norm_reference
    reference: DIN-EN-60068-2 §2 Toleranz 7% bei 900K
"""
    provider = _ScriptedProvider([_good_prereg_yaml(block)])
    with pytest.raises(ValueError, match="must be F2"):
        phase_prereg(
            _make_framing(), provider,
            run_dir=rd, run_id="r",
            timeout_sec=60, telegram_timeout_sec=1,
        )


# ── write_plan_md ───────────────────────────────────────────────────────────


def test_write_plan_md_renders_full_document(tmp_path):
    rd = _make_run_dir(tmp_path)
    framing = _make_framing()
    framing.similarity_hits = [{"run_id": "prior", "score": 0.85, "ts_utc": "2026-01-01T00:00:00Z"}]
    prereg = _make_prereg([
        Threshold("F1", "desc", "5%", "norm_reference", "DIN-EN §4.3 5%"),
    ])
    plan = write_plan_md(rd, task="orig task", framing=framing, prereg=prereg)
    text = plan.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "prereg_hash:" in text
    assert "Cross-Investigation-Similarity-Hinweise" in text
    assert "F1" in text
    assert "DIN-EN" in text


def test_write_plan_md_includes_discipline_warning_block(tmp_path):
    rd = _make_run_dir(tmp_path)
    framing = _make_framing()
    prereg = _make_prereg(
        [Threshold("F1", "d", "5%", "telegram_approval", "", "msg-1", "u")],
        discipline_warning=True,
        discipline_warning_approved=True,
    )
    text = write_plan_md(rd, task="t", framing=framing, prereg=prereg).read_text("utf-8")
    assert "Disziplin-Warnung" in text
    assert "discipline_warning: true" in text


# ── Tool-level integration: I1 happy path ───────────────────────────────────


def test_tool_run_executes_phase0_and_phase05(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    tool = ScientificInvestigationTool()
    # I3 needs framing + prereg + author plan + DA review + Methodiker review
    provider = _ScriptedProvider([
        _good_framing_yaml(),
        _good_prereg_yaml(),
        "```yaml\nsub_tasks:\n  - sub_id: S1\n    title: t\n    description: d\n    "
        "addresses_criteria: [F1]\n    type: data_analysis\n    expected_output: o\n```",
        "```yaml\nfindings: []\n```",
        "```yaml\nfindings: []\n```",
    ])
    result = tool.run("investigate diffusion", provider, cwd=str(tmp_path))
    assert result.success is True
    # I4 reaches phase 3 (execution-loop) end-to-end. _patch_notifier installs
    # a stub Phase-3 executor so the sub-tasks run instantly.
    assert result.error_code == "i4_phase3_done"
    assert result.iterations == 4
    run_dir = next((tmp_path / "docs").glob("scientific-investigation-*"))
    assert (run_dir / "plan.md").is_file()
    assert (run_dir / "audit" / "approvals.jsonl").is_file()
    state_path = next(
        (tmp_path / ".scientific-investigation").glob("*/state.json")
    )
    state = json.loads(state_path.read_text("utf-8"))
    assert state["phase"] == "phase3_execution_done"
    assert state["rigor_cap"] is None  # norm_reference present → no LOW cap


def test_tool_run_returns_phase0_failure_on_bad_yaml(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider(["this is not yaml at all"])
    result = tool.run("x", provider, cwd=str(tmp_path))
    assert result.success is False
    assert result.error_code == "phase0_failed"


def test_tool_run_returns_phase05_failure_on_bad_prereg(monkeypatch, tmp_path):
    _patch_notifier(monkeypatch)
    tool = ScientificInvestigationTool()
    provider = _ScriptedProvider([
        _good_framing_yaml(),
        "```yaml\nthresholds: []\n```",  # empty thresholds
    ])
    result = tool.run("x", provider, cwd=str(tmp_path))
    assert result.success is False
    assert result.error_code == "phase05_failed"


# ── Helpers (data fixtures) ─────────────────────────────────────────────────


def _make_framing():
    from tools.scientific_investigation_phases import FramingResult
    return FramingResult(
        question="Q",
        hypothesis="H",
        bias_statement="B",
        discipline="engineering",
        framing_text="engineering eval text long enough",
    )


def _make_prereg(thresholds, *, discipline_warning=False, discipline_warning_approved=False):
    from tools.scientific_investigation_phases import PreRegResult
    return PreRegResult(
        thresholds=thresholds,
        discipline_warning=discipline_warning,
        discipline_warning_approved=discipline_warning_approved,
        prereg_hash=compute_prereg_hash(thresholds),
    )


# ── Telegram listener routing ──────────────────────────────────────────────


def test_telegram_route_to_si_manager_handles_pending_approval(monkeypatch):
    """The /approve handler must route to the manager when a pending entry matches."""
    # Build a stand-alone listener instance and inject a pending approval.
    from telegram_listener import TelegramListener

    sent: list[str] = []
    monkeypatch.setattr(
        "telegram_listener.send_message",
        lambda text, **kw: sent.append(text),
    )
    monkeypatch.setattr("telegram_listener.TELEGRAM_CHAT_ID", "chat-1")

    listener = TelegramListener.__new__(TelegramListener)  # bypass __init__

    mgr = get_manager()
    # Register a pending entry (simulated)
    pending_event = threading.Event()
    with mgr._lock:
        from tools.scientific_investigation_approvals import _Pending
        mgr._pending[("run-A", INVESTIGATION_CRITERION)] = _Pending(event=pending_event)

    routed = listener._route_to_si_manager("run-A", response="approved")
    assert routed is True
    assert pending_event.is_set()


def test_telegram_route_to_si_manager_returns_false_for_unknown(monkeypatch):
    from telegram_listener import TelegramListener

    monkeypatch.setattr(
        "telegram_listener.send_message",
        lambda text, **kw: None,
    )
    monkeypatch.setattr("telegram_listener.TELEGRAM_CHAT_ID", "chat-1")

    listener = TelegramListener.__new__(TelegramListener)
    routed = listener._route_to_si_manager("unknown-run", response="approved")
    assert routed is False


def test_telegram_route_to_si_manager_handles_criterion_id(monkeypatch):
    from telegram_listener import TelegramListener
    from tools.scientific_investigation_approvals import _Pending

    monkeypatch.setattr("telegram_listener.send_message", lambda *a, **kw: None)
    monkeypatch.setattr("telegram_listener.TELEGRAM_CHAT_ID", "chat-1")

    listener = TelegramListener.__new__(TelegramListener)
    mgr = get_manager()
    ev = threading.Event()
    with mgr._lock:
        mgr._pending[("run-X", "F2")] = _Pending(event=ev)

    routed = listener._route_to_si_manager("run-X F2 some reason text", response="rejected")
    assert routed is True
    assert ev.is_set()
