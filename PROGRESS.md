# PROGRESS — avatar-harness build ledger

**Authoritative, durable, git-tracked record of where the build is.** Read this first when resuming. `HARNESS_DESIGN.md` is *what* we're building and *why*; this file is *how far* we've gotten and *what's next*. Progress is tracked as checklists — a phase advances only when its boxes are ticked.

> **Current position:** Phase 2 ✅ complete (73/73 green; `make check` clean — lint + pyrefly + deptry + docstrings). The edit loop closes: `apply_patch` (atomic, path-confined) under the permission gate, the harness-owned `Verifier` runs its own command to set `outcome`, `ArtifactManager` reports it. Scripted-model smoke: read → patch → verifier runs command → success. **Live model dogfood confirmed 2026-06-08** (investigate task → read → grounded answer → verifier passed → `success`). **Phase 2.5 ✅ complete 2026-06-08** (110/110 green; `make check` clean) — sensitive-path denylist at the gate, `list_files` directory expansion, decision/action ledger, and less-lossy evidence compaction. **Phase 2.6 ✅ complete 2026-06-09** (139/139 green; `make check` clean; CI gate green; PR #6) — built by a **4-lane worktree-isolated agents team**: tool-failure isolation, real phase advance/enforce, honored budgets + cancellation, public `Harness` facade, neutral model boundary (`openai` now an optional extra), plus a kind-aware-prompt addendum (`task_kind` on the `ContextPacket`) and a lazy OpenAI client (a `Harness` is constructible with no API key). A PR-review fix then closed an advertised-vs-admitted-tools drift — the context now advertises exactly what the runner's phase gate admits (one shared predicate), so a live edit task actually discovers `apply_patch` instead of looping on reads. **Phase 3.0 foundation ✅ complete 2026-06-09** (158/158 green; `make check` clean; PR #7) — the foundation per ADR-0001/0002: a typed discriminated `HarnessEvent` union, the async `arun()` core (sync `run()` wraps it), and the two-plane `Session` (`events()` out · `resolve_approval()`/`cancel()` in). PR-review hardening folded in: **per-subscriber event fan-out** (independent observers, not one shared queue) and the **async/session surface exported + documented** (`Harness.arun()`/`session()`, `Session`, typed events in `__all__`). **Phase 3.1 in progress — sequential build** (the lane "team" payoff was judged modest here: `session.py`/`runner.py` are shared hotspots, so the splits are done as the work naturally needs them, not as scaffolding for parallelism): `run_command` ✅ (tier-3, approval-gated, PR #9), the prefix-scoped **`ApprovalGrant`** ✅ (`[a] always`, session-scoped, PR #10), **Lane 1 · engine internals** ✅ (bounded `EventBus` in `bus.py` + privileged write-ahead `JsonlEventJournal` in `journal.py`; PR #11), and **Lane 2a · multi-turn `SessionState`/`ReplSession`** ✅ (the session scope above `TaskState` — history, per-goal tasks, session-scoped grants, visible-mode routing; one code path, batch == degenerate session; 194/194 green). **Lane 2b · Textual cockpit shell** ✅ (PR #13), and **Lane 2c · modals** ✅ (`ApprovalModal`/`DiffModal`/`PlanModal`; the cockpit auto-pops the approval modal on an `ApprovalRequested` and routes the choice to `resolve_approval`; 208/208 green). **Lane 2 (cockpit) is complete**, and the **3.2 tail is underway**: `3.2a · meta commands` ✅, `3.2b · @path grounding` ✅ (`@file` seeds a denylist-checked file as `grounding` evidence), `3.2c · plan mode` ✅ (read-only plan → approve/revise → approved plan seeds the edit task as a constraint, riding the existing phase gate; `submit_plan` drives the flow, with review-hardened plan-run + revise-budget guards), and `3.2d · conversational-verification authority` ✅ (the verifier always runs + reports; a `conversational` runner flag delivers the reply without the §12 repair gate — the REPL default, `--auto` restores strict; 234/234 green). Two **review-driven test-hygiene cleanups** also merged (PRs #18/#19): the `ScriptedModel`/`CyclingModel` stubs consolidated into `conftest.py`, and the dead Phase-0 `run_echo`/`EchoResult` skeleton retired. and `3.2e · CLI launch` ✅ (the `--interactive` flag wires `ReplSession` ↔ `CockpitApp` end-to-end — multi-turn REPL, streamed per-goal runs, approval/plan/diff modals, meta commands, `Ctrl-C` cancel, `--auto` for the strict gate; 242/242 green). **The Phase 3 MVP cockpit is complete.** A post-MVP gap-fix (2026-06-09) closed the cockpit's missing flight recorder: the interactive path now journals its full typed event stream **write-ahead** to `events/<session_id>.jsonl` — one `JsonlEventJournal` per REPL sitting, threaded through `ReplSession` into every per-goal `Session` (shared by reference, like `grants`); `--log` honored; `Harness.session(journal=…)` for SDK parity (248/248 green; `make check` clean). Remaining in Phase 3: only **3.3 durable execution** (deferred past the MVP).

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

## Phase 2.6 — Pre-Phase-3 hardening (extensibility + enforcement)

**Implemented 2026-06-09** on branch `feat/phase-2.6-hardening` (PR #6, CI green) — 4 worktree-isolated agents (one per lane), each TDD red→green in isolation; merged clean (disjoint files). Two integration bugs the *combined* gate caught that no single lane could: (1) a Harness seam test's injected policy had to subclass `PermissionPolicy` (pyrefly); (2) the facade constructs the default `OpenAIModelClient` eagerly, whose `__init__` built `OpenAI(api_key=…)` — which needs credentials, so CI (no key, no `.env`) raised `OpenAIError` *before* the dirty-workspace check → fixed by **lazy client construction** (credentials are inference-time only). Plus a **kind-aware-prompt addendum**: `task_kind` now rides on `ContextPacket` so `build_messages` frames the mission per kind. Combined `make check` **137/137** green. All lane test lists below landed green.

The high/medium-impact items from a core-library assessment (cross-validated by Codex gpt-5.4/xhigh). Two motivations: (1) **enforcement** — turn declared-but-dead control axes into real ones (assessment thesis: *abstractions ahead of enforcement*); (2) these are **prerequisites** the ADR-0001 async/durable migration needs anyway (tool-failure isolation, real phase advancement, consumed budgets/cancellation). The facade + model boundary close the "extensible importable core" gaps.

**Parallelizable into 4 disjoint-file lanes** (clean for a worktree-isolated agents team). The enabling design choice: **phase enforcement lives in the runner** — consult `tool.phases` vs `state.phase` before `execute`, mirroring the permission-gate consult — so it stays out of `tools/base.py` and frees the tool-isolation lane. With that, the four lanes touch **disjoint files** and merge without conflict.

**Two design clarifications (2026-06-09):** (1) **Phase is capability-exposure, not security.** The security boundary for tool execution is the permission tier + path confinement/denylist + the `Workspace` chokepoint + the `task_kind` gate — none of which trust a tool's self-declared `phases` (or `permission_tier`). So phase-in-runner adds no security hole even for a lying tool. *Untrusted third-party* tools are a separate future concern (then `phases`/tier must be harness-assigned, not self-asserted, and `search_repo`'s direct-subprocess bypass of `Workspace` must close) — out of MVP scope (tools are first-party). (2) **Phase advances on first edit *intent*, not a `≥1 read` counter** — a `≥1 read` trigger deadlocks/forbids pure-creation tasks on a bare workspace (nothing to read). Inspect-before-edit is already guaranteed by the clean-apply invariant (`git apply --check`): modifying unseen content fails as stale (model-correctable), while a new-file hunk applies with zero reads. Delete the proxy; keep the load-bearing mechanism (Principle C).

| Lane | Files (disjoint) | Items |
| --- | --- | --- |
| **A — engine loop** | `runner.py` · `state.py` · `deps.py` · `test_runner.py` | phase advance + enforce + event · wall-clock/context budgets · cancellation |
| **B — tool runtime** | `tools/base.py` · `test_tools.py` | tool-failure isolation |
| **C — public API + facade** | `__init__.py` · `harness.py` (new) · `cli.py` · `test_harness.py` (new) | curated `__all__` · `Harness` facade · CLI delegates to it |
| **D — model boundary** | `model_client.py` · `pyproject.toml` · `config.py` · `test_model_client.py` | kind-aware default prompt · prompt behind the adapter · `openai` an optional extra |

**Lane A — engine loop** [landed green]
- [x] `test_phase_advances_to_editing_on_first_edit_intent` (advance on the model's first `apply_patch`; edit tools reachable on `edit`/`test_only` kinds — non-circular)
- [x] `test_pure_creation_from_bare_workspace_succeeds` (new-file hunk, **zero reads** — the creation case that kills a `≥1 read` trigger)
- [x] `test_modify_without_read_fails_stale_then_recovers` (inspect-before-edit **emerges from clean-apply** `git apply --check`; no read-counter needed)
- [x] `test_phase_changed_emitted_on_transition`
- [x] `test_out_of_phase_tool_call_is_model_correctable` (**workflow feedback, NOT a security control** — security = permission tier + Workspace chokepoint + `task_kind` gate)
- [x] `test_repair_falls_back_to_editing` (verifying → editing on failed verification)
- [x] `test_wall_clock_budget_yields_incomplete`
- [x] `test_context_budget_yields_incomplete`
- [x] `test_cancellation_observed_yields_incomplete`
- [x] `test_cancellation_records_feedback`

**Lane B — tool-failure isolation** [landed green]
- [x] `test_tool_handler_exception_becomes_failed_result` (a raising tool → `ToolResult(success=False)`; loop continues)
- [x] `test_runtime_never_raises_into_loop`
- [x] `test_system_failure_is_surfaced_not_retried` (system error distinct from model-correctable)

**Lane C — public API + Harness facade** [landed green]
- [x] `test_public_api_exports_stable_surface` (`from avatar_harness import Harness, TaskState, ToolDefinition, ToolResult, RunDeps, ModelClient, Workspace, HarnessConfig`)
- [x] `test_harness_from_env_runs_investigate_end_to_end`
- [x] `test_harness_overrides_each_seam` (inject model / tools / verifier / policy)
- [x] `test_cli_delegates_to_harness_facade` (CLI wires through the facade, not bespoke construction)

**Lane D — model boundary** [landed green]
- [x] `test_default_prompt_is_kind_aware` (now genuinely kind-aware via the addendum — edit vs investigate framing differs)
- [x] `test_core_imports_without_openai` (`import avatar_harness` works with `openai` absent — lazy/guarded provider import)
- [x] `test_custom_model_client_runs_end_to_end` (provider fully swappable; prompt contract behind the adapter)

**Addendum — kind-aware prompt + lazy client** [landed green]
- [x] `test_context_packet_carries_task_kind` (`ContextBuilder` threads `state.task_kind` onto the packet)
- [x] `test_default_prompt_is_kind_aware` strengthened (edit framing ≠ investigate framing; edit never re-locked to READ-ONLY)
- [x] `test_openai_client_constructs_without_credentials` (lazy `OpenAI` client — a `Harness` builds with no key; the CI regression fix)

**Exit criteria**
- [x] a third-party tool that raises can't crash a run (returns a failed `ToolResult`)
- [x] `state.phase` advances `investigating → editing → verifying`, emits `phase_changed`, and an out-of-phase tool call is refused at execution
- [x] wall-clock/context budgets and the cancellation token are honored by the loop (→ `incomplete`)
- [x] `from avatar_harness import Harness` runs a task in ≤3 lines; the CLI delegates to the same facade
- [x] the default model adapter is provider- and kind-neutral; `openai` is an optional extra
- [x] all lanes green · `make check` clean

## Phase 3 — Interactive cockpit (async engine · two-plane session · TUI)

Design locked in **ADR-0001** (async engine · typed event bus · durable execution) and **ADR-0002** (`docs/adr/0002-interactive-tui-cockpit-and-mvp-feature-set.md` — the MVP coding-agent feature set · **Textual full-screen cockpit** · constrained tier-3 **`run_command`** · **visible modes** over a hidden classifier · **plan mode**), the latter cross-validated with Codex (gpt-5.5/xhigh). Phase 3 is a **layered build**, not a 2.6-style disjoint-file fan-out: a sequential **foundation** (a stable contract the lanes fill in) → a small **contract-first lane team** → a sequential **tail**. Durable execution moves *past* the MVP (ADR-0002 defers crash-resume wiring).

### 3.0 Foundation — the stable contract ✅ complete 2026-06-09 (17 foundation tests; 158/158 suite green; `make check` clean; PR #7)

The single sequential spine the lanes fan out around: the typed event union, the async core, and the two-plane session API. The *interface* is stable (lanes don't edit the event-union types or the session API signatures); lanes fill in implementations behind it. Hardened on PR-review feedback: per-subscriber `events()` fan-out and an explicit, exported async/session SDK surface.

- [x] **Typed `HarnessEvent` union** (`event_types.py`) — closed, versioned, discriminated; `event_id` bus-assigned; `EventLog` round-trips typed events; `EventSink`/`ApprovalController` protocols. The `type` discriminator lives per concrete event (not the mutable base).
  - [x] `test_harness_event_union_round_trips` · `test_event_base_fields_present` · `test_unknown_event_type_is_rejected` · `test_model_update_channel_is_display` · `test_eventlog_writes_and_reloads_typed_events`
- [x] **Async core `arun()`** (`runner.py`) — the real loop; sync `run()` wraps it via `asyncio.run()`; sync model/tool/verifier bodies offloaded with `to_thread`; typed events published in order. (Removed the now-dead sync `_run_tool_call`/`_verify` twins.)
  - [x] `test_arun_drives_loop_to_terminal_outcome` · `test_run_wraps_arun_via_asyncio_run` · `test_sync_tool_body_does_not_block_loop` · `test_cancellation_observed_during_arun` · `test_arun_emits_typed_events_with_monotonic_ids`
- [x] **Two-plane `Session`** (`session.py`) — `events()` out (cannot block/redirect); `resolve_approval()`/`cancel()` in; an event *announces* an approval need, the control method *decides* it (§13). `EventBus` is the foundation's simple unbounded fan-out.
  - [x] `test_session_events_yields_typed_stream` · `test_two_event_consumers_each_see_full_stream` · `test_session_events_subscriber_cannot_alter_control` · `test_resolve_approval_unblocks_gated_call` · `test_cancel_records_feedback_and_stops` · `test_approval_announced_by_event_not_decided_by_it`

### 3.1 Lanes — contract-first team (pending; tests proposed before code)

- [x] **Lane 1 · Engine internals** — the foundation `EventBus` grew into its own `bus.py` with **bounded per-subscriber queues** (soft cap; only droppable `*_update` events are shed at the cap, lifecycle/control always enqueue and may exceed it; **drop-newest**, coalescing deferred), plus a privileged write-ahead `JsonlEventJournal` (`journal.py`): every published event is journaled **losslessly + flushed per event** *before* the (lossy) fan-out, so a slow/broken subscriber never blocks publish or peers and the journal stays complete even when a subscriber sheds (drops show as `event_id` gaps). Journal is **bus-internal** (sync append) so **zero `runner.py` edits**; the awaited journal + durable resume stay 3.3. `Session` gained an optional `journal=`; `EventBus`/`JsonlEventJournal` exported. One hardcoded drop policy (no `DropPolicy` enum until a 2nd consumer — rule of three). (10 tests; 186/186 green; `make check` clean.)
- **Lane 2 · Textual cockpit** — decomposed into three reviewable sub-increments (the cockpit is too big + dependency-heavy for one PR):
  - [x] **2a · multi-turn `SessionState` + `ReplSession`** — the session scope above `TaskState` (§23): conversation `history`, the sequence of per-goal `tasks`, session-scoped `grants`, current `mode`. `ReplSession` runs each goal as one fresh `TaskState` through the existing single-task `Session` (**one code path** — batch is the degenerate one-`submit` case), seeds the new task from prior history (in-session only; explicit, not transcript bleed), and carries grants across tasks (`Session` gained a shared-by-reference `grants=` seam). Visible heuristic `default_mode` → `task_kind` + explicit `set_mode` override (no hidden classifier; `/mode` wiring is the tail). Pure logic, zero new deps. `ReplSession`/`SessionState`/`Turn` exported. (8 tests; 194/194 green; `make check` clean.)
  - [x] **2b · Textual cockpit shell** — `CockpitApp` (`tui/app.py`): full-screen status bar (mode · phase · outcome) + scrollable transcript fed by a worker draining `session.events()` + input box (submit → injected callback). Pure observation subscriber + input source, never in the loop (§13). A `ReplaySession` (`tui/replay.py`) replays a fixed event list through the same `events()`/`resolve_approval`/`cancel` surface (no model/engine) — deterministic `Pilot`/`run_test()` tests + a future `--replay` viewer. Optional **`[textual]` extra** (+ dev group); `load_cockpit()` guards it with an install hint; `import avatar_harness` never pulls in textual. (6 cockpit tests + a core-import guard; 201/201 green; `make check` clean.)
  - [x] **2c · modals** (`tui/modals.py`) — three `ModalScreen`s returning typed results: **`ApprovalModal`** → `ApprovalChoice` (`[y]` once / `[a]` always-scoped→`remember=True` PR-#10 grant / `[d]` deny / `[v]` detail), which `CockpitApp` **auto-pops on an `ApprovalRequested` event** and routes to `session.resolve_approval` (the event announces, the modal decides, §13); **`DiffModal`** → read-only scrollable diff viewer; **`PlanModal`** → `PlanChoice` (editable plan, approve/revise — the plan *flow* is the tail). (7 `Pilot` tests; 208/208 green; `make check` clean.)
- **Lane 3 · `run_command`** — *Independent.*
  - [x] **The tool** — tier-3 over `Workspace.run` (argv, no shell metacharacters), **editing/verifying only** (ADR-0002 — keeps `investigating` read-only, avoids the command-ungrounded verifier dead-end); default-blocked in batch, approval-gated in the REPL; a ran-but-failed command is `success=True` evidence; timeout/empty-command are model-correctable/system failures. **Mutation capture (PR #9 review fix):** a command's created/changed files are attributed into `files_changed` and **staged** (`Workspace.status_paths`/`stage`) so codegen/migrations flow into the diff → artifact → verifier, not a blind subsystem. Registered in `default_registry`. (10 tests; 168/168 green.)
  - [x] **Prefix-scoped `ApprovalGrant`** — `[a] always` (`resolve_approval(remember=True)`) stores a **session-scoped** grant `(tool, program-prefix, tier)` so later calls sharing that program auto-allow without re-prompting; non-matching still prompt; never global (empty prefix matches nothing), never tier-4. The grant lives in **`Session` (control plane), not `PermissionPolicy`** — the harness gate still returns `ask` for every tier-3 call (invariant #4); the Session *answers* from a remembered grant. The runner is unchanged. Auto-allows stay observable via a new `ApprovalResolved.via="grant"` field and emit **no** `ApprovalRequested` (a granted call skips the human). Grant unit = the command's program (`argv[0]`, mirrors `Bash(pytest:*)`); `ApprovalGrant` exported on the public surface. (8 tests; 176/176 green; `make check` clean.)

### 3.2 Tail — sequential (decomposed)

- [x] **3.2a · meta commands** — `ReplSession.is_meta`/`run_meta` handle `/`-input locally (never reach the model, §23.2), returning a typed `MetaResult(kind, text)` the cockpit renders/routes: `/help`, `/quit` (`kind=quit`), `/state` (session summary), `/mode <kind>` (validated; sets the visible-mode override), `/diff` (`kind=diff` carrying `workspace.diff()`, opened `allow_dirty` for a read-only inspection), `/permissions` (lists session grants); unknown → reported, never run. (8 tests; 216/216 green; `make check` clean.)
- [x] **3.2b · `@path` grounding** — a goal mentioning `@path/to/file` seeds that file as initial `kind="grounding"` evidence on the fresh `TaskState` (explicit context, like history seeding). Read **through the `Workspace`** (`allow_dirty`, read-only), so the sensitive-path denylist + confinement apply at the same chokepoint as every read: a refused (`.env`), missing, or out-of-root path is a short note — never a crash, never a leaked secret. Content capped per file. (5 tests; 221/221 green; `make check` clean.)
- [x] **3.2c · plan mode** — a read-only **plan task** (`task_kind="investigate"` + a planning-directive constraint, so mutation stays blocked at the existing phase + `task_kind` gate) proposes a plan; the human **approves or revises**; the approved plan **seeds the edit task as a constraint** (`model_client` surfaces it), which then rides the `investigating→editing` gate. `submit_plan(prompt, decide)` drives the flow — **revise re-runs the plan task** so the model refines it (ADR-0002 D5 mermaid); `start()` in `plan` mode returns the read-only plan session for the cockpit. Plan is a session **mode**, not a `task_kind` (no new control plane); the decision rides a Textual-free `PlanDecision(approved, text)` so the core stays import-light; `/plan` + `/mode plan` enter it. **PR-#17 review hardening:** an empty / `blocked` / `incomplete` plan run is never offered for approval (its terminal planning state is surfaced — you can't approve `""`; a non-empty verifier-rejected plan still goes to the human), the revise loop carries a budget (`_MAX_PLAN_REVISIONS` → `incomplete`), `submit()` in plan mode raises (interactive-only), and the README cockpit-status blurb was reconciled. (10 tests; 231/231 green; `make check` clean.)
- [x] **3.2d · conversational-verification authority** (§23.5, ADR-0002 D7) — the verifier **always runs and always reports** (events + `verifier_results`); *who decides* shifts with who's in the loop. A `conversational: bool` flag on `AgentRunner` (default `False` = the strict §12 gate, unchanged → all prior tests green) branches `_averify`: conversational runs the verifier as **advisory** and delivers the `FinalAnswer` immediately (`outcome="success"`, **no repair loop**), with the real verdict left on `verifier_results[-1]` for the cockpit to render. `Harness._build_runner`/`session()` thread it; `ReplSession(harness, *, auto=False)` defaults conversational (builds runners with `conversational=not auto`), and `--auto` (wired by the CLI in 3.2e) restores strict. The human is terminal authority; the engine never pronounces failure in chat mode. (5 tests; 234/234 green; `make check` clean.)
- [x] **3.2e · CLI launch** — `CockpitApp` gained a `repl=` driving seam (the existing `session=`/`ReplaySession` path stays for replay tests + a future `--replay` viewer): input routes through `ReplSession` — meta handled locally (`/quit`→exit, `/diff`→`DiffModal`, `/mode`→status), goals run as **observable per-goal `Session`s** (`_observe` runs the session while draining its `events()` into the transcript), approvals route to the live session via the auto-popped `ApprovalModal`, and **plan mode** streams the read-only plan → `PlanModal` → (approve) build (revise re-runs the plan). `Ctrl-C` cancels the in-flight run (refeeds as history) else quits. `main(--interactive)` builds `Harness`+`ReplSession`+`CockpitApp(repl=...)` via the `[textual]`-guarded `load_cockpit()`; `--auto` → `ReplSession(auto=True)` (3.2d strict gate). `session_state` grew the shared plan-flow seam the cockpit reuses (`start_plan`, public `extract_plan`/`plan_is_approvable`, `record_goal`; `submit_plan` refactored onto them). (8 Pilot/CLI tests; 242/242 green; `make check` clean.)

### 3.3 Durable execution — deferred past the MVP

- [ ] Per-turn checkpoint + write-ahead intent; `resume()` with **semantics-aware** replay (reuse logged reads, never re-apply a patch — validate the diff hash, resume into a pending approval). *Deferred past this line:* MCP, middleware, graph topology.

**Exit (MVP cockpit) ✅ met (3.2e):** multi-turn REPL streams model+tool activity by phase, prompts approval before `apply_patch`/`run_command`, cancels in-flight work and refeeds, plan→approve→build, `/diff` runs model-free, `--auto` shares the code path.

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

Moved to [`DECISIONS.md`](DECISIONS.md) — the chronological record of *why* the build is shaped as it is. **Record major design decisions there, not here.** This file stays focused on *how far* the build has gotten (checklists).
