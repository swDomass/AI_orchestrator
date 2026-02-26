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
