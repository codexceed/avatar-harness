# ADR 0015 — String-anchored editing (`str_replace`) as the primary edit transport

- **Status:** Proposed
- **Date:** 2026-06-15
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8) — design discussion 2026-06-15, prompted by repeated dogfood failures where the model could not produce a valid unified diff. In one `jo-cli` sitting, two consecutive follow-up goals ("add streaming/colors" then "try again") both ended `incomplete` having landed **zero** edits — every `apply_patch` failed, the model never took the `write_file` fallback, and it thrashed to the budget cap.
- **Supersedes:** the **modification** half of [ADR-0003](0003-file-creation-transport.md). ADR-0003 chose `write_file` (plain content via a structured arg) for *creation* but kept *modification* "diff-anchored" through `apply_patch`. This ADR replaces that diff anchor with a string anchor, extending ADR-0003's own argument (native tool-calls own the envelope, so the model never hand-escapes a patch) from creation to modification.
- **Related:** `HARNESS_DESIGN.md` §10 (clean-apply staleness), §5/§15 (reversible, inspectable edits); §21 (MVP tool set).

## Context

`apply_patch` takes a **git unified diff**, whose hunk headers demand exact line arithmetic: `@@ -<oldStart>,<oldCount> +<newStart>,<newCount> @@`, where `oldCount = context + deletions` and `newCount = context + additions`, counted across multiple overlapping hunks. Models get this wrong reliably, in two flavors we see again and again in the journals:

