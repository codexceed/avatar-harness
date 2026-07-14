# Writing kit — "My eval was wrong five times before any model was"

> **This is a writing kit, not the post.** Narrative, evidence, and decisions for a
> 800–1,200-word article. Guardrails per `blog-candidates.md`: directional case-study, 4-question
> split. The measured base is
> [`../../research/2026-07-11-tetris-tui-eval-development.md`](../../research/2026-07-11-tetris-tui-eval-development.md)
> (design record + four dated addenda — every number below is cited there). Sibling kit:
> [`vacuity-guard-false-lesson.md`](vacuity-guard-false-lesson.md) (the same lesson, one layer
> down, in the harness's own guard).

## TL;DR (the one-paragraph story)

We built a single-shot eval where an agent must write a playable ASCII Tetris — graded by a
deterministic probe that *plays it like a human*: real ANSI arrow bytes in, rendered frames out,
differential assertions, a packing planner that clears a line and checks the score to the point.
Then we ran it against nine models, and **the grader was wrong five times before any model was**:
three README checks that rejected legitimate phrasing ("Size: **10** columns × **20** rows",
arrow keys documented as `ESC [ D`, then as literal `←` glyphs), a frame-count assertion that
failed games for rendering a farewell frame on `q` (a compliant reading the goal never pinned),
and a goal so under-specified about streaming that a top model's slurp-stdin-then-replay
implementation passed every batch phase and deadlocked the interactive one. A sixth defect ran
the other way — a false *pass*: two matrix-passing games rendered a diagonal "staircase" on any
real terminal (`tty.setraw` + bare `\n`), invisible to every pipe-driven check until human
screenshots caught it; a 40-line terminal emulator over a stdlib pty closed it. The punchline is
measurable: **three models flipped from FAIL to PASS with zero capability change** — the only
delta was a sentence added to the task spec. The failures that survived every spec fix (a
bag-drawn-in-reverse RNG, iteration-budget exhaustion, malformed tool calls) were the ones that
were always real.

## The narrative arc (story beats)

1. **The setup.** A README-grounded Tetris task; a nine-phase probe that boots, moves, rotates,
   drops, quits, tops out, packs a line clear, and (eventually) watches the screen — no LLM
   judge, no agent-provided hooks, everything pinned in the goal.
2. **Artifact #1–2 (dev cells, same day):** both first cells died at phase 0 on *wording* —
   a board documented as "10 columns × 20 rows" missing a `10\s*x\s*20` regex; arrow keys
   documented as their byte forms missing the word "arrow". Fix: normalize markdown, match
   semantically. (The false-rejection failure mode we'd already shipped a harness fix for —
   PR #110's vacuity guard — reappearing in our own grader within 48 hours.)
3. **Artifact #3 (the streaming hole):** "render one frame after every key" is satisfiable by
   reading ALL of stdin first and replaying — batch phases can't tell (they close stdin). One
   sentence in the goal ("process keys as they arrive") and the same model's next cell passed:
   `sys.stdin.buffer.read()` became `read(1)`. Specification, not capability.
4. **Artifact #4 (the farewell frame):** 4 of 9 first-matrix failures were `expected 17 frames,
   got 18` — games that render one last frame on `q`. The goal never said not to. The probe now
   ends count-sensitive phases on EOF; both readings pass.
5. **Artifact #5 (the arrow glyphs):** a README wrote ``Left (`←`)`` — the most human notation
   possible — and failed the (already once-fixed) arrow pattern. Fixed; and beneath it the cell
   *still* failed, for its true reason (see beat 7).
6. **The inverse defect (the staircase):** screenshots showed two matrix-*passing* games
   rendering diagonally in a real terminal — `tty.setraw` disables `\n → \r\n` translation and
   they wrote bare `\n`; the one visually-correct game had used `setcbreak`. Pipes have no tty:
   structurally invisible to the whole probe. Fix: run the interactive mode under stdlib
   `os.openpty`, reconstruct the screen with a minimal emulator (`\r`/`\n`/CSI cursor
   addressing — so cooked-mode, `\r\n`-writing, and curses UIs are all graded fairly), assert
   board rows vertically aligned. The failing cells' diagnosis lists the exact staircase
   columns from the screenshot: `[35, 47, 59, 71, 83, 95]`.
7. **What survived is the signal:** a 7-bag drawn in *reverse* (`shuffle` then `pop()` — the
   Python-idiomatic O(1) pop beating the written words "drawn in order") in 4 cells across two
   unrelated model families; iteration-budget exhaustion with no deliverable; a model burning
   its failure budget on schema-invalid tool calls in 25 seconds. Genuine, reproducible,
   model-attributable.
8. **The scoreboard, honestly:** first 5-model matrix recorded pass@1 = 0.40; re-graded after
   artifact fixes = 0.67; three models at pass^3 = 1.0. Every correction is a dated addendum in
   the research note; the recorded results files were never rewritten.

## The reusable principles (pick 2–3 for the post)

- **A probe may only fail behavior the goal pins.** Every false rejection traced to asserting
  something the task description never said. The spec and the grader are one artifact; edit
  them together.
- **False rejections are the expensive direction.** A false pass costs you a wrong number; a
  false rejection costs the *model's* run, teaches would-be readers "model X can't do Y," and
  (in an agent harness) burns real budget. Grade documentation semantically; grade behavior
  exactly.
- **Development cells are the probe's unit tests.** Golden + counter-examples prove a probe
  *can* pass and *can* fail; only real model output tells you it fails for the right reasons.
  Budget for N throwaway cells before trusting any matrix number.
- **"Human-like" has a floor you can automate cheaply.** A pty + a 40-line terminal emulator
  caught what every pipe-based assertion missed — you don't need a browser-grade harness to
  check "does it render for a person."
- **Spec-sensitivity is itself a capability measurement.** Which failures dissolve when the
  spec is one sentence sharper (streaming, terminal modes) vs. which persist against explicit
  words (bag order) cleanly separates "under-specified task" from "model didn't read."

## Honest caveats (pre-written)

- n=3 seeds per model, one task, one project; pass@1 deltas are directional, not a leaderboard.
- The re-graded matrix numbers come from deterministic replays against kept workspaces; the
  canonical baseline against the final nine-phase probe had not been re-run at kit-writing time.
- The staircase check grades presentation + `q`-exit only — interactive *gameplay* remains
  ungraded (documented limit).
- "Three models flipped on spec alone" is 3 paired observations (grok/streaming, sol/terminal
  mode, sol+grok/farewell-frame re-grade) — a pattern, not a controlled experiment.

## Artifacts & references

- **Research note (the citable base):** `docs/research/2026-07-11-tetris-tui-eval-development.md`
  — design record, two dev cells, two matrix waves, all five artifacts + the staircase, the
  combined 27-cell table, repro commands.
- **Task + probe:** `evals/tasks/tetris-tui.toml` (the pinned goal),
  `evals/probes/tetris_tui_smoke.py` (nine phases; the emulator is ~40 lines at the bottom).
- **Probe validity:** `tests/test_evals.py` section M — golden, nine flip counter-examples, one
  pinned-passing tolerated variant.
- **Result files:** `evals/results/20260711T230139Z.jsonl` (wave 1), `…T233131Z.jsonl` (wave 2),
  plus the single-cell runs cited in the note. Kept workspaces under `eval_run_*/` (local).
- **The lineage:** PR #110 (`vacuity-guard-false-lesson.md` kit) — the identical lesson in the
  harness's own declaration guard, two days earlier.

## Suggested angles / titles / hooks

- **Lead hook (HN-shaped):** *"I built a deterministic grader that plays Tetris like a human.
  It rejected correct games five times before any model shipped a real bug — and the fix was
  never code, it was a sentence."*
- **Title options:** "My eval was wrong five times before any model was" · "The probe may only
  fail what the spec pins" · "Grading a TUI like a human (without an LLM judge)" · "False
  rejections are the expensive direction."
- **Fit in the spine:** the practitioner-facing companion to NC7 (same lesson, grader-side) and
  a concrete instance of blog 01's scaffold-not-model thesis — here it's *task-not-model*. Also
  quietly advances the NC3/oracle-integrity thread: five artifacts in a probe *we* wrote and
  tested is the base rate to remember when someone proposes trusting un-audited graders.

## The 4-question scaffold (fill these in the draft)

1. **What did we measure?** Two dev cells, two 3-seed matrix waves (27 cells, 9 models), five
   probe-artifact fixes each verified by replay against kept workspaces, and three
   spec-only FAIL→PASS flips.
2. **What artifact proves it?** The research note's addenda (dated, with run stamps), the
   result JSONLs, the kept workspaces, the counter-example tests, and the staircase screenshots
   reproduced as probe diagnoses.
3. **What did we infer?** Graders fail in both directions; the goal text is part of the grader;
   semantic documentation checks + exact behavior checks; a cheap pty emulator closes the
   pipes-can't-see-terminals gap; surviving failures are the real capability signal.
4. **What could still be wrong?** Small n; single task; re-grade vs re-run distinction; the
   presentation check's narrow scope; unknown sixth artifact (the pattern so far suggests
   assuming there is one).
