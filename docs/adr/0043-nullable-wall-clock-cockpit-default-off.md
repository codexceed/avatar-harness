# ADR 0043 — Nullable per-run wall-clock; the attended cockpit defaults it off

- **Status:** Accepted
- **Date:** 2026-07-09
- **Deciders:** Sarthak Joshi
- **Related:** [ADR-0030](0030-interruptible-runs.md) (interruptible runs — the Ctrl-C hard-cancel this leans on); §5 budgets → `incomplete` (never `failed`). Seams: `avatar/config.py` (`max_wall_clock_seconds`), `avatar/runner.py` (deadline math), `jo-cli/jo/cli.py` (the cockpit default).

## Context

The per-run wall-clock budget (`max_wall_clock_seconds`, 600s) exists to bound *unattended* runaways: a batch or eval run with no human watching must terminate on its own. In the attended cockpit that guillotine misfired — a dogfood build had two agent runs terminated as `incomplete` mid-work by the 600s clock, with the time confirmed to be ~600s of genuine agent work (not human-approval latency, which the deadline already credits back). The operator was sitting right there, able to Ctrl-C at any moment; the clock added nothing but a mid-build decapitation.

## Decision

1. **`max_wall_clock_seconds` becomes `int | None`** — `None` means *no wall-clock bound*. The runner's deadline math threads the `None` through (`deadline`, `_effective_deadline`, `_within_budget`, and the model-call timeout all treat `None` as unbounded).
2. **The attended cockpit (`jo`) defaults it to `None`.** The human's Ctrl-C (ADR-0030) and `max_iterations` are the backstops there. Batch/eval paths keep a finite default — unattended runs still self-terminate. (600s at decision time; `main`'s PR #104 raised it to 1800s for slow reasoning models, folded in at the merge.)
3. **An explicitly configured cap always wins over the cockpit default.** The guard keys on `config.model_fields_set` — pydantic marks env-var *and* `.env`-sourced values alike, and defaults not at all — so any operator-stated cap survives. (The first cut keyed on `os.environ` and silently nulled a `.env`-sourced cap; caught in the PR-#106 review.)

The clock stays **per-agent-run, not cumulative** across a sitting — this ADR changes who opts out, not what it measures.

## Alternatives considered

- **Raise the cockpit default (e.g. 3600s) instead of `None`.** Rejected: any finite value re-creates the same mid-build kill on a longer task; the attended path has a strictly better bound already (the human).
- **Pause the clock while awaiting approvals only.** Already done (`_approval_wait_seconds` credit) — and it wasn't the failure mode; the dogfood terminations were genuine work time.
- **Make `0` mean "unbounded".** Rejected: a sentinel integer is easy to set by accident and ambiguous to read; `None` is the honest type for "no bound", and pydantic already distinguishes set-from-default.

## Consequences

- An attended cockpit run can now run indefinitely; the operator is the bound. `max_iterations` still terminates a model-side loop.
- Unattended consumers (batch CLI, eval driver) are unaffected: the finite default (1800s post-PR #104) unless configured otherwise.
- Any consumer that *wants* a cap in the cockpit states it (`AVATAR_MAX_WALL_CLOCK_SECONDS`, env var or `.env`) and it is honored.
- Two budget axes remain deliberately separate (§5): general budgets (incl. this clock, when set) → `incomplete`; the repair budget → `failed`.
