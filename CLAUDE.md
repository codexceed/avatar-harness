# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

**In active development — TDD, phased.** The engine is built through Phase 3.2 (the MVP cockpit), plus post-MVP dogfood hardening; durable crash-resume (3.3) is the remaining increment. **`PROGRESS.md` is the authoritative, checklist-driven build ledger — read it first** to see what's built and what's next. The build follows the phased plan there, which draws on the §20 component order in the design spec.

## Documentation map — which doc, when

Four docs, deepest to most operational. Pick by the *breadth* of the task:

| Doc | Holds | Consult when |
| --- | --- | --- |
| `HARNESS_DESIGN.md` | Full design spec — every decision + rationale, cross-referenced by §N. Source of truth. | Implementing a component (read its §N first); resolving *why* a thing is shaped as it is. |
| `ARCHITECTURE.md` | A synthesized, **visual** map: high-level component graph + deep dives on task execution and verification + a dry-run walkthrough, with current implementation status. | **Broad, global-context work** — feature implementation, deep debugging, deep Q&A, onboarding — where you need the whole-system picture. |
| `PROGRESS.md` | Phased build ledger (checklists), TDD protocol. | Resuming work; knowing what's done and what's next. |
| `docs/adr/` | Architecture Decision Records — *why* the build is shaped as it is (one decision per ADR: choice, rejected alternatives, trade-offs). The decision log going forward. | Resolving/recording why a design decision was made; before re-litigating a settled choice. |
| `CHANGELOG.md` | The *what shipped* — generated automatically by release-please from Conventional Commits. Do not hand-edit. | Seeing what changed in a release. |
| `research/` | Findings from **formally executed research work** — eval baselines/matrices, failure-mode catalogs, trajectory analyses, experiment write-ups. Evidence-linked + reproducible. | Recording the results of an explicit experiment/eval run; sourcing an empirical claim for a write-up. |
| `src/avatar_harness/tui/ARCHITECTURE.md` + `CLAUDE.md` | Package-local docs for the interactive cockpit (the `tui/` consumer): its component graph, the two planes, and the goal/approval/plan/render flows. | Cockpit-local work — editing `tui/` (the TUI shell, modals, `jo-cli`). |

> `DECISIONS.md` is a **frozen historical archive** (decisions through 2026-06-11); it is no longer appended to. New design decisions are ADRs; new changes are the changelog.

**When to skip `ARCHITECTURE.md`:** highly targeted, local work — a specific edit, a single command, a localized bugfix — where whole-system context would only add noise. Reach for it only when the task spans the system.

**Keep `ARCHITECTURE.md` current:** when a change alters the architecture (new component, changed control flow, a built milestone), update it — including its diagrams and the implementation-status markers — as part of that change.

**Keep `README.md` current:** when a change is **user-facing**, update `README.md` as part of that same change. User-facing means anything that alters how someone installs, configures, or runs the tool — new/changed CLI commands or flags, env vars / config keys, requirements or supported platforms, the set of task kinds the CLI exposes, installation steps, or the project's status as advertised there. Internal refactors, test-only changes, and design-doc edits are not user-facing and need no README update.

**Record design decisions as ADRs:** when you make a **major design decision** — a chosen approach, a rejected alternative, a scope cut, a non-obvious trade-off, or a load-bearing clarification — write a new ADR under `docs/adr/` (Nygard-style, one decision per ADR; supersede rather than edit an accepted one) and add it to `docs/adr/README.md`. Capture the *why* and the alternatives, not just the *what*. Routine implementation that merely follows an existing decision needs no ADR. Do **not** append to `DECISIONS.md` (frozen) or hand-write changelog prose — the *what shipped* comes from Conventional Commit messages, which release-please rolls into `CHANGELOG.md`.

**Record research findings under `research/`:** when you **formally and explicitly execute research work** — an eval run or baseline matrix, a trajectory analysis, a failure-mode investigation, a benchmark — write its findings to a dated, evidence-linked doc under `research/`, citing the raw artifact path and the command to reproduce. This is the durable, citable home for empirical results that feed write-ups and the eval-driven self-improvement signal. A *deliberately executed* experiment belongs here; ad-hoc observations during ordinary dev do not. Distinguish measured fact from interpretation, and don't bury findings in commit messages or scratch files.

## Commands

This project uses `uv`; `uv.lock` is committed. Dev tools live in `[dependency-groups].dev`, which `uv` syncs automatically — `make`/`uv run` need no extra flags. A `Makefile` wraps the common targets.

```bash
make install                     # uv sync (deps + dev group)
make test                        # run the test suite
make run TASK="explain the loop" # run the CLI on a task (drives the full agent loop)
make lint                        # ruff check
make format                      # ruff format
make typecheck                   # pyrefly check (src + tests)
make check                       # lint + typecheck + test — run before committing

uv run pytest tests/test_x.py::test_name   # run a single test
```

External runtime requirement: `ripgrep` (`rg`) must be on `PATH` — the `search_repo` tool shells out to it.

## Contributing: branches, commits, PRs

