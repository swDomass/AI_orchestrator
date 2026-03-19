import config


def test_task_timeout_default_is_fifteen_minutes():
    assert config.TASK_TIMEOUT_SEC == 900


def test_tool_loop_timeouts_are_conservative():
    assert config.TOOL_REVIEW_TIMEOUT_SEC == 1_200
    assert config.TOOL_FIX_TIMEOUT_SEC == 2_400


def test_dev_loop_timeouts():
    assert config.TOOL_DEV_RESEARCH_TIMEOUT_SEC == 3_600
    assert config.TOOL_DEV_PLAN_TIMEOUT_SEC == 1_800
    assert config.TOOL_DEV_EXEC_TIMEOUT_SEC == 7_200
    assert config.TOOL_DEV_QUALITY_REVIEW_TIMEOUT_SEC == 3_600
    assert config.TOOL_DEV_RESOLUTION_REVIEW_TIMEOUT_SEC == 1_800
