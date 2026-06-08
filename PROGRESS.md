# PROGRESS — avatar-harness build ledger

**Authoritative, durable, git-tracked record of where the build is.** Read this first when resuming. `HARNESS_DESIGN.md` is *what* we're building and *why*; this file is *how far* we've gotten and *what's next*. Progress is tracked as checklists — a phase advances only when its boxes are ticked.

> **Current position:** Phase 2 ✅ complete (73/73 green; `make check` clean — lint + pyrefly + deptry + docstrings). The edit loop closes: `apply_patch` (atomic, path-confined) under the permission gate, the harness-owned `Verifier` runs its own command to set `outcome`, `ArtifactManager` reports it. Scripted-model smoke: read → patch → verifier runs command → success. **Live model dogfood confirmed 2026-06-08** (investigate task → read → grounded answer → verifier passed → `success`). **Phase 2.5 ✅ complete 2026-06-08** (110/110 green; `make check` clean) — sensitive-path denylist at the gate, `list_files` directory expansion, decision/action ledger, and less-lossy evidence compaction. Next: Phase 3 (interactive cockpit).

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

**Integration — real model + CLI**
- [x] `test_openai_client_builds_request_and_parses` (mocked transport)
- [x] `test_malformed_decisions_yield_incomplete` (runner recovers from bad output)
- [x] impl `OpenAIModelClient` + prompt assembly (`build_messages`); `cli.run_agent` wired; `tools.default_registry`

**Exit criteria**
- [x] path-confinement refuses out-of-root read
- [x] budgets respected; zero side-effecting tools registered
- [x] `final_answer` routes through the verifier (evidence cited + no unintended diff) — never self-certified
- [x] all Phase 1 tests green (35/35) · `ruff` + `pyright` clean
- [x] CLI runs the real loop end-to-end (scripted-model smoke: read → verify → success)
- [x] **live**: answers a real repo question via a configured model (`AVATAR_API_KEY`, OpenRouter) — dogfooded 2026-06-08 ("explain how apply_patch stays atomic" → read `workspace.py` → grounded answer → verifier passed → `outcome=success`)

## Phase 2 — Closing the loop (MVP)

`PermissionPolicy` + `apply_patch` + bounded `run_tests`/`run_linter` + full `Verifier` (`edit`/`test_only` gates) + `ArtifactManager`. Engine steps §20: 6, 7, 8, 11, 12. Plus closing the known Phase-1 gap: `Workspace` clean-start assertion (§15). **Tests approved 2026-06-07 (~34).**

**Confirmed design forks (2026-06-07):** (1) the **verifier runs the verification command itself** (`ws.run(config.test_command)`), independent of any `run_tests` the model called — the gate's signal is harness-owned, never model-mediated (§5); (2) command source is **explicit config** (`AVATAR_TEST_COMMAND`/`AVATAR_LINT_COMMAND`), not target inference (§21 deferred); (3) the permission gate stays **synchronous** in Phase 2 (`policy.check(...) -> ToolPermission`), called directly by the runner — `async` lands with the Phase 3 REPL; (4) `Workspace` asserts a **clean-or-acknowledged git state** at open (`allow_dirty`), closing the gap logged 2026-06-07.

**Workspace — patch write, command exec, clean-start (§10, §15)**
- [x] `test_workspace_applies_multi_file_patch_atomically`
- [x] `test_workspace_rejects_patch_touching_outside_root`
- [x] `test_workspace_stale_patch_applies_nothing`
- [x] `test_workspace_patch_creates_and_deletes_only_when_explicit`
- [x] `test_workspace_diff_reflects_applied_patch`
- [x] `test_workspace_run_captures_stdout_stderr_exit_code`
- [x] `test_workspace_run_times_out`
- [x] `test_workspace_open_accepts_clean_state_and_pins_head`
- [x] `test_workspace_open_rejects_dirty_unless_allowed`
- [x] impl `workspace.py` (`apply_patch` via `git apply --check`, `run`, clean-start, `PatchError`, `CommandOutput`)

