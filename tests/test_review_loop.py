from tools.review_loop import _is_no_findings_output


def test_no_findings_sentinel_accepts_slash_format():
    assert _is_no_findings_output("No P1/P2/P3 findings.") is True


def test_no_findings_sentinel_accepts_comma_format():
    assert _is_no_findings_output("No P1, P2, P3 findings.") is True
