from avatar_harness.config import HarnessConfig


def test_config_loads_defaults(monkeypatch):
    # Hermetic: ignore a developer's local .env / shell AVATAR_* vars.
    for var in ("AVATAR_API_KEY", "AVATAR_MODEL", "AVATAR_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    config = HarnessConfig(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert config.max_iterations == 50
    assert config.max_repair_attempts == 3
    assert config.interactive is True
    assert config.workspace_root == "."
    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.api_key is None


def test_config_env_override(monkeypatch):
    monkeypatch.setenv("AVATAR_MAX_ITERATIONS", "7")
    monkeypatch.setenv("AVATAR_INTERACTIVE", "false")
    monkeypatch.setenv("AVATAR_API_KEY", "sk-or-test")
    monkeypatch.setenv("AVATAR_MODEL", "anthropic/claude-sonnet-4-6")
    config = HarnessConfig()
    assert config.max_iterations == 7
    assert config.interactive is False
    assert config.api_key == "sk-or-test"
    assert config.model == "anthropic/claude-sonnet-4-6"
