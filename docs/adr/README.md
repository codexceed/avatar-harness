# Architecture Decision Records

Records of significant architectural decisions for avatar-harness. Each ADR captures the context, the decision, and its consequences at a point in time — ADRs are immutable once accepted; supersede rather than edit.

Format: Nygard-style with MADR touches. Flow/sequence detail is given in [Mermaid](https://mermaid.js.org/) (renders on GitHub and in most editors).

| # | Title | Status |
| --- | --- | --- |
| [0001](0001-async-event-bus-and-durable-execution.md) | Async lifecycle event bus, two-plane UX integration, and durable execution | Proposed |

## Conventions

- **Status:** Proposed → Accepted → (Superseded by ADR-N / Deprecated).
- **Numbering:** zero-padded, monotonic (`0001`, `0002`, …).
- **Scope:** one decision per ADR. If a decision changes, write a new ADR that supersedes the old one and link both ways.
