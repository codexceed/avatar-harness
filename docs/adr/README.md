# Architecture Decision Records

Records of significant architectural decisions for avatar-harness. Each ADR captures the context, the decision, and its consequences at a point in time — ADRs are immutable once accepted; supersede rather than edit.

Format: Nygard-style with MADR touches. Flow/sequence detail is given in [Mermaid](https://mermaid.js.org/) (renders on GitHub and in most editors).

| # | Title | Status |
| --- | --- | --- |
| [0001](0001-async-event-bus-and-durable-execution.md) | Async lifecycle event bus, two-plane UX integration, and durable execution | Accepted (durable execution deferred to 3.3) |
| [0002](0002-interactive-tui-cockpit-and-mvp-feature-set.md) | Interactive TUI cockpit and the MVP coding-agent feature set | Accepted (implemented; D3 revised 2026-06-10) |
| [0003](0003-file-creation-transport.md) | A robust transport for file creation (and large mutations) | Accepted (A + B implemented) |
| [0004](0004-internal-eval-harness.md) | Internal eval harness: dogfood incidents as a scored regression suite | Accepted (Eval-0, PR #47; scoring revised to option-A; amended by ADR-0020) |
| [0005](0005-transient-edits-in-investigate.md) | Transient edits in `investigate` tasks (net-zero-diff relaxation) | Accepted — implemented 2026-06-11 |
| [0006](0006-git-independent-project-scope.md) | Git-independent project scope for file discovery | Proposed |
| [0007](0007-dynamic-verification-plan-resolution.md) | Dynamic, no-dependency verification-plan resolution | Accepted — implemented 2026-06-11 |
| [0008](0008-non-executable-edit-verification.md) | Verification contract for non-executable edits | Proposed |
| [0009](0009-execution-sandbox-isolation.md) | Execution sandbox isolation | Accepted (deferred) |
| [0010](0010-git-status-diff-as-model-tools.md) | `git_status` / `git_diff` as model-callable tools | Accepted (deferred) |
| [0011](0011-verifier-integrity-under-self-improvement.md) | Verifier integrity under self-improvement: protected, fingerprinted oracle and held-out checks | Proposed |
| [0012](0012-wire-level-api-mocking.md) | Wire-level API mocking for eval probes (mock the endpoint, not the client library) | Proposed |
| [0013](0013-evals-package-boundary-and-gates.md) | `evals/` stays an in-repo package, held to the harness quality gates via config | Accepted |
| [0014](0014-greenfield-self-authored-verification.md) | Greenfield self-authored verification (the no-contract floor) | Accepted — implemented 2026-06-14 |
| [0015](0015-string-anchored-edit-transport.md) | String-anchored editing (`str_replace`) as the primary edit transport | Proposed |
| [0016](0016-autonomous-approval-disposition.md) | Autonomous approval disposition: unattended runs deny `ask`s by default | Accepted — implemented 2026-06-15 |
| [0017](0017-multi-turn-history-as-chat-turns.md) | Multi-turn conversation history as real chat turns | Accepted — implemented 2026-06-15 |
| [0018](0018-hide-whole-journal-directory.md) | Hide the whole journal directory from the agent's file tools | Accepted — implemented 2026-06-15 |
| [0019](0019-provider-agnostic-tool-schemas.md) | Provider-agnostic tool schemas: no `prefixItems`-without-`items` (the tuple trap) | Accepted — implemented 2026-06-15 |
| [0020](0020-guard-probes.md) | Guard probes: a no-leak check is necessary, not sufficient | Accepted — implemented 2026-06-15 |
| [0021](0021-case-insensitive-sensitive-path-denylist.md) | Case-insensitive sensitive-path denylist (close the case bypass) | Accepted — implemented 2026-06-15 |
| [0022](0022-unobtainable-as-terminal-conclusion.md) | Legitimize "unobtainable" as a terminal conclusion (the won't-conclude fix) | Proposed |
| [0023](0023-two-package-workspace-avatar-sdk-jo-cli.md) | Two-package uv workspace: the `avatar` SDK + the `jo` cockpit, flat layout | Accepted — implemented 2026-06-16 |
| [0024](0024-evals-driven-improvement-loop.md) | Evals-driven improvement loop: two human-gated workflows over a deterministic core | Proposed |
| [0025](0025-persist-journal-refined-failure-mode.md) | Persist the journal-refined failure bucket on `ResultRow` (one classification, one source of truth) | Accepted — implemented 2026-06-18 |
| [0026](0026-bounded-concurrency-in-the-eval-runner.md) | Bounded concurrency in the eval runner (thread pool over hermetic cells, opt-in) | Accepted — implemented 2026-06-19 |
| [0027](0027-sandboxed-execution-trust-and-self-verification-calibration.md) | Sandboxed execution trust + self-verification calibration (Eval-0) | Proposed (R3 implemented 2026-06-20) |
| [0028](0028-transport-retry-and-request-timeout.md) | Transport-layer retry + request timeout for model calls (NUL/hang resilience) | Proposed — R1–R4 implemented 2026-06-20; R5 → ADR-0029 |
| [0029](0029-streaming-idle-timeout-for-model-calls.md) | Streaming idle-timeout for model calls (ADR-0028 R5) | Accepted — implemented 2026-06-21 |
| [0030](0030-interruptible-runs-via-async-model-client.md) | Interruptible runs via an async model client (cancellable in-flight model calls) | Accepted — core implemented 2026-06-17; extended by 0028/0029 |

## Conventions

- **Status:** Proposed → Accepted → (Superseded by ADR-N / Deprecated).
- **Numbering:** zero-padded, monotonic (`0001`, `0002`, …).
- **Scope:** one decision per ADR. If a decision changes, write a new ADR that supersedes the old one and link both ways.
