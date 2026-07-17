# Writing kit — "Your agent's shell command was never running in a shell"

> **This is a writing kit, not the post.** Narrative, evidence, code, and decisions for a
> 700–1,100-word article. Guardrails per `blog-candidates.md`: directional case-study, 4-question
> split. The measured base already exists as a research note —
> [`../../research/2026-07-10-shell-mangling-false-pass.md`](../../research/2026-07-10-shell-mangling-false-pass.md)
> — this kit adds the narrative, the design reasoning, and the review-found edges.

## TL;DR (the one-paragraph story)

Like most harnesses that avoid `shell=True` for sandboxing reasons, ours executes model-authored
commands as `shlex.split(command)` with `shell=False`. The model, of course, writes shell. The
operators don't error — they **pass through as literal argv words of the first program**, and the
result is silent semantic corruption in both loud and quiet forms. Quiet: a declared 10-section
`grep -q A DESIGN.md && grep -q B DESIGN.md && …` verification chain ran as **one grep** where
patterns 2–10 became unopenable *filenames*; `grep -q` exits 0 on the first match despite operand
errors, so verification **passed having actually verified 1 of 10 sections**. Loud: a frozen
heredoc check (`python3 - <<'EOF' …`) blocked forever on stdin no shell would ever feed, and the
model — unable to amend the frozen contract — spiraled for 24 turns seeking "one final
confirmation" until the iteration budget killed the run. The fix (ADR-0045) is **not** adding a
shell: it's a quote-aware syntax boundary (`argv_segments`, built on `shlex` with
`punctuation_chars`) at every seam where the model authors a command — `&&` chains split into one
frozen check per segment; every other operator is rejected *model-correctably* with a steer to an
equivalent supported form. The executor stays dumb and sandboxed; the lie is caught at the boundary
where the model can still fix it.

## The narrative arc (story beats)

1. **The setup, sympathetically.** `shell=False` is the right call: no injection surface, the
   no-shell/sandbox seam is load-bearing (ADR-0042). Nobody "forgot" shell support.
2. **The quiet failure (the scary one).** Session 1 of the `tetris_glm` journal
   (`be46ea27…jsonl`, 403 events): the mangled `&&` chain exits 0 → `verification_end passed:
   true` (event 72) → `agent_end outcome: success` (event 74). The tool output even *says*
   `grep: &&: No such file or directory` — nothing read it. 1 of 10 declared sections verified.
   This corrupts the positive-evidence signal the whole verifier design rests on, and would poison
   any eval that treats journal outcomes as ground truth.
