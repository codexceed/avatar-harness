# Branch reconciliation: `feat/r5-streaming-idle-timeout` vs `main`

**Date:** 2026-06-21
**Author:** analysis for reconciling two diverged feature lines
**Status:** plan — no code changes made

## TL;DR

The two lines are **9/4** apart and collide hard on the model client. The naive read
("main has the async client, this branch doesn't") is **wrong**. This branch is the
*superset* for the entire core (model client, runner, config, cancellation token);
`main` leads only on the **cockpit** (`jo-cli/jo/app.py`). The real merge work is one
file plus an ADR-number collision. Recommended path: merge `main` into this branch,
resolve the core **branch-first** (`model_client.py`/`deps.py` are a clean superset;
`runner.py`/`config.py` need a *manual* semantic read, not a mechanical `ours`),
hand-port the cockpit, renumber main's ADR-0024 → 0030.

> **Resolution rule, refined (codex review, 2026-06-21):** "take-ours core" is too
> blunt for two files. `runner.py` is exactly where the two cancellation models overlap
> (main's `await adecide` + `CancelledError` unwind vs. this branch's `asyncio.wait`
> race) — resolve by reading, confirm no main #80/#81 runner change is dropped.
> `config.py` is a behavior **migration**, not a merge choice (see §config). And the
> cockpit signal handler should **add** this branch's token path *alongside* main's
> `_run_task.cancel()`, not replace it (see §cockpit).

## What's actually on each line

Measured from the working repo (`git log main..<branch>` / `<branch>..main`):

### `main` — 4 commits ahead of this branch
The ADR-0024 *"interruptible runs via async model client"* work:

- `6220657` docs(adr): ADR-0024 — interruptible runs via an async model client (#78)
- `843895f` feat(model): cancellable async model calls via `adecide` (#80)
- `ae7014e` feat(cockpit): prompt history, selection-aware ctrl+c, quit-after-failure (#76)
- `a4d5388` feat(cockpit): instant ctrl+c via run-task race + signal handlers (#81)

### `feat/r5-streaming-idle-timeout` — 9 commits ahead of main
The whole evals stack **plus** ADR-0027/0028/0029 (the R5 commit landed in three:
the feat, a dedup/comment-trim refactor, and an observability follow-up):

- `bd781bd` feat(evals): evals-driven improvement loop design + search_repo guardrail (ADR-0024)
- `943b335` feat(evals): Layer-1 read-only foundation — distill, triage, ChangeProposal (ADR-0024 inc 1)
- `4e4e99a` feat(evals): Workflow A — cluster spine + evals-to-proposals orchestration (ADR-0024 inc 2)
- `6b25e04` feat(evals): persist journal-refined failure_mode on ResultRow (ADR-0025) (#83)
- `502afde` feat(evals): bounded concurrency in the eval runner (ADR-0026) (#84)
- `e21fa70` docs(adr): ADR-0029 — streaming idle-timeout for model calls (R5)
- `1e8538e` feat(model): R5 async streaming model calls with idle-timeout, mid-call cancellation, session-scoped fallback (ADR-0029)
- `3a8748a` refactor(model): dedup parse-retry loop + trim verbose comments (R5 cleanup)
- `ea7005d` feat(model): journal streaming activity + capability fallback (R5 observability)

ADR files present on this branch: `0024`(evals), `0025`, `0026`, `0027`, `0028`, `0029`.
ADR files on main: `0024`(async-client) only.

### Correction to the initial framing

This branch is **not** "ADR-0028 but no adecide." It has `adecide` **and**
`_atransport_retry` **and** the streaming path — the R5 commit (`1e8538e`) built its own
full async model client. The runner on *both* lines `await`s
`self.model_client.adecide(context)`. Both lines **independently asyncified
`model_client.py`** — main with a 431-line rework, this branch with a 504-line rework —
with *different* internal structure.

## The collision surface (files touched on both lines)

`avatar-harness/avatar/model_client.py`, `config.py`, `runner.py`, `deps.py`,
`tests/test_arun.py`, `tests/test_model_client.py`, `tests/test_cockpit*.py`,
`jo-cli/jo/app.py`, plus the ADR docs.

A plain `git merge` will conflict textually **and** — worse — produce a semantically
wrong result (two overlapping async implementations stitched together). This needs
deliberate, per-file resolution.

### `model_client.py` — structural diff

**Common to both:** `ToolCall`, `FinalAnswer`, `AskUser`, `DecisionRetryNote`,
`DecisionUsage`, `ModelDecision`, `_excerpt`, `_carries_patch`, `DecisionParseError`,
`parse_decision`, `ModelClient(ABC)` with `decide`/`adecide`, `_format_tools`,
`build_messages`, `build_tool_schemas`, `_decision_from_tool_call`,
`_assistant_call_message`, `_UsageTally`, `OpenAIModelClient`.

**Only on `main`:** `_ensure_async_client`, `_timeout_kwargs`, `_native_request`,
`_step_native`, `_raise_no_decision`, `_decide_native`, `_adecide_native`,
`_json_request`, `_step_json`, `_decide_json`, `_adecide_json`.

**Only on this branch (superset):** `TransportError`, `EmptyResponseError`,
`StreamingUnsupportedError`, `_is_empty_body`, `_fold_stream_chunk`, `_backoff`,
`_transport_retry`, `_atransport_retry`, `_acreate`, `_acreate_stream`,
`_raise_stream_fault`, `_areassemble`, `_native_decision_from_message`,
`_json_decision_from_message`, `_fetch_sync`, `_afetch`, `_afetch_stream`,
`_parse_retry`, `_aparse_retry`, `_adecide_native_stream`, `_adecide_native_async`,
`_adecide_json_async`, `_aensure_client`, `_UsageTally.add_usage`.

This branch refactored the request/step structure into `_fetch` / `_decision_from_message`
/ `_parse_retry` families (sync + async + stream variants). main's `_step_*` / `_*_request`
helpers are subsumed by these. **Resolution: take ours**, then read to confirm no main-only
behavior is lost (esp. `_timeout_kwargs` and the native-vs-json split).

### `config.py` — keys

- **main:** `request_timeout: float | None = None` (None ⇒ SDK default = effectively no timeout).
- **this branch:** `request_timeout_seconds: float = Field(240.0, gt=0)`,
  `request_idle_timeout_seconds: float = Field(30.0, gt=0)`, `stream_model_calls: bool = True`,
  plus retry/backoff keys.

**Resolution: branch-first — but this is a *migration*, not a harmless merge choice.**
Three things to check, not just "take ours":
- **Default semantics flipped:** main's "no timeout" became a **240s hard ceiling on the
  non-streaming path** (ADR-0028 R1). Calibrated above legit generations *as a per-read
  bound* — but on the non-streaming fallback it is a total ceiling, and we have already
  observed a legitimate **358s** call. `max_wall_clock_seconds` (600, turn-top) remains the
  true upper bound; the streaming path's 30s idle timeout is what actually distinguishes
  slow-from-stalled. Note this caveat in the ADR/README.
- **Field rename is breaking:** `request_timeout` → `request_timeout_seconds`. Verified no
  *code* consumer of the old name survives on the branch (it appears only in this doc). But
  `config.py` uses `env_prefix="AVATAR_"` + `extra="ignore"`, so a stale
  **`AVATAR_REQUEST_TIMEOUT`** in a user's `.env` is **silently ignored** (no error) — they
  lose their override without noticing. Low blast radius for a solo/early project, but worth
  a one-line note.

### `runner.py`

Both poll `self.deps.cancellation.cancelled` at the loop checkpoint. The difference is the
in-flight handling:

- **main:** `await self.model_client.adecide(context)`; a cancel propagates as
  `CancelledError` (a `BaseException`, so the narrow `except` never swallows it) and unwinds
  the loop, aborting the request at the socket (ADR-0024).
- **this branch:** races `asyncio.wait({adecide_task, deps.cancellation.event().wait()})`,
  cancels the task on a cancel event (mid-stream abort), and has a dedicated `TransportError`
  arm that bills the lost attempt, emits `transport_error`, and re-raises as a **system
  failure** (never fed back to the model — §16, ADR-0028 R4).

**Resolution: branch-first, but read — NOT a mechanical `ours`.** This is the one core
file where the two cancellation models genuinely overlap, so a blind `ours` could silently
drop a main #80/#81 runner change. The branch's race **is** the superset (it subsumes
main's plain unwind: `task.result()` re-raises `CancelledError`, a `BaseException`, which
the narrow `except TransportError/DecisionParseError` arms can't swallow — so an external
`Task.cancel()` still unwinds `arun` exactly as on main). Confirm by reading the conflict,
not by resolving it sight-unseen.

### `deps.py` — `CancellationToken`

- **main:** poll-only — `cancelled: bool` + `cancel()`.
- **this branch:** superset — adds a lazy, loop-bound `event()` (`asyncio.Event` mirror) so
  the runner can *race* a cancel against an in-flight model call (ADR-0029 R5).

**Resolution: take ours.**

## The one piece of genuine merge work: the cockpit

This is where `main` is **ahead** and the branch must absorb it.

`jo-cli/jo/app.py` diff (`main` vs this branch): **+164 / −11**. main has, and this branch
**lacks entirely**:

1. **Process-level signal handlers** — `signal` import; `loop.add_signal_handler(SIGINT/SIGTERM,
   _on_terminate_signal)` installed in `on_mount`, removed on unmount (#81). Textual's
   full-screen driver doesn't claim SIGINT/SIGTERM; in the TUI a ctrl+c arrives as a *key*
   (→ `action_cancel`), so these handlers cover *external* signals and headless runs.
2. **`_run_task` tracking** — `self._run_task: asyncio.Task[TaskState] | None`, the handle to
   the in-flight per-goal run.
3. **Prompt history** (#76) — `up`/`down` bindings, `_history`, `_cursor`,
   `action_history_prev/next`.
4. **selection-aware ctrl+c, quit-after-failure** (#76).

This branch's cockpit has only the in-TUI key action: `action_cancel` →
`self.run_worker(session.cancel("cancelled by user"))`.

### The crux: two cancellation routings

- **main:** signal handler → `_run_task.cancel()` → `CancelledError` propagates through `arun`.
- **this branch:** key action → `session.cancel()` → trips `deps.cancellation` (event), which
  the runner *races*.

The runner on this branch aborts via the cancellation **event/token**, not via
`Task.cancel()`. So porting main's signal handler verbatim would call the wrong mechanism.

### Port plan for `app.py`

1. Take main's `app.py` as the base (it has the signal handlers + history + quit-after-failure
   this branch lacks).
2. **`_on_terminate_signal`: ADD the token path, do NOT replace `_run_task.cancel()`**
   (codex review). The in-app *key* cancel routes through `session.cancel()` /
   `deps.cancellation.cancel()` — cooperative, so the runner observes the event at the
   `asyncio.wait` race and stops with clean `_stop_incomplete` bookkeeping. But an **external
   `SIGINT`/`SIGTERM`** must *also* hard-cancel the in-flight task: trip the token **and**
   `_run_task.cancel()`. They compose — `CancelledError` (a `BaseException`) unwinds `arun`
   past the narrow `except` arms. Hard-cancel alone risks losing the bookkeeping; cooperative
   alone risks the app exiting before the runner observes the token, orphaning the in-flight
   model-call task / a dirty shutdown. **Failure mode to watch:** signal → `session.cancel()`
   → app exits before the runner wakes on the event → pending `adecide` task / unclean teardown.
3. Reconcile `action_cancel`: keep main's selection-aware variant, but ensure it triggers the
   same `session.cancel()` path.
4. **Retain main's `_run_task` tracking** — needed both as the await handle *and* as the
   hard-cancel handle for the signal path (per step 2). The normal in-app abort signal comes
   from the token; the process-signal abort uses the task handle.

## ADR-number collision

Both lines define a **different decision under number 0024**:

- `main`: `0024-interruptible-runs-via-async-model-client.md`
- this branch: `0024-evals-driven-improvement-loop.md`

**Resolution (decided): keep the evals `0024→0029` chain intact** (it is internally
cross-referenced and larger), and **renumber main's standalone ADR → `0030`**:
`0030-interruptible-runs-via-async-model-client.md`. Add a note that its async client is
**extended / partly superseded by 0028 (transport-retry) and 0029 (streaming)**, since this
branch's client is the more advanced implementation. Update `docs/adr/README.md` and any
`(ADR-0024)` code comments inherited from main's line.

## Execution order

1. `git merge main` into the branch (do **not** rebase — rebase replays the same conflict
   across all 7 commits).
2. Resolve the core **branch-first**: `model_client.py` / `deps.py` are a clean superset
   (`ours`, then spot-read). `runner.py` and `config.py` get a **manual** read of the
   conflict — runner because the two cancellation models overlap there, config because the
   field rename + default flip is a migration. Confirm no main-only logic is silently dropped.
3. Hand-port `app.py` per the port plan; port main's cockpit tests (`test_cockpit.py`,
   `test_cockpit_repl.py`) and union the model/arun tests (`test_arun.py`,
   `test_model_client.py`).
4. Renumber main's ADR-0024 → 0030; update `docs/adr/README.md`.
5. `make check` — expect the cockpit tests to be where breakage surfaces.

## Open questions (resolve at the start of execution)

1. **Cockpit architecture parity.** Are main's `_run_task`/run-task-race cockpit and this
   branch's `session`-based cockpit the *same* class refactored, or genuinely divergent?
   This determines whether the `app.py` port is a clean graft (step 3) or a partial rewrite.
   De-risk this first.
2. **Default timeout change.** Confirm `request_timeout` (None ⇒ no timeout) →
   `request_timeout_seconds` (240s) is acceptable for any current `main` consumer.

## Risk summary

- **Highest risk:** `jo-cli/jo/app.py` — the only file requiring real semantic merge, hinging
  on open question #1.
- **Low risk:** `model_client.py` / `deps.py` — branch-first superset; risk is only an
  overlooked main-only helper.
- **Medium risk:** `runner.py` (cancellation models overlap — read, don't blind-`ours`) and
  `config.py` (field rename + default flip is a migration: silent `AVATAR_REQUEST_TIMEOUT`
  drop, 240s ceiling vs. observed 358s legit call).
- **Mechanical:** ADR renumber + README + comment updates.