**PermissionPolicy — tiers + gate (§11)**
- [x] `test_tier0_reads_allowed`
- [x] `test_apply_patch_allowed_when_paths_validate`
- [x] `test_apply_patch_blocked_when_path_escapes`
- [x] `test_tier2_commands_allowed_with_timeout`
- [x] `test_tier3_action_blocked_by_default`
- [x] `test_gate_returns_control_decision_not_event`
- [x] impl `permission.py` (`PermissionPolicy`, `ToolPermission`, tier table)

**Side-effecting tools — apply_patch / run_tests / run_linter (§10)**
- [x] `test_apply_patch_tool_reports_changed_files`
- [x] `test_apply_patch_tool_stale_context_is_model_correctable`
- [x] `test_run_tests_passing_surfaces_evidence`
- [x] `test_run_tests_failure_is_not_a_tool_error`
- [x] `test_run_tests_target_not_found_is_model_correctable`
- [x] `test_run_linter_runs_configured_command`
- [x] impl `tools/edit.py` (`apply_patch`), `tools/commands.py` (`run_tests`, `run_linter`)

**Verifier — `edit` + `test_only` gates (§12)**
- [x] `test_edit_gate_passes_with_diff_and_passing_tests`
- [x] `test_edit_gate_fails_with_no_diff`
- [x] `test_edit_gate_fails_on_failing_tests`
- [x] `test_edit_gate_passes_on_clean_lint_when_no_test_target`
- [x] `test_edit_gate_fails_on_disallowed_skip`
- [x] `test_edit_gate_flags_placeholder_or_secret`
- [x] `test_test_only_gate_passes_when_new_tests_added_and_pass`
- [x] `test_test_only_gate_fails_when_no_tests_changed`
- [x] `test_verifier_never_passes_on_zero_positive_signal`
- [x] impl `verifier.py` (`edit`/`test_only` gates; runs the verification command via `ws.run`)

**ArtifactManager — final summary (§14)**
- [x] `test_artifact_status_is_state_outcome_verbatim`
- [x] `test_artifact_lists_files_commands_verification_and_diff_ref`
- [x] impl `artifact.py` (`Artifact`, `ArtifactManager.build`/`render`)

**Runner integration — gate wired + edit end-to-end (§5)**
- [x] `test_runner_consults_gate_before_execution`
- [x] `test_edit_task_runs_to_verified_success`
- [x] `test_bad_patch_leaves_workspace_unchanged_and_loops`
- [x] `test_repair_budget_exhaustion_yields_failed`
- [x] impl runner: consult `policy` before execute; wire `apply_patch`/commands into `default_registry`; config `test_command`/`lint_command`/`command_timeout_seconds`

**Exit criteria**
- [x] `edit` task patches atomically, runs a verifier command, `outcome` set by verifier (not self-certified), artifact has status+files+evidence
- [x] gate blocks a tier-3 action; bad patch leaves workspace unchanged
- [x] all Phase 2 tests green (73/73) · `ruff` + `pyrefly` clean (`make check`)

## Phase 2.5 — Context fidelity & secret safety (dogfood hardening)

Surfaced by a live dogfood (2026-06-08, a "rich chat app" investigate run; log `events/ff24fa3c…jsonl`): the agent (a) read `.env` and the secret propagated to the event log, the model context, **and a third-party API** (`sk-or-v1` appears 3× in the JSONL); (b) **looped** — turns 9–13 replayed turns 1–5 — because it has no memory of its own actions and evidence is a hard `[-5:]` FIFO that drops, not summarizes; (c) `list_files` silently returned 0 for a directory-matching glob (`rich*` matched a dir, filtered out by `is_file()`). These are *friction actually hit* (Principle C / Phase-4 spirit), pulled ahead of Phase 3 because the interactive cockpit only amplifies a looping, leaky agent. **Placement is a maintainer call** — trivially movable. **Approved & implemented 2026-06-08** (red → green; 19 tests; 110/110 green, `make check` clean).

