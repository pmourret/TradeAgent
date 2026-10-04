import os

from tradeagent.envfile import load_env_file


def test_loads_values_and_returns_only_names(tmp_path, monkeypatch):
    for name in ("TA_ONE", "TA_TWO", "TA_THREE"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text('# commentaire\n\nTA_ONE=abc\nTA_TWO="quoted value"\nTA_THREE=\'single\'\nbad line\n')
    loaded = load_env_file(env)
    assert loaded == ["TA_ONE", "TA_TWO", "TA_THREE"]
    assert os.environ["TA_ONE"] == "abc"
    assert os.environ["TA_TWO"] == "quoted value"
    assert os.environ["TA_THREE"] == "single"


def test_never_overrides_existing_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("TA_KEEP", "from-shell")
    env = tmp_path / ".env"
    env.write_text("TA_KEEP=from-file\n")
    assert load_env_file(env) == []
    assert os.environ["TA_KEEP"] == "from-shell"


def test_commented_and_empty_values_are_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("# ANTHROPIC_API_KEY=sk-xxx\nANTHROPIC_API_KEY=\n")
    assert load_env_file(env) == []
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_missing_file_is_fine(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == []
