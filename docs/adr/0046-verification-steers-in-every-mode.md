# ADR 0046 — Verification steers in every mode; conversational exhaustion defers to the human (supersedes the advisory stance of §23.5 / ADR-0002 D7)

- **Status:** Proposed
- **Date:** 2026-07-11
- **Deciders:** Sarthak Joshi
- **Related:** [ADR-0002](0002-interactive-tui-cockpit-and-mvp-feature-set.md) (Decision 7 — the *advisory* interactive-verification stance this supersedes), [ADR-0038](0038-model-declared-semi-frozen-verification-contract.md) (the declared contract + immutable floor a failing verdict now steers against), [ADR-0039](0039-scoped-autonomous-amendment-disposition.md) (the gated `alter_verification` amendment — the mid-loop human-consent path), [ADR-0014](0014-greenfield-self-authored-verification.md) (the smoke floor whose failure this stops laundering). Seams: `avatar/runner.py` (`_averify`, `_settle_terminal_outcome`), `HARNESS_DESIGN.md` §23.5, `avatar/session_state.py` (`ReplSession(auto=…)` → `conversational=not auto`).

## Context

§23.5 / ADR-0002 Decision 7 made interactive verification **advisory**: the verifier ran and reported, but a `final_answer` was delivered immediately as `outcome="success"` regardless of the verdict, with no repair loop — "the human is terminal authority." The reasoning was that a chat assistant should not force every "explain this function" through an edit-shaped CI gate.

Two dogfood journals of the same objective (build an ASCII Tetris game across a REPL sitting, same model) exposed the hole:

- `tetris_grok2/events/e97dc153a547400bac58dbc87e6736bd.jsonl` (2026-07-10): **3 of 5** turns emitted `verification_end passed=false` immediately followed by `agent_end outcome="success"`. On the final turn the model called the tier-3 `alter_verification` (`approval_requested → approval_resolved` — **the human consented to the contract change**), the verifier ran the amended contract *plus* the immutable floor (ADR-0038), the floor failed (`verification failed: ['floor']` — a broken file would not compile), and the turn was still reported `success`.

The failure is threefold and each part contradicts a load-bearing invariant:

1. **The model self-certifies.** Invariant #3 — *"Done is a proposal the verifier disposes of; only the harness sets `success`, on positive external evidence."* Advisory mode let the model's `final_answer` become `success` on a *failed* verdict.
2. **The steering is discarded.** The verifier's purpose is to drive the model toward functional correctness. Short-circuiting on the first verdict means the model never gets the failure-driven turns in which it would repair — or reach for the designed escape valve, a gated `alter_verification` amendment (ADR-0038/0039).
3. **The immutable floor is buried.** ADR-0038 promises the floor can never be weakened, *even by a human-approved amendment*. `passed=false on ['floor'] → outcome="success"` is a direct violation of that guarantee.

The original §23.5 concern (don't gate "explain this" behind an edit-shaped check) was already handled by a *different* mechanism: `task_kind` (§7) selects **which** checks run, so an `investigate` turn passes on producing a grounded explanation and never faces an edit gate. Advisory-on-authority was the wrong lever; the guard against edit-shaped gating lives in `task_kind`, not in disabling the repair loop.

## Decision

**Verification steers in every mode.** `_averify` no longer branches on `conversational`: a failing verdict always feeds the evidence + `recommended_next_action` back, increments `repair_failures`, and drops to `editing`, so the model repairs or proposes a gated `alter_verification`. What differs between modes is only **who is deferred to at the terminal boundary**, settled in `_settle_terminal_outcome`:

- **Autonomous (`--auto`, `conversational=False`):** the §12 gate owns `outcome`. Repair exhaustion → `failed` (unchanged).
- **Conversational (interactive `ReplSession` default):** identical steering. At repair exhaustion the turn **`blocks`** — a first-class hand-off to the human: the block reason (`"Verification is still failing after N repair attempt(s) (<verdict>). How should I proceed?"`) is appended to `open_questions`, so the REPL/cockpit surface it as an ask rather than a silent failure. The last `final_answer` and the failing verdict remain on the state for rendering.

The human is still terminal authority in the REPL — but *after* the verifier has steered to success or exhaustion, and continuously *during* steering through the permission gate (every mutating tool, and the tier-3/ungrantable `alter_verification` amendment). Deferral is a designed hand-off, not a blanket `success`.

Consequently a **floor failure can never be reported as `success` in any mode** — the specific `tetris_grok2` regression is closed by construction.

## Alternatives considered

- **Keep advisory; only fix floor→success.** Rejected: it treats the symptom. The floor laundering is one instance of the general defect (a failed verdict laundered to success); the same journal shows non-floor declared checks (`declared_2`, `declared_8`) laundered identically. Restoring steering fixes the class.
- **Conversational exhaustion → `failed` (reuse the autonomy outcome).** Viable and minimal, but a bare `failed` in a REPL reads as a dead end and discards the one thing interactivity affords: a human who can redirect. `blocked` + an `open_question` is the literal "defer to a human" and reuses the existing ask-user rendering path (`session_state.py` already treats `outcome=="blocked" and open_questions` as the turn's reply). It also keeps the `conversational` flag meaningful after the `_averify` unification.
- **Route conversational exhaustion through a new fifth `outcome` value.** Rejected: threads a new value through `ArtifactManager`/§7 for no gain over `blocked`, which already carries the correct "needs human input" semantics.
- **Drop the `conversational` flag entirely (make `--auto` and interactive engine-identical).** Rejected: the flag now has exactly one honest job — the exhaustion disposition (`failed` vs `blocked`) — and that difference *is* "who is terminal authority," which the design deliberately keeps.

## Consequences

- The interactive REPL becomes a real steering loop: a broken edit is repaired (or the model proposes a gated amendment) across turns until it verifies or the repair budget exhausts. The human answers approvals throughout and is explicitly asked at exhaustion.
- `investigate`/explanatory turns are unaffected — they pass their evidence-shaped contract in one turn, so `task_kind`, not authority mode, is what keeps casual chat from feeling like CI.
- A REPL edit against a repo with **no** verification contract now `blocks` at exhaustion instead of reporting vacuous `success` (previously masked by advisory mode). With a contract (declared, detected, or the greenfield floor) it verifies and succeeds as before. Tests that leaned on the vacuous-success path now configure a real passing contract.
- `_settle_terminal_outcome` centralizes the terminal disposition; `_exit_reason` (repair-exhaustion → `failed`, else `incomplete`) is unchanged and still governs every non-conversational and non-repair exit.
