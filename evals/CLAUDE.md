# CLAUDE.md — `evals/` (the eval harness + the evals-driven improvement loop)

Package-local guidance for work under `evals/`. The full design is **`evals/improvement-loop-design.md`** (decision: **ADR-0024**). The eval harness itself is ADR-0004; its scoring is ADR-0020 (guard probes). **Read `evals/improvement-loop-design.md` before building any loop component.**

## What this package is

Two things live here, layered:
1. **The eval harness (ADR-0004, built).** `run.py` provisions a hermetic scratch repo per task, runs the harness, and scores deterministically (success/guard **probe** when present, else the **Verifier**); `metrics.py`/`stats.py`/`diff.py` give pass@1, pass^k, clustered CIs, and paired **McNemar** regression; `classify.py` buckets failures. The journal is the dataset; the verifier is the scorer (no LLM judge).
2. **The improvement loop (ADR-0024, building).** Turns eval signal into *reviewed* harness changes through two ad-hoc Claude workflows over a deterministic core.

## Core principles of the improvement loop (do not violate)

1. **Two workflows, three human gates — not one auto-run.** `G0` (human runs `make eval`) → **Workflow A `evals-to-proposals`** (read-only, **zero eval spend**) → `G1` (human funds proposals) → **Workflow B `proposal-to-pr`** (the **only** eval spender) → `G2` (human reviews & merges). The two costly/irreversible acts — running evals and merging — stay human longest.
2. **Two layers + one typed seam.** Deterministic **Layer-1 CLIs** in `evals/` (`distill`, `triage`, `score`/`route`, `validate`) hold everything exact/cheap (no model, TDD'd). **Layer-2 workflow scripts** orchestrate only reasoning subagents and shell out to Layer 1. A typed **`ChangeProposal`** (`evals/proposal.py`) is the A→B seam; `remediation_type` (prompt/guardrail/code/doc) is **orthogonal** to `blast_radius`.
3. **Dedup before debug.** Match each failure cluster against `docs/research/failure-modes.md` (the A/B/C/D catalog = the loop's memory) + open ADRs *first*. Only **novel** clusters reach the analysis fan-out — never re-diagnose a catalogued/already-ADR'd mode (e.g. C1 → ADR-0022).
4. **Route on blast-radius/risk, not implementation size.** A one-line always-on change (e.g. a prompt rule) is *high* blast-radius. Grader-touching changes (specs, probes, fixtures, verifier, scoring) are always high-governance → ADR-route.
5. **Validate globally, never per-failed-task.** Use the built machinery: `make eval` → `python -m evals.diff` (full matrix + McNemar + clustered CI + the model-agnosticism check). A single re-run is too narrow and too noisy.
6. **Freeze the grading surface during `validate`.** Run against `evals/` (specs · probes · fixtures) restored from a trusted ref, never the agent's worktree — a pragmatic ADR-0011 D1+D2. Necessary, **not sufficient**: it does not stop special-casing a frozen-but-visible test, cannot fix a construct-validity gap (a guard probe), and cannot cover the verifier when the verifier is itself the target.
7. **Cost is intentional.** The eval re-run dominates (one full matrix ≈ 2.5M tokens; 85% of the 2026-06-15 run's tokens were the 10 failures). Workflow A spends **$0**; Workflow B is the only spender, via the **canary ladder** (unit/local → 1-seed canary on affected models → full matrix on survival) with a hard rework cap. Every proposal carries a `predicted_validation_cost`.
8. **HITL stays until the integrity substrate exists.** No auto-merge, and no automating the eval-run trigger, until **ADR-0011 D1–D4 + a train/test split** are built and calibrated. Only then do the gates become triggers (cron / selection policy / auto-merge on held-out green) — the "golden loop." The human moves author → reviewer → auditor, never skips to auditor.
9. **No LLM-judge scoring; no SendMessage agent team.** Scoring is deterministic (ADR-0004). Proposal compatibility is handled by a single reconciliation **barrier**, not open-ended cross-agent chat.

## Conventions for code here

- Every Layer-1 module is a `python -m evals.<name>` CLI with an argparse `main()`, mirrors the pydantic style of `result.py`/`spec.py` (`to_jsonl` / `load_*` / `write_*`), and is **TDD'd in `tests/test_evals.py`** with an injected `ScriptedModel` (offline, no network).
- `evals/` is held to the same gates as `src/` (ADR-0013): ruff · pyrefly · pydoclint · deptry. `evals/probes/` and `evals/fixtures/` are the only carve-outs. Run `make check` before committing.
- Saved Workflow scripts live in `evals/workflows/` (invoked via `Workflow({scriptPath})`); proposal artifacts in `evals/proposals/<stamp>/`.
- Run journals must stay **distillable**: never let a tool dump unbounded output into `ToolEnd.content`, and keep the journal out of the agent-searchable tree (the 875 MB blowup; Increment 0).
