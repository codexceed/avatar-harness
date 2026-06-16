# CLAUDE.md — `jo` (the cockpit)

Package-scoped guidance for the interactive Textual cockpit — the standalone `jo-cli`
distribution (import `jo`, command `jo`). For the whole-package picture (component graph +
the goal/approval/plan/render flows, with diagrams), read [`./ARCHITECTURE.md`](./ARCHITECTURE.md)
first. The repo-root [`../CLAUDE.md`](../CLAUDE.md) still governs the repo-wide rules (commits,
the doc map, ADRs); this file narrows to what's load-bearing inside this package.

## Scope & boundary

The cockpit is **one consumer of the core**, not part of it — a separate distribution (`jo-cli`)
that depends on `avatar-harness`. Consume the public surface — `Harness` / `ReplSession` /
`Session` (and the typed `HarnessEvent`s), imported from the top-level `avatar` package — and
**never make the core import the cockpit**. The import direction is strictly **consumer → core**
(`cli.py` docstring); nothing under `avatar/` may import `jo`. The cockpit owns its own launcher
(`jo`) precisely because the harness is an independent core under many consumers (ADR-0023).

## Load-bearing invariants for this package

- **Observer + control-caller only (§13).** Observation flows OUT via `events()`; control flows
  IN via the modals → `resolve_approval` / `cancel`. **Control never flows through `events()`** —
  an `ApprovalRequested` event only *announces*; the decision returns through `resolve_approval`.
  The cockpit never sits inside the loop the runner awaits.
- **Depend only on the public surface.** Import from the top-level `avatar` package, not its
  submodules. `textual`/`rich` are this package's own runtime deps; reach the Textual app only
  through `load_cockpit()` (`__init__.py`) so `replay.py` (which carries no Textual import) stays
  usable wherever events are, without forcing a Textual import at package load.
- **Headless-testable.** Assert on the `rendered` line mirror and the status fields
  (`phase`/`outcome`/`verdict`), not on the rendered screen. Drive the UI with `ReplaySession`
  (a fixed event stream, no engine) and Textual's `Pilot` (`App.run_test()`) — **never snapshot
  the screen.** `_write` mirrors each line's plain text into `self.rendered: list[str]` for
  exactly this.
- **A worker exception must not tear down the app.** A goal that raises is caught and surfaced as
  a transcript line; the REPL stays alive (the dogfood crash a `DirtyWorkspaceError` once caused).

## Where things live

| File | What |
| --- | --- |
| `jo/app.py` | `CockpitApp` — the shell (status bar + transcript + input); observe vs drive modes; `_handle`/`_format`/`_format_decision`/`_write`; the goal / plan / approval / cancel flows. |
| `jo/cli.py` | `jo` — the cockpit's entry point; builds a journaled `ReplSession` and runs the app. |
| `jo/modals.py` | `ApprovalModal` / `DiffModal` / `PlanModal` and their typed results (`ApprovalChoice`/`PlanChoice`). |
| `jo/replay.py` | `ReplaySession` — an engine-free fixed event stream for tests / a future `--replay`. |
| `jo/__init__.py` | `load_cockpit()` — the guarded (lazy) import of the Textual app. |
| `ARCHITECTURE.md` | The package architecture map (diagrams of the two planes + every flow). |

## Run / test

```bash
jo                                       # launch the cockpit
uv run pytest tests/test_cockpit.py      # the cockpit tests (test_cockpit*.py — at the repo root)
make check                               # lint + typecheck + full suite — run before committing
```
