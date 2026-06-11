# avatar-harness

A minimal, ground-up **coding agent harness** — the runtime around an LLM that turns a natural-language coding task into a bounded, verifiable engineering loop. The model proposes actions; the harness owns execution, state, permissions, logging, and verification.

> The name: the LLM steps into an *avatar* and goes off to do real work in a repo — inspect, edit, verify — under a harness that keeps it safe, observable, and reversible.

## Status

**In active development (TDD, phased).** Both the non-interactive engine and the interactive cockpit are built and tested: the read-only investigate loop runs end-to-end against a live model, the editing path (`apply_patch`/`write_file` under a permission gate → harness-owned verifier → artifact) is implemented and covered, and the Phase 3 cockpit (async engine + two-plane session + Textual TUI) drives a multi-turn REPL.

What this means for you today:

- **Working now (batch):** ask a question about a repo and get a grounded, verifier-checked answer (`investigate` tasks).
- **Working now (interactive):** `jo-cli` — a multi-turn cockpit that reads/edits/runs/verifies with you in the loop (approval prompts, plan mode, `@path`, meta commands; see [Usage](#usage)). Needs the `[textual]` extra.
- **Use it as a library:** `from avatar_harness import Harness` — embed the engine in your own app (`Harness.from_env().run(...)`), or drive the two-plane `Session`/`ReplSession` surface; every collaborator (model, tools, verifier, policy) is overridable.
- **Built but not yet wired into batch-CLI intake:** classifying a free-text goal into an edit task on the *non-interactive* path (the cockpit already routes modes); the engine handles edit tasks today.
- **Not yet:** durable crash-resume (Phase 3.3; see [`docs/adr/`](docs/adr)).

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

> **Embedding it as a library** (outside this repo's dev env): the package is **not yet published to PyPI** — install from source: from a clone, `pip install -e '.[openai]'`; or straight from GitHub, `pip install 'avatar-harness[openai] @ git+https://github.com/codexceed/avatar-harness'` (equivalently `uv add 'avatar-harness[openai] @ git+…'`; uses your git credentials while the repo is private). Always quote the extras — zsh glob-expands the brackets. `openai` is an **optional extra** for the default `OpenAIModelClient`; install the base package and inject your own `ModelClient` instead. The core imports without `openai`; a `Harness` is constructible without an API key (credentials are needed only at inference).

> **Interactive cockpit (jo):** the Textual TUI is a second optional extra — add `textual` to the extras when installing from source (e.g. `pip install -e '.[openai,textual]'` from a clone). The core engine and SDK import without it; `import avatar_harness` never pulls in `textual`. The cockpit is a *consumer* of the harness, so it ships its own entry point — launch it with `jo-cli` (see [Usage](#usage)): event-streamed transcript + status bar + input, approval/plan/diff modals, meta commands, `@path` grounding, the plan-mode flow (read-only plan → approve/revise → constrained edit), and conversational verification (`--auto` restores the strict gate). Durable crash-resume (Phase 3.3) is the remaining cockpit increment (see [`docs/adr/`](docs/adr)).

## Configuration

The harness reads configuration from environment variables (prefix `AVATAR_`) or a local `.env` file. At minimum, set a model API key.

```bash
# .env  (or export these in your shell)
AVATAR_API_KEY=sk-or-...                       # required; falls back to OPENAI_API_KEY if unset
AVATAR_MODEL=openai/gpt-4o-mini                # default; any model your endpoint serves
AVATAR_BASE_URL=https://openrouter.ai/api/v1   # default (OpenRouter); change for OpenAI/local
AVATAR_WORKSPACE_ROOT=.                         # repo the agent operates on (default: cwd)
```

Other useful knobs (all optional, with sane defaults): `AVATAR_MAX_ITERATIONS`, `AVATAR_MAX_REPAIR_ATTEMPTS`, `AVATAR_TEST_COMMAND`, `AVATAR_LINT_COMMAND`, `AVATAR_COMMAND_TIMEOUT_SECONDS`, `AVATAR_SENSITIVE_PATH_GLOBS`, `AVATAR_CONTEXT_MAX_DETAIL_CHARS` / `AVATAR_CONTEXT_DETAIL_CHAR_BUDGET` (how much verbatim tool output the model's context retains per item / in total). See `src/avatar_harness/config.py` for the full list.

**Mode routing (cockpit).** Each goal's `task_kind` is classified by one cheap, schema-constrained call on `AVATAR_CLASSIFIER_MODEL` (default `openai/gpt-5-nano`; same endpoint/key as the main model). The verdict is announced in the transcript (`▶ mode: edit (classifier) — /mode to change`) and always overridable with `/mode`; set the variable empty to disable classification (a word heuristic takes over).

**Editing tools.** Modification rides `apply_patch` (a unified diff, applied atomically with a clean-apply staleness check); file **creation** rides `write_file` (plain content; refuses an existing target unless `overwrite=true`, steering modification back to the diff-anchored path). Both are tier-1, path-confined, denylist-checked, and staged into the workspace diff the verifier judges.

**Decision transport.** By default the model's actions ride the provider's native **function-calling** channel (tool schemas sent as `tools=`, the chosen action returned as a structured call — far more robust for large patches than hand-written JSON). Endpoints that ignore `tools=` and answer in prose still work (automatic fallback to the JSON decision protocol); for an endpoint with actively *broken* tool-call support, set `AVATAR_NATIVE_TOOL_CALLS=false` to force the legacy JSON protocol.

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

**Interactive cockpit.** Launch `jo-cli` (needs the `[textual]` extra) for a full-screen multi-turn REPL instead of a one-shot batch run:

```bash
uv run jo-cli
```

A status bar (mode · phase · outcome), a streaming transcript, and an input box. Type goals across turns; the agent reads/edits/runs/verifies with you in the loop. Slash **meta commands** are handled locally (never hit the model): `/help`, `/mode <edit|investigate|test_only|plan>`, `/plan`, `/diff`, `/state`, `/permissions`, `/quit`. Reference a file with `@path/to/file` to ground a goal in it. `run_command` (and any sensitive-path call) prompts for approval (`[y]` once · `[a]` always for this session · `[d]` deny); `apply_patch` is tier-1 — auto-allowed once its target paths validate inside the workspace, with the verifier judging the resulting diff; plan mode proposes a read-only plan you approve or revise before any edit. By default the cockpit is **conversational** — verification always runs and is reported, but the reply isn't blocked on it (you're the terminal authority); pass `--auto` to keep the strict gate. The whole sitting is journaled **write-ahead** to `events/<session_id>.jsonl` (or `--log PATH`), the same layout as a batch run — every event hits disk *before* the TUI renders it, so even a crashed cockpit session leaves a complete, replayable record.

**Flags:**

| Flag | Command | Default | Meaning |
| --- | --- | --- | --- |
| `--auto` | `jo-cli` | off | Keep the strict verification gate (default: conversational — verify runs + reports, the human decides). |
| `--log PATH` | both | `events/<session_id>.jsonl` | Where to write the append-only JSONL event log (the cockpit journals one file per sitting). By default each run gets its own per-session file, and `events/latest.jsonl` points at the newest. Pass an explicit path to write there instead (no `latest` pointer is maintained for explicit paths). |
| `--allow-dirty` | both | off | Run despite uncommitted **tracked** changes in the workspace. |

**Clean-tree note.** The workspace pins git `HEAD` as its diff baseline, so by default it refuses to start on a tree with uncommitted *tracked* changes (untracked files are ignored). Commit/stash first, pass `--allow-dirty` to acknowledge them, or point `AVATAR_WORKSPACE_ROOT` at a clean checkout. For an `investigate` task, pre-existing tracked changes will cause verification to refuse `success` (they look like an unintended diff) — so a clean tree gives the cleanest result. **In the cockpit the check applies to the first goal of a sitting only**: changes the session itself makes (its staged edits between goals) never block a follow-up goal — they're the session's own work product.

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

**Public exports** (`avatar_harness.__all__`): the core entry points (`Harness`, `HarnessConfig`, `TaskState`, `Workspace`, `RunDeps`), decision types, tool contracts, the **two-plane surface** (`Session`, `EventBus`, `JsonlEventJournal`, `EventSink`, `ApprovalController`, `ApprovalGrant`), the **multi-turn session scope** (`ReplSession`, `SessionState`, `Turn`), and the typed lifecycle events (`HarnessEvent` + `ApprovalRequested`, `ToolStart`/`ToolEnd`, `PhaseChanged`, …). Build on these rather than deep-importing internals.

## Documentation

The docs site under [`docs/`](docs/) (Mintlify; `make docs-serve` previews it locally) carries the user-facing documentation:

- **[Quickstart](docs/guides/quickstart.mdx)** — install → configure → a verifier-checked answer, CLI and library.
- **[SDK guide](docs/guides/sdk.mdx)** — the curated surface: the `Harness` facade, the two-plane `Session` contract, the typed-event catalog, multi-turn `ReplSession`, and every `AVATAR_*` config knob.
- **[Tutorial: build a terminal agent](docs/tutorials/terminal-agent.mdx)** — a streaming, approval-answering agent of your own in ~90 lines.
- **[API reference](docs/api-reference)** — generated from docstrings (always in sync with the source).

Design-depth docs live at the repo root: [`ARCHITECTURE.md`](ARCHITECTURE.md) (system map), [`HARNESS_DESIGN.md`](HARNESS_DESIGN.md) (canonical spec), [`docs/adr/`](docs/adr) (decision records).

## Development

```bash
make test         # run the test suite (pytest)
make lint         # ruff check
make format       # ruff format
make typecheck    # pyrefly
make check        # lint + typecheck + test — run before committing

uv run pytest tests/test_x.py::test_name   # run a single test
```

The API reference under [`docs/api-reference/`](docs/api-reference) is generated from docstrings (the code is the source of truth). When you change a public module/class/function signature or docstring, regenerate and commit it:

```bash
make docs-api      # regenerate the MDX pages + docs.json nav from docstrings
```

Contribution conventions (commit format, branch names, PR sections) are in [`CLAUDE.md`](CLAUDE.md).

## License

MIT — see [`LICENSE`](LICENSE).
