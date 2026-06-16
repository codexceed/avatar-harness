# ADR 0023 — Two-package uv workspace: the `avatar` SDK and the `jo` cockpit, flat layout

- **Status:** Accepted — implemented 2026-06-16 (workspace + rename in PR1; `jo` extraction stacked in PR2)
- **Date:** 2026-06-16
- **Deciders:** Sarthak Joshi
- **Related:** ADR-0013 (`evals/` stays an in-repo package, *not* a separate distribution — the contrasting call); `jo-cli/CLAUDE.md` (the consumer → core boundary this split formalizes); `HARNESS_DESIGN.md` §2 (MVP scope), §5 (shells). Supersedes the implicit single-package layout shipped through v1.2.0.

## Context

The cockpit (`jo-cli`) was always *designed* as a reference coding agent built **on** the harness — "one consumer of the core," with a clean consumer → core import direction and its own entry point. But it shipped as a **subpackage inside the `avatar-harness` distribution** (`src/avatar_harness/tui/`, behind the `[textual]` extra). The intent (a standalone, independently-publishable consumer) and the reality (a bundled module wearing a console-script hat) had drifted: the only way to get `jo-cli` was to install `avatar-harness`.

Two further frictions surfaced while resolving this:

1. **Layout.** The `src/avatar_harness/` layout used the verbose import root `avatar_harness` and the `src/` prefix. We want the conventional flat layout and a short, namesake import (`avatar`).
2. **Release.** Release automation (release-please) was wired for a single package at the repo root. A second distributable package needs multi-package release configuration, and both should be independently versioned and releasable.

The architecture was already prepared for the split — nothing outside `tui/` imports `tui/`, the cockpit consumes only the public surface, and it owns its launcher — so this is a **packaging** decision, not a re-architecture.

## Decision

Restructure the repository into a **uv workspace with two distributable members**, executed as two stacked PRs:

| Member | Distribution (PyPI) | Import package | CLI command | Lives in |
| --- | --- | --- | --- | --- |
| SDK / engine | `avatar-harness` (unchanged) | `avatar` | `avatar` | `avatar-harness/` |
| Reference cockpit | `jo-cli` | `jo` | `jo` | `jo-cli/` |

Concretely:

1. **Virtual workspace root.** The repo-root `pyproject.toml` declares `[tool.uv.workspace]` and owns the shared dev toolchain + all lint/type/test config; it ships nothing itself. `evals/` (dev tooling, per ADR-0013) and a unified `tests/` stay at the root.
2. **Flat layout + rename.** `src/avatar_harness/` → `avatar-harness/avatar/` (member dir + flat import package); the import root `avatar_harness` → `avatar` everywhere. The **distribution** name stays `avatar-harness` (no PyPI disruption; dist ≠ import, like `pillow`/`PIL`); the batch CLI command is renamed to its namesake `avatar`.
3. **`jo` extraction** (PR2). `avatar-harness/avatar/tui/` → `jo-cli/jo/`; `jo-cli` declares `dependencies = ["avatar-harness"]` via `[tool.uv.sources] avatar-harness = { workspace = true }`, owns the `textual`/`rich` runtime deps and the `jo` entry point. The `jo-cli` console script and the `[textual]` extra are removed from `avatar`. The four symbols the cockpit reached for via submodule paths (`DecisionError`, `resolve_log_path`, `update_latest_pointer`, `DirtyWorkspaceError`) are promoted into `avatar.__all__` so the reference consumer depends only on the public surface.
4. **Multi-package release.** `release-please-config.json` / `.release-please-manifest.json` track each member by path. `avatar-harness` keeps its existing bare `v<version>` tag scheme (no disruption to its tag history); `jo-cli` uses component tags (`jo-cli-v<version>`). The two schemes never collide, so neither package's tags need migrating.

## Alternatives considered

- **Keep `jo-cli` bundled in `avatar-harness`.** Rejected — it contradicts the stated design (a standalone reference consumer) and forces every SDK install to carry Textual/Rich, or hides the cockpit behind an extra that can never be installed on its own.
- **Two separate repositories.** Rejected — the cockpit is co-developed with the core and is the primary dogfooding surface; a single repo keeps them in lockstep, shares one CI gate and test suite, and lets `jo` track core changes via a workspace path dependency. A workspace gives separate *distributions* without separate *repos*.
- **Keep the `src/` layout / `avatar_harness` import.** Rejected per the explicit preference for the conventional flat layout and a short namesake import. `src`-layout's import-hygiene benefit is preserved well enough by the editable workspace install + pinned first-party config.
- **Core at the repo root, only `jo` under a member dir (asymmetric "Layout A").** Rejected in favor of symmetry: both distributions are members under their own directories, so neither is privileged and the root is a pure workspace coordinator.
- **Rename the distribution to `avatar` too.** Rejected — no upside and it would orphan the existing `avatar-harness` PyPI name and tag history.
- **Why this differs from ADR-0013 (which kept `evals/` in-repo, not split).** `evals/` is dev-only tooling (~a dozen files) with no external consumers — packaging it would be pure overhead. `jo` is a *shipped product* meant to be installed independently by users. The trade-off that argued against a separate distribution for `evals` argues *for* one here.

## Consequences

- **Positive.** The cockpit becomes installable on its own (`pip install jo-cli`); the SDK sheds its Textual/Rich extra; the consumer → core boundary is now enforced by package boundaries, not just convention; each package versions and releases independently; the import surface (`avatar`) is short and conventional.
- **Negative / cost.** A large one-time mechanical rename (≈475 references); contributors must learn the workspace layout (`avatar-harness/`, `jo-cli/`, root-level `tests/`+`evals/`); the gate runs deptry per-member (the virtual root declares no deps). `evals/` dependency hygiene is no longer deptry-gated (it is dev tooling, not a distribution) — an accepted reduction.
- **Follow-ups.** PyPI publishing is still not wired for either package (the pre-existing release flow stops at GitHub Releases + version bump); adding `uv publish` needs PyPI Trusted Publishing and is a separate change.
