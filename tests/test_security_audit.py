"""Tests for the security audit tool.

Focus: the provider-failure paths. RunResult has no .error_code/.retryable —
reading them raised AttributeError on every provider failure in both phases.
Classification now goes through providers.base (see
test_provider_error_classification.py for the classifier itself).
"""
from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    from providers.base import RunResult
    from tools.security_audit import SecurityAuditTool


# ── Helpers ────────────────────────────────────────────────────────────


class _FailAtProvider:
    """Succeeds until the given call index, then returns the supplied RunResult."""

    def __init__(self, name: str, fail_on_call: int, result: RunResult):
        self.name = name
        self._fail_on_call = fail_on_call
        self._result = result
        self.calls: list[dict] = []

    def run(self, task: str, cwd: str | None = None, timeout: int = 0,
            read_only: bool = False) -> RunResult:
        self.calls.append({"task": task, "cwd": cwd, "timeout": timeout, "read_only": read_only})
        if len(self.calls) == self._fail_on_call:
            return self._result
        return RunResult(success=True, output="no findings")


def _noop(*_args, **_kwargs):
    pass


@pytest.fixture
def _patch(monkeypatch):
    """Suppress notifications and external calls."""
    monkeypatch.setattr("tools.security_audit.notify_tool_done", _noop)
    monkeypatch.setattr("tools.security_audit.notify_tool_progress", _noop)
    monkeypatch.setattr("tools.security_audit.is_cached_provider_available", lambda _name: True)


def _run(tmp_path, fail_on_call: int, result: RunResult):
    provider = _FailAtProvider("claude", fail_on_call, result)
    return SecurityAuditTool().run("Audit", provider, cwd=str(tmp_path)), provider


# ── Both phases, parametrised over the phase that fails ────────────────

# call 1 = audit phase, call 2 = fix phase. The same defect sat in both.
PHASES = [pytest.param(1, id="audit-phase"), pytest.param(2, id="fix-phase")]


class TestProviderFailureDoesNotCrash:
    @pytest.mark.parametrize("call", PHASES)
    def test_transient_error_is_retryable(self, tmp_path, _patch, call):
        """Regression: this raised AttributeError instead of returning a ToolResult."""
        result, provider = _run(tmp_path, call, RunResult(success=False, error="rate_limit"))

        assert result.success is False
        assert result.error_code == "rate_limit"
        assert result.retryable is True
        assert len(provider.calls) == call

    @pytest.mark.parametrize("call", PHASES)
    @pytest.mark.parametrize("code", ["unreachable", "hang"])
    def test_codes_an_earlier_whitelist_missed(self, tmp_path, _patch, call, code):
        """A hung or unreachable run must reach the orchestrator's retry handling."""
        result, _ = _run(tmp_path, call, RunResult(success=False, error=code))

        assert result.error_code == code
        assert result.retryable is True

    @pytest.mark.parametrize("call", PHASES)
    def test_prefixed_error_is_still_classified(self, tmp_path, _patch, call):
        """OpenRouter emits "rate_limit: <detail>", which exact matching missed."""
        result, _ = _run(tmp_path, call, RunResult(success=False, error="rate_limit: 429 slow down"))

        assert result.error_code == "rate_limit"
        assert result.retryable is True

    @pytest.mark.parametrize("call", PHASES)
    def test_prose_error_is_not_retryable_and_yields_no_code(self, tmp_path, _patch, call):
        result, _ = _run(tmp_path, call, RunResult(success=False, error="claude CLI not found"))

        assert result.success is False
        assert result.error_code == ""
        assert result.retryable is False
        # The human-readable text stays available on .error
        assert "claude CLI not found" in result.error


class TestFailureWithEmptyError:
    """success=False with an empty .error must not be read as success."""

    def test_audit_phase_does_not_continue_into_fix(self, tmp_path, _patch):
        result, provider = _run(tmp_path, 1, RunResult(success=False, error=""))

        assert result.success is False
        assert len(provider.calls) == 1, "must not have started the fix phase"

    def test_fix_phase_failure_is_not_reported_as_success(self, tmp_path, _patch):
        result, _ = _run(tmp_path, 2, RunResult(success=False, error=""))

        assert result.success is False


class TestSuccessPath:
    def test_clean_run_reports_success(self, tmp_path, _patch):
        """Guards the failure checks against over-triggering on healthy runs."""
        provider = _FailAtProvider("claude", fail_on_call=0, result=RunResult(success=True))
        result = SecurityAuditTool().run("Audit", provider, cwd=str(tmp_path))

        assert result.success is True
        assert result.retryable is False
        assert len(provider.calls) == 2