**Sensitive-path denylist — secret reads blocked at the gate (§11)**
Design: the *sensitivity policy* (denylist patterns) lives on **`HarnessConfig`** as an `AVATAR_*` pydantic-settings field with a built-in default set, overridable per run; `PermissionPolicy` enforces it **centrally** over every tool's *declared* paths, so it can't drift or be forgotten. The *tool* self-declares only **which inputs are paths** — a `paths(args) -> Sequence[str]` field on the `ToolDefinition` dataclass, default `lambda args: ()` (the pass-through). This **unifies** the existing `apply_patch` confinement special-case (`permission.py:67`): targets become declared paths, and confinement + denylist consume them uniformly. *Rejected alt:* a per-tool `validate()` carrying the denylist itself — opt-in security drifts/gets forgotten; defer a general validate hook until a 2nd tool-local need (rule of three). *Mechanism:* dataclass field, **not** an ABC (keeps tools as values; avoids per-tool subclasses).
- [x] `test_read_file_denied_for_sensitive_path` (`.env`, `*.pem`, `id_*`, `**/.ssh/**`, `.netrc`, …)
- [x] `test_denylist_configured_via_harness_config` (default set + `AVATAR_*` override)
- [x] `test_apply_patch_denied_when_target_is_sensitive` (denylist spans all declared paths, not just reads)
- [x] `test_non_sensitive_path_still_allowed`

*Redaction deferred (decided 2026-06-08):* content-level secret scrubbing is **out**. Secret *detection* is heuristic; a bad scrub risks corrupting legitimate context or giving false confidence. The denylist is deterministic prevention (path-pattern match, no detection). **Residual risk accepted:** a secret reaching state via a non-denylisted file or a command's stdout is not scrubbed.

**list_files — directory patterns expand to contents (§10)**
- [x] `test_list_files_dir_pattern_lists_contained_files` (`rich*` / `src` → files under the dir)
- [x] `test_list_files_result_is_capped_with_overflow_note` (a dir match must not dump 10k paths into context)

**Action ledger — the agent sees what it already did (§7/§9)**
- [x] `test_runner_records_decision_each_turn` (populate `state.decisions`, currently never written)
- [x] `test_context_includes_prior_actions` (compact `tool(args)→outcome` lines; long horizon — cheap)
- [x] `test_repeated_identical_tool_call_is_flagged` (and optionally served from a **run-scoped** memo)

*Caching: not `functools.cache`/`lru_cache`* — those are process-global (leak results across runs/workspaces → violate run-scoped `RunDeps` + replayability) and have no mutation invalidation (a re-read after `apply_patch` must not return stale content). A result cache also saves only a cheap FS read, not the wasted *turn* — the loop is fixed by the model not re-emitting, not by a fast hit. If we cache at all: a small dict on the runtime keyed by `(tool_name, normalized_input)`, **gated to idempotent read tools**, **cleared on any successful mutation**, with the repeat **surfaced to the model** (visible nudge, not a silent speedup).

**Less-lossy compaction — degrade, don't drop (§9)**
Replace `ContextBuilder`'s fixed `evidence[-5:]` slice with a char/token budget filled most-recent-first: recent evidence verbatim → older collapsed to `summary` only (summary/detail already stored separately, so this tier is free + deterministic) → oldest names-only; pin the latest verifier output verbatim regardless of budget; collapse duplicate evidence. LLM roll-up deferred (Principle C).
- [x] `test_old_evidence_degrades_to_summary_not_dropped`
- [x] `test_recent_verifier_output_pinned_verbatim`
- [x] `test_duplicate_evidence_collapsed`
- [x] `test_context_respects_char_budget`

**Exit criteria**
- [x] reading a denylisted path is blocked at the gate *before its contents are read*, so denylisted secrets never enter state/log/context/provider (non-denylisted channels = accepted residual risk; redaction deferred)
- [x] `list_files` on a directory pattern returns its files (capped with an overflow note)
- [x] a >5-turn investigate run does not replay earlier tool calls (loop closed); prior actions are visible in-context
- [x] aged-out evidence degrades to summaries (model still sees what it found); latest verifier output stays verbatim
- [x] all Phase 2.5 tests green · `make check` clean

## Phase 3 — Interactive cockpit

Interaction layer (§23): REPL, streaming render, allow-once/deny approval, Ctrl-C cancel, `/quit` + `/diff`.

