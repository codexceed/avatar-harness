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


def test_config_native_tool_calls_defaults_on_and_overrides(monkeypatch):
    # ADR-0003 A: native function-calling is the default transport; the env flag is the
    # escape hatch for "OpenAI-compatible" endpoints with broken tool-call support.
    assert HarnessConfig().native_tool_calls is True
    monkeypatch.setenv("AVATAR_NATIVE_TOOL_CALLS", "false")
    assert HarnessConfig().native_tool_calls is False


def test_config_verification_commands_are_override_tier_not_defaults(monkeypatch):
    """ADR-0007: the static `pytest -q`/`ruff check` defaults are gone.

    `AVATAR_TEST_COMMAND`/`AVATAR_LINT_COMMAND` survive as the always-wins override
    tier (empty = unset → the planner detects), and the LLM fallback is opt-in.
    """
    for var in ("AVATAR_TEST_COMMAND", "AVATAR_LINT_COMMAND", "AVATAR_PLANNER_MODEL"):
        monkeypatch.delenv(var, raising=False)
    config = HarnessConfig(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert config.test_command == ""
    assert config.lint_command == ""
    assert config.planner_model is None
    monkeypatch.setenv("AVATAR_TEST_COMMAND", "go test ./...")
    monkeypatch.setenv("AVATAR_PLANNER_MODEL", "openai/gpt-5-nano")
    override = HarnessConfig(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert override.test_command == "go test ./..."
    assert override.planner_model == "openai/gpt-5-nano"


def test_config_classifier_model_default_and_override(monkeypatch):
    """The mode classifier rides a cheap dedicated model; empty disables it.

    Defaults asserted with `_env_file=None` + env cleared (the PR-#29 review pattern).
    """
    monkeypatch.delenv("AVATAR_CLASSIFIER_MODEL", raising=False)
    assert HarnessConfig(_env_file=None).classifier_model == "openai/gpt-5-nano"
    monkeypatch.setenv("AVATAR_CLASSIFIER_MODEL", "")
    assert HarnessConfig().classifier_model in (None, "")  # empty = heuristic-only
