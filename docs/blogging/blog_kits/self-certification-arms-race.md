# Writing kit — "One task, four models, four ways my harness let the agent grade itself"

> **This is a writing kit, not the post.** Narrative, evidence, and decisions for a 900–1,400-word
> saga article (this one earns extra length; cut chapters before cutting evidence). Guardrails per
> `blog-candidates.md`: directional case-study, never a leaderboard; 4-question split throughout.
> Sibling kits: [`vacuity-guard-false-lesson.md`](vacuity-guard-false-lesson.md) (chapter 1 in
> depth), [`shell-syntax-boundary.md`](shell-syntax-boundary.md) (chapter 2's execution half).

## TL;DR (the one-paragraph story)

Our harness's load-bearing invariant is *"done is a proposal the verifier disposes of; the model
never self-certifies."* We dogfooded **one recurring objective** — "build ASCII Tetris," run
interactively in the cockpit — against different model families over three days. Each model found a
**different hole** in that invariant: a vacuity guard that condemned real checks and passed fake
ones (`tetris_glm` → PR #110); a declared `grep && grep && …` contract that executed as *one* argv
and verified 1 of 10 sections while reporting success, plus change-kind mislabeling that dodged
execution checks entirely (`tetris_glm` again → PR #112, ADR-0044/0045); an *advisory* interactive
mode that laundered failing verdicts — including a failing **immutable floor**, after a human
approved the amendment — into `outcome="success"` (`tetris_grok2` → PR #113, ADR-0046/0047); and a
misrouted fix-task that could edit but never run or keep anything, thrashing 43 turns until a human
killed it (`tetris_grok3`/`grok4` → PR #114, ADR-0048/0049). Four PRs, six ADRs, and an arms-race
lesson: every fix's own adversarial review found the failure class re-emerging in a new shape. The
through-line: verification stopped being a model-authored checklist and became a **governed
protocol** — declared appropriately, executed faithfully, enforced on failure, and reachable even
when the run starts in the wrong mode.

## The frame that makes it a post, not a changelog

**A repeated dogfood objective is a cheap integrity fuzzer.** Same goal, same harness, different
models: each model's idioms and failure styles probe different seams. GLM's shell-idiomatic checks
found the parser seams; Grok's conversational runs found the authority and routing seams. The
methodology beat — *"we didn't design these tests; we replayed one task until the harness stopped
lying"* — is what a reader can reuse tomorrow. Every chapter follows the same anatomy: journal
evidence → hermetic red test → narrowly-scoped invariant → ADR (with rejected alternatives) →
adversarial review that finds the residue. That anatomy **is** the project's Phase-4 posture.

## Chapter 1 — the guard was lying to the model (PR #110, merged 2026-07-09)

The non-vacuity guard on declared checks judged only the line's first token: it rejected the real
`printf 'q' | python3 -m ascii_tetris.main` (cost: a repair turn + a tier-3 human approval + the
model *internalizing* the false verdict), then accepted the replacement by parse accident. Fix:
judge per pipe-stage/segment, then — after review — per *contract*. Full kit:
[`vacuity-guard-false-lesson.md`](vacuity-guard-false-lesson.md). **Beat to keep:** guards that are
too strict don't add safety, they teach lies; this chapter is the counterweight that keeps the saga
honest about *both* failure directions.

## Chapter 2 — the contract was semantically hollow (PR #112, merged 2026-07-11; ADR-0044/0045)

Two independent holes from the same `tetris_glm` repo:

- **Executed unfaithfully (ADR-0045).** `Workspace.run` execs `shlex.split(command)` with
  `shell=False`; a declared 10-section `grep -q A DESIGN.md && grep -q B DESIGN.md && …` chain ran
  as **one grep** with the other 9 patterns as unopenable filenames — `grep -q` exits 0 on the
  first match despite operand errors → `verification_end passed: true`, **1 of 10 sections actually
  verified**. Fix: a quote-aware shell-syntax boundary (`shell_syntax.argv_segments`) at every
  model-authored command seam — `&&` splits into one frozen check per segment; other operators are
  rejected model-correctably with a steer. Full kit:
  [`shell-syntax-boundary.md`](shell-syntax-boundary.md); research note
  `docs/research/2026-07-10-shell-mangling-false-pass.md` has the one-line repro.
- **Declared inappropriately (ADR-0044).** One evidence rule for all changes let a content-only
  check cover a code change. Fix: the model declares `change_kinds` (`code`/`content`); each kind
  gets its own vacuity rulebook at declaration time, and a verifier-owned `change_kind_coverage`
  check audits the declaration against the **actual final diff** — under-declaring fails. Murky
  cases fail toward the stricter (`code`) rulebook.
- **Consent hardening rider:** `alter_verification` became permanently ungrantable — no
  "always-allow this session" for contract amendments; enforced at two seams in the core `Session`
  (grant storage and grant matching), so a hand-built grant is inert.

**Arms-race beat (the review, `PR-112-2026-07-10.md`):** the fix's own review found the class
re-emerging — `.txt` classified behavior-bearing config (`requirements.txt`) as `content`,
coverable by a bare `pip install` (contradicting ADR-0044's own murky-fails-toward-code rule);
`check_covers_content('python -c "print(\'README.md\')"')` passed the content rulebook without ever
reading the artifact; the `&&` split silently dropped shell short-circuit semantics; and the tier-3
planner-model fallback path bypassed the new gate entirely. Each verified empirically in the review
addendum. The hole-fix-hole rhythm is the saga's texture, not an embarrassment — say so.

## Chapter 3 — the verdict had no authority (PR #113, merged 2026-07-11; ADR-0046/0047)

The starkest journal (`tetris_grok2/events/e97dc153…jsonl`): interactive verification was
*advisory*, so **3 of 5 turns emitted `verification_end passed=false` immediately followed by
`agent_end outcome="success"`**. On the final turn the model amended the contract via tier-3
`alter_verification` — **the human approved** — the immutable floor (the one guarantee ADR-0038
says can never be weakened) *still* failed (`['floor']`), and the turn was still reported
`success`. Fix (ADR-0046, superseding the advisory stance of §23.5/ADR-0002): the verifier steers
in **every** mode; a failing verdict always feeds the repair loop; conversational exhaustion ends
**`blocked` with an open question handed to the human** — a first-class hand-off, never a fake
`success`; a floor failure can no longer be reported as success in any mode. The eval harness's
external-grading behavior survives only as an explicit `advisory` flag so baselines don't silently
shift. ADR-0047 scoped the smoke floor to the *deliverable*, so the model's own throwaway
`verify_*` scaffolding can't poison the floor (the other grok2 regression).

**Beat to keep:** "advisory verification" sounds humble; in the journal it reads as the harness
co-signing the model's self-certification — with a human's approval spent as cover.

## Chapter 4 — the right loop was unreachable (PR #114, merged 2026-07-11; ADR-0048/0049)

`tetris_grok3`: a pasted traceback ("fix this") classified as `investigate`. Over ~43 turns the
model made 7 *successful* `str_replace` edits (transient by the investigate contract), could never
run the code (`run_command` was phase-gated out), could never keep the edits (net-zero-diff
contract), and looped re-reading files until a human killed it. **The affordances lied** — edits
succeeded, the needed capability was invisible, no lever existed. Fix (ADR-0048): `run_command`
admitted during investigation (side effects contained in the baseline diff), plus a consent-gated,
one-way, once-only `switch_to_editing` escalation (tier-3, ungrantable) with a harness-side thrash
detector that **nudges** the model toward the lever rather than auto-escalating; eager
baseline-clean plan resolution so a file planted mid-investigation can't be frozen as its own
rubric. `tetris_grok4` added the sibling (ADR-0049): an edit task claiming done with *no* edit and
no contract froze an empty plan and thrashed through `alter_verification` ×3; the declaration gate
now also fires at the claim-done boundary — while `declare_verification` is still reachable — and
the naive alternative ("allow declare during editing") was rejected *in the ADR* because a tier-0
declare could replace a frozen contract, bypassing tier-3 consent.

**Arms-race beat (the review, `PR-114-2026-07-11.md`):** phase-only gating reopened both fought
classes inside the same PR — `switch_to_editing` false-succeeded on tasks that can't escalate (a
lying affordance, the PR's own thesis), and `run_command`-in-investigation opened a planted-contract
window on the plain-edit path; the thrash detector measured `git status` (pre-existing dirt) instead
of the pinned-baseline diff; and ADR-0048 as first written described a phase-jumping mechanism the
implementation deliberately doesn't have.

## The through-line (write it once, at the end)

- **Declared appropriately** — change-kind rulebooks; murky fails strict (ADR-0044).
- **Executed faithfully** — what the guard judged is what the runtime runs (ADR-0045).
- **Enforced on failure** — the verdict steers in every mode; floors can't be laundered (ADR-0046/0047).
- **Reachable from the wrong mode** — misrouted tasks escalate with consent instead of thrashing (ADR-0048/0049).

And the two-sided tension that frames it: a harness too trusting lets the model self-certify
(chapters 2–4); a harness too strict gaslights the model (chapter 1). These four PRs are one
project learning, empirically, where that line sits.

## Honest caveats (pre-written)

- **All evidence is n=1-per-journal, one project, interactive dogfood** — failure-mode discovery,
  not frequency claims. No eval deltas are claimed for these fixes yet (that's the follow-up post
  when Phase-4 runs land).
- **The arms race is not over and say so:** several review findings shipped as follow-ups, not in
  the PRs (the `.txt` classifier hole, `&&` short-circuit semantics, the planner-fallback bypass,
  the thrash-signal fix). Check follow-up status at draft time and report it plainly.
- **Escalation legibility is half-delivered:** `TaskEscalated.trigger="thrash"` was unreachable at
  merge (the call site hardcodes `"model"`), so the journal can't yet distinguish a thrash-rescued
  run from a self-aware one. Don't overclaim the eval-signal payoff.
- **Eval-integrity retro-caveat** (from the research note): any *prior* run whose declared contract
  contained `&&` chains may carry the vacuous-pass pattern; re-examine before using as baselines.
- **Squash-merge caveat:** these landed on the `feat/declared-verification-contract` line, not yet
  `main` at kit-writing time; verify merge state before publishing.

## Artifacts & references

- **PRs:** #110 (2026-07-09) · #112, #113, #114 (all merged 2026-07-11, stacked; collapsed in
  reverse order into the `feat/declared-verification-contract` line).
- **Reviews (repo root):** `PR-110-2026-07-09.md`, `PR-112-2026-07-10.md` (incl. the 4-finding
  verified addendum), `PR-114-2026-07-11.md`.
- **ADRs:** 0038 (declared contract — the stage), 0044, 0045, 0046, 0047, 0048, 0049 under
  `docs/adr/`.
- **Research note:** `docs/research/2026-07-10-shell-mangling-false-pass.md` (measured trajectory
  analysis + repro).
- **Journals (not committed; quote excerpts):** `tetris_glm/events/7e49b161…jsonl`,
  `tetris_glm/events/be46ea27…jsonl`, `tetris_grok2/events/e97dc153…jsonl`, `tetris_grok3`,
  `tetris_grok4`.
- **Test counts at merge (suite growth as a proxy for pinned behavior):** 668 (#110) → 730 (#113)
  → 742 (#114).

## Suggested angles / titles / hooks

- **Lead hook (HN-shaped):** *"3 of 5 turns: the verifier said FAIL, the run reported SUCCESS. A
  human had just approved the amendment. Here's the three-day arms race that followed."*
- **Title options:** "One task, four models, four self-certification holes" · "The verifier said
  no; the harness said done" · "An arms race with my own harness" · "Verification as a governed
  protocol, not a model-authored checklist."
- **The reusable principle:** *"the model never grades itself" is not a feature you add — it's an
  invariant you defend at every seam (declaration, execution, disposition, routing), and your own
  dogfood journals + adversarial review are the cheapest fuzzer for finding the seams you missed.*
- **Fit in the spine:** this is the **harness-side prequel to the oracle-gaming flagship
  (NC3/blog 06)** — same Goodhart family, evidence already in hand; publish it as the
  flag-planting teaser amendment #3 calls for, and let it link forward to the demo when built.
  Draws on blog 00/04 concepts (verifier owns done) with the empirics they lacked.

## The 4-question scaffold (fill these in the draft)

1. **What did we measure?** Five dogfood journals of one objective across model families; per-hole
   trajectory evidence (false verdicts, a 1-of-10 verified "pass", success-after-floor-failure, a
   43-turn thrash); plus each PR's review findings, empirically verified.
2. **What artifact proves it?** The journals (event ids quoted in the PR bodies), the research
   note's repro one-liner, the hermetic regression tests replaying each journal, the review files.
3. **What did we infer?** Self-certification isn't one bug but a seam family; each seam needs its
   own guard with the right failure direction; adversarial review of the fix is part of the loop,
   not overhead.
4. **What could still be wrong?** n=1 per hole; follow-ups still open at kit time; no measured eval
   uplift yet; prior `&&`-era baselines suspect; merge-to-main pending.
