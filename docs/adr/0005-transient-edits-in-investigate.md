# ADR 0005 — Transient edits in `investigate` tasks (net-zero-diff relaxation)

- **Status:** Accepted — implemented 2026-06-11 (maintainer call)
- **Date:** 2026-06-10
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8) — raised in design discussion 2026-06-10 ("are there not investigative workflows that involve edits?")
- **Related:** `HARNESS_DESIGN.md` §7/§12 (task kinds as verification contracts), §15 (pinned-baseline diff); `DECISIONS.md` 2026-06-08 ("investigate can't mutate — prevention, not detection")

## Context

`task_kind` is a taxonomy of **verification contracts**, and `investigate`'s contract is *grounded answer, repo untouched*: the verifier requires `no_unintended_diff`, and the permission gate blocks tier-1 mutation up front. This is correct for the contract — but real investigation sometimes *instruments*: add a debug print, run, observe, revert; write a scratch probe script and delete it. Today those workflows are impossible in an `investigate` task, and misusing `edit` for them forces the wrong verification contract ("a diff must exist").

The key observation: the verifier's rule is **no diff at the end**, not *no writes ever* — and the pinned-baseline diff (§15) already measures exactly "net change since task start".

## Decision (proposed)

Allow tier-1 tools (`apply_patch`/`write_file`) in `investigate` tasks, **keeping the verifier's `no_unintended_diff` check unchanged**: the tree must net to **zero diff vs the pinned baseline at verification**. Transient instrumentation becomes legal; an investigation that *leaves* a change still fails its contract.

Mechanics when implemented:
- Remove the gate's `task_kind == "investigate"` tier-1 block; the §12 contract becomes the enforcement point (detection where prevention used to be — acceptable because the deliverable is unchanged and the diff is fully observable).
- The investigate prompt framing changes from "WITHOUT editing the repo" to "the repo must be unchanged when you answer — revert any instrumentation".
- The edit-intent phase bootstrap stays edit-kinds-only; in `investigate`, tier-1 admission would ride a new explicit rule, not `is_edit_intent`.

## Alternatives considered

- **Status quo (chosen for now):** no dogfood or eval task has yet *needed* transient edits; building ahead of friction violates Principle C.
- **A fourth `task_kind` ("experiment"):** rejected — it is not a distinct verification contract (the contract is exactly investigate's), so it would re-create the vacuous-gate problem ADR-0002 D5 avoided for plan mode.
- **Auto-revert by the harness** (snapshot/restore around investigate runs): heavier machinery duplicating what the pinned baseline already measures; also hides from the model that its instrumentation persisted.

## Consequences

- When implemented: prevention at the gate is traded for detection at the verifier for this one kind — the secret/placeholder diff guard and the denylist still apply to every write through the `Workspace` chokepoint.
- A model that forgets to revert fails verification with a legible reason ("unintended diff") and can repair by reverting — the repair loop already exists.
- Trigger to implement: implemented by maintainer directive 2026-06-11 (a maintainer call, ahead of the originally anticipated dogfood/eval friction).
