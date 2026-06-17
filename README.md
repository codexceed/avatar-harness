# avatar-harness

A minimal, ground-up **coding agent harness** — the runtime around an LLM that turns a natural-language coding task into a bounded, verifiable engineering loop. The model proposes actions; the harness owns execution, state, permissions, logging, and verification.

Use it three ways: a **batch CLI** (`avatar`) for one-shot tasks, an **interactive cockpit** (`jo`, the Textual TUI from the separate `jo-cli` package) for a multi-turn REPL, or a **library** (`from avatar import Harness`) to embed the engine.

> This repository is a **uv workspace** with two distributable packages: the `avatar-harness` SDK lives in [`avatar/`](avatar-harness/) (import `avatar`, CLI `avatar`) and the reference cockpit ships as [`jo-cli`](jo-cli/) (import `jo`, CLI `jo`). `evals/` and `tests/` stay at the repo root. See [ADR-0023](docs/adr/0023-two-package-workspace-avatar-sdk-jo-cli.md).

> Status: the engine and the interactive cockpit are built and tested; durable crash-resume is the remaining increment — see [`PROGRESS.md`](PROGRESS.md). New here? Start with the **[Quickstart](docs/guides/quickstart.mdx)**.

## Requirements

- **Python 3.12+**
- [**uv**](https://docs.astral.sh/uv/) — dependency management and running the CLI
- [**ripgrep**](https://github.com/BurntSushi/ripgrep) (`rg`) on `PATH` — `search_repo` shells out to it
- **git** — the workspace pins HEAD as its diff baseline; `str_replace`/`write_file` edits are staged so the diff reflects them
- An **OpenAI-compatible LLM endpoint** (configurable base URL + model)

## Installation

```bash
git clone https://github.com/codexceed/avatar-harness.git
cd avatar-harness
make install          # uv sync — deps + dev tools, reproducible from uv.lock
```

**As a library** (from source — not yet on PyPI): `pip install -e './avatar-harness[openai]'` from a clone (the SDK member), or `pip install 'avatar-harness[openai] @ git+https://github.com/codexceed/avatar-harness#subdirectory=avatar-harness'`. For the cockpit, install the separate `jo-cli` package (`pip install -e ./jo-cli` from a clone). Quote the extras (zsh expands brackets). `openai` is optional — install the base package and inject your own `ModelClient`.

## Configuration

Set config via environment variables (prefix `AVATAR_`) or a local `.env`. Minimum: a model API key.

```bash
# .env  (or export in your shell)
AVATAR_API_KEY=sk-or-...                       # required; falls back to OPENAI_API_KEY
AVATAR_MODEL=openai/gpt-4o-mini                # any model your endpoint serves
AVATAR_BASE_URL=https://openrouter.ai/api/v1   # default (OpenRouter); swap for OpenAI/local
AVATAR_TEMPERATURE=0.0                          # sampling temperature (0 = as deterministic as the provider allows)
AVATAR_REQUEST_TIMEOUT=                          # per-call model timeout (s); unset = SDK default (10 min)
AVATAR_WORKSPACE_ROOT=.                         # repo the agent operates on (default: cwd)
AVATAR_CONTEXT_VERIFIER_PIN_COUNT=2             # verifier outputs pinned verbatim in context
```

Point at OpenAI instead: `AVATAR_BASE_URL=https://api.openai.com/v1`, `AVATAR_MODEL=gpt-4o-mini`.

For `edit` tasks the harness auto-detects how to verify the work (CI / manifests / Makefile); a greenfield repo that declares no contract gets a model-authored **smoke check**, run by the harness, as a fallback floor (ADR-0014) — so a from-scratch project verifies out of the box. Set `AVATAR_TEST_COMMAND` / `AVATAR_LINT_COMMAND` for a stronger, declared contract (it always wins over the floor). The **[SDK guide](docs/guides/sdk.mdx)** documents every `AVATAR_*` knob; `avatar-harness/avatar/config.py` is the source of truth.

## Usage

### Batch CLI

```bash
uv run avatar "where does the agent loop terminate, and what sets outcome=success?"
make run TASK="explain how str_replace anchors edits"   # via the Makefile
```

It prints a timestamped event trajectory, then a `Status:` line and the cited answer. The full run is written to a JSONL event log (`events/<session_id>.jsonl`) for replay.

### Interactive cockpit (jo)

The cockpit ships as the separate [`jo-cli`](jo-cli/) package (the `jo` command). In this
workspace it's already installed by `make install`; standalone, `pip install jo-cli`.

```bash
uv run jo               # launch the cockpit (the jo-cli package)
```

A full-screen multi-turn REPL — status bar (mode · phase · outcome), streaming transcript, input box — where the agent reads/edits/runs/verifies with you in the loop:

- **Meta commands** (handled locally, never hit the model): `/help`, `/mode <edit|investigate|test_only|plan>`, `/plan`, `/diff`, `/state`, `/permissions`, `/quit`.
- **Ground a goal** in a file with `@path/to/file`.
- **Approval prompts** for `run_command` and sensitive-path calls (`[y]` once · `[a]` always this session · `[d]` deny); the edit tools (`str_replace`/`write_file`/`delete_file`) auto-allow when their paths validate inside the workspace.
- **Conversational by default** — verification runs and is reported, but the reply isn't gated on it (you're the terminal authority); `--auto` keeps the strict gate.

### Flags

| Flag | Command | Default | Meaning |
| --- | --- | --- | --- |
| `--auto` | `jo` | off | Keep the strict verification gate (default: conversational — verify runs + reports, you decide). |
| `--log PATH` | both | `events/<session_id>.jsonl` | Where to write the append-only JSONL event log. |
| `--allow-dirty` | both | off | Run despite uncommitted **tracked** changes in the workspace. |

> **Clean-tree note.** The workspace refuses to start on uncommitted *tracked* changes (untracked files are ignored) — commit/stash first, pass `--allow-dirty`, or point `AVATAR_WORKSPACE_ROOT` at a clean checkout. In the cockpit this applies to the first goal of a sitting only; the session's own edits never block a follow-up goal.

### As a library

```python
from avatar import Harness

harness = Harness.from_env()                 # config from AVATAR_* / .env; no API key needed to construct
state = harness.run("explain how str_replace anchors edits")
print(state.outcome, state.final_answer)
```

Override any seam (model, tools, verifier, policy) via the `Harness(...)` constructor, or drive the async two-plane `Session` / multi-turn `ReplSession` surface for an interactive UI. The **[SDK guide](docs/guides/sdk.mdx)** and the **[tutorial](docs/tutorials/terminal-agent.mdx)** (a streaming, approval-answering agent in ~90 lines) cover the full surface.

### Evaluating the agent

A deterministic, model-agnostic eval harness lives under [`evals/`](evals/README.md) (dev tooling; live runs cost API spend):

```bash
make eval MODELS="openai/gpt-5.1,anthropic/claude-sonnet-4-6" SEEDS=3   # score the task suite (per-model pass@1/pass^k)
make eval-diff BASELINE=evals/results/A.jsonl CANDIDATE=evals/results/B.jsonl   # regression-diff (clustered CI + McNemar)
```

See [`evals/README.md`](evals/README.md) for task specs, probes, the run workspace/cleanup flags, and how scoring works.

## Documentation

The README is the on-ramp; explore depth intentionally:

- **[Quickstart](docs/guides/quickstart.mdx)** — install → configure → a verifier-checked answer, CLI and library.
- **[SDK guide](docs/guides/sdk.mdx)** — the curated surface, the two-plane `Session`, the typed-event catalog, and every `AVATAR_*` knob.
- **[Tutorial](docs/tutorials/terminal-agent.mdx)** — build a terminal agent of your own.
- **[API reference](docs/api-reference)** — generated from docstrings (always in sync with the source).

Design depth: [`ARCHITECTURE.md`](ARCHITECTURE.md) (system map) · [`HARNESS_DESIGN.md`](HARNESS_DESIGN.md) (canonical spec) · [`docs/adr/`](docs/adr) (decision records) · [`PROGRESS.md`](PROGRESS.md) (build ledger). The docs site under [`docs/`](docs/) is Mintlify — `make docs-serve` previews it locally.

## Development

```bash
make test         # pytest
make lint         # ruff check
make format       # ruff format
make typecheck    # pyrefly
make check        # lint + typecheck + test — run before committing

uv run pytest tests/test_x.py::test_name   # a single test
make docs-api                              # regenerate the API reference from docstrings (commit it)
```

Contribution conventions (commit format, branch names, PR sections) are in [`CLAUDE.md`](CLAUDE.md).

## License

MIT — see [`LICENSE`](LICENSE).
