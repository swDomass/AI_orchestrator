import config


def test_task_timeout_default_is_five_minutes():
    assert config.TASK_TIMEOUT_SEC == 300


def test_tool_loop_timeouts_are_conservative():
    assert config.TOOL_REVIEW_TIMEOUT_SEC == 1_200
    assert config.TOOL_FIX_TIMEOUT_SEC == 2_400
