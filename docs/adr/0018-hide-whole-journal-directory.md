# ADR 0018 — Hide the whole journal directory from the agent's file tools

- **Status:** Accepted — implemented 2026-06-15
- **Date:** 2026-06-15
- **Deciders:** Sarthak Joshi
- **Related:** ADR-0001/0002 (event journal, interactive cockpit); `HARNESS_DESIGN.md` §13 (observation vs. control), §15 (workspace confinement); invariant #5 (everything observable, on a path-confined `Workspace`). Supersedes the journal-hiding scope decided inline in `Workspace` (previously: hide only the active log + its `latest.jsonl` pointer). Surfaced by a dogfood cockpit session.

## Context

The harness writes its event journal under the workspace by default (`events/<session_id>.jsonl` plus an `events/latest.jsonl` pointer), and the workspace root defaults to the launch directory — so the journal lives *inside* the tree the agent operates on. `Workspace` hid that file from the file tools (`search_repo`/`list_files`/`read_file`) so the agent could not read its own event log.

But it hid **only the current session's two files**. A directory that accumulates journals across runs — exactly the dogfood case — leaves every *sibling* session's `events/<other_id>.jsonl` fully listable and readable. In a dogfood sitting the agent did just that: it `list_files`'d the tree, found prior-run journals, and read one, leaking the harness's own trajectory data back into the agent's context (and sending it into a confused loop chasing a non-existent `events/latest.jsonl`). The earlier choice explicitly declined to hide the whole `events/` directory "since a real project may legitimately own one" — but that left the leak open.

## Decision

**Hide the entire journal *directory* from the file tools** — every file under it (all sessions' journals + the `latest.jsonl` pointer), matched as a path prefix — whenever that directory is a real subdirectory of the workspace. When the journal sits *directly* in the workspace root (e.g. `--log ./run.jsonl`), fall back to hiding only its file and pointer, never the whole workspace.

`_journal_ignores` now returns a `(files, dirs)` pair; `Workspace.is_ignored` returns `True` for an exact file match **or** any path under an ignored directory. The hide still governs `read_file` (raises `FileNotFoundError`), `list_files`, and `search_repo`, exactly as before — only the *scope* widened.

## Consequences

- The leak is closed: the agent can no longer enumerate or read any session's event journal, not just the current one.
- Trade-off: if a real project legitimately owns the *same* directory the harness journals into (default `events/`), that directory is now hidden from the agent. This is an acceptable default — the harness is actively writing there, so the project and the journal already collide. The escape hatch is `--log <path>` to point the journal somewhere the agent should not see (or directly in the root, which hides only the journal pair). Relocating the journal *outside* the workspace entirely would remove the trade-off but changes the advertised `events/<id>.jsonl` layout; deferred as a larger, user-facing change.
- The guard (`journal_dir != root`) ensures a root-level `--log` can never blank the whole workspace.

## Alternatives considered

- **Keep hiding only the active file + pointer (status quo).** Rejected: it is the leak.
- **Hide nothing; rely on the agent not to read journals.** Rejected: not observable-safe, and the dogfood proved the agent will read them.
- **Write the journal outside the workspace by default.** The cleanest root-cause fix (no in-tree journal, nothing to hide), but it changes the documented log location and the `--log` default, and touches `update_latest_pointer`. Deferred to a future ADR if the directory-hide trade-off proves limiting.
