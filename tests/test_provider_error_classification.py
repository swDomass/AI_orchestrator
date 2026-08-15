"""Tests for error_code_of() / is_transient() in providers/base.py.

RunResult.error is an unconstrained string. Providers put stable codes there,
but also prefixed details ("rate_limit: 429 ...") and raw stderr. Tools classify
it to fill ToolResult.error_code/.retryable — getting this wrong means either a
transient failure is finalized instead of retried, or a stderr dump lands in a
field the taxonomy expects to hold a stable code.
"""
from unittest.mock import patch

import pytest

with patch("config._load_dotenv"):
    from providers.base import TRANSIENT_ERRORS, error_code_of, is_transient


class TestTransientSetMatchesOrchestrator:
    def test_orchestrator_uses_this_very_object(self):
        """The in-run backoff bail-out must classify identically to the tools.

        This asserts identity, not equality: orchestrator imports the constant
        rather than repeating it. Reintroducing an inline tuple there fails here.
        """
        with patch("config._load_dotenv"):
            import orchestrator

        assert orchestrator.TRANSIENT_ERRORS is TRANSIENT_ERRORS

    def test_documents_the_current_set(self):
        """Pins the values so a silent widening shows up as a failing test."""
        assert set(TRANSIENT_ERRORS) == {
            "rate_limit", "unreachable", "timeout", "hang", "stdin_incomplete"
        }

    @pytest.mark.parametrize("code", ["unreachable", "hang"])
    def test_codes_that_an_earlier_whitelist_missed(self, code):
        """Regression: both were omitted, so a hung run was finalized, not retried."""
        assert is_transient(code) is True


class TestPrefixedErrors:
    """OpenRouter and Gemini emit "code: detail" instead of a bare code."""

    @pytest.mark.parametrize("error", [
        "rate_limit: 429 Too Many Requests",
        "unreachable: connection reset by peer",
        "timeout: read timed out after 600s",
    ])
    def test_prefixed_transient_is_recognised(self, error):
        assert is_transient(error) is True
        assert error_code_of(error) == error.split(":")[0]

    def test_prefixed_non_transient_keeps_its_code(self):
        assert error_code_of("auth_error: invalid api key") == "auth_error"
        assert is_transient("auth_error: invalid api key") is False

    def test_model_refusal_keeps_its_code(self):
        """Gemini emits this prefix; it is its own taxonomy category.

        Without it in the known set the code is lost and the failure is booked
        as a generic tool error instead of a refusal.
        """
        assert error_code_of("model_refusal: prompt blocked (SAFETY)") == "model_refusal"
        assert is_transient("model_refusal: prompt blocked (SAFETY)") is False

    def test_http_status_is_deliberately_not_a_code(self):
        """http_<status> is not part of the stable taxonomy — no code, no retry."""
        assert error_code_of("http_429: rate limited upstream") == ""


class TestProseIsNotAnErrorCode:
    """Raw stderr and exception text must not be passed off as a taxonomy code."""

    @pytest.mark.parametrize("error", [
        "Traceback (most recent call last): ValueError: boom",
        "claude CLI not found",
        "empty output",
        "incomplete stream-json output, 3 of 5 chunks",
    ])
    def test_prose_yields_empty_code(self, error):
        assert error_code_of(error) == ""
        assert is_transient(error) is False

    def test_prose_with_a_colon_does_not_leak_a_fake_code(self):
        assert error_code_of("Error: something went wrong") == ""


class TestEmptyAndEdgeCases:
    def test_empty_string(self):
        assert error_code_of("") == ""
        assert is_transient("") is False

    def test_whitespace_around_code_is_tolerated(self):
        assert error_code_of("  rate_limit : detail") == "rate_limit"

    def test_bare_codes_round_trip(self):
        for code in TRANSIENT_ERRORS:
            assert error_code_of(code) == code
            assert is_transient(code) is True
