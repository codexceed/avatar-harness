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

## Configure

`jo` reads its model config from `AVATAR_*` environment variables (or a `.env` file in the
directory you launch it from). **At minimum, set an API key** — without it the first goal
fails when the model client is called. Create a `.env`:

```bash
# .env  (or export these in your shell)
AVATAR_API_KEY=sk-or-...                       # required; falls back to OPENAI_API_KEY
AVATAR_MODEL=openai/gpt-4o-mini                # any model your endpoint serves
AVATAR_BASE_URL=https://openrouter.ai/api/v1   # default (OpenRouter); swap for OpenAI/local
```

Point at OpenAI instead: `AVATAR_BASE_URL=https://api.openai.com/v1`, `AVATAR_MODEL=gpt-4o-mini`.
For a local server (Ollama, vLLM, LM Studio): set `AVATAR_BASE_URL` to its `/v1` endpoint and
any non-empty `AVATAR_API_KEY`. The full set of `AVATAR_*` knobs (workspace root, temperature,
test/lint commands, …) is documented in the
[project repository](https://github.com/codexceed/avatar-harness); `avatar`'s `config.py` is
the source of truth.

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
- **Multi-line prompts** — the input box composes across lines: **Enter** sends, **Shift+Enter**
  (or **Ctrl+J**, which works in every terminal) inserts a newline. The box grows as you type,
  then scrolls.
- **Prompt history** — `↑`/`↓` recall the prompts you submitted this sitting; recall is
  **edge-gated**, so in a multi-line draft the arrows only reach history from the first/last line
  and otherwise just move the cursor.
- **`Ctrl+C`** copies the current selection if one is active, else interrupts the in-flight
  run — **instantly**, even mid model call (it aborts the request and frees the cockpit) —
  else quits. An external `SIGINT`/`SIGTERM` (`kill`) shuts down gracefully. To copy with your
  OS shortcut (e.g. `Cmd+C`), use your terminal's native selection — in **iTerm2** hold
  **Option** while drag-selecting, in **Terminal.app** / **GNOME Terminal** / **Windows
  Terminal** hold **Shift** — then copy as usual.

## Documentation

Architecture and design notes live in the
[project repository](https://github.com/codexceed/avatar-harness) (`jo/ARCHITECTURE.md`,
`jo/CLAUDE.md`).

## License

MIT — see [LICENSE](LICENSE).
