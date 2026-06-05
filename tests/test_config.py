from avatar_harness.config import HarnessConfig


def test_config_loads_defaults():
    config = HarnessConfig()
    assert config.max_iterations == 50
    assert config.max_repair_attempts == 3
    assert config.interactive is True
    assert config.workspace_root == "."


def test_config_env_override(monkeypatch):
    monkeypatch.setenv("AVATAR_MAX_ITERATIONS", "7")
    monkeypatch.setenv("AVATAR_INTERACTIVE", "false")
    config = HarnessConfig()
    assert config.max_iterations == 7
    assert config.interactive is False
