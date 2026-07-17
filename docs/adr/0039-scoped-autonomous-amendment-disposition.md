# ADR 0039 — Scoped autonomous amendment disposition (config-gated auto-approve)

- **Status:** Proposed
- **Date:** 2026-07-07
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8[1m]) — 2026-07-07 design session.
- **Extends:** [ADR-0016](0016-autonomous-approval-disposition.md). ADR-0016's scope cut (§32) deferred an `ask → auto-allow` mode until "a real unattended-write use case appears"; its rejected-alternatives note anticipated *"Revisit if a second autonomous policy (e.g. trusted-allow) appears."* This is that revisit.
- **Related:** [ADR-0038](0038-model-declared-semi-frozen-verification-contract.md) (the semi-frozen contract whose amendment this dispositions); [ADR-0040](0040-held-out-verified-vs-self-reported-success.md) (what makes unattended auto-approve *safe to measure*); `HARNESS_DESIGN.md` §11 (permission tiers; "a `--trusted`/autonomous mode may promote `ask → auto`"), §13 (control vs. observation).

## Context

ADR-0038 lets the model **amend** its semi-frozen verification contract when a check goes obsolete, but only through a **gated action** (`alter_verification`, tier 3). ADR-0016 established that an unattended `Session` **denies every `ask` by default** (deny-only, to avoid the 51-minute `secret-safety` deadlock). So under today's rules an unattended greenfield run that needs an amendment gets it *denied* — the contract is effectively frozen, and if that check was genuinely wrong the run thrashes against an unsatisfiable bar (bounded by the repair/iteration/wall-clock budgets) and ends `incomplete`/`failed`.

That is the correct conservative default, and it stays. But for **artifact-producing** greenfield runs — an autonomous "build me X" where a human inspects the result, or an eval graded by a **held-out** oracle (ADR-0040) — auto-*denying* the amendment is pure loss: the model can't adapt its own developmental scaffold, gives up at a known-bad state, and produces *less* than it would have. When the operator has granted full autonomy, auto-*approving* the amendment lets the model go from nothing to a useful artifact.

The danger auto-approve raises — a model rewriting a *failing* check to pass it, moving its own goalposts — is real, but it is **contained by two independent mechanisms**: (1) the immutable floor (ADR-0038) that no amendment can touch, and (2) the held-out oracle (ADR-0040) that grades the eval against a check the model never sees. So auto-approve corrupts the *self-reported* label but not the *held-out* verdict, and the eval measures the gap.

## Decision (proposed)

**1. Add a config-gated, scoped auto-approve disposition.** A new `HarnessConfig.autonomous_amendment_policy: Literal["deny", "approve"] = "deny"` (env `AVATAR_AUTONOMOUS_AMENDMENT`), threaded through `Harness.session(...)` exactly as `approval_timeout_seconds` is. Default **`deny`** — ADR-0016's posture is unchanged unless the operator opts in.

**2. Scope the auto-approve to the amendment action *by tool name*, never by tier.** In `Session.request_approval`, where an unattended session currently always auto-denies, branch: if `tool == "alter_verification"` **and** `policy == "approve"`, emit `ApprovalResolved(allowed=True, via="auto")` and allow; **otherwise auto-deny, unchanged.** Scoping by *tool name* (not tier 3) is load-bearing: `run_command` and denylisted reads are also tier 3 and **must stay deny-only** — auto-approve must never widen to destructive/external actions. `via="auto"` (introduced by ADR-0016 for auto-deny) covers "no human present" for the allow direction too.

**3. Keep the control-plane invariant (#4).** The gate still returns `ask`; the *Session* still answers it — now from one of two dispositions (deny / scoped-approve) instead of deny-only. No new controller class (ADR-0016's §35 reasoning holds: the Session owns approval state; a policy field reuses the announce/record path).

**4. Every disposition is announced and audited.** Attended: `ApprovalRequested` → human modal → `ApprovalResolved(via="human")`. Unattended-deny: `ApprovalResolved(via="auto", allowed=False)`. Unattended-approve: `ApprovalResolved(via="auto", allowed=True)`, plus the amendment's old→new checks and the model's rationale in the journal. Auto-approve is therefore never *silent* — it is a visible, replayable record, which is what lets ADR-0040 count it.

**5. Attended sessions never standing-grant an amendment (added 2026-07-10).** The unattended config-gated policy above is the *only* sanctioned auto-approve for `alter_verification`. In an attended (TUI) session, the `[a] always` grant path must not cover it: a standing `ApprovalGrant` on the amendment tool would auto-approve every later goalpost move in the sitting after one ratification — the silent widening this ADR's tool-name scoping exists to prevent, arriving through the other door. Enforced in the core (`Session` neither stores nor matches a grant for `_UNGRANTABLE_TOOLS = {"alter_verification"}` — `remember` degrades to allow-once) and reflected in the cockpit (`ApprovalModal` offers no `[a]`/Always for an amendment). The guarantee is core-owned; the modal only keeps the UI honest.

## Alternatives considered

| Option | Verdict |
| --- | --- |
| Keep deny-only (ADR-0016 as-is) | Rejected for artifact runs — auto-denying a needed amendment is guaranteed loss; the model strands at a known-bad state. Retained as the **default**. |
| Auto-approve **on** by default in unattended | Rejected — flips ADR-0016's conservative posture globally with no opt-in step; the operator must consciously grant it. |
| Scope auto-approve by **tier** (all tier-3) | **Rejected — dangerous.** `run_command`/denylist are tier 3; auto-approving them reopens the `secret-safety` leak class. Scope strictly by the `alter_verification` tool name. |
| A general `--trusted` allow-all mode | Rejected (still, per ADR-0016 §32) — no unattended-write use case justifies allow-all; this is the *narrow* trusted-allow the ADR anticipated, confined to one non-destructive action. |
| A separate `AutonomousApprovalController` | Rejected — same as ADR-0016 §35; a policy field on `Session` is less surface. |

## Consequences

- Unattended greenfield **artifact** runs can adapt an obsolete contract instead of thrashing to `incomplete` — the developmental win from the design discussion.
- **The blast radius is one tool.** Every other `ask` (`run_command`, denylisted reads) stays deny-only in unattended mode; the `secret-safety` guarantee is untouched. This is enforced by name-scoping, and tested by asserting `run_command` still auto-denies under `policy="approve"`.
- **Auto-approve is safe *to measure*, not safe *to trust*.** It weakens the self-reported label; the immutable floor (ADR-0038) and the held-out oracle (ADR-0040) are what keep the *graded* outcome honest. Turning this on **without** ADR-0040's held-out grading would produce untrustworthy `success` labels — the two ship together for autonomous/eval use.
- Default-`deny` means zero behavior change until an operator sets `AVATAR_AUTONOMOUS_AMENDMENT=approve`; the cockpit (attended) never consults the policy — a human always ratifies there.
- ADR-0016's approval-timeout backstop still applies to any *attended* amendment left unanswered.
