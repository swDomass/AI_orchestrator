from tools import registry


def test_list_tools_matches_executable_registry(monkeypatch):
    fake_tool = type("FakeTool", (), {"description": "desc"})()
    monkeypatch.setattr(registry, "_TOOLS", {"only-exec": fake_tool})

    assert registry.list_tools() == {"only-exec": "desc"}
