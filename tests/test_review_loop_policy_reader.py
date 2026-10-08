"""review-loop reads its two policy switches for real (2026-10-08).

`_should_verify` and `_drift_check_mode` imported a `policy.load_policy` that never
existed; the ImportError was caught, so `verification: skip` and `drift_check_mode`
never took effect — and tests/test_review_loop.py replaces `_drift_check_mode` with
a lambda, so nothing ever noticed. They now go the way dev-loop's plan_approval goes
since 2026-10-06: `get_engine().get_tool_phase("review-loop", …)`.

Real PolicyEngine on a real policy.yaml in tmp_path, installed as the singleton —
the reader itself is never mocked (after tests/test_dev_loop_plan_approval.py).
"""

import pytest

import policy as policy_module
from providers.base import RunResult
from tools.review_loop import _VERIFICATION_PROMPT_BODY, ReviewLoopTool


def _install_engine(tmp_path, monkeypatch, policy_yaml: str | None) -> policy_module.PolicyEngine:
    """`policy_yaml=None` writes no file at all (the "policy.yaml missing" case)."""
    ai = tmp_path / "vault" / "99_System" / "AI"
    ai.mkdir(parents=True)
    if policy_yaml is not None:
        (ai / "policy.yaml").write_text(policy_yaml, encoding="utf-8")
    engine = policy_module.PolicyEngine(vault_path=tmp_path / "vault")
    monkeypatch.setattr(policy_module, "_engine", engine)
    return engine


def _phases(**keys: str) -> str:
    body = "".join(f"    {k}: {v}\n" for k, v in keys.items())
    return f"tool_phases:\n  review-loop:\n{body}"


def _boom(*_a, **_kw):
    raise RuntimeError("engine not loadable")


_UNPARSEABLE = "tool_phases: {review-loop: [\n"


# ── verification ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("policy_yaml, expected", [
    (_phases(verification="skip"), False),
    (_phases(verification="auto"), True),
    (_phases(verification="Skip"), True),          # case is not folded: only `skip` skips
    (_phases(drift_check_mode="always"), True),    # key missing
    ("tool_phases:\n  dev-loop:\n    plan_approval: approve\n", True),  # no review-loop entry
    ("tool_providers:\n  default: [claude]\n", True),                   # no tool_phases
    ("", True),                                    # empty file = nothing configured
    (None, True),                                  # file missing
    (_UNPARSEABLE, True),                          # unreadable → the safe side
    ("tool_phases:\n  review-loop: skip\n", True),  # entry not a mapping → unreadable
], ids=[
    "skip", "auto", "capitalised", "key_missing", "tool_missing", "no_tool_phases",
    "empty_file", "file_missing", "unparseable", "entry_not_mapping",
])
def test_should_verify(monkeypatch, tmp_path, policy_yaml, expected):
    _install_engine(tmp_path, monkeypatch, policy_yaml)
    assert ReviewLoopTool()._should_verify() is expected


def test_should_verify_engine_failure_means_verify(monkeypatch):
    monkeypatch.setattr(policy_module, "get_engine", _boom)
    assert ReviewLoopTool()._should_verify() is True


def test_an_unreadable_switch_is_logged(monkeypatch, tmp_path, caplog):
    _install_engine(tmp_path, monkeypatch, _UNPARSEABLE)

    with caplog.at_level("WARNING", logger="tools.review_loop"):
        ReviewLoopTool()._should_verify()
        ReviewLoopTool()._drift_check_mode()

    messages = [r.getMessage() for r in caplog.records if r.name == "tools.review_loop"]
    assert any("verification" in m and "nicht lesbar" in m for m in messages), messages
    assert any("drift_check_mode" in m and "nicht lesbar" in m for m in messages), messages


# ── drift_check_mode ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("policy_yaml, expected", [
    (_phases(drift_check_mode="always"), "always"),
    (_phases(drift_check_mode="skip"), "skip"),
    (_phases(drift_check_mode="auto"), "auto"),
    (_phases(drift_check_mode="alwyas"), "auto"),      # typo
    (_phases(drift_check_mode="yes"), "auto"),         # yaml bool → "True"
    (_phases(verification="skip"), "auto"),            # key missing
    (None, "auto"),                                    # file missing
    (_UNPARSEABLE, "auto"),                            # unreadable
], ids=["always", "skip", "auto", "typo", "yaml_bool", "key_missing", "file_missing", "unparseable"])
def test_drift_check_mode(monkeypatch, tmp_path, policy_yaml, expected):
    _install_engine(tmp_path, monkeypatch, policy_yaml)
    assert ReviewLoopTool()._drift_check_mode() == expected


def test_drift_check_mode_engine_failure_means_auto(monkeypatch):
    monkeypatch.setattr(policy_module, "get_engine", _boom)
    assert ReviewLoopTool()._drift_check_mode() == "auto"


# ── end to end: the switch reaches the run ───────────────────────────────────

class _Scripted:
    """After tests/test_review_loop.py::_ScriptedProvider."""
    name = "codex"

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = list(outputs)
        self.prompts: list[str] = []
        self._forced_model: str | None = None

    def run(self, task, cwd=None, timeout=0, read_only=False, **_kw):
        self.prompts.append(task)
        if not self._outputs:
            return RunResult(success=False, error="no scripted output left")
        return RunResult(success=True, output=self._outputs.pop(0))


def _patch_run(monkeypatch):
    monkeypatch.setattr("tools.review_loop.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.review_loop.time.sleep", lambda _: None)
    monkeypatch.setattr("tools.review_loop.is_cached_provider_available", lambda _name: True)


_VERIFY_MARK = _VERIFICATION_PROMPT_BODY.strip().splitlines()[0]


@pytest.mark.parametrize("policy_yaml, verified", [
    (_phases(verification="skip", drift_check_mode="skip"), False),
    (_phases(verification="auto", drift_check_mode="skip"), True),
], ids=["skip", "auto"])
def test_verification_switch_reaches_the_run(monkeypatch, tmp_path, policy_yaml, verified):
    _patch_run(monkeypatch)
    _install_engine(tmp_path, monkeypatch, policy_yaml)
    provider = _Scripted([
        "No P1/P2/P3 findings.",      # review iter 1 — clean
        "VERIFIED",                   # verification (only when it runs)
        "Pattern: x\nTool-Hint: y",   # summarizer
    ])

    result = ReviewLoopTool().run("Review now", provider, cwd=str(tmp_path))

    assert result.success is True
    assert any(_VERIFY_MARK in p for p in provider.prompts) is verified


def test_drift_check_switch_reaches_the_run(monkeypatch, tmp_path):
    """`always` adds the Goal-Adherence call in round 1 — read from the yaml, not a lambda."""
    _patch_run(monkeypatch)
    _install_engine(tmp_path, monkeypatch, _phases(drift_check_mode="always"))
    provider = _Scripted([
        "- [P2] Minor issue",            # review iter 1
        "ON_TOPIC: looks fine",          # drift check iter 1
        "Fixed it",                      # fix iter 1
        "No P1/P2/P3 findings.",         # review iter 2
        "VERIFIED",                      # verification
        "Pattern: x\nTool-Hint: y",      # summarizer
    ])

    result = ReviewLoopTool().run("Review now", provider, cwd=str(tmp_path))

    assert result.success is True
    assert "Goal-Adherence" in provider.prompts[1]
