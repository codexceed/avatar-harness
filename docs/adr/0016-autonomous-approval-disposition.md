# ADR 0016 — Autonomous approval disposition: unattended runs deny `ask`s by default

- **Status:** Accepted — implemented 2026-06-15
- **Date:** 2026-06-15
- **Deciders:** Sarthak Joshi
- **Related:** ADR-0001 (two-plane session, approval as an awaited control hook); ADR-0002 (interactive cockpit, tier-3 `run_command`); `HARNESS_DESIGN.md` §11 (permission tiers; "a `--trusted`/autonomous mode may promote `ask → auto`"), §13 (control vs. observation). Surfaced by the first live Eval-0 baseline run.

## Context

The permission gate (§11) returns a *control decision* — `blocked` and, for a tier-3 action or a denylisted path, `ask` — and the runner consults an `ApprovalController` to dispose of it (invariant #4: the gate decides nothing about who is present; the Session decides the `ask`). The only controller was the interactive `Session`, whose `request_approval` emits `ApprovalRequested` and then **blocks on `await future` until a human calls `resolve_approval`**.

This is correct for a REPL and wrong for everything else. The first live Eval-0 baseline (frontier-trio × 5 seeds) hung for **51 minutes** on `secret-safety seed0`: the agent attempted a denylisted `read_file('credentials')`, the gate refused it with `blocked=True, ask=True` (the denylist carries `ask` so a human *could* override it in the cockpit), and the batch run — which installs the `Session` as its own controller via `session.run()` — blocked awaiting a `resolve_approval` that no human would ever send. Two aggravating facts:

- The per-task **wall-clock budget did not save it.** Budgets are checked at loop turn boundaries; a run blocked *inside* the awaited approval never returns to that checkpoint. An awaited gate is invisible to the loop's bounds.
- Only `secret-safety` triggered it (the only task that reaches an `ask`); the other 15 runs completed in ~3 minutes, so the matrix silently stalled at run 16.

The general gap: **an unattended run has no disposition for an `ask`.** Any tier-3/denylist gate in batch deadlocks.

## Decision

An **unattended `Session` denies an `ask` immediately** instead of awaiting a human — deny-by-default, the conservative posture §11 anticipated. Concretely:

- `Session(..., unattended=True)` makes `request_approval` announce the gated call (`ApprovalRequested`, for observability) and then **auto-deny** it, recording `ApprovalResolved(allowed=False, via="auto")` — a new `via` value distinct from `"human"`/`"grant"`. No future is created; the loop continues, treating the deny as a model-correctable refusal (the `secret-safety` agent gets "`credentials` refused" and proceeds *not* to leak — exactly what the task tests).
- An **approval-timeout backstop** (`AVATAR_APPROVAL_TIMEOUT_SECONDS`, `Session(approval_timeout=…)`) denies a *blocking* (attended) approval that no human answers in time, so even a present-but-silent controller can't hang a run forever. Default unset (a human at a REPL is not rushed); an unattended run never blocks regardless.
- The control plane keeps the decision (invariant #4): the gate still returns `ask`; the *Session* answers it — now from a deny disposition instead of only a human.
- Wired into `evals/run.py` (`client.session(..., unattended=True)`); the interactive cockpit is unchanged (`unattended=False` default).

## Consequences / alternatives

- The 60-run baseline (and any batch/autonomous run) can no longer deadlock on a gated call; an auto-deny is journaled and visible to the Eval-0 failure classifier.
- The denylist stays *prevention*: a denylisted read is refused in every mode; only the cockpit ever offers a human the override.
- **Scope cut (rule of three):** the disposition is deny-only. No `--trusted` / `ask → auto-allow` mode yet — it earns its own decision when a real unattended-write use case appears.
- *Rejected — make the denylist a hard block (`ask=False`).* It fixes `secret-safety` but not the class: a batch tier-3 `run_command` would still deadlock. It also forecloses the cockpit's human-override of a denylisted read, a deliberate ADR-0002 affordance.
- *Rejected — rely on the wall-clock budget.* It cannot preempt an awaited approval (the run never reaches the turn-boundary check); the timeout backstop exists precisely because the budget can't see inside the gate.
- *Rejected — a separate `AutonomousApprovalController` class.* The Session already owns approval state (pending map, grants); a mode flag reuses the announce/record path with less surface than a parallel controller. Revisit if a second autonomous policy (e.g. trusted-allow) appears.
