# ADR 0025 — Persist the journal-refined failure bucket on `ResultRow` (one classification, one source of truth)

- **Status:** Accepted — implemented 2026-06-18
- **Date:** 2026-06-18
- **Deciders:** Sarthak Joshi
- **Related:** ADR-0004 (internal eval harness — deterministic, no-LLM scoring); ADR-0020 (guard probes — the `probe_failed`/`guard_violation` split this depends on); ADR-0024 (evals-driven improvement loop — `evals/cluster.py` is Workflow A's spine). Evidence: `docs/research/failure-modes.md` **B4**; proposal `evals/proposals/20260618T162508Z/CP-cluster-outcome-vs-grading-axis.md` (this ADR supersedes/implements it). Supersedes that proposal.

## Context

The deterministic failure classifier (`evals/classify.py::classify(row, events=None)`) has two tiers:

- a **row-only** tier (from the `ResultRow` alone — survives workspace cleanup), and
- a **journal-refined** tier: when the run's journal events are supplied, an `incomplete` run is refined into `loop_oscillation` / `decision_error` — distinctions only the trajectory reveals.

This *dual mode* leaked an inconsistency. The runtime (`evals/run.py`) classifies at the **refined** tier — it reads each row's journal *before* cleanup deletes it, so `summary.json`'s histogram is fully refined. But that refined verdict was **computed and thrown away**: it was only aggregated into the summary, never persisted onto the per-run `ResultRow`. So every *downstream* consumer that loads `results/<stamp>.jsonl` later — the report scripts, **and `evals/cluster.py`** — re-ran `classify(row)` with no events (the scratch journals are usually long gone by then), silently dropping to the **row-only** tier. The same run could be `loop_oscillation` in `summary.json` but `budget_exhausted` everywhere downstream.

This compounded a second, related defect (catalog **B4**): `cluster.py` keyed failure clusters on the harness `outcome` axis (`(row.task, row.outcome or "unknown")`), not the grading verdict. In the probe path `outcome="success"` can label a genuinely *failed* run (`solved=False, probe_exit=1`), so a `probe_failed` run clustered as a self-contradictory "create-chatbot **success**" *failure* cluster — misleading the triage prefilter (it mis-fired onto A6) and the judge. The classifier already names that run `probe_failed`; the clusterer ignored it.

Both are the same root cause: **failure classification was re-derived ad hoc by each consumer, at the wrong axis and the wrong tier, instead of being computed once and recorded.**

## Decision

**Compute the journal-refined bucket exactly once — at scoring time, while the journal is live — persist it on `ResultRow.failure_mode`, and make every consumer read that field.**

1. **Schema.** Add `failure_mode: str = ""` to `ResultRow` (`evals/result.py`). It serializes with the row, so the dataset itself carries the verdict.
2. **Persist at the source.** `run_task` (`evals/run.py`) sets `row.failure_mode = classify(row, _journal_events(row))` immediately after scoring, when the scratch journal still exists — so `loop_oscillation` / `decision_error` are captured. Solved rows carry `"solved"`.
3. **One read path.** `evals/classify.py::resolve_failure_mode(row)` returns `row.failure_mode or classify(row)` — the persisted value, falling back to a row-only classify **only** for results files written before the field existed. `failure_histogram(rows)` now tallies `resolve_failure_mode` and **no longer takes an `events_for` resolver**; `build_summary` drops it too. The clusterer, the notebook, and `scripts/eval_report.py` all read through `resolve_failure_mode`.
4. **Cluster on the grading truth (closes B4).** `cluster_failures` keys on `(row.task, resolve_failure_mode(row))` and the `Cluster.outcome` field becomes `Cluster.bucket` (the classifier verdict); the triage `symptom` is built from `task + bucket + tokens`. The workflow script `evals/workflows/evals_to_proposals.js` is updated to the `bucket` field.

`classify(row, events)` remains the single engine — but in the normal path it is now invoked at **exactly one site** (scoring), always with the journal. The dual mode is gone *at the consumer boundary*: nothing re-classifies row-only except the explicit legacy fallback.

## Consequences

- **Consistency.** `summary.json`, the clusterer, and the report scripts all read the identical, journal-refined bucket. The histogram can never silently disagree with itself depending on whether the scratch journal still exists.
- **The dataset is self-describing.** A `results/<stamp>.jsonl` row now records *why* it failed, not just `solved`/`outcome`. Trajectory dirs are no longer needed to recover the refined bucket after the fact.
- **B4 is fixed at the spine.** A `probe_failed` run clusters as `probe_failed`, not "success"; genuinely-stuck (`budget_exhausted`) runs no longer fold into declared-done-but-broken (`probe_failed`) runs under one task. This supersedes proposal `CP-cluster-outcome-vs-grading-axis` (which proposed the cluster-keying fix alone) by addressing the underlying classification seam, not just the one consumer.
- **Back-compat is non-breaking.** Old results files (no `failure_mode`) still load and bucket via the row-only fallback — they just don't get the refinement. No re-run is forced; new runs are authoritative.
- **Cost.** None. Classification is deterministic and was already being computed at runtime; this records it instead of discarding it. Zero additional eval spend (ADR-0024 principle 7).
- **Trade-off.** `Cluster.outcome` → `Cluster.bucket` is a breaking rename of the cluster schema, requiring the Layer-2 workflow script to move in lockstep. Accepted: the field's *meaning* changed (it is no longer the harness outcome), so keeping the name would be a worse lie than the rename.
