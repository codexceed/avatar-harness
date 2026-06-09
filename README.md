# avatar-harness

A minimal, ground-up **coding agent harness** — the runtime around an LLM that turns a natural-language coding task into a bounded, verifiable engineering loop. The model proposes actions; the harness owns execution, state, permissions, logging, and verification.

> The name: the LLM steps into an *avatar* and goes off to do real work in a repo — inspect, edit, verify — under a harness that keeps it safe, observable, and reversible.

## Status

**In active development (TDD, phased).** The non-interactive engine is built and tested: the read-only investigate loop runs end-to-end against a live model, and the editing path (`apply_patch` under a permission gate → harness-owned verifier → artifact) is implemented and covered by tests.

What this means for you today:

- **Working now:** ask a question about a repo and get a grounded, verifier-checked answer (`investigate` tasks).
- **Use it as a library:** `from avatar_harness import Harness` — embed the engine in your own app (`Harness.from_env().run(...)`), with every collaborator (model, tools, verifier, policy) overridable.
- **Built but not yet wired into CLI intake:** edit tasks (`"fix the bug in X"` → patch → verify). The engine handles them; classifying a goal into an edit task from the CLI lands in a later phase.
- **Not yet:** the interactive REPL (Phase 3 — async engine, durable execution, TUI; see [`docs/adr/`](docs/adr)).

See [`PROGRESS.md`](PROGRESS.md) for the build ledger, [`ARCHITECTURE.md`](ARCHITECTURE.md) for the system map, and [`HARNESS_DESIGN.md`](HARNESS_DESIGN.md) for the full design spec.

## The loop

```text
Goal
  -> build relevant context
  -> ask model for next action
  -> execute a typed tool safely
  -> update structured state
  -> verify with external evidence
  -> return a patch, result, or blocker
```

## Design at a glance

- **Structured `TaskState`** is the source of truth — not the chat transcript.
- **Constrained model decisions** (`tool_call` / `final_answer` / `ask_user`), validated before execution.
- **Typed, permissioned tools** acting on a confined, diff-tracked `Workspace`.
- **Verifier-owned completion** — "done" is proven by external evidence (tests, lint, diff), never self-certified.
- **Observable + reversible** — append-only JSONL event log; every edit is an inspectable diff.

Full rationale, component contracts, and the MVP scope cut live in [`HARNESS_DESIGN.md`](HARNESS_DESIGN.md).

## Requirements

