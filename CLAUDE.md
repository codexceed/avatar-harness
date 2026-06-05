# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

**Pre-implementation.** `HARNESS_DESIGN.md` specifies the complete architecture; no code exists yet beyond the package scaffold (`src/avatar_harness/__init__.py`). When implementing, follow the MVP build order in **§20** of that document — each step is ordered to plug into the previous one (e.g. `Workspace` before any tool, `PermissionPolicy` before any side-effecting tool).

`HARNESS_DESIGN.md` is the source of truth for design decisions. Read the relevant section before implementing a component; the doc cross-references sections (§N) heavily. The suggested package layout is in §17.

## Commands

This project uses `uv`. There is no committed `uv.lock` yet (it is intentionally *not* gitignored — commit it once dependencies are installed).

```bash
uv sync                          # install deps (including dev extras)
uv run avatar-harness            # run the CLI (entry point: avatar_harness.cli:main — not yet implemented)

uv run ruff check .              # lint
uv run ruff format .             # format
uv run pyright                   # type-check (standard mode; src + tests)
uv run pytest                    # run tests (asyncio_mode=auto, testpaths=tests)
uv run pytest tests/test_x.py::test_name   # run a single test
```

External runtime requirement: `ripgrep` (`rg`) must be on `PATH` — the `search_repo` tool shells out to it.

## Architecture: what requires reading multiple files to understand

This is a **coding-agent harness**, not a chat app. The defining inversion: *the model proposes actions; the harness owns execution, state, permissions, logging, and verification.* The loop terminates on **external verification**, not on a text reply from the model.

Five load-bearing invariants thread through every component — violating one quietly breaks the design:

1. **`TaskState` is the source of truth, not the chat transcript.** The model's message history is *derived* from `TaskState` each turn. State is explicit, structured (pydantic), and append-mostly.

2. **The runner owns all mutation; tools are pure-ish.** Tools receive a run-scoped `RunDeps` (never globals), touch the filesystem/run commands *only* through the `Workspace` handle, and return a `ToolResult` — they do **not** mutate `TaskState`. The `AgentRunner` applies results to state *after* logging and permission checks. This is what makes a run replayable from the event log.

3. **"Done" is a proposal the verifier disposes of.** A `final_answer` action or a tool returning `terminate: true` marks the task *ready for verification* — it never ends the run. Only the harness-owned `Verifier` sets `outcome = "success"`, and only on positive external evidence (tests/lint/diff). The model never self-certifies. The verifier is **not** a tool.

4. **Control hooks vs. observation events are a hard line (§13).** The permission gate (`before_tool_call`) is an *awaited control hook* that can block/redirect the loop. The event emitter is *observation-only*: synchronous, fire-and-forget, cannot alter control flow. `EventLog` (JSONL) and the CLI display are subscribers. Never route control through the emitter, and never make permission an event subscriber.

5. **Everything is reversible and observable.** Operate on a tracked, path-confined `Workspace`; every edit is an inspectable diff (`apply_patch` is atomic/all-or-nothing). Append-only JSONL event log gives replay/debug/eval for free.

### Two axes that are deliberately kept separate

- **`phase`** (`investigating → editing → verifying`) is a *control* axis: it gates which tools are active (§10/§21 capability groups) and how context is assembled.
- **`outcome`** (`success` / `incomplete` / `blocked` / `failed`, `None` while live) is the *terminal result* axis, and is exactly what `ArtifactManager` reports as status.

Conflating them is what leaves budget-exhaustion vs. verification-failure ambiguous. Relatedly, two *different* bounds map to two outcomes: general budgets (max iterations, wall-clock, consecutive tool failures) → `incomplete`; the repair budget (consecutive verification rejections) → `failed`.

### Other design choices worth knowing before editing

- **`task_kind`** (`edit` / `investigate` / `explain` / `test_only`) selects the verification contract (§12) — it prevents edit-shaped verification ("a diff must exist") from being forced onto investigative/explanatory tasks. The verifier passes only on *required* checks with positive external signal, never vacuously on skipped checks.
- **`ModelDecision` is a constrained, validated union** (`tool_call` / `final_answer` / `ask_user`). `thought_summary` is for logging/context only — never for control flow. Invalid decisions are fed back as recoverable errors, never executed.
- **Retry semantics are narrow (§10):** only *model-correctable* errors (stale patch context, missing arg, bad path format, test target not found) loop back through the model. *System failures* (permission denied, timeout, network blocked, tool bug) are surfaced, never auto-retried.
- **`ToolResult.content` vs `details`:** the model only ever sees `content` (or a context-builder summary); `details`/`stdout`/`stderr` are retained for the event log, rendering, and artifacts — kept out of the model's context.
- **`ContextBuilder` (§9)** assembles a compact per-iteration packet, not the whole repo. The model discovers context incrementally via search/read tools. A compaction hook prunes old evidence to summaries while keeping recent verifier output verbatim.

### MVP deliberate scope cuts (§2)

No multi-agent orchestration, no browser automation, no autonomous dependency install, no automatic git commit/push/PR/deploy. Avoid a general `run_shell` tool in v1; the MVP tools are `search_repo`, `list_files`, `read_file`, `apply_patch`, `run_tests`, `run_linter`, `git_status`, `git_diff`. These are deferred (§21), not designed out — keep the architecture compatible.

## Reuse note

§18 lists fiddly, already-debugged plumbing to lift from an adjacent CLI chat app (`cli_chat/`) rather than re-derive: the cancellation race (`asyncio.wait(FIRST_COMPLETED)`), streaming/tool-call delta reassembly, LLM-valid history construction (`tool_call_id` pairing), pydantic-settings + OpenAI-compatible client. §19 lists mechanics adapted from [Pi](https://pi.dev).