- [ ] Tests proposed & approved
- [ ] Tests red → green
- [ ] Exit: multi-turn REPL streams tool activity, prompts approval before `apply_patch`, cancels in-flight command and refeeds as feedback, `/diff` runs without a model call, batch (`--auto`) shares the code path

## Phase 4+ — Earned extensions

From §21, one at a time, each justified by friction actually hit.

- [ ] **Eval & observability harness** — *scheduled after Phase 3 (REPL)*. Make agent performance measurable so changes iterate against data, not vibes:
  - **internal eval harness**: a fixed task set (goal + checkable outcome) → run agent → the **`Verifier` scores pass/fail** (the verifier *is* the scorer) → aggregate **resolution rate (pass@1)**, iterations-to-solve, tokens/cost-per-solved, and a failure-mode histogram (incl. oscillation/loop detection); diff vs. the previous run for regression.
  - **tracer**: wire one as an emitter subscriber (Langfuse recommended — self-hostable); adopt OpenTelemetry GenAI conventions to stay vendor-neutral.
  - **external comparability** (later): SWE-bench Verified · Terminal-Bench · Aider polyglot.
  - Landscape + rationale in the 2026-06-07 decision-log entry.

---

## Standing design principles (complexity guardrails)

- **A — Extensible at the edges, closed at the core.** Capability lives in registries (tools/checks/permissions); adding one touches no core file. Protocols over inheritance. *Add the seam, not the framework.*
- **B — The code reads like the design.** `runner.py` mirrors the §5 pseudocode. One mutator (the runner owns all `TaskState` mutation); no globals (`RunDeps`); domain vocabulary = identifiers; debug by replaying the event log.
- **C — Conservative complexity ceiling.** Build the shape, keep implementations thin. No abstraction until a second concrete case exists (rule of three). One mechanism per concern. Minimal, boring dependencies. The §2 non-goals and §21 defer-list are written permission to say no.

## Decision log

