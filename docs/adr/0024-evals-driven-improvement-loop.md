# ADR 0024 — Evals-driven improvement loop: two human-gated workflows over a deterministic core

- **Status:** Proposed
- **Date:** 2026-06-16
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8) and Codex (gpt-5.4, xhigh) — two independent design critiques, consolidated into `evals/improvement-loop-design.md`
- **Related:** the full build spec is **`evals/improvement-loop-design.md`** (this ADR records the decision; the design doc is the architecture). ADR-0004 (the eval harness — verifier-as-scorer / journal-as-dataset, the substrate this builds on), ADR-0011 (verifier integrity under self-improvement — the **unbuilt** substrate that gates HITL removal), ADR-0013 (`evals/` package boundary + gates), ADR-0020 (guard probes), ADR-0022 (won't-conclude — the first failure mode this loop will process); `docs/research/failure-modes.md` (the A/B/C/D catalog = the loop's memory).

## Context

Harness improvements have come almost entirely from manual dogfooding, which doesn't scale and ignores the measured signal Eval-0 already produces (scored runs, lossless journals, a failure-mode catalog). We want to turn that signal into *reviewed* harness changes, progressively reducing the human in the loop.

The hazard is structural: the moment a loop validates its own fixes by re-running the eval tasks it is optimizing against, **ADR-0011's Goodhart problem applies** — the agent can make a task "pass" by editing the grading surface (specs, probes, fixtures, the verifier) rather than fixing the harness. ADR-0011's defenses (protected oracle paths, fingerprinting, held-out tests, calibration, train/test split) are **Proposed, not built**. The latest matrix also showed the cost reality (85% of 2.53M tokens spent on the 10 failures) and that all 10 failures were a single already-catalogued mode (C1 → ADR-0022), i.e. an undisciplined loop would re-debug solved problems at high cost.

## Decision

Adopt the design in **`evals/improvement-loop-design.md`**:

1. **Two ad-hoc, independently-invokable Claude workflows separated by three human gates.** `GATE 0` (human runs `make eval`, manual) → **Workflow A `evals-to-proposals`** (read-only, *zero eval spend*) → `GATE 1` (human funds proposals) → **Workflow B `proposal-to-pr`** (the *only* eval spender, via a canary ladder) → `GATE 2` (human reviews & merges).
2. **A two-layer abstraction with a typed seam.** Deterministic Layer-1 CLIs in `evals/` (`distill`, `triage`, `score`/`route`, `validate`) hold everything exact/cheap; Layer-2 workflow scripts orchestrate only reasoning subagents and shell out to Layer 1. A typed **`ChangeProposal`** artifact is the A→B seam, carrying `remediation_type` (prompt/guardrail/code/doc) **orthogonal to** `blast_radius`.
3. **Disciplines.** Dedup each failure cluster against `failure-modes.md` + open ADRs *before* any agent debugging; route on **blast-radius/risk, not implementation complexity**; validate **globally** (full matrix + paired McNemar via `evals.diff`), not per-failed-task; `validate` runs against **frozen `evals/` assets** (a pragmatic ADR-0011 D1+D2).
4. **HITL stays** on every merge and every grader-touching change until ADR-0011's D1–D4 + a train/test split exist. Only then do the gates become triggers (cron / selection policy / auto-merge on held-out green) — the "golden loop." Collaboration uses a reconciliation **barrier**, not a SendMessage agent team.

## Consequences / alternatives

- **Consequences:** a cheap, safe MVP ships first (Layer-1 + Workflow A are free and read-only); eval spend is confined to one human-funded stage and staged by the canary ladder; the `search_repo`/journal-blowup guardrail (Increment 0) is a prerequisite cleanup; the failure-mode catalog becomes the loop's durable memory. The path to autonomy is explicit but gated on the integrity substrate — no new control plane is added to reach it.
- **Rejected:** one monolithic auto-loop (couples the expensive/dangerous steps, removes the cost+safety gates); a SendMessage "team that consults each other" (O(n²), racy — a reconciliation barrier is cheaper and more correct); routing on complexity (mis-files a one-line-but-always-on change like ADR-0022); validating only the failed tasks (blind to the cross-task regression a global change causes); and eliminating HITL now (unsafe while ADR-0011 is unbuilt).
- **Status → Accepted** when Increments 0–2 of the design doc land green and a dry-run of Workflow A over a real results dir produces a correctly deduplicated proposals set.
