# ADR 0040 — Held-out-verified vs self-reported success; wire the ADR-0011 D3 oracle

- **Status:** Proposed
- **Date:** 2026-07-07
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8[1m]) — 2026-07-07 design session.
- **Extends / implements:** [ADR-0011](0011-verifier-integrity-under-self-improvement.md) **D3** (held-out FAIL_TO_PASS / PASS_TO_PASS — reserved in `evals/spec.py` (`hidden`/`oracle`/`fail_to_pass`/`pass_to_pass`), previously unbuilt).
- **Related:** [ADR-0039](0039-scoped-autonomous-amendment-disposition.md) (auto-approve, which makes this measurement *necessary*); [ADR-0038](0038-model-declared-semi-frozen-verification-contract.md) (the model-declared contract being measured); [ADR-0020](0020-guard-probes.md) (`probe_role`; a check is necessary-not-sufficient); [ADR-0027](0027-sandboxed-execution-trust-and-self-verification-calibration.md) (capability ≠ authority; the probe is *hidden + external*, so the agent "can fool itself, never the score"); [ADR-0033](0033-per-task-passing-outcome-whitelist.md) (`passing_outcomes`); `evals/CLAUDE.md` (grader-touching → ADR-routed + globally validated).

## Context

ADR-0039 lets an unattended run **auto-approve** the model's amendment of its own verification contract. That is deliberately *safe to measure, not safe to trust*: a model can rewrite a failing check to pass it, so **its self-declared contract passing certifies nothing.** If the eval scored the model's own contract, auto-approve would drive every model to a self-reported 100% and pass@1 would measure self-consistency, not capability.

The harness already has the antidote in embryo. The per-task `success_probe` (ADR-0020/0027) is **hidden + external**: it lives under `evals/probes/`, is never seeded into the agent's scratch repo, and runs post-run against the final workspace — the agent "can fool itself, never the score." And `is_solved` (`evals/score.py`) *already* treats a success probe's exit code as authoritative, ignoring the model's self-report. What is missing: (1) the two signals — self-report and held-out verdict — are **conflated** into one `solved` bool on `ResultRow`, so gaming is invisible; and (2) ADR-0011's richer **D3** held-out mechanism (per-file `fail_to_pass`/`pass_to_pass` injected into a throwaway copy) is reserved in the schema but **unwired**.

## Decision (proposed)

**1. Split the two signals on `ResultRow`.** Add explicit `self_reported_success: bool` (the model's own outcome — reached `final_answer` / its declared contract passed) and `held_out_passed: bool` (the hidden oracle's verdict). Both source values already exist in `run_task`.

**2. Key pass@1 to the held-out verdict.** `pass_at_1` / `pass_caret_k` count `held_out_passed`, not the conflated `solved`. For a success-probe task this is already the effective behavior (`is_solved` returns `probe_exit == 0`); this ADR makes it explicit and names it.

**3. Report the gap as a first-class metric.** `gamed_rate = mean(self_reported_success ∧ ¬held_out_passed)` — the fraction of runs the model *claimed* done but the held-out oracle *rejected*. Add a `self_certified_only` / `gamed` bucket to `evals/classify.py`. This is the number that makes ADR-0039's auto-approve honest: it quantifies exactly how often the model moved its own goalposts.

**4. Wire ADR-0011 D3.** Consume the reserved `hidden` / `oracle` / `fail_to_pass` / `pass_to_pass` fields:
- **Withhold** `hidden`/oracle files from the seeded scratch repo at provision — the agent cannot special-case a test it never saw.
- **At grade time**, copy the final workspace to a throwaway dir, **inject** the hidden oracle, and run `fail_to_pass` (must now pass) + `pass_to_pass` (must still pass) via the existing `run_probe` primitive (`cwd = throwaway`). The conjunction is `held_out_passed`.
- `success_probe` remains the simpler single-script hidden oracle; D3 is the richer per-test form. A task may use either; both feed `held_out_passed`.

**5. Greenfield tasks that run under auto-approve must carry a held-out oracle.** A greenfield `edit` task graded under ADR-0039 auto-approve without a hidden oracle would have no trustworthy signal at all — so such tasks declare `success_probe` or `fail_to_pass` oracle files (under `evals/probes/`, never seeded).

## Alternatives considered

| Option | Verdict |
| --- | --- |
| Grade on the model's self-reported outcome | Rejected — auto-approve makes it trivially gameable; measures self-consistency, not capability. |
| Reuse `success_probe` only, skip D3 | Viable and minimal, but D3's per-file `fail_to_pass`/`pass_to_pass` gives richer, injected-at-grade-time greenfield grading and finally lights up ADR-0011's reserved schema — chosen deliberately. |
| Keep `solved` conflated, add no split | Rejected — gaming stays invisible; `gamed_rate` is the whole point of measuring under auto-approve. |
| LLM-judge the held-out grade | Rejected — `evals/CLAUDE.md` §9 (deterministic exit codes only). |

## Consequences

- pass@1 measures **real** capability even when ADR-0039 auto-approve is on; `gamed_rate` exposes goalpost-moving instead of hiding it inside a green checkmark.
- ADR-0039 auto-approve becomes **safe to ship for evals**: the held-out oracle is the immutable external anchor that a self-amended contract can't reach. (The two ADRs are co-dependent for autonomous/eval use.)
- **Grader-touching** (`ResultRow`, `is_solved`, `metrics`, `classify`, task specs) → per `evals/CLAUDE.md` this is ADR-routed, TDD'd in `tests/test_evals.py` (including a deliberately-gaming `ScriptedModel` that proves `gamed_rate` fires), and **globally validated** against frozen assets via `python -m evals.diff` (McNemar, clustered CI, model-agnosticism) — never per-task.
- D3 adds a provision-time withhold + a grade-time throwaway-copy-inject step; the eval runner owns hashing / withholding / injection (ADR-0011 consequences), keeping the oracle outside the agent-visible tree.
- Existing success-probe tasks keep working (their `held_out_passed` = `probe_exit == 0`); the new fields are additive, with `solved` retained as the composed verdict for back-compat until the metrics migration lands.
