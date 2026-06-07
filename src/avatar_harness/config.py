"""Harness configuration.

Loaded from defaults, then overridden by environment variables (prefix
``AVATAR_``) or a local ``.env``. This is the single, explicit config object
threaded through ``RunDeps`` (§8) — never read from globals.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class HarnessConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AVATAR_", env_file=".env", extra="ignore")

    # Budgets — the bounding conditions of the loop (§5).
    max_iterations: int = 50
    max_wall_clock_seconds: int = 600
    max_consecutive_failures: int = 5
    max_repair_attempts: int = 3
    max_context_tokens: int = 100_000

    # Session / UX (§23).
    interactive: bool = True

    # Workspace (§15).
    workspace_root: str = "."

    # Model endpoint (OpenAI-compatible). Defaults to OpenRouter so we can test
    # many models by overriding AVATAR_MODEL; override AVATAR_BASE_URL for others.
    model: str = "openai/gpt-4o-mini"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str | None = None  # AVATAR_API_KEY; if unset, the client falls back to OPENAI_API_KEY
