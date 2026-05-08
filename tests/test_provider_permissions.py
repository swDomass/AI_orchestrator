from types import SimpleNamespace

from providers.claude import ClaudeProvider
from providers.codex import CodexProvider
from providers.gemini import GeminiProvider


def test_claude_read_only_disables_write_capable_tools(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="empty output")

    monkeypatch.setattr("providers.claude.subprocess.run", fake_run)

    ClaudeProvider().run("inspect", read_only=True)

    cmd = calls[0][0]
    assert "--dangerously-skip-permissions" not in cmd
    assert "--allowedTools" in cmd
    # Read-only scope plus Task (for read-only subagent flows like
    # deep-security-audit's 6-persona fan-out).
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "Read" in allowed and "Glob" in allowed and "Grep" in allowed
    assert "Task" in allowed
    assert "Write" not in allowed
    assert "Edit" not in allowed
    assert "Bash" not in allowed


def test_codex_read_only_uses_read_only_sandbox_without_approvals(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="empty output")

    monkeypatch.setattr("providers.codex.subprocess.run", fake_run)

    CodexProvider().run("inspect", read_only=True)

    cmd = calls[0][0]
    assert "--ask-for-approval" in cmd
    assert cmd[cmd.index("--ask-for-approval") + 1] == "never"
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--full-auto" not in cmd


def test_gemini_read_only_uses_default_approval_mode(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="empty output")

    monkeypatch.setattr("providers.gemini.subprocess.run", fake_run)

    GeminiProvider().run("inspect", read_only=True)

    cmd = calls[0][0]
    assert "--approval-mode" in cmd
    assert cmd[cmd.index("--approval-mode") + 1] == "default"
    assert "--yolo" not in cmd


def test_gemini_forced_model_appends_model_flag(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="empty output")

    monkeypatch.setattr("providers.gemini.subprocess.run", fake_run)

    provider = GeminiProvider()
    provider._forced_model = "gemini-3-flash-preview"
    try:
        provider.run("inspect")
    finally:
        provider._forced_model = None

    cmd = calls[0][0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gemini-3-flash-preview"


def test_gemini_no_forced_model_omits_model_flag(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="empty output")

    monkeypatch.setattr("providers.gemini.subprocess.run", fake_run)

    GeminiProvider().run("inspect")

    cmd = calls[0][0]
    assert "--model" not in cmd


def test_codex_forced_model_appends_model_flag(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="empty output")

    monkeypatch.setattr("providers.codex.subprocess.run", fake_run)

    provider = CodexProvider()
    provider._forced_model = "gpt-5.4-mini"
    try:
        provider.run("inspect")
    finally:
        provider._forced_model = None

    cmd = calls[0][0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gpt-5.4-mini"


def test_codex_no_forced_model_omits_model_flag(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="empty output")

    monkeypatch.setattr("providers.codex.subprocess.run", fake_run)

    CodexProvider().run("inspect")

    cmd = calls[0][0]
    assert "--model" not in cmd


# ── Phase B: session-id / resume + feature flag ───────────────────────────────

def test_claude_session_id_added_when_flag_enabled(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="empty output")

    monkeypatch.setattr("providers.claude.subprocess.run", fake_run)
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)

    ClaudeProvider().run("task", session_id="abc-123", resume=False)

    cmd = calls[0][0]
    assert "--session-id" in cmd
    assert cmd[cmd.index("--session-id") + 1] == "abc-123"
    assert "--resume" not in cmd


def test_claude_resume_flag_when_flag_enabled(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="empty output")

    monkeypatch.setattr("providers.claude.subprocess.run", fake_run)
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)

    ClaudeProvider().run("task", session_id="abc-123", resume=True)

    cmd = calls[0][0]
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "abc-123"
    assert "--session-id" not in cmd


