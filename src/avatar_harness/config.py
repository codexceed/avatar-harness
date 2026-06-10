"""Harness configuration.

Loaded from defaults, then overridden by environment variables (prefix
``AVATAR_``) or a local ``.env``. This is the single, explicit config object
threaded through ``RunDeps`` (§8) — never read from globals.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sensitive-path denylist (§11, Phase 2.5). Patterns are matched per path component
# (no slash) or against the whole relative path (with a slash, `**` allowed). Reading
# or patching a matching path is refused at the permission gate — deterministic
# prevention, never content-level secret detection. Overridable via AVATAR_SENSITIVE_PATH_GLOBS.
# Context-compaction defaults (§9) — the single source both `HarnessConfig` and
# `ContextBuilder`'s bare-constructor defaults read, so the two can't drift.
DEFAULT_CONTEXT_MAX_DETAIL_CHARS = 16_000
DEFAULT_CONTEXT_DETAIL_CHAR_BUDGET = 48_000

DEFAULT_SENSITIVE_PATH_GLOBS: list[str] = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".ssh",
    ".aws",
    ".gnupg",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials*",
    "secrets",
]


class HarnessConfig(BaseSettings):
    """The single, explicit run configuration: budgets, session, workspace, endpoint (§8)."""

    model_config = SettingsConfigDict(env_prefix="AVATAR_", env_file=".env", extra="ignore")

    # Budgets — the bounding conditions of the loop (§5).
    max_iterations: int = 50
    max_wall_clock_seconds: int = 600
    max_consecutive_failures: int = 5
    max_repair_attempts: int = 3
    max_context_tokens: int = 100_000

    # Verification commands (§12). The verifier runs these ITSELF — independent of
    # any run_tests/run_linter the model called — so its signal is harness-owned,
    # never model-mediated. Empty disables the command (an allowed/disallowed skip
    # per the gate). Target inference is deferred (§21); the command is explicit.
    test_command: str = "pytest -q"
    lint_command: str = "ruff check"
    command_timeout_seconds: int = 120

    # Session / UX (§23).
    interactive: bool = True

    # Workspace (§15).
    workspace_root: str = "."

    # Sensitive-path denylist (§11, Phase 2.5) — globs the permission gate refuses to
    # read or patch. Defaults cover common secret files; override via AVATAR_SENSITIVE_PATH_GLOBS.
    sensitive_path_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_SENSITIVE_PATH_GLOBS))

    # Model endpoint (OpenAI-compatible). Defaults to OpenRouter so we can test
    # many models by overriding AVATAR_MODEL; override AVATAR_BASE_URL for others.
    model: str = "openai/gpt-4o-mini"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str | None = None  # AVATAR_API_KEY; if unset, the client falls back to OPENAI_API_KEY

    # Decision transport (ADR-0003 A). Native provider function-calling is the default —
    # the provider owns the JSON envelope, so a large patch can't die in hand-escaping.
    # `false` restores the legacy single-JSON-object protocol for endpoints whose
    # tool-call support is broken (content-only replies also fall back automatically).
    native_tool_calls: bool = True

    # Context compaction (§9; the Phase-2.5 budgets made visible + realistic). Per-item
    # and total caps on verbatim evidence *detail* in the model's context. Sized so an
    # ordinary source file fits whole per item — modifying a file requires seeing all of
    # it at once (the 2026-06-10 dogfood burned a 50-turn budget re-reading a file that
    # was silently cut at 1,500 chars) — with several files' worth of total verbatim
    # detail. `max_context_tokens` still bounds the whole packet.
    context_max_detail_chars: int = DEFAULT_CONTEXT_MAX_DETAIL_CHARS
    context_detail_char_budget: int = DEFAULT_CONTEXT_DETAIL_CHAR_BUDGET
