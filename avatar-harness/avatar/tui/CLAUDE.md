# CLAUDE.md — `tui/` (the cockpit)

Package-scoped guidance for the interactive Textual cockpit. For the whole-package picture
(component graph + the goal/approval/plan/render flows, with diagrams), read
[`./ARCHITECTURE.md`](./ARCHITECTURE.md) first. The root [`../../../CLAUDE.md`](../../../CLAUDE.md)
still governs the repo-wide rules (commits, the doc map, ADRs); this file narrows to what's
load-bearing inside this directory.

## Scope & boundary

The cockpit is **one consumer of the core**, not part of it. Consume the public surface —
`Harness` / `ReplSession` / `Session` (and the typed `HarnessEvent`s) — and **never make the
core import the TUI**. The import direction is strictly **consumer → core** (`cli.py` docstring);
nothing under `avatar-harness/avatar/` outside `tui/` may import from `tui/`. The cockpit owns its
own launcher (`jo-cli`) precisely because the harness is an independent core under many consumers.

## Load-bearing invariants for this package

- **Observer + control-caller only (§13).** Observation flows OUT via `events()`; control flows
  IN via the modals → `resolve_approval` / `cancel`. **Control never flows through `events()`** —
  an `ApprovalRequested` event only *announces*; the decision returns through `resolve_approval`.
  The cockpit never sits inside the loop the runner awaits.
- **Behind the `[textual]` extra.** `import avatar` must work without `textual`, so keep
  heavy/Textual imports out of the core import path — reach the app only through `load_cockpit()`
  (`__init__.py`), which guards the import with a clear install hint. `replay.py` carries no
  Textual import on purpose, so it stays usable wherever events are.
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
| `app.py` | `CockpitApp` — the shell (status bar + transcript + input); observe vs drive modes; `_handle`/`_format`/`_format_decision`/`_write`; the goal / plan / approval / cancel flows. |
| `cli.py` | `jo-cli` — the cockpit's entry point; builds a journaled `ReplSession` and runs the app. |
| `modals.py` | `ApprovalModal` / `DiffModal` / `PlanModal` and their typed results (`ApprovalChoice`/`PlanChoice`). |
| `replay.py` | `ReplaySession` — an engine-free fixed event stream for tests / a future `--replay`. |
| `__init__.py` | `load_cockpit()` — the guarded import behind the `[textual]` extra. |
| `ARCHITECTURE.md` | The package architecture map (diagrams of the two planes + every flow). |

## Run / test

```bash
jo-cli                                   # launch the cockpit (needs the [textual] extra)
uv run pytest tests/test_cockpit.py      # the cockpit tests (test_cockpit*.py)
make check                               # lint + typecheck + full suite — run before committing
```
