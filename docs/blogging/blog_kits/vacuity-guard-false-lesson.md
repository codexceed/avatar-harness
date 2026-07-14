# Writing kit — "The guardrail that taught my agent a lie"

> **This is a writing kit, not the post.** It collects the narrative, evidence, code, and decisions
> for later distillation into a 700–1,200-word article. Follow the `blog-candidates.md` guardrails:
> directional case-study (one journal, one project), and the 4-question split (*what measured · what
> proves it · what inferred · what could still be wrong*).

## TL;DR (the one-paragraph story)

Our harness has a non-vacuity guard on model-declared verification checks (ADR-0038): reject a
declared check that provably doesn't exercise the deliverable (`echo OK`, `test -f file`). The guard
judged only the **first token** of the command line. In a dogfood run
(`tetris_glm/events/7e49b161…jsonl`), the model declared the idiomatic
`printf 'q' | python3 -m ascii_tetris.main --no-raw` — drive a TUI's stdin, run the real entry
point — and the guard rejected it as "vacuous (doesn't run the code)" because `printf` is a
denylisted no-op. The fallout was worse than a lost turn: the model burned a **tier-3 human
approval** on a replacement that was then accepted **by parse accident** (an env-assignment regex
popped `out=$(printf`, so the judged "program" was the literal string `jjj q` — simply not on the
denylist), and the model **internalized the false verdict** — its amendment rationale calls its own
correct check "vacuous." The guard didn't just cost resources; it *taught the model a wrong lesson
inside its own context*. The fix (PR #110): judge vacuity **per pipeline stage and per segment**
(one real stage redeems the line), and — after the PR's own review found the mirror-image failures —
judge it **per contract** (one executing check redeems the contract). The design principle: a
**lower-bound guard on model-authored text must fail open**; precision belongs to the verifier's
immutable floor, not the lexical gate.

## The narrative arc (story beats)

1. **The intent was right.** ADR-0038 lets the model declare its own verification contract, with a
   guard so it can't declare `echo done` and call that verification. Reasonable, load-bearing.
2. **The heuristic was shallow.** `vacuous_declared_check` passed the whole line to
   `effective_invocation`, which classifies token 0 only — violating that function's own documented
   precondition ("one command segment, no chaining").
3. **The false rejection.** `printf 'q' | python3 -m ascii_tetris.main --no-raw` condemned without
   ever looking at the `python3 -m` stage that runs the deliverable.
4. **The triple cost.** (a) A burned repair turn re-authoring a correct check. (b) A tier-3 human
   approval spent on an `alter_verification` amendment that should never have been needed. (c) The
   epistemic hit: the model's amendment rationale *agrees with the guard* that the original check
   was vacuous. A guard verdict is in-context training signal — a false verdict is a poisoned lesson
   the model carries for the rest of the run.
5. **The accidental acceptance (the guard was wrong in both directions).** The replacement,
   `out=$(printf 'jjj q' | python3 …); echo "$out" | grep …`, passed only because the
   env-assignment-stripping regex mis-parsed it. Meanwhile `grep -q Overview DESIGN.md` — pure
   inspection, zero execution — was judged "real."
6. **The fix.** Split the line into `&&`/`||`/`;` segments and `|` pipeline stages
   (`_split_segments` + new `_vacuous_segment`); reject only when **every** stage is vacuous.
   Read-only inspectors (`grep`, `rg`, `head`, `tail`, `wc`, `sleep`) join `_VACUOUS_PROGRAMS` so
   inspection-only chains stay rejected regardless of token order.
7. **The review finds the residue (same PR, second red/green pair).** Two mirror-image holes:
   *(a) bypass via unlisted builtins* — `grep -q X || exit 1` and `command -v pytest && grep -q X`
   were accepted, since unknown programs count as real and `exit`/`command` were unlisted; shell
   builtins/probes and more inspectors joined the denylist, while execution-capable wrappers
   (`env`, `eval`, `xargs`, `find`) deliberately stayed off so mis-parses keep failing open.
   *(b) burn-a-turn recreated one level up* — a legitimate two-check contract
   (`[pytest, grep-the-artifact]`) was rejected for its grep member, because checks were judged
   per-command. `_validate_checks` now judges the **contract whole**: one executing check redeems
   it; only an all-vacuous contract is rejected, and the error steers toward adding an executing
   check.
8. **The principle, made explicit.** The quote-blind split can only mis-split toward *accepting* —
   chosen deliberately, because this is a lower-bound gate. The real anti-vacuity anchor is the
   verifier's immutable floor (ADR-0014/0038), which actually runs the artifact. Lexical guards
   filter garbage; they must never be sharp enough to cut real work.

## The evidence table (from the journal, pinned as tests)

| Command (from the `7e49b161` log) | Old verdict | Correct verdict |
| --- | --- | --- |
| `printf 'q' \| python3 -m ascii_tetris.main` | vacuous ❌ | real (the pipe stage runs the app) |
| `echo starting && pytest -q` | vacuous ❌ | real |
| `grep -q Overview DESIGN.md` | real ❌ | vacuous (inspection, not execution) |
| `test -f X && grep … && echo OK` | vacuous ✅ | vacuous — but only by luck of `test` coming first |
| `grep -q X \|\| exit 1` *(review round)* | real ❌ | vacuous (inspector + unlisted builtin) |

`test_vacuous_declared_check_judges_every_segment` pins the exact commands from the log — including
the turn-5 amendment, now accepted on the strength of its `python3` stage, not by parse accident.
Red commit first per the TDD protocol; full suite 668 passed at merge.

## Why "the model learned a lie" is the post's spine (not the parser bug)

The parser fix is a dime-a-dozen bug story. The durable insight is about **what guard verdicts do to
an agent**: every rejection message enters the model's context as ground truth about its own
behavior. A false "your check is vacuous" does three things a crash never does — it wastes a repair
turn, it spends scarce human-approval attention on a non-problem, and it **rewrites the model's
belief about what good verification looks like** for the remainder of the run (observable here: the
amendment rationale echoes the guard's wrong claim). Harness-integrity discourse is almost entirely
about guards that are too lax (self-certification, reward hacking). This is the counterweight case:
a guard that is too strict doesn't just add friction — it *gaslights* the agent, and the damage is
legible in the journal.

## Design rules to extract (the reusable checklist)

- **Match the guard's failure direction to its role.** A lower-bound "is this obviously garbage"
  gate must fail open; only the executing verifier (the floor) gets to fail closed.
- **Judge the unit the model actually authored.** The model declares a *contract* (N checks, each
  possibly multi-stage); judging token 0 of check i is answering a different question than the one
  asked. Vacuity moved token → stage → segment → contract before it matched the model's unit.
- **Denylist conservatively; never denylist wrappers.** `env`, `eval`, `xargs`, `find` can execute
  anything — listing them as vacuous would create fail-closed mis-parses.
- **Rejection messages are steering, not just errors.** The all-vacuous rejection now says *what to
  add* (an executing check), because the message is the model's only recovery path.
- **A guard that burns a human approval is a guard bug, not model noise.** Tier-3 attention is the
  scarcest resource in an attended run; the journal makes each wasted approval visible.

## Honest caveats (the "what could still be wrong" section, pre-written)

- **n=1 journal, one project, one model family.** This is failure-mode discovery, not a frequency
  claim about how often over-strict guards bite.
- **The guard is still lexical.** A real program that ignores its arguments and exits 0 passes the
  gate — deliberately; that's the immutable floor's job to catch. Don't oversell the gate.
- **Fail-open has a cost, stated plainly:** previously-rejected vacuous lines like `echo "a | b"`
  are now accepted (quote-blind split), and a `\|&` pipe yields an unlisted-stage acceptance. The
  PR's docstring records these as the accepted trade — the floor backstops them.
- **The epistemic-damage claim rests on one artifact** (the amendment rationale echoing the false
  verdict). It's a strong artifact — quote it verbatim — but call it an observation, not a measured
  effect.

## Mechanisms / code reference

- `avatar-harness/avatar/planner.py` — `vacuous_declared_check`, `_vacuous_segment`,
  `_split_segments`, `_VACUOUS_PROGRAMS`, `effective_invocation`.
- `avatar-harness/avatar/tools/verification.py` — `_validate_checks` (contract-level judgment; the
  steering rejection message).
- `tests/test_planner.py::test_vacuous_declared_check_judges_every_segment` — the journal commands,
  pinned.

## Artifacts & references

- **PR:** #110 `fix(planner): judge declared-check vacuity per segment, not the line's first token`
  (merged 2026-07-09 into `feat/declared-verification-contract`); review-round commits `f925a2f`
  (red) + `27807b4`.
- **Review:** `PR-110-2026-07-09.md` (repo root) — found both mirror-image holes (beats 7a/7b) plus
  an unrelated `.env` wall-clock bug fixed in `b712f0c`; evidence that adversarial review is part of
  the same loop.
- **ADRs:** `docs/adr/0038-model-declared-semi-frozen-verification-contract.md` (the guard's
  charter); ADR-0014 (the immutable floor that backstops fail-open).
- **Journal:** `~/Repos/tetris_glm/events/7e49b161…jsonl` (interactive cockpit dogfood run; not
  committed — quote excerpts, including the amendment rationale).

## Suggested angles / titles / hooks

- **Lead hook (HN-shaped):** *"My agent declared a correct verification check. My guardrail called
  it fake, made a human approve a replacement — then the agent started believing the lie."*
- **Title options:** "The guardrail that taught my agent a lie" · "False rejections are worse than
  false passes (sometimes)" · "Your agent believes your error messages" · "Fail open at the gate,
  fail closed at the floor."
- **The reusable principle:** *a guard's rejection message is in-context training data. Before you
  make a guard stricter, ask what lesson a false rejection teaches — and make lower-bound gates
  fail open, keeping the strictness in the layer that actually executes the work.*
- **Fit in the spine:** Path C (incidents) material; the counterweight to the oracle-gaming thread
  (`06` / `self-certification-arms-race`) — integrity work errs strict, this is the cost of
  over-steering. Pairs naturally with the shell-syntax kit (same journal family, opposite failure
  direction).

## The 4-question scaffold (fill these in the draft)

1. **What did we measure?** One dogfood journal: a false rejection, the wasted turn + tier-3
   approval, the parse-accident acceptance, and the model's amendment rationale echoing the false
   verdict.
2. **What artifact proves it?** The `7e49b161` journal excerpts; the before/after verdict table
   pinned in `test_vacuous_declared_check_judges_every_segment`; PR #110 + its review file.
3. **What did we infer?** Lower-bound guards on model-authored text must fail open and judge the
   model's authored unit (the contract); rejection messages steer the model, so false ones do
   lasting in-context damage; precision belongs to the executing floor.
4. **What could still be wrong?** n=1; the guard remains lexical (floor backstops it); fail-open
   readmits some vacuous lines by design; the "learned a lie" effect is one observed artifact, not
   a measured rate.
