# PROGRESS — avatar-harness build ledger

**Authoritative, durable, git-tracked record of where the build is.** Read this first when resuming. `HARNESS_DESIGN.md` is *what* we're building and *why*; this file is *how far* we've gotten and *what's next*. Progress is tracked as checklists — a phase advances only when its boxes are ticked.

> **Current position:** Phase 0 — ✅ complete (10/10 tests green, `ruff` + `pyright` clean, CLI runs end-to-end). Next: Phase 1 — propose tests for sign-off.

## How to use this file

- **Resuming?** Read *Current position*, then the first phase with unticked boxes.
- **Source-of-truth rule:** this file (durable, in-repo) outranks any in-session task list (ephemeral). A session task list is a scratchpad for active work; reconcile it back here before stopping.
- **A box is ticked only when true** — a passing test, a met criterion. No aspirational checks.

## TDD protocol (every phase)

1. **Propose** the test list — each test's name, what it asserts, why it's the right contract.
2. **Check in** — maintainer approves the list *before any production code*. (Standing rule: do not confirm a phase's tests without explicit sign-off.)
3. **Red** — commit the approved tests; they fail.
4. **Green** — implement the thinnest code that passes (honor the complexity ceiling).
5. **Refactor** under green.
6. **Record** — tick the boxes; update *Current position*.

Tests are the phase's exit contract: "done" means required tests pass and exit criteria hold, not "the code looks finished."

---

## Phase 0 — Walking skeleton

CLI shell + `config` + `TaskState` + event spine; loop echoes. No model, no tools.

**Tests** (`tests/`)
- [x] `test_config_loads_defaults`
- [x] `test_config_env_override`
- [x] `test_taskstate_roundtrips`
- [x] `test_terminal_property`
- [x] `test_add_feedback_appends_evidence`
- [x] `test_emitter_is_fire_and_forget`
- [x] `test_eventlog_writes_valid_jsonl`
- [x] `test_subscriber_cannot_alter_control`
- [x] `test_run_emits_start_and_end`
- [x] `test_echo_roundtrip`

**Implementation**
- [x] `config.py` — `HarnessConfig` (pydantic-settings, `AVATAR_*` env)
- [x] `state.py` — `TaskState` + `Evidence`/`DecisionRecord`/`CommandRecord`/`VerifierResult`
- [x] `events.py` — observation-only `Emitter`
- [x] `eventlog.py` — JSONL subscriber
- [x] `cli.py` — echo loop + `main()`

**Exit criteria**
- [x] `uv run avatar-harness "<task>"` starts, emits `agent_start … agent_end`, exits clean
- [x] `TaskState` round-trips through pydantic JSON
- [x] all Phase 0 tests green · `ruff` + `pyright` clean

---

## Phase 1 — Read-only agent

`Workspace`/`RunDeps` + read tools + `ModelClient` + loop. Investigate/explain only; tier-0; safe to dogfood. Engine steps §20: 4, 5, 9, 10.

- [ ] Tests proposed & approved
- [ ] Tests red → green
- [ ] Exit: answers a repo question citing files/lines; path-confinement refuses out-of-root read; budgets respected; zero side-effecting tools

## Phase 2 — Closing the loop (MVP)

`PermissionPolicy` + `apply_patch` + one verifier command + `Verifier` + `ArtifactManager`. Engine steps §20: 6, 7, 8, 11, 12.

- [ ] Tests proposed & approved
- [ ] Tests red → green
- [ ] Exit: `edit` task patches atomically, runs a verifier command, `outcome` set by verifier (not self-certified), artifact has status+files+evidence; gate blocks a tier-3 action; bad patch leaves workspace unchanged

## Phase 3 — Interactive cockpit

Interaction layer (§23): REPL, streaming render, allow-once/deny approval, Ctrl-C cancel, `/quit` + `/diff`.

- [ ] Tests proposed & approved
- [ ] Tests red → green
- [ ] Exit: multi-turn REPL streams tool activity, prompts approval before `apply_patch`, cancels in-flight command and refeeds as feedback, `/diff` runs without a model call, batch (`--auto`) shares the code path

## Phase 4+ — Earned extensions

From §21, one at a time, each justified by friction actually hit.

- [ ] (none scheduled)

---

## Standing design principles (complexity guardrails)

- **A — Extensible at the edges, closed at the core.** Capability lives in registries (tools/checks/permissions); adding one touches no core file. Protocols over inheritance. *Add the seam, not the framework.*
- **B — The code reads like the design.** `runner.py` mirrors the §5 pseudocode. One mutator (the runner owns all `TaskState` mutation); no globals (`RunDeps`); domain vocabulary = identifiers; debug by replaying the event log.
- **C — Conservative complexity ceiling.** Build the shape, keep implementations thin. No abstraction until a second concrete case exists (rule of three). One mechanism per concern. Minimal, boring dependencies. The §2 non-goals and §21 defer-list are written permission to say no.

## Decision log

- **2026-06-05** — Interaction layer formalized in `HARNESS_DESIGN.md` §23 (REPL/session wraps the unchanged task engine; reuses event emitter, permission hook, cancellation).
- **2026-06-05** — Build tracked here in `PROGRESS.md` (durable) rather than `ROADMAP.md`; in-session task lists are scratch, this file is source of truth. Progress measured as checklists.
- **2026-06-05** — Multi-agent "agent teams" not the primary build driver (sequential, TDD-gated, human-reviewed). Reserved for bounded fan-out sub-tasks: test brainstorming, design exploration, adversarial phase review.
- **2026-06-05** — TDD adopted: tests proposed and approved at each phase start before production code. Phase 0 tests approved.
