from tools.test_loop import _tests_passed


def test_tests_passed_accepts_zero_failed_zero_errors_summary():
    output = "==================== 12 passed, 0 failed, 0 errors in 3.21s ===================="
    assert _tests_passed(output) is True


def test_tests_passed_rejects_nonzero_failed_summary():
    output = "==================== 10 passed, 2 failed, 0 errors in 3.21s ===================="
    assert _tests_passed(output) is False