def test_claude_session_id_ignored_when_flag_disabled(monkeypatch):
    """Kill-switch: even with session_id passed, no session flag appears
    when CLAUDE_SESSION_ENABLED is False."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="empty output")

    monkeypatch.setattr("providers.claude.subprocess.run", fake_run)
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", False)

    ClaudeProvider().run("task", session_id="abc-123", resume=True)

    cmd = calls[0][0]
    assert "--session-id" not in cmd
    assert "--resume" not in cmd


def test_claude_no_session_id_omits_flags(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="empty output")

    monkeypatch.setattr("providers.claude.subprocess.run", fake_run)
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)

    ClaudeProvider().run("task")

    cmd = calls[0][0]
    assert "--session-id" not in cmd
    assert "--resume" not in cmd


def test_claude_session_missing_typed_error(monkeypatch):
    """--resume against a non-existent UUID returns error_code=session_missing."""
    monkeypatch.setattr(
        "providers.claude.subprocess.run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="No conversation found with session ID: deadbeef-0000",
        ),
    )
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)

    result = ClaudeProvider().run("task", session_id="deadbeef-0000", resume=True)

    assert result.success is False
    assert result.error == "session_missing"


def test_supports_sessions_capability_flag():
    """Claude opts into sessions; Codex/Gemini do not (yet)."""
    assert ClaudeProvider.supports_sessions is True
    assert CodexProvider.supports_sessions is False
    assert GeminiProvider.supports_sessions is False


# ── Phase C: capability-switch routing for tools ─────────────────────────────

def test_critical_review_same_provider_uses_sessions(monkeypatch, tmp_path):
    """Critical-review with same primary+pass2 provider AND flag → SessionContext active.
    Detection: a Claude run() call should receive `session_id` in its kwargs."""
    from tools.critical_review import CriticalReviewTool

    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)
    monkeypatch.setattr("session_registry.ORCH_SESSION_REGISTRY",
                        tmp_path / "orch-sessions.jsonl")

    captured_kwargs: list[dict] = []

    def fake_run(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return SimpleNamespace(
            success=True, output="No findings.", error="",
            error_code="", retryable=False, input_tokens=0, output_tokens=0,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )

    fake_provider = SimpleNamespace(
        name="claude", supports_sessions=True, run=fake_run,
    )
    monkeypatch.setattr("tools.critical_review.is_cached_provider_available", lambda _: True)
    monkeypatch.setattr("tools.critical_review.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.critical_review.notify_tool_done", lambda *a, **kw: None)

    tool = CriticalReviewTool()
    tool.run("Review uncommitted changes", fake_provider, cwd=str(tmp_path),
             pass_providers={})  # no pass2 → defaults to primary

    # At least one provider.run() call should have received session_id (sessions are active)
    session_calls = [kw for kw in captured_kwargs if "session_id" in kw]
    assert session_calls, "Expected provider.run() to receive session_id when same-provider chain is enabled"


def test_critical_review_cross_provider_skips_sessions(monkeypatch, tmp_path):
    """Cross-provider runs (#pass1:gemini #pass2:claude) → no session_id in kwargs."""
    from tools.critical_review import CriticalReviewTool

    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)

    primary_calls: list[dict] = []
    pass2_calls: list[dict] = []

    def primary_run(*args, **kwargs):
        primary_calls.append(kwargs)
        return SimpleNamespace(
            success=True, output="No findings.", error="",
            error_code="", retryable=False, input_tokens=0, output_tokens=0,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )

    def pass2_run(*args, **kwargs):
        pass2_calls.append(kwargs)
        return SimpleNamespace(
            success=True, output="No findings.", error="",
            error_code="", retryable=False, input_tokens=0, output_tokens=0,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )

    primary = SimpleNamespace(name="gemini", supports_sessions=False, run=primary_run)
    monkeypatch.setattr("tools.critical_review.is_cached_provider_available", lambda _: True)
    monkeypatch.setattr("tools.critical_review.notify_tool_progress", lambda *a, **kw: None)
    monkeypatch.setattr("tools.critical_review.notify_tool_done", lambda *a, **kw: None)
    monkeypatch.setattr(
        "tools.critical_review._resolve_pass2_provider",
        lambda pass_providers, default: SimpleNamespace(
            name="claude", supports_sessions=True, _forced_model=None, run=pass2_run,
        ),
    )

    tool = CriticalReviewTool()
    tool.run("Review", primary, cwd=str(tmp_path),
             pass_providers={1: "gemini", 2: "claude"})

    # No call should have received session_id (cross-provider mode → same_provider_chain=False)
    assert all("session_id" not in kw for kw in primary_calls + pass2_calls), \
        "Expected no session_id when cross-provider"


def test_deep_security_audit_routing(monkeypatch):
    """deep-security-audit dispatches to subagent-mode vs sequential-mode based on flag."""
    from tools.deep_security_audit import DeepSecurityAuditTool

    tool = DeepSecurityAuditTool()
    sub_calls: list[bool] = []
    seq_calls: list[bool] = []
    monkeypatch.setattr(
        DeepSecurityAuditTool, "_run_subagent_mode",
        lambda self, *a, **kw: sub_calls.append(True) or SimpleNamespace(success=True),
    )
    monkeypatch.setattr(
        DeepSecurityAuditTool, "_run_sequential_mode",
        lambda self, *a, **kw: seq_calls.append(True) or SimpleNamespace(success=True),
    )

    claude = SimpleNamespace(name="claude", supports_sessions=True)
    codex = SimpleNamespace(name="codex", supports_sessions=False)

    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)
    tool.run("audit", claude, cwd="/p")
    assert sub_calls and not seq_calls

    sub_calls.clear(); seq_calls.clear()
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", False)
    tool.run("audit", claude, cwd="/p")
    assert seq_calls and not sub_calls

    sub_calls.clear(); seq_calls.clear()
    monkeypatch.setattr("config.CLAUDE_SESSION_ENABLED", True)
    tool.run("audit", codex, cwd="/p")
    assert seq_calls and not sub_calls  # codex doesn't support sessions
