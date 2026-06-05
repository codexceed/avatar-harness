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

    # Model — unused until Phase 1; declared now so the config shape is stable.
    model: str = "gpt-4o-mini"
    base_url: str | None = None
