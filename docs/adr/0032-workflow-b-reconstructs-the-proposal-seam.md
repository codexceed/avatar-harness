# ADR-0032 · Workflow B reconstructs the `ChangeProposal` seam from the funded digest entry

Status: Accepted — implemented 2026-06-28 (ADR-0024 Increment 3). Resolves the deferral in
**ADR-0031** and completes the A→B seam of **ADR-0024**; the rest of both stands.

## Context

ADR-0024 specified a typed **`ChangeProposal`** (`evals/proposal.py`) as the seam between Workflow A
(produces proposals) and Workflow B (builds the PR), with A emitting one structured artifact per
proposal. ADR-0031 then found that the structured per-`<id>.md` artifact was a poor *human* control
surface and changed Workflow A's output to a single human-readable **digest**
(`evals/proposals/<stamp>/proposals.md`), explicitly **deferring** the structured emission "until
Workflow B is built."

Building Workflow B (Increment 3) forces the question the deferral left open: B needs machine fields
the digest deliberately omits — `target_tasks`, `affected_models` (the canary scope), `blast_radius`
and `touches_grader` (the governance route), and a `tdd_plan`. Where do they come from?

Two options:
1. **Re-introduce structured emission in Workflow A** — A writes both the digest *and* a
   `ChangeProposal` JSONL per entry. This re-adds the artifact ADR-0031 removed and couples A's
   read-only output to B's schema, for entries a human may never fund.
2. **Reconstruct the proposal in Workflow B** — B's first phase reads the *funded* digest entry plus
   the baseline rows/summary and reconstructs the typed `ChangeProposal` on demand.

## Decision

**Workflow B reconstructs the `ChangeProposal` from the funded digest entry** (option 2). A `Scope`
phase reads the chosen `## N · …` entry of `proposals.md` and the baseline (`<stamp>.jsonl` +
`.summary.json`), and emits the typed fields; the governance **route** is then computed
deterministically in the workflow with the same rule as `ChangeProposal.route()`
(`global` blast-radius **or** `touches_grader` → `adr_only`, else `implement`).

Consequences of the reconstruction being driven by the *funded* entry:
- Workflow A stays purely **read-only and human-facing** (ADR-0031 holds) — it never emits a machine
  artifact, and no schema work is spent on unfunded entries.
- The structured seam is computed **just-in-time, only for funded proposals**, keeping the typed
  contract (`evals/proposal.py`) as the *internal* A→B interface without a persisted A-side artifact.
- The scope (`affected_models` × `target_tasks`) is derived from the *actual baseline failure rows*,
  so the canary targets exactly what failed — not a human's restatement in the digest.

Two supporting choices made in `evals/validate.py` while wiring the ladder this seam feeds:
- **The canary screens on a raw flip-count, not McNemar.** A 1-seed canary on the affected models has
  no statistical power; requiring significance there would never let a real fix through. Significance
  (paired McNemar) is applied only at the full-matrix rung. The canary's job is a cheap *screen*
  (target newly passing, nothing raw-regressed), not a verdict.
- **Agnosticism is a per-model regression check.** The matrix verdict requires a significant overall
  improvement **and** no single model showing a significant regression — so a change that lifts the
  aggregate while breaking one model is rejected (the global-validation guarantee of ADR-0024 §safety).

## Consequences / alternatives

- **Consequences:** the loop's A→B hand-off is complete without reintroducing a per-proposal artifact;
  the human gate (the digest) and the machine seam (the reconstructed `ChangeProposal`) are cleanly
  separated by *funding*. `evals/proposal.py` remains the schema of record and is still used to
  validate the reconstructed object's shape.
- **Rejected:** A-side structured emission (option 1) — reverses ADR-0031's human-surface win and pays
  schema cost for proposals that are never funded; threading raw scope through args by hand —
  error-prone and divorced from the baseline evidence.
- **Risk:** reconstruction is a reasoning step, so a mis-scoped proposal could target the wrong
  tasks/models. Mitigated by grounding it in the baseline rows and by the ladder itself — a wrong
  scope shows up as "no improvement" at the canary and stops cheaply.
