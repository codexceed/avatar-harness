# ADR 0041 — Retire the `test_only` task kind; subsume it into `edit` + a declared executing contract

- **Status:** Proposed
- **Date:** 2026-07-08
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8[1m]) — design discussion prompted by scoping the mandatory-pre-edit verification-declaration gate, which surfaced that `test_only` shares 100% of `edit`'s capabilities and differs only in its verification rubric.
- **Supersedes:** the 2026-06-06 decision (DECISIONS.md) that merged `task_kind` to three values (`edit | investigate | test_only`) — this removes `test_only`, leaving two.
- **Related:** [ADR-0038](0038-model-declared-semi-frozen-verification-contract.md) (the model-declared executing contract that now carries the test mandate); [ADR-0007](0007-dynamic-verification-plan-resolution.md) (plan resolution + freeze); [ADR-0011](0011-verifier-integrity-under-self-improvement.md); `HARNESS_DESIGN.md` §7 (`task_kind`) + §12 (verification contracts); `avatar/verifier.py` (`_verify_edit`/`_verify_test_only`), `avatar/state.py:114` (the `task_kind` union).

## Context

`task_kind` is defined (HARNESS_DESIGN §7) as **a taxonomy of *verification contracts*, not of user intents** — that is the stated reason there are only three. `investigate` earns its place: it is read-only, its contract is "cite real evidence, leave no diff," and forcing edit-shaped verification ("a diff must exist") onto it would be wrong. `test_only` does **not** clear that bar on inspection.

`test_only` shares **100%** of `edit`'s capabilities. Both are in `EDIT_KINDS` (`tools/base.py:41`); they expose the identical tool surface (`str_replace`/`write_file`; and in the editing/verifying phases, `run_tests`/`run_linter`/`run_command`). `test_only` is **not a distinct execution mode** — it is `edit` with a different verifier rubric. The rubric differs in exactly three ways (`verifier.py`):

1. the diff must touch a **test file** — `tests_changed` (`_is_test_path`, :304) rather than `diff_present` (:194);
2. a test command that **collects zero tests fails** rather than being a tolerated skip — `no_target_allowed=False` (:131) vs edit's exit-5 tolerance for `kind=="test"` checks (:116, :188, :242);
3. only `kind=="test"` checks count as **positive signal** — lint alone cannot pass it (:130, :141).

All three are *inversions of edit's tolerances* — they encode **required-ness**, not capability. An `edit` task can already write tests, run them, and be verified by them; `test_only` only makes tests *mandatory*.

Two developments make that required-ness better expressed elsewhere:

- **ADR-0038's model-declared executing contract already carries it, more rigorously.** A declared check is *harness-executed* (real exit code, never self-certified), and declared checks carry `kind="declared"` — which is **not** granted edit's exit-5 skip (the tolerance is gated on `check.kind == "test"`, `verifier.py:188`). So a declared `pytest test_foo.py` that collects zero tests **fails**, giving `test_only`'s constraint 2 for free. And a *passing* declared test check is behavioral proof that tests exist and run — stronger than constraint 1's `_is_test_path` heuristic, which a model can satisfy vacuously by touching a `test_`-named file that asserts nothing.

- **The mandate is fundamentally a greenfield concern, and there the declared contract nails it.** In a repo that *already has* a detected suite, `test_only` barely delivers: its check runs the whole detected suite, which collects the *existing* tests, so "must collect ≥1 test" passes regardless of whether *this change's* tests exist — leaving only `tests_changed` (a vacuously-satisfiable path heuristic) with real teeth.

So `test_only` is a coarse, weakly-enforcing specialization of `edit` whose one genuine guarantee ("new tests must actually run") is now expressed more precisely by a declared/detected *executing* check.

## Decision (proposed)

**1. Remove `test_only` from the `task_kind` union.** `task_kind` becomes `Literal["edit", "investigate"]` (`state.py:114`). Delete `_verify_test_only` (`verifier.py:128-141`) and every `test_only` branch/reference across the tree (verifier, `intent.py` classifier, `cli.py`/`harness.py` surface, `model_client.py` `_KIND_FRAMING`, `session_state.py`, `tools/base.py` `EDIT_KINDS`, `evals/spec.py`, and the tests that assert on it).

**2. Test-writing intent becomes an `edit` task with a test-based executing contract.** "Add tests for X" is an `edit` whose contract (detected, or model-declared per ADR-0038) is the command that runs those tests. The harness runs it and reads the real exit code, exactly as for any other contract. Because declared checks bypass the exit-5 skip, "the new tests must actually run" is enforced without a dedicated kind.

**3. If a mandatory-tests-atop-a-detected-suite requirement ever materialises, express it as a contract-level flag on `edit`, never a resurrected kind.** A `require_collection` (turn edit's tolerated exit-5 into a hard fail) and/or `require_test_diff` boolean is strictly more expressive than the enum — it composes, unlocking the combination impossible today: **one run that must ship both a working code change *and* passing new tests** (a run is exactly one `task_kind`). This is deferred: no current task needs it, and it should be built against a measured need, not ahead of one.

## Alternatives considered

| Option | Verdict |
| --- | --- |
| Keep `test_only` as-is | Rejected — it is `edit` + two mandatory constraints, not a distinct contract shape; it violates the "taxonomy of contracts, not intents" principle it was admitted under, and its one real guarantee is now better carried by a declared executing check. |
| Keep `test_only`, but only for greenfield | Rejected — greenfield is precisely where the ADR-0038 declared contract already subsumes it (and more rigorously); the kind adds nothing there. |
| Remove it *and* immediately add the `require_collection` flag | Rejected for now — YAGNI. The residual case (mandatory new tests over an existing suite) is narrow and today served only weakly by `test_only` anyway. Add the flag when a task demands it. |
| Fold `test_only` into `investigate` | Rejected — `investigate` is read-only (no-diff contract); test-writing mutates the tree. It belongs under `edit`. |

## Consequences

- **Simpler, truer taxonomy.** Two kinds — `edit` (mutating; contract = detected/declared checks) and `investigate` (read-only; contract = cited evidence + no diff). The "which tests, and must they run" question moves entirely into the *contract*, where it is executable and precise, and off the coarse kind axis.
- **The declared executing contract (ADR-0038) becomes the primary vehicle for the test mandate.** This tightens the coupling to ADR-0038: retiring `test_only` leans on the declared-contract gate actually being in place, so the two land together (or `test_only` removal follows it).
- **A previously impossible combination becomes reachable** (via the future flag): an `edit` that must both change behavior and ship passing new tests.
- **Migration surface.** Callers/evals passing `task_kind="test_only"` move to `edit` with a test contract; `intent.py`'s classifier drops the third label; `HARNESS_DESIGN` §7/§12 tables and `README` (if it advertises the kind) update. `DECISIONS.md` is frozen — this ADR is the superseding record, not an edit there. `test_only`-specific tests (`test_verifier.py`, `test_security.py`) are removed or rewritten as `edit` cases.
- **Lost:** the named-intent legibility of `--task-kind test_only` at the CLI/API boundary. Judged minor — the intent is now legible in the contract the task carries.
- **Verifier integrity is unaffected or improved.** The harness still owns attainment (runs the checks, reads exit codes); "tests must run" shifts from a kind-encoded skip-flag to a declared *executing* check the model proposes and the harness disposes of — the same definition-vs-attainment split ADR-0038 draws.