- **2026-06-08** — Phase 2.5 implemented (red → green, 19 tests, 110/110 green, `make check` clean). **Denylist:** `ToolDefinition` gained a `paths(args)` extractor (default pass-through); `read_file`/`apply_patch` declare their paths; `PermissionPolicy` now runs confinement **and** the sensitive-path denylist centrally over declared paths (subsuming the old `apply_patch` special-case), with `DEFAULT_SENSITIVE_PATH_GLOBS` + `HarnessConfig.sensitive_path_globs` (`AVATAR_*`). Matching: a slashless glob hits any path *component* (gitignore-style "anywhere"), a slashed glob `fnmatch`es the whole path. `search_repo` passes `-g !<pat>` excludes so it can't become a read bypass. A sensitive hit blocks with `ask=True` (→ an `ask` once the REPL lands). **Ledger:** `DecisionRecord` gained `outcome`; the runner records one per turn (filled with the tool summary / verifier verdict / block reason) and flags an identical re-issued call as `repeat` evidence; `ContextPacket.prior_actions` surfaces them and `build_messages` prints an "Actions so far (do NOT repeat these)" block. **Compaction:** `ContextBuilder` replaced the hard `evidence[-5:]` slice with newest-first budgeting — full detail until `detail_char_budget`, then summary-only, latest verifier output pinned verbatim, adjacent duplicates collapsed to `... (xN)`, capped at `max_evidence_lines`. **`list_files`:** directory matches expand to contained files (`rglob`), tool caps output at `_LIST_CAP` with an overflow note (full count kept in the summary). Notes: used `fnmatch` (not `PurePath.full_match`, which the type checker doesn't yet know); `_run_tool_call` extracted from `run()` to stay under the statement cap.
- **2026-06-08** — Phase 2.5 opened from a live dogfood (`events/ff24fa3c…jsonl`, a "rich chat app" investigate run that produced functional-but-UI-buggy code). Three classes of finding, logged as objectives. **(1) Secret leak:** `read_file('.env')` succeeded (confinement only blocks paths *outside* root, not sensitive paths *inside* it), and the key propagated to the event log (3× in plaintext), the model context, and the third-party provider. Fix = a *central, configurable* denylist in `PermissionPolicy` enforced over each tool's *self-declared* paths (`ToolDefinition.paths` field, default pass-through) + egress redaction. Deliberately **not** a per-tool `validate()` carrying the denylist (opt-in security drifts) and **not** an ABC (keeps tools as values); the declared-paths approach also unifies the existing `apply_patch` confinement special-case. **(2) Loop:** turns 9–13 replayed turns 1–5 because `state.decisions` is never written and `ContextBuilder` uses a hard `evidence[-5:]` FIFO — the agent has no memory of its own actions and old evidence is dropped, not summarized. Fix = an action ledger (cheap, long horizon) + the §9 tiered/budgeted compaction that degrades detail→summary→name instead of dropping (summary/detail already stored separately, so the degrade tier is free). **(3) `list_files` dir blindness:** a glob matching a directory returns 0 (filtered by `is_file()`); fix = expand dir matches to contained files, capped. Placement (2.5, before the interactive cockpit) is a maintainer call; tests proposed, **pending approval**. *Refined same day:* (i) **redaction dropped** — heuristic secret detection is too risky to mutate context with (corruption / false confidence); denylist-only prevention, non-path leak accepted as residual risk; (ii) denylist patterns live on **`HarnessConfig`** (`AVATAR_*`, default set + override); (iii) tool-call caching, if added, is a **run-scoped memo, not `functools.cache`** (global + no mutation invalidation; and it saves a cheap FS read, not the wasted turn) — gated to idempotent reads, cleared on mutation, repeat surfaced to the model.
- **2026-06-08** — Session grouping made explicit (REPL precursor; §23). Spotted while reading a real log: the default `--log events/session.jsonl` is a static path, so every run *appended* to one file and the only divider was `task_id` — grouping was incidental, and the filename's "session" did no work. Two untangled properties: append-only durability (deliberate, kept — invariant #5) vs. the grouping *boundary* (was just a filename). Fix: (a) `session_id` is now a property of the `Emitter` (constructor arg), stamped on every event right after `ts` like a grouping key — a session-less `Emitter()` omits it, so tests/ad-hoc use stay clean and grouping is never synthetic; (b) the CLI mints one `session_id` per process and defaults the log to **`events/<session_id>.jsonl`** (one session = one self-identifying file: stem == stamped id) plus a best-effort **`events/latest.jsonl`** symlink to the newest; an explicit `--log` opts out of the managed layout (no pointer). Console rendering skips `session_id` (constant per run — noise per line); JSONL keeps it. Defines "session" = one process invocation today; in the Phase-3 REPL the session becomes the long-lived process and a `SessionState` will hold the workspace/config/log + the ordered list of per-task `TaskState`s, while each user goal still gets a fresh `TaskState` (invariant #1) and cross-task context is assembled explicitly by `ContextBuilder`, never by transcript bleed. 89/89 green.
- **2026-06-05** — Interaction layer formalized in `HARNESS_DESIGN.md` §23 (REPL/session wraps the unchanged task engine; reuses event emitter, permission hook, cancellation).
- **2026-06-05** — Build tracked here in `PROGRESS.md` (durable) rather than `ROADMAP.md`; in-session task lists are scratch, this file is source of truth. Progress measured as checklists.
- **2026-06-05** — Multi-agent "agent teams" not the primary build driver (sequential, TDD-gated, human-reviewed). Reserved for bounded fan-out sub-tasks: test brainstorming, design exploration, adversarial phase review.
- **2026-06-05** — TDD adopted: tests proposed and approved at each phase start before production code. Phase 0 tests approved.
- **2026-06-06** — `task_kind` merged from four values to three (`edit | investigate | test_only`): `explain` folded into `investigate` (identical verification contract). Pure command-execution also maps to `investigate` — no `execute` kind, since `task_kind` is a taxonomy of verification contracts, not user intents. Updated `HARNESS_DESIGN.md` §7/§12 and `state.py`. Noted an open edge in §12: non-executable edits (docs/config) have no command-based positive signal.
- **2026-06-06** — Added `ARCHITECTURE.md`: a visual, synthesized whole-system map (high-level graph + deep dives on task execution and verification + a dry-run walkthrough), with `[Implemented]`/`[Designed]` status tags. To be kept current with each architecture-altering change. `CLAUDE.md` updated with a documentation map and when-to-consult guidance (broad/global tasks yes; targeted edits no).
- **2026-06-06** — `task_kind` confirmed kept as-is (`edit | investigate | test_only`; no rename, no fold). Clarified: kind is classified at intake from the goal, then its verification contract is applied — verification is per-kind (working diff / passing new tests / cited diff-free answer), not a choice made from what the agent happened to do.
- **2026-06-06** — Documented two load-bearing verification clarifications (`ARCHITECTURE.md` §4.0, `HARNESS_DESIGN.md` §15): (a) the verifier runs **no LLM** — structural inspection is predicates over `TaskState`; (b) verification reads the **uncommitted** working tree vs a **pinned baseline** (HEAD-at-start), and the harness **never commits** — the diff is the deliverable. `state.files_modified` is the git-independent primary signal.
- **2026-06-07** — Dogfooding bug fix: the runner recorded only tool *summaries* into evidence, so the model never saw tool *content* (search hits, file text) and looped calling `search_repo` until the iteration budget. Now `_apply_tool_result` stores `result.content` as evidence detail and `ContextBuilder` surfaces it (truncated). Also enriched the event trajectory: `model_decision` (thought + action), tool `input`/`content`, `verification_end` (summary + next_action), `decision_error`. Console truncates long values; JSONL keeps full. Observability tooling (Langfuse/Helicone/Phoenix) still TBD.
- **2026-06-07** — Tabled the agent-performance-analysis / eval work until **after Phase 3 (interactive REPL)**; scheduled in Phase 4+. Landscape surveyed — tracing: Langfuse / Arize Phoenix / LangSmith / Braintrust / Helicone / OTel-GenAI; eval frameworks: Inspect (AISI) / promptfoo / DeepEval; coding benchmarks: SWE-bench Verified, Terminal-Bench, Aider polyglot. Key insight driving the plan: our **`Verifier` is already a deterministic scorer** and the **event log is already a trajectory dataset**, so the highest-leverage move is a small *internal* eval harness (pass@1 on a held task set, verifier-as-scorer) + one tracer — not a platform purchase. North-star metric: resolution rate on a held task set; secondary: iterations/tokens/cost per solved task + failure-mode distribution.
- **2026-06-08** — Review steps 3/4 dispositioned. **Step 4 (docs):** reconciled `ARCHITECTURE.md` §2 status note + §6 footprint to post-wiring reality (live dogfood done; mutation gated by `task_kind` not phase; `apply_patch` via `git apply --index`; runner mirrors `command_log` → `commands_run`; `main` reports via `ArtifactManager`). **Step 3 (hardening) deferred, with rationale:** (a) pytest exit-code narrowing (#6) would force several verifier/tool tests onto real `pytest` subprocesses (slower, env-dependent) to guard a hypothetical non-pytest runner — pytest is the only runner in play, so this is the "no abstraction until a second concrete case" Principle C warns against; the assumption stays documented until a real second runner appears. (b) Broad test-constant/helper dedup (#7) is high-churn/low-value against the project's clarity discipline (the LOC target was the one review item pushed back on); `run_echo` retirement left in place as it still provides event-spine smoke coverage and ripples into docs for marginal gain. Revisit both when they're actually earned.
- **2026-06-08** — Correctness fixes (review steps 2/4). (a) **Created-file diff bug:** `apply_patch` now applies with `git apply --index`, so a brand-new file is tracked and appears in `ws.diff()` — previously created files were left untracked and invisible to the secret scan and the artifact (a real hole: a secret in a *new* file would pass the gate). (b) **Investigate can't mutate (prevention, not detection):** `PermissionPolicy` blocks tier-1 `apply_patch` when `task_kind == "investigate"`, up front at the gate — the verifier's `no_unintended_diff` was only a post-hoc catch. 85/85 green. Note on the deferred half of review #1: an execute-time *phase* guard is intentionally NOT added, because `phase` isn't advanced yet — enforcing it now would block `apply_patch` (phases={editing}) in an edit task still sitting in `investigating`. The tier-1/investigate rule gives the real safety win (no mutation in a read-only task) without that coupling; full phase advancement stays Phase 3.
- **2026-06-08** — Product-path wiring (from an external review that flagged unit-green-but-unwired seams; step 1 of 4). Scripted unit tests had passed each component in isolation but never exercised the edit path as one CLI flow, so three integration gaps lurked: (a) `state.commands_run` was read by the artifact/verifier but **never written** — fixed by recording every `ws.run` in a new `Workspace.command_log` and having the runner mirror it into `commands_run` (the verifier runs commands but is pure, so the runner does the recording); (b) `ArtifactManager` was built/tested but **not wired into the CLI** — `main` now reports through it (one reporting contract: status + files + verification + commands + answer); (c) a dirty workspace dumped a **raw traceback** — `main` now catches `DirtyWorkspaceError` and prints a hint (exit 2). `run_agent`/`main` gained a `task_kind` param (default `investigate`) so edit tasks are drivable/testable; full NL goal→kind classification at intake is still deferred. Added an end-to-end edit-task test through `run_agent` + a `main`-renders-artifact test — the integration the unit tests missed. 82/82 green. Remaining review items (steps 2–4): diff must include created/untracked files (secret scan + artifact blind spot — a real bug), execute-time phase guard, pytest-exit-code narrowing, targeted test-helper dedup + `run_echo` retirement, doc reconciliation.
- **2026-06-08** — §15 clean-start refinement (found while dogfooding). The clean-start guard now ignores **untracked** files (`git status --porcelain --untracked-files=no`): only tracked modifications can pollute `ws.diff()` (working tree vs pinned HEAD), so only they should block a run — stray untracked files (logs, scratch, PR drafts) no longer trip `DirtyWorkspaceError`. Added a CLI `--allow-dirty` flag (threading the existing `Workspace(allow_dirty=)`) for the acknowledged-tracked-dirty case. 77/77 green.
- **2026-06-07** — Phase 2 complete (closing the loop). `apply_patch` applies a multi-file unified diff atomically via `git apply --check` then `git apply` (path-confined first, so an escape is refused before any write; a stale hunk raises `PatchError` and nothing is written). `PermissionPolicy` is a synchronous `before_tool_call` control gate (tier table: 0/2 allow, 1 allow iff patch paths resolve inside the root, 3+ block) consulted by the runner before every execution — never an emitter subscriber. The `Verifier` gained `edit`/`test_only` gates that **run the verification command themselves** (`config.test_command`/`lint_command` via `ws.run`), so the success signal is harness-owned, never the model's `run_tests`; the gate enforces all three §12 criteria (no required fail · no disallowed skip · ≥1 positive signal), with allowed-skip reasons whitelisted and an always-on secret/placeholder diff guard. `ArtifactManager` reports `status = state.outcome` verbatim. `Workspace.open(allow_dirty=False)` now asserts a clean git tree and pins HEAD, closing the dirty-repo gap. 73/73 green, `make check` clean. Two scope notes: phase transitions (investigating→editing→verifying) are not yet automated — tools are registered phase-gated but the runner doesn't advance `phase`, which only matters once a real model drives tool exposure (Phase 3); and run_tests maps pytest exit 4→model-correctable "target not found", exit 5→allowed "no test target", which is pytest-specific (revisit if the default command changes).
- **2026-06-07** — Phase 1 complete (logic + real-model/CLI integration). Known gap for Phase 2: `Workspace` does not yet assert a clean-or-acknowledged git state at task start (§15 `workspace.open`). The verifier's `no_unintended_diff` diffs vs. the pinned baseline, so on a *dirty* repo a read-only investigate would wrongly fail (pre-existing uncommitted changes count). Fine on a clean checkout; add the clean-start assertion in Phase 2.
- **2026-06-06** — Phase 1 will include a **minimal `investigate`-only verifier gate** (not deferred to Phase 2). It inspects `TaskState`/workspace only (positive evidence cited + no unintended diff), so it is self-contained and sidesteps the §4.3 command-source gap. The full `edit`/`test_only` verifier (which needs test/lint command resolution) stays Phase 2. This keeps "verifier-owned completion" true from the first loop.
