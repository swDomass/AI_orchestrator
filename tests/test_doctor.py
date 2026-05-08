from unittest.mock import patch

import doctor


def test_check_claude_cli_does_not_fake_auth_verification():
    base_result = doctor.CheckResult(doctor.PASS, "Claude CLI", "claude 1.2.3")

    with (
        patch.object(doctor, "_check_cli", return_value=base_result),
        patch.object(doctor.subprocess, "run", side_effect=AssertionError("unexpected subprocess call")),
    ):
        result = doctor.check_claude_cli()

    assert result.status == doctor.PASS
    assert result.label == "Claude CLI"
    assert "auth not verified" in result.message.lower()


# ── check_model_aliases ───────────────────────────────────────────────────────

def test_check_model_aliases_pass_when_all_alive():
    with patch("heartbeat._probe_model", return_value=(True, "")):
        r = doctor.check_model_aliases()
    assert r.status == doctor.PASS
    assert "verified" in r.message.lower()


def test_check_model_aliases_fail_on_dead_id():
    def fake_probe(provider, model_id, **kw):
        if model_id.startswith("claude-haiku"):
            return False, "model not found"
        return True, ""

    with patch("heartbeat._probe_model", side_effect=fake_probe):
        r = doctor.check_model_aliases()

    assert r.status == doctor.FAIL
    assert "dead" in r.message.lower()
    assert "claude-haiku" in r.message
    assert "MODEL_ALIASES" in r.fix_hint


def test_check_model_aliases_warn_on_transient():
    def fake_probe(provider, model_id, **kw):
        return True, "transient (rate_limit)"

    with patch("heartbeat._probe_model", side_effect=fake_probe):
        r = doctor.check_model_aliases()

    assert r.status == doctor.WARN
    assert "unverified" in r.message


def test_check_model_aliases_handles_probe_exceptions():
    """Per-probe exceptions must not crash the doctor — they count as transient (alive=True)."""
    def fake_probe(provider, model_id, **kw):
        raise RuntimeError("boom")

    with patch("heartbeat._probe_model", side_effect=fake_probe):
        r = doctor.check_model_aliases()

    # Exceptions wrapped as transient → since detail starts with "transient", warn
    assert r.status == doctor.WARN
