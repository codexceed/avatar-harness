# ADR 0026 — Bounded concurrency in the eval runner (thread pool over hermetic cells, opt-in)

- **Status:** Accepted — implemented 2026-06-19
- **Date:** 2026-06-19
- **Deciders:** Sarthak Joshi
- **Related:** ADR-0004 (internal eval harness — the runner this parallelizes); ADR-0024 (evals-driven improvement loop — principle 7, *cost is intentional*: a full matrix ≈ 2.5M tokens, so wall-clock, not money, is what concurrency buys back). Touches `evals/run.py` only — not a grader-touching change (the scoring surface is untouched).

## Context

The Eval-0 runner (`evals/run.py::main`) drove the `model × spec × seed` matrix with three nested `for` loops, one `run_task` call at a time. Each cell is wholly I/O-bound — it is dominated by model API round-trips and subprocess probe/tool execution — so a sequential matrix leaves nearly all wall-clock idle on the network. A 3-model × N-task × 3-seed matrix runs as the *sum* of every cell's latency when it could run as the latency of the slowest few.

`run_task` is already the right unit to parallelize, and the harness's invariants make it safe to: each cell is **hermetic** — `provision` creates a uniquely-labelled scratch repo per `(model, spec, seed)` (so no two cells share a workspace), the per-cell `HarnessConfig` is a fresh `model_copy`, and `run_task` drives its own `asyncio.run(session.run())`. There is no shared mutable state across cells and no shared event loop. The blocker to naive parallelism is only the outer driver, not the cell.

Two shapes were available:

- **Async refactor** — make `run_task` a coroutine and `asyncio.gather` the matrix under a `Semaphore`. This would touch the TDD'd unit (`run_task` is called synchronously with a `ScriptedModel` across the test suite) and force the harness session's loop ownership to be reconsidered. High blast-radius for the tested core.
- **Thread pool over the existing sync unit** — leave `run_task` exactly as-is (each call spins its own event loop via `asyncio.run`, which is legal and isolated in a worker thread) and bound the outer fan-out with a `ThreadPoolExecutor`. The GIL is released during the network and subprocess waits that *are* the cost, so threads give real overlap for this workload.

## Decision

**Add bounded, opt-in concurrency to the matrix driver via a `ThreadPoolExecutor`, leaving `run_task` untouched. Default `--concurrency=1` (strictly sequential — zero behaviour change); the pool spans the whole `model × spec × seed` matrix; results are always reassembled in matrix order.**

1. **A new `_run_matrix` helper** (`evals/run.py`) owns the fan-out: it builds the indexed list of cells, submits each to a `ThreadPoolExecutor(max_workers=max(1, concurrency))`, and reassembles rows **by submission index** so the persisted `results/<stamp>.jsonl` is deterministic regardless of completion order. Only the live progress lines arrive as cells finish (each line is self-labelled `model task seed -> …`, so out-of-order is legible). `main` calls it instead of the inline nested loop.
2. **`_run_one`** wraps `run_task` to catch a *provision-stage* failure (which propagates out of `run_task`) into an error row, so one bad cell never sinks the matrix — preserving the prior inline guard, now per-worker.
3. **`--concurrency N` flag**, default `1`. The default is sequential so existing runs are byte-for-byte unchanged; raising it is a deliberate act. Exposed as a `CONCURRENCY=` make passthrough.
4. **Whole-matrix pool, not per-model.** A single bounded pool over all cells maximizes overlap and is simplest; the operator caps total in-flight load with one number rather than reasoning about per-model sub-pools.

## Consequences

- **Wall-clock scales with the cap, correctness does not change.** At `--concurrency=N` up to `N` cells run at once; the results artifact and the per-model metrics are identical to a sequential run because rows are ordered by index and metrics group by `(model, task)`, not by row position.
- **Safety rests on the hermetic-cell invariant.** Parallelism is correct *because* each cell owns its scratch repo, config copy, and event loop. A future change that introduces cross-cell shared mutable state (a shared workspace, a process-global) would silently break this — the invariant is load-bearing and must be preserved.
- **Opt-in default is conservative by design.** `1` keeps the safe path the default; the operator raises concurrency only when they accept the trade-off below. No surprise rate-limit storms on an unsuspecting run.
- **Trade-off: provider rate limits.** Higher concurrency raises the chance of provider 429s on large matrices — "controlled" is the whole point of the bound. The default sidesteps it; a too-high cap re-introduces it. This is the operator's dial, not the runner's problem to solve (no built-in backoff was added — deferred until a *measured* need, consistent with the project's build-on-friction posture).
- **The tested core is untouched.** `run_task` keeps its signature and sync contract, so the `ScriptedModel`-driven suite is unaffected. `_run_matrix` is itself TDD'd offline (patching `run_task`) for matrix-order determinism, the concurrency cap (peak in-flight `== concurrency`), and provision-failure containment — no network spend.
- **Cost.** None to add the capability; the eval *spend* is unchanged (same number of cells, same tokens). Concurrency trades wall-clock for rate-limit headroom, not dollars (ADR-0024 principle 7).