- **Commits** follow [Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>)>: <subject>`, e.g. `feat(events): stamp ts at emit time`.
- **Commit authorship** is always the local git user — never Claude or any agent. Do **not** add `Co-Authored-By: Claude` (or similar) trailers, and do not override the author/committer; commits must be attributed to the configured local user only.
- **Branch names** use `<type>/<issue-id>-<description>`, e.g. `fix/42-stale-patch-context`. When the work tracks a GitHub issue, put the issue number in the branch name. If a new branch isn't being created (or the issue number isn't in the name), tag the issue in the PR body instead.
- **PR descriptions** must contain these sections, in this order:
  1. **Description** — brief on what the changes are about.
  2. **Motivation** — why we're doing this.
  3. **Changes** — list of changes.
  4. **Testing** — list of tests and validations.

## Architecture: what requires reading multiple files to understand

This is a **coding-agent harness**, not a chat app. The defining inversion: *the model proposes actions; the harness owns execution, state, permissions, logging, and verification.* The loop terminates on **external verification**, not on a text reply from the model.

Five load-bearing invariants thread through every component — violating one quietly breaks the design:

1. **`TaskState` is the source of truth, not the chat transcript.** The model's message history is *derived* from `TaskState` each turn. State is explicit, structured (pydantic), and append-mostly.

2. **The runner owns all mutation; tools are pure-ish.** Tools receive a run-scoped `RunDeps` (never globals), touch the filesystem/run commands *only* through the `Workspace` handle, and return a `ToolResult` — they do **not** mutate `TaskState`. The `AgentRunner` applies results to state *after* logging and permission checks. This is what makes a run replayable from the event log.

3. **"Done" is a proposal the verifier disposes of.** A `final_answer` action or a tool returning `terminate: true` marks the task *ready for verification* — it never ends the run. Only the harness-owned `Verifier` sets `outcome = "success"`, and only on positive external evidence (tests/lint/diff). The model never self-certifies. The verifier is **not** a tool.

4. **Control hooks vs. observation events are a hard line (§13).** The permission gate (`before_tool_call`) is an *awaited control hook* that can block/redirect the loop. The event emitter is *observation-only*: synchronous, fire-and-forget, cannot alter control flow. `EventLog` (JSONL) and the CLI display are subscribers. Never route control through the emitter, and never make permission an event subscriber.

5. **Everything is reversible and observable.** Operate on a tracked, path-confined `Workspace`; every edit is an inspectable diff (`str_replace`/`write_file` are atomic/all-or-nothing). Append-only JSONL event log gives replay/debug/eval for free.

### Two axes that are deliberately kept separate

- **`phase`** (`investigating → editing → verifying`) is a *control* axis: it gates which tools are active (§10/§21 capability groups) and how context is assembled.
- **`outcome`** (`success` / `incomplete` / `blocked` / `failed`, `None` while live) is the *terminal result* axis, and is exactly what `ArtifactManager` reports as status.

Conflating them is what leaves budget-exhaustion vs. verification-failure ambiguous. Relatedly, two *different* bounds map to two outcomes: general budgets (max iterations, wall-clock, consecutive tool failures) → `incomplete`; the repair budget (consecutive verification rejections) → `failed`.

### Other design choices worth knowing before editing

- **`task_kind`** (`edit` / `investigate` / `test_only`) selects the verification contract (§12) — it prevents edit-shaped verification ("a diff must exist") from being forced onto investigative/explanatory tasks. (`investigate` subsumes pure explanation; there is no separate `explain` kind.) The verifier passes only on *required* checks with positive external signal, never vacuously on skipped checks.
- **`ModelDecision` is a constrained, validated union** (`tool_call` / `final_answer` / `ask_user`). `thought_summary` is for logging/context only — never for control flow. Invalid decisions are fed back as recoverable errors, never executed.
- **Retry semantics are narrow (§10):** only *model-correctable* errors (stale patch context, missing arg, bad path format, test target not found) loop back through the model. *System failures* (permission denied, timeout, network blocked, tool bug) are surfaced, never auto-retried.
- **`ToolResult.content` vs `details`:** the model only ever sees `content` (or a context-builder summary); `details`/`stdout`/`stderr` are retained for the event log, rendering, and artifacts — kept out of the model's context.
- **`ContextBuilder` (§9)** assembles a compact per-iteration packet, not the whole repo. The model discovers context incrementally via search/read tools. A compaction hook prunes old evidence to summaries while keeping recent verifier output verbatim.

### MVP deliberate scope cuts (§2)

No multi-agent orchestration, no browser automation, no autonomous dependency install, no automatic git commit/push/PR/deploy. Avoid a general `run_shell` tool in v1; the MVP tools are `search_repo`, `list_files`, `read_file`, `str_replace`, `write_file`, `delete_file`, `run_tests`, `run_linter`, `git_status`, `git_diff`. These are deferred (§21), not designed out — keep the architecture compatible.

## Reuse note

§18 lists fiddly, already-debugged plumbing to lift from an adjacent CLI chat app (`cli_chat/`) rather than re-derive: the cancellation race (`asyncio.wait(FIRST_COMPLETED)`), streaming/tool-call delta reassembly, LLM-valid history construction (`tool_call_id` pairing), pydantic-settings + OpenAI-compatible client. §19 lists mechanics adapted from [Pi](https://pi.dev).
