# ADR 0045 — Shell syntax is rejected, not interpreted, at command boundaries; `&&` normalizes to check conjunction

- **Status:** Proposed
- **Date:** 2026-07-10
- **Deciders:** Sarthak Joshi
- **Related:** [ADR-0038](0038-model-declared-semi-frozen-verification-contract.md) (the declared contract these commands enter), [ADR-0044](0044-declared-change-kinds-per-kind-vacuity-rulebooks.md) (the per-kind rulebooks that now judge post-split segments), [ADR-0042] (the sandbox seam that makes no-shell execution load-bearing), [ADR-0007] (frozen plan). Seams: `avatar/shell_syntax.py` (the shared gate), `avatar/tools/verification.py` (`declare_verification`/`alter_verification`), `avatar/tools/commands.py` (`run_command`), `avatar/workspace.py` (`run` — unchanged, deliberately).

## Context

`Workspace.run` executes every command as a single argv with **no shell**: `shlex.split` + `subprocess.run(shell=False)`, wrapped by the ADR-0042 sandbox. That is deliberate — it is what makes commands sandboxable, injection-proof, and replayable. But `&&`, `|`, `;`, redirects, and heredocs are *shell language*, not exec features. `shlex.split` is only a tokenizer: it resolves quotes and hands operators through as literal words, so a chained command runs its **first** program with the rest of the line as that program's literal arguments.

Until now nothing enforced this at the boundaries where the *model* authors commands, and one dogfood journal (`tetris_glm/events/be46ea273029486fbc62ac5360a6c82f.jsonl`, 2026-07-10) showed both failure modes in a single file:

1. **Silent false pass.** A declared/amended contract froze `grep -q '^# ASCII Tetris' DESIGN.md && grep -q '^## Overview' DESIGN.md && …` (10 sections). Executed argv-style this is ONE grep whose later patterns become unopenable *filenames* — and `grep -q` exits 0 on the first match even when other operands error. The verifier graded exit 0 → `verification passed` → `outcome: success`, having actually verified 1 of 10 declared sections. Self-certification laundered through a tokenizer quirk.
2. **Hang → distrust → burned run.** The next session froze a heredoc check (`python3 - <<'EOF' …`). `python3 -` reads the program from stdin; no shell ever feeds the heredoc body, so it blocked to the command timeout. The model had watched a check in its own unamendable frozen contract hang, verified via a workaround file instead, and circled "one final confirmation" for ~24 turns without proposing completion — the run died on `max_iterations` as `incomplete`.
3. **Manufactured evidence.** The model's ad-hoc `run_command` chains (`python3 test_logic.py && python3 -c "import …"`) reported `exit=0` while the second command never executed (it rode along as `sys.argv` of the first).

The internal inconsistency: the planner already *classifies* per `&&`/`||`/`;` segment (PR #110, ADR-0044) and emits one `PlannedCheck` per segment for planner-proposed lines — but the declared path froze raw chains, and execution honored none of the operators. The harness understood chains at judgment time and mangled them at run time.

## Decision

A shared gate (`avatar/shell_syntax.py: argv_segments`) is applied at every **model-authored command boundary**, *before* a string can reach `Workspace.run`:

1. **`&&` normalizes to conjunction.** A chained declaration splits — quote-aware, via `shlex` operator lexing, never the planner's regex (which is deliberately quote-blind for classification) — into one `PlannedCheck` per segment. Verification semantics are exactly the shell's (`all segments must exit 0`), each segment is graded and evidenced individually, and execution finally matches the per-segment classification.
2. **Every other operator is rejected, never mangled.** `;`, `|`, `||`, redirects, and heredocs have no no-shell equivalent the harness is willing to emulate (`||` is alternation — it cannot become required checks). `declare_verification` / `alter_verification` reject with a model-correctable steer (§10): *declare each command as its own check; for multi-line logic, write a script file and declare `python <file>`*. The rejection lands at declaration time — the cheap feedback point — instead of freezing a contract that can only mis-execute.
3. **`run_command` rejects all operators, chains included.** A chain "working" while running only its first program is manufactured evidence; the tool's own description always said "no shell metacharacters". One command per call.
4. **`Workspace.run` itself is unchanged.** The gate lives at the model-input boundaries, not the execution seam: harness-owned commands (configured `test_command`/`lint_command`, the floor) are human-authored, and the sandbox/replay properties of pure-argv execution stay intact.

## Alternatives considered

- **Teach `Workspace.run` shell-operator semantics** (interpret `&&`/`|`/redirects itself, or route through `sh -c`). Rejected: `sh -c` reopens the injection/quoting surface ADR-0042 sealed, and a partial hand-rolled interpreter is a shell with fewer tests. The no-shell execution model is a feature; the defect was accepting input written for a different model.
- **Lint/dry-run declared checks at freeze time instead of declaration time.** Rejected: the freeze is a runner-internal moment with no model turn attached — rejection there burns context legibility. Declaration is where ADR-0038/0044 already push back model-correctably; this extends the same rulebook. (A freeze-time executability dry-run also cannot distinguish "fails because the code doesn't exist yet" from "can never run" for greenfield contracts.)
- **Reject `&&` too (strict one-command-per-check).** Rejected: chains are the natural way models (and CI files) express conjunction — the journal and the ADR-0044 dogfood both show it — and conjunction has an exact, lossless no-shell normalization. Rejecting it re-imports the burn-a-turn friction PR #110 removed, for zero integrity gain.

## Consequences

- A declared `&&` chain now yields N frozen checks (`declared_1…declared_N`); journals show the split segments in `verification_plan_frozen`, and each segment passes or fails on its own evidence. The tetris_glm false pass becomes a legible per-section failure.
- Models get steered at the moment of authorship; the frozen contract can no longer contain a command `Workspace.run` cannot honor — removing both the vacuous-pass hole and the hang-then-spiral trigger observed in the journal.
- Tier-3 LLM plan proposals route through the same gate (PR #112 review): a cited `a && b` splits into chained per-segment checks; a proposal with any other operator is discarded (detection-only degradation), never frozen. Deterministic tiers 1–2 already split per segment at proposal time.
- Split segments share a `PlannedCheck.chain` id and the verifier stops a chain at its first failure (PR #112 review): shell short-circuit is preserved — a failing segment still guards a later mutating one — and a skipped segment reports `fail` with "not run" evidence, never a vacuous pass. `chain` is optional-with-default, so older journals parse unchanged.
- Bare quoted operator arguments (`grep -q '&&' f`) are rejected legibly (PR #112 review): posix lexing strips the quotes, making them indistinguishable from real operators — a quote-preserving probe catches them before the split instead of silently mis-splitting.
- Eval-integrity: prior runs graded against chained declared contracts may contain vacuous passes (see the research note of 2026-07-10) — baselines built on them should be re-examined before Phase-4 comparisons treat them as ground truth.