3. **The loud failure (the expensive one).** Session 2: the heredoc check hangs at the command
   timeout (the run's only failed tool call, event 83). The model diagnoses it correctly, fixes a
   real `rotate()` bug via a workaround file — then, distrusting a contract it watched hang and
   cannot amend, circles "one final confirmation" from turn 27 to turn 50: **50 consecutive
   `tool_call` decisions, zero `final_answer`**, dead at `max_iterations`, `incomplete`. The
   verifier never ran.
4. **Manufactured evidence, in passing.** Confirmation chains like
   `python3 test_logic.py && python3 -c "import tetromino, …"` reported `exit=0` while the import
   half **rode along as `sys.argv`** of the first script — never executed.
5. **The repro is one line** (from the research note):
   ```
   python3 -c "import shlex,subprocess; cmd=\"grep -q '^# A' D.md && grep -q '^## MISSING' D.md\"; print(subprocess.run(shlex.split(cmd),capture_output=True).returncode)"
   ```
   against a `D.md` containing only `# A` → exit **0** (shell semantics: exit 1).
6. **The design space (why not just add a shell?).** `shell=True` reopens injection + widens the
   sandbox surface; silently stripping operators lies to the model in a new way; accepting-and-
   warning still executes the wrong thing once. The chosen shape: **move the check to the
   boundary** — parse the command *as the model meant it*, then either normalize it into supported
   semantics or reject it while the model can still correct (rejections at declaration time are
   model-correctable errors, the cheap kind).
7. **The mechanism.** `shell_syntax.argv_segments`: quote-aware lexing via `shlex` with
   `punctuation_chars`; returns clean argv segments or a legible refusal naming the operator.
   Declared `a && b && c` → N separate frozen checks (conjunction preserved — every plan check is
   required); `||`/`;`/`|`/redirection/heredocs → rejection with a steer ("declare each command as
   its own check"; "put the script in a file and run the file"). Same gate at `run_command`.
   `Workspace.run` itself is untouched.
8. **The payoff, concretely.** The session-1 contract now yields 10 genuinely-graded checks; the
   session-2 heredoc dies at turn 3 with a steer instead of seeding a 24-turn spiral.
9. **The twist that makes it an essay (tie to PR #110):** the *same codebase* had just shipped a
   deliberately quote-**blind** splitter for the vacuity guard. Not an inconsistency — a rule.
   The vacuity guard is a **classifier** whose false negatives are backstopped by the executing
   floor, so it fails open and cheap. `argv_segments` feeds an **executor**, where a mis-parse
   *is* the failure, so it fails closed and precise. Same parsing problem, opposite failure
   directions, chosen by who consumes the output.

## Review-found edges (report these; they're part of the story)

From `PR-112-2026-07-10.md` (each verified empirically there):

- **Quoted all-punctuation arguments mis-split:** `grep -q '&&' script.sh` → two segments (shlex
  strips quotes, so a quoted operator is indistinguishable from a real one). Fails **closed**
  downstream (`grep -q` alone exits 2; the chain rejection trips in `run_command`), so no false
  pass — but the steer is misleading and the docstring initially overclaimed. Legible-rejection
  fix + property tests are the follow-up.
- **`&&` split loses short-circuit semantics** (confirmed P1): the verifier runs every plan check
  unconditionally, so `failing-check && mutating-command` executes the mutation, unlike shell.
- **The tier-3 planner-model fallback bypassed the gate** (confirmed P1): LLM-*proposed* checks
  weren't routed through `argv_segments` — the exact false-pass class, still open on the proposal
  path at review time.

The meta-point (feeds the arms-race saga kit): the boundary was right, and its own review
immediately found three more seams. Syntax boundaries are a *program*, not a patch.

## Honest caveats (pre-written)

- **One journal, one model family, one project.** The repro is deterministic, but the *frequency*
  of shell-idiom checks is model- and prompt-dependent.
- **The finalization-spiral causality is interpretation**, not measurement: the model's turns
  14/17/49 thoughts all return to the check it watched hang — plausible cause, stated as such in
  the research note.
- **Eval-integrity retro-caveat:** any prior run whose declared contract contained `&&` may carry
  the vacuous-pass pattern; re-examine before using as a baseline.
- **Follow-up status:** verify at draft time which review edges (short-circuit semantics, planner
  fallback, quoted-operator rejection) have landed, and report plainly.

## Mechanisms / code reference

- `avatar-harness/avatar/shell_syntax.py` — `argv_segments` (the boundary).
- `avatar-harness/avatar/tools/verification.py` — `_validate_checks` gate ordering (shell syntax →
  per-kind coverage); the `&&`-to-conjunction split.
- `avatar-harness/avatar/tools/commands.py` — the `run_command` chain rejection.
- `avatar-harness/avatar/workspace.py` — `Workspace.run` (unchanged; the no-shell seam).
- `tests/test_shell_syntax_boundary.py` — both journal failure modes, pinned hermetically.

## Artifacts & references

- **Research note (measured base + repro):** `docs/research/2026-07-10-shell-mangling-false-pass.md`.
- **ADRs:** `docs/adr/0045-shell-syntax-rejected-at-command-boundaries.md` (the decision + rejected
  alternatives); ADR-0042 (why `Workspace.run` stays no-shell); ADR-0038 (the declared-contract
  stage it protects).
- **PR:** #112 (merged 2026-07-11) — ADR-0045 is one of its three increments; review
  `PR-112-2026-07-10.md` incl. the verified addendum.
- **Journal:** `~/Repos/tetris_glm/events/be46ea27…jsonl` (403 events, two sessions; not committed
  — quote event 72/74 and the turn-27–50 spiral).
- **Contrast artifact:** PR #110 / [`vacuity-guard-false-lesson.md`](vacuity-guard-false-lesson.md)
  (the quote-blind classifier — beat 9's other half).

## Suggested angles / titles / hooks

- **Lead hook (HN-shaped):** *"My agent's verification passed. The tool output literally said
  `grep: &&: No such file or directory`. It had verified 1 of 10 things — and reported success."*
- **Title options:** "Your agent's shell command was never running in a shell" · "The quiet false
  pass: `shlex.split` vs. everything your model learned from bash" · "Don't add a shell — add a
  boundary" · "`&&` is a lie your agent tells without knowing it."
- **The reusable principle:** *if you exec model-authored strings with `shell=False`, shell syntax
  isn't a missing convenience — it's silent semantic corruption. Parse at the boundary: normalize
  what you can preserve exactly, reject the rest legibly while the model can still correct, and
  pick the failure direction by consumer (classifiers fail open, executors fail closed).*
- **Fit in the spine:** the strongest bite-sized standalone of the wave (NC5-class: unique, ready,
  hard to nitpick — web discourse on agent tooling barely touches argv-vs-shell semantics). Path C
  material; also advances rung B by removing a false-pass confound from journal-as-ground-truth.
  Chapter 2 of the arms-race saga if published as a series.

## The 4-question scaffold (fill these in the draft)

1. **What did we measure?** One two-session dogfood journal: a vacuous verification pass (1/10
   verified, reported success), a heredoc hang → 24-turn finalization spiral → `incomplete`, and
   `&&`-chained confirmation commands whose second halves never ran; plus a deterministic local
   repro of the mangling.
2. **What artifact proves it?** The research note (event ids, the repro one-liner), the journal,
   and `tests/test_shell_syntax_boundary.py` replaying both modes hermetically.
3. **What did we infer?** No-shell execution + shell-authoring models = a lying seam; the fix is a
   quote-aware boundary that normalizes-or-rejects at declaration time, not a shell and not silent
   stripping; guard failure-direction should be chosen by consumer.
4. **What could still be wrong?** Frequency is model-dependent (n=1 family); spiral causality is
   interpretive; review edges (short-circuit, planner fallback, quoted operators) may still be
   open; prior `&&`-era baselines are suspect.