1. **No ranges** — a bare `@@`. Git: `No valid patches in input`. (Now caught with guidance, but it's a symptom.)
2. **Wrong counts** — e.g. a hunk declaring `@@ -77,6 +95,8 @@` whose body actually has 7 old-side and 9 new-side lines. Git: `corrupt patch at line N` / `patch fragment without header`. A single off-by-one corrupts the *whole* patch, and a miscounted early hunk desyncs git's parser so later, correctly-formed hunks also fail.

Mode 2 is not a formatting slip a better error message fixes — it is an **arithmetic-competence** problem intrinsic to the format. Frontier models reduce but do not eliminate it; under real-world noise (whitespace, long files, many hunks) it recurs. The harness already carries a `write_file(overwrite=true)` escape, but (a) it forces resending the whole file — costly and risky for large files — and (b) the model has to *choose* it mid-failure, which it empirically does not.

ADR-0003 deferred a non-line-numbered format under the "rule of three: translate only if better guidance doesn't stop the bleeding." The bleeding has not stopped — we are well past three incidents.

## Decision (proposed)

Make **string-anchored replacement the primary edit transport.** Introduce `str_replace`, a structured tool the model calls with native function-calling arguments — **no line numbers, no diff syntax**:

```
str_replace(path, old_string, new_string, replace_all=false)
```

The harness finds `old_string` in the current file and swaps it for `new_string`. This is the same family as Claude Code's own Edit tool, and it makes ADR-0003's reasoning whole: structured args mean the model never hand-writes a patch *and* never counts a line.

### Why this fits the invariants (it's a better fit, not a looser one)

| Invariant | How `str_replace` honors it |
| --- | --- |
| §5 — every edit is an **inspectable diff** | The diff becomes **purely derived**: the harness applies the swap, then `diff()` (git vs. the pinned baseline) renders it. The model stops *authoring* diffs entirely. |
| §10 — **clean-apply staleness** (read-before-edit) | `old_string` **is** the staleness proof. If it doesn't match the current file, the edit is rejected — exactly what diff-context matching did, minus the arithmetic. |
| §8/§2 — runner owns mutation; tools pure-ish via `Workspace` | Identical shape to `apply_patch`: routes through a new `Workspace.replace()` chokepoint (confinement + denylist + staging), returns a `ToolResult`. |

### The contracts (settled in review)

- **Uniqueness → error-back, model-correctable (§10).** `old_string` must resolve to exactly one match unless `replace_all=true`.
  - **0 matches** → `old_string not found … re-read and copy the exact text` (the stale/mistyped-anchor signal).
  - **N>1 matches** → `matches N locations … extend old_string with surrounding lines until it uniquely identifies ONE` (or `replace_all`).
  Both point at a fix the model *can execute* (widen a string it can see), unlike "corrupt patch at line 27."
- **Exact whitespace matching to start.** `old_string` must match byte-for-byte, indentation included. The failure (0 matches → re-read) is self-correcting. Whitespace-tolerant/fuzzy matching is deliberately deferred — add only if dogfood shows indentation drift dominates.
- **Atomicity.** All-or-nothing **per file**: validate the match before writing, so a rejected edit leaves the file byte-for-byte unchanged. Cross-*file* atomicity (which `apply_patch` could do in one call) is dropped — multi-file changes become sequential single-file edits (the Claude Code / Cursor model).
- **Empty/identical guards.** An empty `old_string` (would match everywhere) and `old_string == new_string` (no-op) are rejected up front with a model-correctable message.

### The resulting tool set — split by *altitude*, not by *mechanism*

The point of replacing `apply_patch` (rather than adding alongside it) is to avoid two tools that *both patch* — the choice-point that makes weaker models thrash. The end state is two write tools with an unambiguous boundary:

| Intent | Tool |
| --- | --- |
| create a new file | `write_file` (no `overwrite`) |
| change a span in place | **`str_replace`** (the default edit; anchor = staleness proof) |
| wholesale rewrite | `write_file(overwrite=true)` |

This is *fewer* competing choices than today (where the model must pick `apply_patch` vs. `write_file` for any modification).

## Alternatives considered

| Option | Verdict |
| --- | --- |
| Keep unified diff, improve guidance only | Rejected — guidance fixes mode 1 (bare `@@`), not mode 2 (count arithmetic), which is intrinsic to the format. |
| Marker-block SEARCH/REPLACE (Aider-style `<<<<<<<`/`=======`/`>>>>>>>`) | Rejected as the form — removes line numbers but reintroduces the in-blob delimiter escaping ADR-0003 rejected; structured args are strictly cleaner under native tool-calls. |
| Support OpenAI's `*** Begin Patch` context format | Rejected — line-number-free (its virtue) but still a hand-written text dialect to parse/validate; `str_replace` gets the same benefit through typed args. |
| Add `str_replace` **alongside** `apply_patch` | Rejected — two patch tools is the model-confusion failure this ADR exists to remove. `str_replace` *replaces* it. |
| `write_file(overwrite)` as the only edit path | Rejected — resending whole files is costly and risks mangling unrelated content; targeted edits need a targeted tool. |

## Consequences

- The line-arithmetic failure mode is **designed out** of the default edit path; both `incomplete` dogfood runs would have landed their edits.
- `apply_patch` is **superseded**, removed on the phased rollout below. The `*** Begin Patch` dialect guard and the malformed-hunk guard retire with it.
- The verifier, the diff/baseline pipeline, the permission gate (now keyed on the single `path` arg, not parsed diff targets), and the edit-intent phase bootstrap (`str_replace` is a tier-1 tool, so it advances the phase automatically) all carry over unchanged in shape.
- **New residual failure mode: whitespace-exact anchor misses.** Strictly more correctable than arithmetic (re-read vs. recompute), and bounded by the deferred option to add tolerance.
- **Cross-file atomicity is lost** (acceptable; see contracts).

## Rollout (phased, like ADR-0003)

1. **This PR** — land `str_replace` + `Workspace.replace()`, fully tested, registered, and advertised as the primary edit tool; steer `write_file`'s "modify with…" hint to it. `apply_patch` stays registered during migration so nothing breaks.
2. **Follow-up** — migrate the dogfood/eval suite and the ~7 `apply_patch`-based test files to `str_replace`; flip the `ContextBuilder` to stop advertising `apply_patch`; then **remove `apply_patch`** and its dialect/hunk guards. At that point the tool set is the two-row table above.

## Implementation notes (non-binding)

- `Workspace.replace(path, old, new, *, replace_all=False) -> str`: resolve + confine + denylist, read current text, count occurrences, raise `MatchNotFoundError` (0) / `AmbiguousMatchError` (N>1) / `FileNotFoundError` (no such file), else `text.replace(...)`, write, `stage([rel])`. Exceptions subclass a `ReplaceError(ValueError)`.
- `str_replace` tool (`tools/edit.py`): `permission_tier=1`, `phases={editing}`, `paths=lambda a: (a.path,)`; maps each `Workspace` exception to its model-correctable `ToolResult.error`.
- Trigger: now (active dogfood blocker).
