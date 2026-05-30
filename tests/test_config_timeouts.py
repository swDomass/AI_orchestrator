import config


def test_task_timeout_default_is_hard_backstop():
    # Watchdog refactor: TASK_TIMEOUT_SEC is now the generous HARD backstop, not
    # an aggressive deadline. Liveness/idle handles hang detection.
    assert config.TASK_TIMEOUT_SEC == 5400


def test_idle_timeout_defaults():
    assert config.TASK_IDLE_TIMEOUT_SEC == 300  # Claude (tool-aware)
    assert config.CLI_IDLE_TIMEOUT_NO_LIVENESS_SEC == 1200  # Gemini/Codex (byte-only)
    assert config.MAX_HANG_RETRIES == 2
    assert config.TOOL_DEFAULT_MAX_RUNTIME_SEC == 3600


def test_tool_loop_timeouts_are_conservative():
    assert config.TOOL_REVIEW_TIMEOUT_SEC == 1_200
    assert config.TOOL_FIX_TIMEOUT_SEC == 2_400


def test_dev_loop_timeouts():
    assert config.TOOL_DEV_RESEARCH_TIMEOUT_SEC == 3_600
    assert config.TOOL_DEV_PLAN_TIMEOUT_SEC == 1_800
    assert config.TOOL_DEV_EXEC_TIMEOUT_SEC == 7_200
    assert config.TOOL_DEV_QUALITY_REVIEW_TIMEOUT_SEC == 3_600
    assert config.TOOL_DEV_RESOLUTION_REVIEW_TIMEOUT_SEC == 1_800
