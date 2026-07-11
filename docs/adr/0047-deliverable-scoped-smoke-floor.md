# ADR 0047 — The greenfield smoke floor scopes to the deliverable, excluding model-authored scaffolding (amends ADR-0014/0038)

- **Status:** Proposed
- **Date:** 2026-07-11
- **Deciders:** Sarthak Joshi
- **Related:** [ADR-0014](0014-greenfield-self-authored-verification.md) (the model-authored smoke floor), [ADR-0038](0038-model-declared-semi-frozen-verification-contract.md) (the immutable floor beneath a declared contract), [ADR-0046](0046-verification-steers-in-every-mode.md) (which stopped a floor failure from being laundered to `success`, making the floor's *file set* load-bearing). Seams: `avatar/planner.py` (`_SMOKE_SYSTEM`, `propose_smoke_check`), `avatar/runner.py` (`_maybe_add_smoke_floor`, which passes `state.files_modified`).

## Context

The greenfield floor (ADR-0014) is authored by the model: `propose_smoke_check` excerpts the files a run wrote and asks the model to author ONE non-executing check (`py_compile`/`ruff`/`node --check`/…) that the harness then runs, grading on the real exit code. The candidate file set is `state.files_modified` — the union of every successful write/edit's `files_changed` (`runner.py`), which is **undifferentiated**: a delivered source file, a throwaway manual-test script, and a scratch file all land in the same set. The `_SMOKE_SYSTEM` prompt then said *"Reference the files just written"*, actively pointing the model at the whole set.

In `tetris_grok2/events/e97dc153a547400bac58dbc87e6736bd.jsonl` (2026-07-10, final turn) the model wrote `verify_arrow_keys.py` as a throwaway script to manually check its own work, ran it, and left it in the tree. It entered `files_modified`; the smoke prompt handed it back as a "file just written"; the model authored `python -m py_compile input_handler.py verify_arrow_keys.py`; the scratch file did not compile, and the floor failed. So the immutable anchor gated on **scaffolding the user never asked for**, not on the deliverable.

The floor's *stated purpose* (ADR-0038) is a floor under the **deliverable** — "success can never drop below *it compiles/parses*." Its *implemented scope* was "everything the run wrote." Those diverge precisely when the model creates its own scratch files, which the harness workflow actively encourages (manual verify scripts run through `run_command`). Under ADR-0046 (a failing floor is no longer laundered to `success`) this divergence now has teeth: it can steer a run to exhaustion over disposable scaffolding.

## Decision

The floor scopes to the **deliverable**, enforced at *authoring time* (the model already authors the command, so the lever is the prompt, not a new classification axis). `_SMOKE_SYSTEM` is tightened to instruct the model to:

- name **only** the files that constitute the deliverable — the artifact the task was asked to produce and the code reachable from its real entry point; and
- **exclude** its own throwaway scaffolding — scratch files and ad-hoc `verify_*` / `test_*` scripts or manual harnesses it wrote to check its own work are not the deliverable and must not gate it (a broken scratch file must never fail the floor).

`state.files_modified` remains the *candidate* context (the model still sees what it wrote); the change is that the prompt no longer implies "name all of them." The allowlist safety gate (`_is_safe_smoke`: non-executing checkers, workspace-confined) is unchanged.

## Alternatives considered

- **Harness-side filter on `files_modified`** (drop paths matching a scratch pattern, or keep only files referenced by the declared contract / reachable from an entry point). Rejected: "scratch" has no reliable marker — both deliverables and throwaway scripts are written through the same `write_file` seam — so any heuristic is fragile and would either miss real scratch files or exclude genuine deliverables. The model, which wrote the files, is the best-placed judge of which are the deliverable.
- **Keep the full modified set (status quo); a broken `.py` left in the tree *is* a real regression of "it compiles".** Rejected as the floor's contract: gating `success` on the model's disposable test harness conflates "my scaffolding is broken" with "my change is broken," and it is non-deterministic (the model chooses whether to name the scratch file). A model that leaves broken scratch files is a tidiness issue, not a reason to fail the deliverable's floor.
- **Deterministic floor (harness authors `py_compile <all deliverable sources>` with no model call).** Rejected here: it re-raises the same "which files are the deliverable" problem the harness cannot answer deterministically, and discards ADR-0014's cross-stack reach (the model picks the right checker for the language). Revisit only if authoring-time scoping proves unreliable in eval data.

## Consequences

- The floor anchors what the task delivered, not what the model scribbled to test it. The `tetris_grok2` false-floor-failure stops recurring for the common "model wrote a throwaway verify script" shape.
- Scoping is model judgment, so it is not a hard guarantee — a model may still misjudge. That is acceptable: the floor is a *floor* (a lowest-precedence sanity check), the allowlist still bounds what it can run, and eval data over floor outcomes will show whether the authoring constraint holds. If it does not, the deterministic-floor alternative is the fallback.
- No change to the candidate file set, the freeze boundary, `_maybe_add_smoke_floor`'s decline/declared cases, or the allowlist — this is a targeted authoring-prompt change plus the ADR-0046 invariant that makes floor scope matter.
