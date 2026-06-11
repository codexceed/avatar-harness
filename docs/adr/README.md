# Architecture Decision Records

Records of significant architectural decisions for avatar-harness. Each ADR captures the context, the decision, and its consequences at a point in time — ADRs are immutable once accepted; supersede rather than edit.

Format: Nygard-style with MADR touches. Flow/sequence detail is given in [Mermaid](https://mermaid.js.org/) (renders on GitHub and in most editors).

| # | Title | Status |
| --- | --- | --- |
| [0001](0001-async-event-bus-and-durable-execution.md) | Async lifecycle event bus, two-plane UX integration, and durable execution | Accepted (durable execution deferred to 3.3) |
| [0002](0002-interactive-tui-cockpit-and-mvp-feature-set.md) | Interactive TUI cockpit and the MVP coding-agent feature set | Accepted (implemented; D3 revised 2026-06-10) |
| [0003](0003-file-creation-transport.md) | A robust transport for file creation (and large mutations) | Accepted (A + B implemented) |
| [0004](0004-internal-eval-harness.md) | Internal eval harness: dogfood incidents as a scored regression suite | Proposed |
| [0005](0005-transient-edits-in-investigate.md) | Transient edits in `investigate` tasks (net-zero-diff relaxation) | Accepted — implemented 2026-06-11 |
| [0006](0006-git-independent-project-scope.md) | Git-independent project scope for file discovery | Proposed |
| [0007](0007-dynamic-verification-plan-resolution.md) | Dynamic, no-dependency verification-plan resolution | Accepted — implemented 2026-06-11 |
| [0008](0008-non-executable-edit-verification.md) | Verification contract for non-executable edits | Proposed |
| [0009](0009-execution-sandbox-isolation.md) | Execution sandbox isolation | Proposed |
| [0010](0010-git-status-diff-as-model-tools.md) | `git_status` / `git_diff` as model-callable tools | Proposed |

## Conventions

- **Status:** Proposed → Accepted → (Superseded by ADR-N / Deprecated).
- **Numbering:** zero-padded, monotonic (`0001`, `0002`, …).
- **Scope:** one decision per ADR. If a decision changes, write a new ADR that supersedes the old one and link both ways.
