# ADR 0013 — `evals/` stays an in-repo package, held to the harness quality gates via config

- **Status:** Accepted
- **Date:** 2026-06-14
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8)
- **Related:** ADR-0004 (internal eval harness); `evals/README.md`; `pyproject.toml` (gate config); `Makefile` (gate invocation); Principle C (conservative complexity ceiling)

## Context

The Eval-0 harness (ADR-0004) lives in a root-level `evals/` package, deliberately **outside**
`avatar-harness/avatar` because it is dev tooling, not shipped code (the wheel only packages
`avatar-harness/avatar`). A side effect of that placement: `evals/` was held to a *weaker* quality bar
than the harness. `ruff` (lint + format) and `pytest` covered it, but the three remaining hard gates —
**pyrefly** (type), **pydoclint** (docstring/signature agreement), **deptry** (dependency hygiene) —
and the compile/import **smoke** were all scoped to `src` (or `src`+`tests`). So eval-harness code could
ship a type error, an undocumented argument, or an undeclared dependency that the gate would never catch.

Two ways to close the gap:

1. **Split `evals/` into a separate package** — its own `pyproject.toml`, a uv workspace member, its own
   lock/editable dependency on `avatar-harness`, and its own gate invocation.
2. **Keep one package; extend the existing gates to cover `evals/`** — a configuration change only.

## Decision

**Keep `evals/` as an in-repo package and extend the existing gates to cover it.** Do not split it out.

The evals *library* modules (`run`, `spec`, `score`, `stats`, `classify`, `diff`, `result`,
`provision`, `metrics`) are held to the **same** hard gate as `src`. Two sub-trees keep carve-outs,
mirroring the carve-outs `ruff` already grants them:

- **`evals/probes/`** — runtime scripts that import agent-generated modules (e.g. `import calc` from the
  scratch repo), unresolvable statically; excluded from pyrefly/pydoclint/deptry.
- **`evals/fixtures/`** — deliberately-imperfect sample repos (a buggy function, a secret file); input
  *data*, not maintained code; excluded everywhere.

Concretely:

- **pyrefly:** `project-includes = ["src", "tests", "evals"]`; `project-excludes` adds
  `evals/fixtures` + `evals/probes`.
- **pydoclint:** `exclude = '\.venv|evals/fixtures|evals/probes'`; invoked as `pydoclint src evals`.
- **deptry:** `known_first_party = ["evals"]` (it lives outside `src/`, so its intra-package imports
  otherwise read as a missing third-party dep) + `extend_exclude = ["evals/fixtures", "evals/probes"]`;
  invoked as `deptry src evals`.
- **smoke:** `compileall -q src tests evals`.

## Alternatives considered

- **Separate package / uv workspace member:** rejected. evals is ~11 dev-only files whose only
  non-stdlib dependency is the harness itself — a second build target, lockfile, and cross-package
  editable dependency is machinery ahead of need (Principle C: no abstraction until a second concrete
  case exists). The "not shipped" boundary is already enforced by placement outside `src/` and the
  wheel's `packages` list, so a split buys no isolation; it would only make the import story worse.
  Revisit if evals ever grows real third-party dependencies of its own that must not leak into the
  harness's dependency graph.
- **Leave evals on the weaker bar:** rejected — it lets eval-harness regressions (the thing that exists
  to catch harness regressions) ship unguarded.

## Consequences

- `make check` now type-checks, docstring-checks, dependency-checks, and compile-checks the evals
  library alongside the harness; a regression there fails the same gate.
- The probe/fixture carve-outs are declared in one place each (per tool), consistent with the existing
  `ruff` per-file-ignores — a new probe or fixture is covered by the path glob automatically.
- No new build artifact, lockfile, or workspace topology; the dependency graph is unchanged.
