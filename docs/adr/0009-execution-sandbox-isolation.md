# ADR 0009 — Execution sandbox isolation

- **Status:** Accepted (deferred) — the decision (keep the in-process model for the MVP; isolate behind `Workspace` when needed) is in force; the container/VM backend is the deferred build
- **Date:** 2026-06-11
- **Deciders:** Sarthak Joshi
- **Related:** `HARNESS_DESIGN.md` §15 ("Honest gap: true isolation"); ADR-0001 (Workspace as chokepoint).

## Context

The MVP runs in-process against a tracked workspace: path confinement, command timeouts, and stdout/stderr capture, but no real isolation. There is no ephemeral environment, no resource ceiling, no credential scoping, and network is only "avoided by default." §15 names this the demo-to-production line.

## Decision (proposed)

Keep the in-process model for the MVP. When isolation is needed, run the workspace inside an **ephemeral container/VM**: repo checkout, limited network, no secrets by default, CPU/memory/time limits, disposable environment, patch as the only output. The `Workspace` handle is the seam — every filesystem touch and command already funnels through it (ADR-0001), so isolation slots in *behind* `Workspace.run`/read/write without changing a single tool.

## Consequences / alternatives

- Tools and the runner are unaffected when isolation lands; only the `Workspace` backend swaps.
- *Rejected:* building containerization into the MVP (premature; §22 reliability-before-autonomy). *Deferred, not designed out.*
