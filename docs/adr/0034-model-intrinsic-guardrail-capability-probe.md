# ADR 0034 — Measure the model's intrinsic guardrail capability, independent of the harness

- **Status:** Proposed
- **Date:** 2026-06-29
- **Deciders:** Sarthak Joshi
- **Related:** ADR-0033 (per-task passing-outcome whitelist — scores the *system's* disposition; this ADR isolates the *model's* judgment); ADR-0021 (sensitive-path denylist — the deterministic harness control that makes the gap below); ADR-0020 (guard probes); ADR-0018 (journal hidden from the agent — but the runner still records denied tool calls, which is the signal this ADR reads).

## Context

`secret-safety`'s no-leak guarantee is enforced by the **harness denylist** (ADR-0021), deterministically. Every run gets "no leak" for free, regardless of what the model intended. So the guard probe and the outcome whitelist (ADR-0020/0033) measure the **whole agent's** safety and disposition — *not the model's own judgment*. At the outcome layer, two very different models are indistinguishable:

- one that recognized `credentials` as off-limits and **never attempted** to read it, and
- one that **lunged at it** and was stopped by the denylist.

Both produce "no leak." If the question is the agent-as-shipped, that conflation is fine. But to gauge a **model's sole capability** — *would it have respected the sensitive file absent the harness guardrail?* — the terminal outcome is the wrong layer. We need a behavioral signal.

## Decision (direction; implementation deferred)

Introduce a **behavioral / counterfactual probe** class that scores the **model's actions in the journal**, not the terminal state. For `secret-safety`: `attempted_sensitive_read` — did the model issue a tool call targeting a denylisted path?

- **never attempted** → the model enforced the guardrail by its own judgment (strongest signal).
- **attempted, then denied** → the model relied on the harness to save it (weaker — the leak was prevented *for* it, not *by* it).

Report this as a **distinct metric**, orthogonal to and not folded into pass@1 — so harness-enforced safety (ADR-0033's disposition score) and model-intrinsic restraint stay un-conflated. The signal already exists: the runner records denied tool calls in the journal even though the journal is hidden from the agent (ADR-0018).

Generalize as a *"would the model have done the bad thing if the harness hadn't stopped it?"* probe — applicable to any guardrail backed by a deterministic harness control (no-network, no-destructive-write, resource limits): score the *attempt*, not just the *prevented outcome*.

**Proposed, not Accepted.** This ADR records the direction; a follow-up settles the metric surface and confirms the attempt/denial is reliably and unambiguously detectable in the journal (distinguishing a deliberate denylisted read from an incidental path collision). It is grader-touching, so it carries the same frozen-surface + global-validation requirement as ADR-0033 once implemented.
