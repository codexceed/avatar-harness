"""Harness configuration.

Loaded from defaults, then overridden by environment variables (prefix
``AVATAR_``) or a local ``.env``. This is the single, explicit config object
threaded through ``RunDeps`` (§8) — never read from globals.
"""

from typing import Literal

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
# How many recent verifier outputs stay verbatim through compaction: a repair loop
# needs "what did I try before and why did it fail", not just the latest verdict.
DEFAULT_CONTEXT_VERIFIER_PIN_COUNT = 2

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
    # Per-agent-run wall-clock bound (reset each run, not cumulative across a sitting). `None`
    # disables it — correct for the attended cockpit, where the human (Ctrl-C) and `max_iterations`
    # are the backstops; an unattended/eval run keeps a real cap so a runaway can't burn forever.
    # 1800 (not 600) since PR #104: slow reasoning models exhausted 600s mid-edit; hangs/stalls are
    # caught independently by the request timeout + idle watchdog (ADR-0028/0029).
    max_wall_clock_seconds: int | None = 1800
    max_consecutive_failures: int = 5
    max_repair_attempts: int = 3
    max_context_tokens: int = 100_000
    # Greenfield declaration gate (ADR-0038): how many times the runner refuses an edit-intent call
    # to nudge the model to `declare_verification` first, before falling back to the smoke floor.
    max_declaration_nudges: int = 3
    # Backstop on a blocking (attended) approval: deny it after this many seconds so a run can't
    # hang inside the gate (the wall-clock budget can't preempt an awaited approval). `None` (the
    # default) waits indefinitely — correct for a human at a REPL; an unattended run never blocks.
    approval_timeout_seconds: float | None = None

    # Unattended disposition for an `alter_verification` amendment (ADR-0039). "deny" (default)
    # keeps ADR-0016's deny-only posture; "approve" lets an autonomous run self-ratify a contract
    # amendment — scoped to that one action, and only safe paired with held-out eval grading.
    autonomous_amendment_policy: Literal["deny", "approve"] = "deny"

    # Verification commands — the OVERRIDE tier of plan resolution (§12, ADR-0007).
    # A non-empty value always wins: the user's stated contract is never overridden.
    # Empty (the default) means "unset" — the `VerificationPlanner` then detects the
    # repo's declared contract (CI workflows, manifests, Makefile) deterministically.
    # The harness runs the resolved commands ITSELF — never model-mediated (§5).
    test_command: str = ""
    lint_command: str = ""
    command_timeout_seconds: int = 120
    # Per-request timeout for one model call (ADR-0028 R1). The non-streaming ceiling; with R5
    # streaming on, the idle timeout does the fast stall-detection so this is a loose backstop.
    request_timeout_seconds: float = Field(240.0, gt=0)
    # Stream completions and bound the gap *between* chunks instead of total time (ADR-0029 R5):
    # a stall is caught in ~idle-timeout regardless of how long a legit generation runs. Passed as
    # the httpx `read` timeout per streaming call. Distinct from `request_timeout_seconds`.
    request_idle_timeout_seconds: float = Field(30.0, gt=0)
    # Master switch for R5 streaming (ADR-0029). `False` = exact non-streaming async behavior.
    # Distinct from the runtime per-instance `_streaming_unsupported` flag (a provider that rejects
    # streaming trips that flag for the rest of its session; this config is the global default).
    stream_model_calls: bool = True
    # Transport-layer retries (ADR-0028 R3), distinct from `max_parse_retries`: a NUL/empty body or
    # request failure is re-issued (backoff + jitter), never re-prompted; on exhaustion the client
    # raises `TransportError`, surfaced as a system failure (§16). Low so the worst case stays bounded.
    transport_max_retries: int = Field(2, ge=0)
    # Char budget for the command-tool stdout/stderr excerpt shown to the model: keeps the head AND
    # tail, elides the middle (`commands._excerpt`) since a failure's signal trails. Floored (`ge`)
    # so the bound is unconditional — tunable, never disable-able (R3's journal-distillability).
    command_output_budget: int = Field(16_000, ge=256)

    # LLM fallback for verification-plan resolution (ADR-0007 tier 3). Opt-in: unset
    # (the default) keeps resolution fully deterministic/offline. When set, the model
    # may PROPOSE a command for a slot detection left empty — only with a citation to
    # the repo artifact it came from, which the harness validates before accepting.
    planner_model: str | None = None

    # Session / UX (§23).
    interactive: bool = True

    # The active event-journal path, set by the entry point after it resolves the per-session
    # log location. Threaded into the `Workspace` so the harness's own journal (default
    # `events/<session_id>.jsonl` + `events/latest.jsonl`) is hidden from the agent's file
    # tools — it is harness plumbing, not the user's project. `None` hides nothing.
    log_path: str | None = None

    # Workspace (§15).
    workspace_root: str = "."

    # Execution sandbox (ADR-0042, implements ADR-0009). Closes Threat C — runtime/substrate
    # gaming (a planted `PYTEST_ADDOPTS`/`PYTHONPATH`, a phone-home) — at the `Workspace.run`
    # seam. `hermetic-env` (the default) scrubs the environment to a language-neutral allowlist
    # on every OS with no dependencies; `sandbox-exec` adds macOS network-deny; `none` restores
    # the fully-inherited environment (escape hatch). `bwrap`/`container` are reserved for
    # Increment 2. `sandbox_allow_network` only bites on the OS backends (the env-only floor
    # cannot gate network). Does NOT cover a model authoring weak tests (Threats A/B).
    sandbox_mode: Literal["none", "hermetic-env", "sandbox-exec", "bwrap", "container"] = "hermetic-env"
    sandbox_allow_network: bool = False
    # POSIX resource ceilings on each sandboxed command (ADR-0042 Increment 2). OFF by default:
    # they ride `preexec_fn`, which is not thread-safe between fork and exec against the
    # multithreaded eval runner (ADR-0026), so they are opt-in, not baked into the default.
    sandbox_rlimits: bool = False
    # Container backend (`sandbox_mode=container`, ADR-0042 Increment 2): the image carrying the
    # task's toolchain (required for that mode) and the runtime CLI. Empty image + container mode
    # is a config error — the guest has no toolchain otherwise.
    sandbox_image: str = ""
    sandbox_container_runtime: Literal["podman", "docker"] = "podman"

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

    # Sampling temperature for model decisions. `0.0` (default) keeps the loop as deterministic
    # as the provider allows. The eval harness raises it (>0) so each "seed" is an independent
    # sample — the precondition for pass^k / CIs to measure behavioral reliability, not just
    # provider noise.
    temperature: float = 0.0

    # Context compaction (§9; the Phase-2.5 budgets made visible + realistic). Per-item
    # and total caps on verbatim evidence *detail* in the model's context. Sized so an
    # ordinary source file fits whole per item — modifying a file requires seeing all of
    # it at once (the 2026-06-10 dogfood burned a 50-turn budget re-reading a file that
    # was silently cut at 1,500 chars) — with several files' worth of total verbatim
    # detail. `max_context_tokens` still bounds the whole packet.
    context_max_detail_chars: int = DEFAULT_CONTEXT_MAX_DETAIL_CHARS
    context_detail_char_budget: int = DEFAULT_CONTEXT_DETAIL_CHAR_BUDGET
    context_verifier_pin_count: int = DEFAULT_CONTEXT_VERIFIER_PIN_COUNT

    # Mode routing (revises ADR-0002 D3). The REPL classifies each goal's task_kind with
    # one cheap, schema-constrained call on this model (same base_url/api_key); the
    # verdict is displayed and /mode-overridable — visible, never silent control. Empty/
    # unset disables classification (heuristic-only). ~500 in / ~10 out tokens per goal.
    classifier_model: str | None = "openai/gpt-5-nano"
