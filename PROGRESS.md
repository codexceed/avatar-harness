# PROGRESS — avatar-harness build ledger

**Authoritative, durable, git-tracked record of where the build is.** Read this first when resuming. `HARNESS_DESIGN.md` is *what* we're building and *why*; this file is *how far* we've gotten and *what's next*. Progress is tracked as checklists — a phase advances only when its boxes are ticked.

> **Current position:** Phase 1 — all 23 approved tests green (33/33 total); the read-only loop runs end-to-end with a scripted model. Remaining: wire a real `OpenAIModelClient` + CLI run-loop to dogfood ("answers a repo question").

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

`Workspace`/`RunDeps` + read tools + `ModelClient` + loop + a **minimal `investigate` verifier gate**. Tier-0 only; safe to dogfood. Engine steps §20: 4, 5, 9, 10 (+ a thin slice of 11). Tests approved 2026-06-06 (22).

**Workspace — path confinement**
- [x] `test_workspace_reads_inside_root`
- [x] `test_workspace_refuses_path_outside_root`
- [x] `test_workspace_refuses_symlink_escape`
- [x] `test_workspace_read_respects_line_range`
- [x] impl `workspace.py` (confinement, line-range read, pinned-baseline diff)

**Read tools — typed `ToolResult`s**
- [x] `test_search_repo_finds_matches`
- [x] `test_search_repo_no_matches_is_clean_success`
- [x] `test_list_files_matches_glob`
- [x] `test_read_missing_file_is_model_correctable`
- [x] impl `tools/filesystem.py` (`read_file`, `list_files`), `tools/search.py` (`search_repo`), `deps.py` (`RunDeps`)

**ToolRuntime + registry — phase gating & validation**
- [x] `test_registry_exposes_only_phase_tools`
- [x] `test_unknown_tool_name_rejected`
- [x] `test_invalid_tool_input_fed_back`
- [x] impl `tools/base.py` (`ToolResult`, `ToolDefinition`, `ToolRegistry`, `ToolRuntime`)

**ModelClient — constrained decision protocol (mocked)**
- [x] `test_parses_tool_call_decision`
- [x] `test_parses_final_answer_decision`
- [x] `test_malformed_decision_is_recoverable`
- [x] impl `model_client.py` (decision models, `parse_decision`, `ModelClient` protocol)

**Verifier — minimal `investigate` gate**
- [x] `test_investigate_gate_passes_with_cited_evidence`
- [x] `test_investigate_gate_fails_on_zero_evidence`
- [x] `test_investigate_gate_fails_on_unintended_diff`
- [x] impl `verifier.py` (`investigate` gate — structural, no model)

**AgentRunner — the read-only loop**
- [x] `test_investigate_loop_runs_to_answer_and_verifies`
- [x] `test_final_answer_without_evidence_is_rejected`
- [x] `test_iteration_budget_yields_incomplete`
- [x] `test_ask_user_noninteractive_yields_blocked`
- [x] impl `runner.py` (the §5 loop; runner-owned mutation; bounding)

**ContextBuilder — the compact packet**
- [x] `test_context_contains_goal_phase_and_recent_evidence`
- [x] `test_context_omits_out_of_phase_tools`
- [x] impl `context.py` (`ContextPacket`, phase-gated tool list, recent evidence)

**Exit criteria**
- [ ] answers a repo question citing files/lines *(needs real `OpenAIModelClient` + CLI run-loop)*
- [x] path-confinement refuses out-of-root read
- [x] budgets respected; zero side-effecting tools registered
- [x] `final_answer` routes through the verifier (evidence cited + no unintended diff) — never self-certified
- [x] all Phase 1 tests green (23/23) · `ruff` + `pyright` clean

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
- **2026-06-06** — `task_kind` merged from four values to three (`edit | investigate | test_only`): `explain` folded into `investigate` (identical verification contract). Pure command-execution also maps to `investigate` — no `execute` kind, since `task_kind` is a taxonomy of verification contracts, not user intents. Updated `HARNESS_DESIGN.md` §7/§12 and `state.py`. Noted an open edge in §12: non-executable edits (docs/config) have no command-based positive signal.
- **2026-06-06** — Added `ARCHITECTURE.md`: a visual, synthesized whole-system map (high-level graph + deep dives on task execution and verification + a dry-run walkthrough), with `[Implemented]`/`[Designed]` status tags. To be kept current with each architecture-altering change. `CLAUDE.md` updated with a documentation map and when-to-consult guidance (broad/global tasks yes; targeted edits no).
- **2026-06-06** — `task_kind` confirmed kept as-is (`edit | investigate | test_only`; no rename, no fold). Clarified: kind is classified at intake from the goal, then its verification contract is applied — verification is per-kind (working diff / passing new tests / cited diff-free answer), not a choice made from what the agent happened to do.
- **2026-06-06** — Documented two load-bearing verification clarifications (`ARCHITECTURE.md` §4.0, `HARNESS_DESIGN.md` §15): (a) the verifier runs **no LLM** — structural inspection is predicates over `TaskState`; (b) verification reads the **uncommitted** working tree vs a **pinned baseline** (HEAD-at-start), and the harness **never commits** — the diff is the deliverable. `state.files_modified` is the git-independent primary signal.
- **2026-06-06** — Phase 1 will include a **minimal `investigate`-only verifier gate** (not deferred to Phase 2). It inspects `TaskState`/workspace only (positive evidence cited + no unintended diff), so it is self-contained and sidesteps the §4.3 command-source gap. The full `edit`/`test_only` verifier (which needs test/lint command resolution) stays Phase 2. This keeps "verifier-owned completion" true from the first loop.