- **Python 3.12+**
- [**uv**](https://docs.astral.sh/uv/) — used for dependency management and running the CLI
- [**ripgrep**](https://github.com/BurntSushi/ripgrep) (`rg`) on `PATH` — the `search_repo` tool shells out to it
- **git** — `apply_patch` applies diffs via `git apply`, and the workspace pins HEAD as its diff baseline
- An **OpenAI-compatible LLM endpoint** (configurable base URL + model)

## Installation

```bash
git clone https://github.com/codexceed/avatar-harness.git
cd avatar-harness
make install          # uv sync — installs deps + dev tools
```

`uv` reads the committed `uv.lock`, so the environment is reproducible. `make install` is a thin wrapper over `uv sync` (the dev env includes the `openai` client).

> **Embedding it as a library** (outside this repo's dev env): `openai` is an **optional extra**. Run `pip install avatar-harness[openai]` (or `uv add avatar-harness[openai]`) to use the default `OpenAIModelClient`, or install the base package and inject your own `ModelClient`. The core imports without `openai`; a `Harness` is constructible without an API key (credentials are needed only at inference).

## Configuration

The harness reads configuration from environment variables (prefix `AVATAR_`) or a local `.env` file. At minimum, set a model API key.

```bash
# .env  (or export these in your shell)
AVATAR_API_KEY=sk-or-...                       # required; falls back to OPENAI_API_KEY if unset
AVATAR_MODEL=openai/gpt-4o-mini                # default; any model your endpoint serves
AVATAR_BASE_URL=https://openrouter.ai/api/v1   # default (OpenRouter); change for OpenAI/local
AVATAR_WORKSPACE_ROOT=.                         # repo the agent operates on (default: cwd)
```

Other useful knobs (all optional, with sane defaults): `AVATAR_MAX_ITERATIONS`, `AVATAR_MAX_REPAIR_ATTEMPTS`, `AVATAR_TEST_COMMAND`, `AVATAR_LINT_COMMAND`, `AVATAR_COMMAND_TIMEOUT_SECONDS`, `AVATAR_SENSITIVE_PATH_GLOBS`. See `src/avatar_harness/config.py` for the full list.

> **Point at OpenAI instead of OpenRouter:** `AVATAR_BASE_URL=https://api.openai.com/v1`, `AVATAR_API_KEY=sk-...`, `AVATAR_MODEL=gpt-4o-mini`.

**Secret safety.** Files matching a sensitive-path denylist (`.env`, `*.pem`, SSH/AWS/GnuPG dirs, `.netrc`, …) are refused for read and patch, and excluded from `search_repo` results, so a task can't read your secrets into the model context or the event log. Enforcement is two-layer: the permission gate blocks the call up front, and the workspace re-checks the **resolved** path (so an innocuously-named symlink can't launder a secret, and the refusal holds even for a non-gated caller). Override the patterns with `AVATAR_SENSITIVE_PATH_GLOBS` (a JSON list, e.g. `'["*.secret", ".env"]'`); the value *replaces* the default set. This is prevention by path-matching, not content secret-detection — a secret reachable through some other channel (e.g. a command's stdout) is not scrubbed.

**`run_command` (a trust-widening capability).** The agent can run a model-chosen project command (build, codegen, migration, a scoped test target) via the tier-3 `run_command` tool. Unlike `apply_patch`, a command is **opaque to the path denylist** — its backstop is the **human approval prompt**, not path-matching — so it is **default-blocked in non-interactive runs** and only reachable, with per-call approval, through the interactive session (Phase 3.1+). It runs as an argv (no shell metacharacters: no pipes, `&&`, redirection) and its file mutations are captured into the workspace diff like any edit. The verifier still owns the success outcome; a command never self-certifies.

## Usage

Ask the harness a question about the repo it's pointed at:

```bash
uv run avatar-harness "where does the agent loop terminate, and what sets outcome=success?"
# or via the Makefile:
make run TASK="explain how apply_patch stays atomic"
```

It prints a timestamped event trajectory as it works (`[model_decision] … [tool_execution_end] … [verification_end]`), then a `Status:` line and the cited answer. The full run is also written to a JSONL event log for replay/debugging. Every event carries a `session_id` identifying the run, so a log can always be grouped back into its sessions.

**Flags:**

| Flag | Default | Meaning |
| --- | --- | --- |
| `--log PATH` | `events/<session_id>.jsonl` | Where to write the append-only JSONL event log. By default each run gets its own per-session file, and `events/latest.jsonl` points at the newest. Pass an explicit path to write there instead (no `latest` pointer is maintained for explicit paths). |
| `--allow-dirty` | off | Run despite uncommitted **tracked** changes in the workspace. |

**Clean-tree note.** The workspace pins git `HEAD` as its diff baseline, so by default it refuses to start on a tree with uncommitted *tracked* changes (untracked files are ignored). Commit/stash first, pass `--allow-dirty` to acknowledge them, or point `AVATAR_WORKSPACE_ROOT` at a clean checkout. For an `investigate` task, pre-existing tracked changes will cause verification to refuse `success` (they look like an unintended diff) — so a clean tree gives the cleanest result.

## Use it as a library

The engine is importable behind a `Harness` facade — the model and any UI live outside it:

```python
from avatar_harness import Harness

# Defaults from the environment (AVATAR_* / .env), then run a task:
harness = Harness.from_env()
state = harness.run("explain how apply_patch stays atomic")
print(state.outcome, state.final_answer)

# Or override any seam — model, tools, verifier, policy — and keep the rest:
from avatar_harness import HarnessConfig
harness = Harness(
    config=HarnessConfig(workspace_root="./repo", test_command="pytest -q"),
    model=my_model_client,   # any ModelClient; no `openai` needed
)
state = harness.run("fix the failing auth test", task_kind="edit")
```

`Harness.from_env()` needs no API key to construct (credentials are used only when the model is first called).

### Async + interactive (the two-plane surface)

For an interactive UI or an autonomous wrapper, build on the **session** surface — typed events flow *out* (observation, never blocking the run), approval/cancel flow *in* (control). The same engine powers it; `run()` is just the batch degenerate case.

```python
import asyncio
from avatar_harness import Harness, ApprovalRequested

async def main():
    harness = Harness.from_env()

    # Bare async loop (returns only the terminal state):
    state = await harness.arun("explain the retry loop", task_kind="investigate")

    # Or an interactive session — observe the stream, answer approvals:
    session = harness.session("fix the failing auth test", task_kind="edit")
    run_task = asyncio.create_task(session.run())
    async for event in session.events():
        render(event)                                   # your UI
        if isinstance(event, ApprovalRequested):        # control: explicit, awaited
            await session.resolve_approval(event.approval_id, allow=ask_user(event))
            # `[a] always`: remember=True stores a session-scoped ApprovalGrant so later
            # commands sharing that program (argv[0]) auto-allow without re-prompting.
    state = await run_task
    # session.cancel("user pressed esc") interrupts from anywhere

asyncio.run(main())
```

**Public exports** (`avatar_harness.__all__`): the core entry points (`Harness`, `HarnessConfig`, `TaskState`, `Workspace`, `RunDeps`), decision types, tool contracts, the **two-plane surface** (`Session`, `EventBus`, `EventSink`, `ApprovalController`, `ApprovalGrant`), and the typed lifecycle events (`HarnessEvent` + `ApprovalRequested`, `ToolStart`/`ToolEnd`, `PhaseChanged`, …). Build on these rather than deep-importing internals.

## Development

```bash
make test         # run the test suite (pytest)
make lint         # ruff check
make format       # ruff format
make typecheck    # pyrefly
make check        # lint + typecheck + test — run before committing

uv run pytest tests/test_x.py::test_name   # run a single test
```

Contribution conventions (commit format, branch names, PR sections) are in [`CLAUDE.md`](CLAUDE.md).

## License

MIT — see [`LICENSE`](LICENSE).
