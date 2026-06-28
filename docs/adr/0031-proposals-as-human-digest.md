# ADR-0031 · Workflow A emits a human-readable proposals digest; defer the structured `ChangeProposal` seam

Status: Accepted — implemented 2026-06-28. Amends the output contract of **ADR-0024** (Workflow A);
the rest of ADR-0024 stands.

## Context

ADR-0024 defined the eval-driven improvement loop as two human-gated workflows over a deterministic
core, with a typed **`ChangeProposal`** (`evals/proposal.py`) as the seam between Workflow A
(produces proposals) and a future Workflow B (builds PRs). Workflow A was specified to write one
`evals/proposals/<stamp>/<id>.md` per proposal — YAML front-matter (every `ChangeProposal` field) +
a prose body.

In practice (run `20260627T211653Z`) that artifact proved a poor **human** control surface, which is
the whole point of a human-gated loop:

- The front-matter dumped the full structured schema (a multi-paragraph `tdd_plan`, an `evidence`
  list, routing fields) at the *top* of the doc — noise before signal.
- Bodies ran long and text-dense — hard to skim, hard to compare proposals at a glance.
- Proposals referenced failure-mode **catalog codes** (`A6`, `B4`, `ADR-0028`) that mean nothing to
  a reader looking at the proposal in isolation; the doc was not self-contained.
- Workflow B does not yet exist, so the structured seam had **no consumer** — its cost (verbosity)
  was being paid with none of its benefit (automated hand-off) realized.

## Decision

Workflow A's sole output is a **human-readable digest**, `evals/proposals/<stamp>/proposals.md`:

- An **At a glance** index table (one row per issue: what's wrong · the fix · size · risk), then one
  self-contained entry per issue with fixed sections — **The issue**, **Related history**, **The
  proposed change**, **How we'd verify** — balancing brief prose with one small visual.
- The digest is **self-contained and code-free**: it cites no failure-mode catalog codes. The
  workflow may still *consult* `docs/research/failure-modes.md` for historical grounding and append
  newly-confirmed modes back to it (that file keeps its codes), but the reader is never sent to it.
- **Defer the structured seam.** Workflow A no longer emits `ChangeProposal` JSONL or per-proposal
  front-matter. `evals/proposal.py` (the type, `route()`, `score_impact()`, serialization) **remains
  in the tree, tested and unchanged** — it is the contract Workflow B will produce/consume; it is
  simply not *emitted by A* until that consumer exists.

## Consequences

- The human gate ("review the proposals dir, fund which to build") operates on a document built to
  be reviewed: skimmable, comparable, no lookups.
- When Workflow B is built, structured emission returns alongside the digest (digest for humans,
  JSONL for the machine) — re-amending this ADR at that point. Until then there is one artifact and
  no unused-schema verbosity tax.
- Docs that described the per-`<id>.md` contract (ADR-0024, `evals/improvement-loop-design.md`,
  `evals/CLAUDE.md`) are annotated to point here; prior-run proposal dirs are left as historical
  artifacts in the old format.

## Alternatives considered

- **Keep front-matter, shorten the body.** Rejected: the front-matter dump itself is the primary
  readability cost, and it serves a consumer that does not exist yet.
- **Slim structured block per entry in the same file.** Rejected (this run): one artifact is simpler
  and the structured fields have no reader today; revisit when Workflow B lands.
- **Emit both digest and JSONL now.** Rejected as premature: no consumer validates the JSONL, so it
  would drift untested. Deferring keeps the seam honest — it returns with the code that uses it.
