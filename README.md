# avatar-harness

A minimal, ground-up **coding agent harness** — the runtime around an LLM that turns a natural-language coding task into a bounded, verifiable engineering loop. The model proposes actions; the harness owns execution, state, permissions, logging, and verification.

> The name: the LLM steps into an *avatar* and goes off to do real work in a repo — inspect, edit, verify — under a harness that keeps it safe, observable, and reversible.

## Status

**In active development (TDD, phased).** The non-interactive engine is built and tested: the read-only investigate loop runs end-to-end against a live model, and the editing path (`apply_patch` under a permission gate → harness-owned verifier → artifact) is implemented and covered by tests.

What this means for you today:

- **Working now:** ask a question about a repo and get a grounded, verifier-checked answer (`investigate` tasks).
- **Built but not yet wired into CLI intake:** edit tasks (`"fix the bug in X"` → patch → verify). The engine handles them; classifying a goal into an edit task from the CLI lands in a later phase.
- **Not yet:** the interactive REPL (Phase 3).

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

`uv` reads the committed `uv.lock`, so the environment is reproducible. `make install` is a thin wrapper over `uv sync`.

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

**Secret safety.** The permission gate refuses to read or patch files matching a sensitive-path denylist (`.env`, `*.pem`, SSH/AWS/GnuPG dirs, `.netrc`, …) and excludes them from `search_repo` results, so a task can't read your secrets into the model context or the event log. Override the patterns with `AVATAR_SENSITIVE_PATH_GLOBS` (a JSON list, e.g. `'["*.secret", ".env"]'`); the value *replaces* the default set. This is prevention by path-matching, not content secret-detection — a secret reachable through some other channel (e.g. a command's stdout) is not scrubbed.

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
