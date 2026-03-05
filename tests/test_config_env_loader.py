import os

import config


def test_load_dotenv_strips_surrounding_quotes(monkeypatch, tmp_path):
    env_dir = tmp_path / "cfg"
    env_dir.mkdir()
    (env_dir / ".env").write_text(
        'ORCH_VAULT_PATH="D:\\path with spaces"\n'
        "ORCH_QUEUE_FILE='D:\\queue\\agent-queue.md'\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("ORCH_VAULT_PATH", raising=False)
    monkeypatch.delenv("ORCH_QUEUE_FILE", raising=False)
    monkeypatch.setattr(config, "__file__", str(env_dir / "config.py"))

    config._load_dotenv()

    assert os.environ["ORCH_VAULT_PATH"] == r"D:\path with spaces"
    assert os.environ["ORCH_QUEUE_FILE"] == r"D:\queue\agent-queue.md"


def test_normalize_dotenv_value_strips_comments():
    from config import _normalize_dotenv_value
    assert _normalize_dotenv_value("VALUE # comment") == "VALUE"
    # No whitespace before # → not treated as comment (protects URLs/paths)
    assert _normalize_dotenv_value("VALUE#comment") == "VALUE#comment"
    assert _normalize_dotenv_value("https://example.com#anchor") == "https://example.com#anchor"
    assert _normalize_dotenv_value('"QUOTED VALUE" # comment') == "QUOTED VALUE"
    assert _normalize_dotenv_value("'QUOTED VALUE'#comment") == "QUOTED VALUE"
    assert _normalize_dotenv_value("VALUE") == "VALUE"
