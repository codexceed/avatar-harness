# jo-cli

`jo` — an interactive **Textual cockpit** for the
[`avatar-harness`](https://pypi.org/project/avatar-harness/) coding agent. A reference
coding agent built **on** the harness core: a full-screen, multi-turn REPL where the agent
reads, edits, runs, and verifies with you in the loop.

`jo` is one consumer of the harness; it depends on `avatar-harness` and consumes only its
public surface. The import direction is strictly consumer → core — the core never imports
the cockpit.

## Install

```bash
pip install jo-cli   # pulls avatar-harness[openai] + textual
```

Requires Python 3.12+, [ripgrep](https://github.com/BurntSushi/ripgrep) on `PATH`, and an
OpenAI-compatible LLM endpoint (configure via `AVATAR_*` env vars or a `.env`).

## Run

```bash
jo                 # launch the cockpit
jo --auto          # keep the strict verification gate (default: conversational)
```

- **Meta commands** (local, never hit the model): `/help`, `/mode`, `/plan`, `/diff`,
  `/state`, `/permissions`, `/quit`.
- **Ground a goal** in a file with `@path/to/file`.
- **Approval prompts** for `run_command` and sensitive-path calls.
- **Conversational by default** — verification runs and is reported, but the reply isn't
  gated on it (you're the terminal authority); `--auto` keeps the strict gate.

## Documentation

Architecture and design notes live in the
[project repository](https://github.com/codexceed/avatar-harness) (`jo/ARCHITECTURE.md`,
`jo/CLAUDE.md`).

## License

MIT — see [LICENSE](LICENSE).
