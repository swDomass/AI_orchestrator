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
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Glob,Grep"


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
