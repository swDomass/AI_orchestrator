"""Pytest fixtures shared across the suite.

- ``sys.path``: makes project packages importable.
- ``_isolate_replay_store``: autouse — prevents tests from polluting the
  production ``logs/runs.jsonl`` when they exercise code paths that emit
  replay records (e.g. orchestrator ``_RunSpan.emit``). Individual test
  files can still override the path with their own fixture.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import replay  # noqa: E402 — must follow sys.path tweak


@pytest.fixture(autouse=True)
def _isolate_replay_store(tmp_path: Path):
    """Redirect the replay JSONL store + archive into pytest's tmp_path.

    Restores the previous paths after each test so production defaults are
    not mutated globally. Falls back to a no-op when ``replay`` lacks the
    expected hooks (e.g. partial import during collection-time errors).
    """
    saved_store = getattr(replay, "_store_path", None)
    saved_archive = getattr(replay, "_archive_dir", None)
    setter = getattr(replay, "set_store_path", None)
    resetter = getattr(replay, "reset_for_tests", None)
    if setter is None:
        yield
        return
    setter(tmp_path / "runs.jsonl", tmp_path / "runs-archive")
    try:
        yield
    finally:
        if resetter is not None:
            try:
                resetter()
            except Exception:  # noqa: BLE001 — teardown must never fail tests
                pass
        if saved_store is not None and setter is not None:
            setter(saved_store, saved_archive)


@pytest.fixture(autouse=True)
def _isolate_gemini_api_key(monkeypatch):
    """Default GEMINI_API_KEY to empty so the suite is hermetic against a real
    key in the developer's .env.

    With a key present, the Gemini provider switches to HTTP-API mode
    (always-available, cclimits refresh skipped) — which would otherwise flip
    the CLI-mode / limits-governed assumptions baked into unrelated dispatcher,
    limits and provider-permission tests. Tests that exercise HTTP mode set the
    key explicitly (see test_providers_gemini.py)."""
    monkeypatch.setattr("config.GEMINI_API_KEY", "", raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_openrouter_api_key(monkeypatch):
    """Default OPENROUTER_API_KEY to empty so the suite is hermetic against a
    real key in the developer's .env.

    Added alongside limits.py's opencode budget override (2026-09-04):
    limits._get_limits_fresh() now calls openrouter_budget.fetch_budget()
    unconditionally on every refresh, which makes a real GET /api/v1/key HTTP
    call whenever config.OPENROUTER_API_KEY is truthy. Dozens of existing
    tests in tests/test_limits.py call limits._get_limits_fresh()/get_limits()
    directly without mocking that call — without this fixture they would fire
    real network requests using the developer's real key on every run (this
    repo's .env has one configured, confirmed 2026-09-04). Same problem, same
    fix shape as _isolate_gemini_api_key above; test_heartbeat_model_check.py
    already carries a narrower, file-local version of this exact guard for the
    same underlying reason (there: dispatcher._llm_check_for_newer_models
    trying OpenRouter first when configured).

    Tests that need a real-shaped key set it explicitly (see
    tests/test_providers_openrouter.py's `provider` fixture, tests/
    test_openrouter_budget.py's `_configured_key` fixture) — those run after
    this one and simply override the value for their own test.
    """
    monkeypatch.setattr("config.OPENROUTER_API_KEY", "", raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_active_runs_dir(tmp_path: Path, monkeypatch):
    """Redirect ActiveRunRegistry writes into pytest's tmp_path.

    Tool-tests construct ToolTracer instances which mirror lifecycle events
    into a central ``logs/active_runs/`` directory. Without isolation those
    files leak between tests and pollute the production repo.
    """
    try:
        from tools import base_tool
    except ImportError:
        yield
        return
    monkeypatch.setattr(base_tool, "ACTIVE_RUNS_DIR", tmp_path / "active_runs")
    yield


@pytest.fixture(autouse=True)
def _isolate_policy_engine(tmp_path: Path, monkeypatch):
    """Point the PolicyEngine singleton at an empty vault so the suite is hermetic
    against the developer's real ``99_System/AI/policy.yaml``.

    Without this the live file decides test outcomes: since provider lookups are
    filtered through ``tool_providers`` (dispatcher.policy_allows_provider and the
    forced-tag gate), a machine whose policy.yaml bars gemini/openrouter/vibe gets
    different routing results than a machine without a policy file at all — and the
    failure looks like a routing bug, not a fixture leak.

    Tests that need a policy build their own engine and monkeypatch
    ``policy._engine`` themselves; that assignment simply wins over this one.
    """
    try:
        import policy as policy_module
    except ImportError:
        yield
        return
    # Path deliberately not created: PolicyEngine tolerates a missing policy.yaml
    # (_reload_if_changed returns early), so this costs one stat() per test.
    monkeypatch.setattr(
        policy_module,
        "_engine",
        policy_module.PolicyEngine(vault_path=tmp_path / "_empty_vault"),
    )
    yield
