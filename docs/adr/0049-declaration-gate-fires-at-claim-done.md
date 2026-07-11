# ADR 0049 — The greenfield declaration gate fires at claim-done, not only at first-edit (and declare stays investigating-only)

- **Status:** Proposed
- **Date:** 2026-07-11
- **Deciders:** Sarthak Joshi
- **Related:** [ADR-0038](0038-model-declared-semi-frozen-verification-contract.md) (the declaration gate this refines — its trigger was too narrow), [ADR-0014](0014-greenfield-self-authored-verification.md) (the smoke floor, the fallback after nudges exhaust), [ADR-0046](0046-verification-steers-in-every-mode.md) (the repair loop that pushed an empty-contract edit into `editing`, where declare was unreachable). Seams: `avatar/runner.py` (the `final_answer` handler + the shared `_refuse_for_declaration`), `avatar/tools/verification.py` (`_DECLARE_PHASES`, unchanged deliberately).

## Context

The ADR-0038 declaration gate refuses a *greenfield* edit and nudges the model to `declare_verification` — but it fires **only at the edit-intent bootstrap**: `_arun_tool_call` checks `is_edit_intent(tool) and phase == "investigating"`. A run that never makes an edit-intent call slips past it.

A dogfood journal (`tetris_grok4/events/4478b338…jsonl`, 2026-07-11; goal *"provide a design spec in markdown"*) showed the failure end to end. The model read once, then called `final_answer` with the entire spec **pasted inline** — no `write_file`, no `declare_verification`. So:

1. The edit-intent bootstrap never fired (no edit) → no declaration nudge.
2. `_freeze_plan` at verify froze an **empty** plan (greenfield, nothing detected/declared).
3. The edit verifier failed with the cryptic `verification_contract` → *"no verification contract discovered."*
4. The repair loop (ADR-0046) pushed the phase to `editing` — where **`declare_verification` is unreachable** (`_DECLARE_PHASES = {"investigating"}`). The model was forced to *create* its never-declared contract through `alter_verification` (the *amend* tool), thrashing three times past the shell-syntax and change-kind validators before one froze.

The gate was coupled to the wrong boundary: it guarded the *first edit*, but "claim done" is the other way an edit task reaches verification with no contract.

## Decision

**The declaration gate also fires at the claim-done boundary.** When a `final_answer` is proposed on a greenfield, undeclared `edit` task with nudges remaining, the runner refuses it (does not verify), nudges the model to `declare_verification`, and — critically — **keeps the phase in `investigating`**, where declare is reachable. The nudge phrasing is claim-done-specific (*declare a contract, then write the file(s) — do not answer with the content inline*). The bootstrap and claim-done paths share one `_refuse_for_declaration` helper, so the nudge budget, feedback, and `declaration_required` journaling are identical. When nudges exhaust, `final_answer` proceeds exactly as before (empty plan → smoke floor → verdict) — the gate is a bounded nudge, never an infinite refusal.

This closes the loop *before* the empty freeze: the model declares while still in `investigating`, freezes a real contract, and verifies — the empty-contract failure and the `alter`-to-create thrash never occur.

## Alternatives considered

- **Make `declare_verification` reachable in `editing` (the obvious "Fix 2").** **Rejected — it opens a gate bypass.** `declare_verification` is tier-0 (ungated) because declaring a contract *before any exists* is author-and-run, not a goalpost move; `alter_verification` is tier-3 (gated, ungrantable) precisely so *changing* a contract needs consent (ADR-0038/0039). If declare were allowed in `editing` — where a contract has already frozen — a model could **replace or weaken its frozen contract through the ungated declare instead of the gated alter**, dodging the consent gate. And it is unnecessary: with the claim-done gate above, declaration always happens in `investigating` (the bootstrap refuses edits, and claim-done refuses `final_answer`, until a contract is declared), so the model never legitimately reaches `editing` *needing* to declare. Keeping declare investigating-only preserves the tier-0/tier-3 split.
- **Freeze an empty plan → let the smoke floor cover it (status quo).** Rejected as the primary path: the floor is the *decline* fallback (ADR-0014), and routing every claim-done-without-a-diff through an empty-freeze-then-fail is exactly the cryptic, thrash-inducing path the journal exposed. The floor stays as the after-exhaustion fallback, not the first response.
- **Reclassify a claim-done-with-no-diff edit as investigate.** Rejected: it may genuinely be an edit the model under-executed (answered inline instead of writing the file); the nudge tells it to *write the file and declare*, which is the intended recovery. Kind changes are the ADR-0048 escalation's job, one-directional `investigate → edit`, not the reverse.

## Consequences

- A greenfield edit that claims done with no contract is nudged to declare **up front**, in `investigating`, instead of hitting the empty-contract verdict and the backwards `alter` thrash. The `tetris_grok4` sequence collapses to: nudge → declare → write → verify.
- The declaration gate now has two symmetric triggers (first-edit, claim-done) sharing `_refuse_for_declaration`; both honor `max_declaration_nudges` and fall through to the smoke floor at the cap.
- `declare_verification` stays `investigating`-only by decision, not by omission — the tier-0/tier-3 (declare/alter) split is a consent boundary, and this ADR records *why* it must not be relaxed.
- Composes with ADR-0046/0048: fewer runs reach the repair loop with an empty contract, and those that do (nudges exhausted) still get the floor; the escalation path is unaffected.
