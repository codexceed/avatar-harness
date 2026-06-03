# avatar-harness

A minimal, ground-up **coding agent harness** — the runtime around an LLM that turns a natural-language coding task into a bounded, verifiable engineering loop. The model proposes actions; the harness owns execution, state, permissions, logging, and verification.

> The name: the LLM steps into an *avatar* and goes off to do real work in a repo — inspect, edit, verify — under a harness that keeps it safe, observable, and reversible.

## Status

**Pre-implementation.** The design is specified in full; no code has been written yet. See [`HARNESS_DESIGN.md`](HARNESS_DESIGN.md) for the complete architecture, and §20 of that doc for the MVP build order.

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

## Requirements (planned)

- Python 3.12+
- [`ripgrep`](https://github.com/BurntSushi/ripgrep) (`rg`) on `PATH` — used by the repo-search tool
- An OpenAI-compatible LLM endpoint (configurable `base_url` + model)

## Development (once implementation begins)

```bash
uv sync            # install deps
uv run avatar-harness   # run the CLI (not yet implemented)
```

## License

MIT — see [`LICENSE`](LICENSE).
