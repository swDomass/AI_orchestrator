from tools.test_loop import _tests_passed


def test_tests_passed_accepts_zero_failed_zero_errors_summary():
    output = "==================== 12 passed, 0 failed, 0 errors in 3.21s ===================="
    assert _tests_passed(output) is True


def test_tests_passed_rejects_nonzero_failed_summary():
    output = "==================== 10 passed, 2 failed, 0 errors in 3.21s ===================="
    assert _tests_passed(output) is False


def test_tests_passed_returns_false_for_unknown_output_with_failure_keywords():
    # "failed" in output should NOT return True — that would be inverted logic
    assert _tests_passed("some error occurred during test run") is False


def test_tests_passed_returns_false_for_unknown_output_without_keywords():
    # Unknown format with no success or failure markers → assume failed
    assert _tests_passed("some unknown test runner output") is False
