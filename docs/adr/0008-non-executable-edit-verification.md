# ADR 0008 — Verification contract for non-executable edits

- **Status:** Proposed
- **Date:** 2026-06-11
- **Deciders:** Sarthak Joshi
- **Related:** `HARNESS_DESIGN.md` §12 ("Open edge — non-executable edits"); ADR-0007 (verification plan); ADR-0005 (net-zero-diff at the verifier).

## Context

§12 requires a positive external signal before `success` — a verifier never passes on zero evidence. An `edit` to a file with no tests *and* no meaningful lint/types (docs, a static config, an asset) has no command-based signal: tests skip, lint skips. Partly addressed today (a clean lint command counts as signal when no test target exists), but a truly non-executable, non-lintable edit still has no contract.

## Decision (proposed)

When neither a test nor a lint/type signal is available for an `edit`, fall back to a **weaker external signal**: a diff that parses/validates for its file type (e.g. JSON/YAML/TOML parses, Markdown is well-formed) plus the always-on secret/placeholder guard over the diff. When even that is unavailable, route to human confirmation (`ask_user` → `blocked`) rather than passing vacuously. Build when a dogfood run actually hits it (record the journal id here).

## Consequences / alternatives

- Keeps "never pass on zero evidence" intact for the file classes that have no command.
- *Rejected:* treating a non-executable edit as auto-pass (re-creates the vacuous gate); a new `task_kind` (this is a signal-tier fallback within `edit`, not a distinct contract).
