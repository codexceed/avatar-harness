# ADR 0010 — `git_status` / `git_diff` as model-callable tools

- **Status:** Proposed
- **Date:** 2026-06-11
- **Deciders:** Sarthak Joshi
- **Related:** `HARNESS_DESIGN.md` §10 (tool table lists both at tier 0), §14 (artifact diff), §23.4 (`/diff` meta command).

## Context

§10's tool table lists `git_status` and `git_diff` as tier-0 model tools, but the engine never registered them. Diff/status data reaches the harness directly through `Workspace.diff()`/status (verifier, artifact) and reaches the human through the `/diff` meta command — so nothing in the built flow has needed the *model* to call them.

## Decision (proposed)

Leave them deferred. Add model-callable `git_status`/`git_diff` only when a task genuinely needs the model to read its own working-tree delta mid-run (e.g. self-review before `final_answer`). They are thin tier-0 wrappers over the existing `Workspace.diff()`/status — cheap to add when earned, noise to register before then.

## Consequences / alternatives

- No drift risk: `Workspace` remains the single source of diff/status; these tools would just expose it to the model.
- *Rejected:* registering them now for spec parity (capability nobody calls; §22, §21 "earned extensions").
