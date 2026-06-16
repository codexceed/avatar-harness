# avatar-harness

A minimal, ground-up **coding agent harness** — the runtime around an LLM that turns a
natural-language coding task into a bounded, verifiable engineering loop. The model
proposes actions; the harness owns execution, state, permissions, logging, and
verification. The loop terminates on **external verification**, not on a text reply.

This is the **SDK** (import `avatar`) plus a batch CLI (`avatar`). The interactive
cockpit built on top of it ships as a separate package, [`jo-cli`](https://pypi.org/project/jo-cli/).

```python
from avatar import Harness

state = Harness.from_env().run("explain the retry loop")
print(state.outcome, state.final_answer)
```

```bash
avatar "where does the agent loop terminate, and what sets outcome=success?"
```

## Install

```bash
pip install "avatar-harness[openai]"   # quote the extras (zsh expands brackets)
```

`openai` is an optional extra — install the base package and inject your own
`ModelClient` to target any backend. Requires Python 3.12+ and
[ripgrep](https://github.com/BurntSushi/ripgrep) on `PATH` (the `search_repo` tool
shells out to it).

## Documentation

Full design, architecture, and the SDK guide live in the
[project repository](https://github.com/codexceed/avatar-harness). Start with the
Quickstart and the SDK guide under `docs/`.

## License

MIT — see [LICENSE](LICENSE).
