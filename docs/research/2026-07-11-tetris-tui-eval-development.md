# `tetris-tui` eval development — two single-model cells, two task defects fixed, two genuine model defects caught

**Date:** 2026-07-11
**Status:** measured — trajectory analysis of the task's first two development cells (one cell per
model, seed 0), plus manual probe replays against the kept workspaces. Task/probe fixes derived
from these cells landed on `feat/declared-verification-contract` the same day (design record
below; probe validity re-pinned in `tests/test_evals.py`).
**Artifacts:** `eval_run_20260711T213822Z/z-ai-glm-5.2__tetris-tui__seed0__4itzsb0n/` and
`eval_run_20260711T220702Z/x-ai-grok-4.5__tetris-tui__seed0__91icxj09/` (kept scratch repos with
`journal.jsonl`; local, not committed), result rows `evals/results/20260711T213822Z.jsonl` /
`…T220702Z.jsonl`.
**Reproduce:** `make eval TASKS=tetris-tui SEEDS=1 MODELS=<model> NO_CLEANUP=1`; replay the probe
with `cd <workspace> && python3 evals/probes/tetris_tui_smoke.py tetris.py`.

## Design record (the task's durable rationale — this note is its home)

The task grades a terminal deliverable "like a human" while staying inside the suite's
deterministic-scoring contract (ADR-0004: no LLM judge). The load-bearing choices:

1. **Probe-owned UI scanning; no model-provided hooks.** An agent-authored state hook
   (`get_board_state()`, a dump flag) is an agent-authored *grading surface* — the channel
   ADR-0011/0040 exist to close (an agent has been observed editing its contract file in an
   ordinary run; a hook can report the state the probe wants while the rendered UI does
   something else). The probe parses the same rendered frames a human sees and never imports
   the game. What makes screen-scraping deterministic is the ADR-0035/0036 move: **pin the
   interface in the goal** — input bytes, frame layout, glyphs, sentinel, scoring values, spawn
   shapes, rotation rule, randomizer.
2. **The graded surface is the pinned `--no-raw --seed <int>` scripted mode** — turn-based (no
   timers; gravity only via Down/space), one flushed frame per key, **streaming** (keys
   processed as they arrive, never slurp-stdin-to-EOF; pinned after the grok dev cell shipped
   exactly that shape). The `--no-raw` idiom comes from the interactive dogfoods
   (`printf 'q' | python3 -m ascii_tetris.main --no-raw`, pinned as a real declared check in
   `tests/test_planner.py`).
3. **Input is real ANSI arrow escape sequences (`\x1b[D/C/B/A`), not tokens** — token-per-line
   would grade a different parser than the one a keyboard exercises; the genuine bytes make
   "the arrow keys work" the thing verified.
4. **Determinism via a pinned seeded 7-bag** (`random.Random(seed)`, shuffle per refill, drawn
   in order) plus turn-based gravity: the game is a pure function of (seed, key script), which
   is what makes the line-clear scoring assertion decidable. Assertions stay *differential*
   (frame N vs N+1), and the phase-7 packing planner is adaptive (reads spawn position from
   frames; clears on 35 of the first 60 seeds — the task pins 42).
5. **The README is a graded artifact, never the grading input** (a probe that *obeyed* it would
   let a wrong-README + matching-wrong-game pair self-certify), and its documentation check
   matches **semantically, not literally** — normalized text, phrasing-tolerant patterns (the
   false-rejection lesson below, learned three times).
6. **The interactive mode is presentation-graded on a real pseudo-terminal** (added 2026-07-12,
   addendum 4): stdlib `os.openpty` + a minimal terminal emulator assert vertically aligned
   board rows (no raw-mode staircase) and a prompt `q` exit — presentation only; the
   timer-driven mode's gameplay stays ungraded.

**Rejected:** model-provided hooks (above); pexpect/pty *gameplay* driving (heavy, flaky, and
still needs a pinned rendering contract — the presentation smoke is the deliberate partial
exception); an LLM judge for "looks like Tetris" (nondeterministic and itself gameable); free
RNG with invariant-only scoring (guaranteed line clears become unreachable). **Unpinned by
decision:** whether `q` renders a farewell frame (both readings compliant; count-sensitive
phases end on EOF). **Known limits:** interactive gameplay is unexercised (only presentation +
q-exit); wall-kick-free rotation is pinned, so SRS-style implementations fail — the goal says
so. Probe validity is pinned in `tests/test_evals.py` the ADR-0035/0036 way: a golden passes
all nine phases; nine surgical counter-examples (inverted movement, rotation no-op, fake score,
no line clears, deviating RNG, stdin slurping, raw-mode staircase, canned-frame cheat, missing
README) each flip it; a farewell-frame variant is pinned as *passing*.

## What the cells showed (measured)

| | z-ai/glm-5.2 | x-ai/grok-4.5 |
| --- | --- | --- |
| outcome / wall | `incomplete` at the (then) 900 s wall clock, 17 turns | `success` (advisory: reached `final_answer`), 141 s, 18 turns |
| verifier | never ran (budget died first) | ran; **failed `['declared_1']`** — the model declared `python3 verify_tetris.py` but never wrote that file |
| probe | exit 1 at phase 0 (README wording) | exit 1 at phase 0 (README wording) |
| beneath phase 0 (manual replay) | boot fails: first piece contradicts the pinned seed-42 bag — `bag.pop()` draws the shuffled bag **in reverse**; contract pins "drawn in order" | phases 1–6 **all pass**; phase 7 hangs — `sys.stdin.buffer.read()` slurps stdin to EOF before processing any key |

Common trajectory shape in both cells:

1. **The declaration-time steering worked as designed.** Both models had 2–3 declarations
   rejected model-correctably (quoted `|`; `>&`; `;`; missing `content`-kind coverage) and
   recovered in-turn — ADR-0044/0045 behaving exactly as built, in the wild.
2. **Eval agents build blind on this task.** `run_tests` finds no command (declared checks are
   invisible to it — the known seam from the PR #112 review), and tier-3 `run_command` is
   auto-denied in unattended runs (glm ×2, grok ×3, each trying to run its own driver script).
   The only execution feedback available is the post-claim verify/repair loop — which glm never
   reached (900 s wall; two ~4-minute model calls ate half the budget) and grok reached with a
   contract whose executing check pointed at a file it never created.

## Task defects found and fixed (the point of development cells)

1. **Phase-0 README regexes were too literal — a false-rejection gate.** Both cells died at
   phase 0 on *wording*: grok documented the board as `Size: **10** columns × **20** rows`
   (a genuine documentation of the pinned size; the `10\s*[x×]\s*20` pattern missed it — a
   clear probe bug), and glm documented arrow keys as their `ESC [ D` byte forms without the
   word "arrow" (borderline). Fix: normalize the README text (strip markdown emphasis) and
   match semantically (`10\D{0,20}20`; arrow evidence includes `ESC [ <letter>` / `\x1b[`
   forms). This is the PR #110 vacuity-guard lesson recurring in a probe: a lower-bound
   documentation gate must fail open on phrasing.
2. **The goal under-pinned the streaming obligation phase 7 depends on.** "Render one frame
   after every recognized key" is satisfiable by slurp-stdin-then-replay — grok shipped exactly
   that, passing every batch phase (they close stdin) and deadlocking the interactive phase.
   Fixes: (a) the goal now pins "process keys as they arrive — do not read stdin to EOF first";
   (b) the probe's interactive phase reads via `select` with a 15 s per-frame deadline and
   diagnoses the slurp shape legibly (previously: a 120 s watchdog hang and a generic "went
   silent"); (c) a new `slurps_stdin` counter-example pins the shape in `tests/test_evals.py`.
3. **Wall clock raised 900 → 1800 s** (task budget in the TOML; note `AVATAR_MAX_WALL_CLOCK_SECONDS`
   cannot override it — `run_task` applies `**spec.budgets` on top of the env-derived config).

Post-fix replays against the *unmodified* kept workspaces: glm fails at boot with the
reverse-bag message (true positive); grok passes phases 0–6 and fails phase 7 in 15 s with the
slurp diagnosis (true positive). Golden + 8 counter-examples green; `make check` 755 passed.

## Interpretation (kept separate from the measurements)

- Both surviving failures are **genuine contract violations by capable models** — draw-order
  RNG deviation and non-streaming input — i.e. the task discriminates on exactness of contract
  adherence, not on trivia. n=2, one seed each: no capability claims.
- grok's cell is a live instance of the ADR-0040 gap the `gamed_rate` metric exists for:
  `self_reported_success` (advisory `final_answer`) with `held_out_passed` false, and a declared
  contract whose executing check referenced a never-created file. A declared check naming a
  nonexistent script passes declaration (lexically a real program) and fails only at verify —
  vacuity-guard-adjacent; worth a look when ADR-0011 D1–D4 work starts.
- The unattended tier-3 denial of `run_command` + the `run_tests`/declared-check seam means
  this task currently measures *blind* one-shot building. That is a defensible construct
  (the harness owns execution), but it couples badly with slow models and tight wall clocks;
  revisit if pass rates stay at zero across the matrix.

## Addendum — post-fix cell: first solve (same day)

A fresh `x-ai/grok-4.5` seed-0 cell against the fixed task **passed** (`evals/results/
20260711T224132Z.jsonl`: `solved=true`, `probe_exit=0`, 17 turns, 148 s, ~21.5k completion
tokens; workspace `eval_run_20260711T224132Z/…6h3pcx64/`, kept). All eight probe phases green,
including the interactive packing clear. The goal's new streaming sentence did its job: the new
implementation reads stdin byte-at-a-time (`sys.stdin.buffer.read(1)`) instead of slurping to
EOF — the exact defect of the previous cell, fixed by specification alone. The task is
demonstrably solvable by a frontier model as pinned (the golden is no longer the only
existence proof). Recurring wart, now 2/2 grok cells: it declares `python3 verify_tetris.py`
(this time with a Makefile referencing it) but never writes the file — the verifier fails
`['declared_1']` at verify time while the deliverable is genuinely good; in advisory mode this
is reported, not gated. Phantom-check declarations remain the standing vacuity-adjacent
observation for ADR-0011 D1–D4 work.

## Addendum 2 — first 5-model matrix (2026-07-12 local time; run stamp `20260711T230139Z`)

`make eval TASKS=tetris-tui SEEDS=3 MODELS=x-ai/grok-4.5,poolside/laguna-m.1,openai/gpt-5.6-sol,z-ai/glm-5.2,anthropic/claude-opus-4.8 CONCURRENCY=8 NO_CLEANUP=1`
(15 cells; results `evals/results/20260711T230139Z.jsonl`, workspaces kept).

**As recorded, pass@1 = 0.40** — but 4 of the 9 failures were one further probe artifact:
`movement: expected 17 frames, got 18`. Cause: those games render a farewell frame for `q`
(verified: `printf 'q' | …` yields 2 frames), a compliant reading the goal does not pin, and the
probe's count-sensitive phases appended a trailing `q`. Fix (same day): movement/rotation/drop
now end on EOF instead of `q`; a farewell-frame golden variant is pinned as *passing* in
`tests/test_evals.py`. This is the third false-rejection shape found by development cells, after
the README-wording and streaming-ambiguity ones — all three share one lesson: **the probe may
only fail behavior the goal pins**.

**Re-graded with the fixed probe against the same kept workspaces** (deterministic replay of the
identical artifacts; the recorded rows retain the artifact-era verdicts):

| model | recorded | re-graded | surviving failure reasons |
| --- | --- | --- | --- |
| anthropic/claude-opus-4.8 | 3/3 | **3/3** | — |
| openai/gpt-5.6-sol | 1/3 | **3/3** | (both failures were the q-frame artifact) |
| x-ai/grok-4.5 | 1/3 | **3/3** | (both failures were the q-frame artifact; no phantom `verify_*` regression) |
| z-ai/glm-5.2 | 1/3 | **1/3** | seed 0: reverse-order bag draw again (`(0,4)(1,3)(1,4)(1,5)` ≠ pinned J — same defect as the dev cell, now 2/2 occurrences); seed 1: died at 8 turns to the consecutive-tool-failure budget inside the declaration gate — five straight `content`-coverage / `\|` rejections, no file ever written |
| poolside/laguna-m.1 | 0/3 | **0/3** | seeds 1–2: `max_iterations` (40) exhausted with no README (seed 1–2) ; seed 0: malformed frame mid-stack in the game-over phase |

**Corrected pass@1 = 10/15 ≈ 0.67; pass^3 = 1.0 for opus-4.8, gpt-5.6-sol, grok-4.5.**

Interpretation (separate from the measurements): the surviving failures are all genuine —
glm-5.2's bag-order defect is now reproduced across independent cells (a stable model-level
misreading of "drawn in order"), its seed-1 death is a legibility data point on the ADR-0044
rejection message (consider naming a concrete satisfying example, e.g. "add
`grep -q '<heading>' README.md`", in the steer), and laguna-m.1 exhausts its iteration budget
without converging. The task now discriminates: one model family at ceiling, one at floor, and
mid-field failures that are real contract violations rather than scoring artifacts. The results
file was not rewritten; treat this note as the corrected reading of run `20260711T230139Z`, or
re-run the matrix against the fixed probe for a canonical baseline.

## Addendum 3 — second matrix wave: 4 more models (run stamp `20260711T233131Z`)

`make eval TASKS=tetris-tui SEEDS=3 MODELS=deepseek/deepseek-v4-pro,google/gemini-3.5-flash,qwen/qwen3.7-max,minimax/minimax-m3 CONCURRENCY=8 NO_CLEANUP=1`
(12 cells, graded by the post-fix probe; zero transport retries/fallbacks — the ~30 min wall time
was minimax latency, not throttling).

| model | pass | surviving failure reasons |
| --- | --- | --- |
| qwen/qwen3.7-max | **3/3** (pass^3 = 1.0) | — (slow but exact: 1317–1790 s cells) |
| deepseek/deepseek-v4-pro | 1/3 | seeds 0+1: **reverse-order bag draw** (seed 0's was initially masked by probe artifact #4, below); both also wall-clock `incomplete` |
| minimax/minimax-m3 | 1/3 | seed 0: wall clock at 6 turns (~5 min/model-call latency), no deliverable; seed 1: renders **zero `@` cells** — the falling piece is not distinguished, a genuine glyph-contract violation |
| google/gemini-3.5-flash | 0/3 | all three: died in 25–45 s with no `tetris.py` — a rapid-fire loop of schema-invalid `declare_verification` calls (pydantic `model_type` errors) until the consecutive-tool-failure budget killed the run; a tool-call-adherence failure, not a task failure |

**One further probe artifact (#4, fixed):** a README documenting arrow keys as literal glyphs
(``Left (`←`)``) failed the arrow-evidence pattern; the pattern now accepts `←→↑↓`. Re-grading
flips no verdict in either matrix (the masked cell fails for its true reason, the bag order).

**Combined corrected 9-model picture (27 cells, both waves): pass@1 ≈ 0.56.**
Ceiling (3/3): claude-opus-4.8, gpt-5.6-sol, grok-4.5, qwen3.7-max. Mid (1/3): glm-5.2,
deepseek-v4-pro, minimax-m3. Floor (0/3): gemini-3.5-flash, laguna-m.1.

**The dominant genuine contract defect is now a pattern: the reverse-order bag draw (4 cells,
2 model families — glm-5.2 ×2, deepseek ×2).** The goal pins "drawn from it in order"; these
implementations `shuffle` then `pop()` from the *end* (the Python-idiomatic O(1) pop). A written
spec detail losing to a strong code idiom is exactly the exactness signal this task exists to
measure — but it is worth knowing that one clause carries 4 of the 12 genuine failures. If the
matrix should discriminate on more than this clause, a future revision could pin `pop(0)`
semantics in the goal's own words (e.g. "pop the first element") rather than relax the contract.

## Addendum 4 — the raw-mode staircase: interactive mode gains a pty presentation phase (2026-07-12)

Human inspection of three matrix workspaces (screenshots, 2026-07-12) found that two *passing*
implementations — `openai-gpt-5.6-sol seed1` and `anthropic-claude-opus-4.8 seed0` — render
their **interactive** mode as a diagonal "staircase" on a real terminal, while
`x-ai-grok-4.5 seed2` renders correctly. Mechanism (verified in source): both broken
implementations call `tty.setraw` in their interactive loops and then write bare-`\n` frames;
raw mode disables the terminal's output post-processing (`OPOST`/`ONLCR`), so `\n` descends
without returning to column 0. grok uses `tty.setcbreak` (input-only), which leaves output
processing intact. Every pipe-driven probe phase is structurally blind to this — pipes have no
tty — which was the design record's documented "interactive mode is only obliged to exist"
limit, here demonstrated in the wild.

**Disposition:** probe phase 8 ("presentation") runs the interactive mode under stdlib
`os.openpty()`, reconstructs the screen with a ~40-line terminal emulator (`\r`/`\n`/CSI cursor
addressing — so cooked-mode, `\r\n`-writing, and curses-style UIs are all graded fairly), and
asserts vertically aligned board rows plus a prompt exit on `q`. Goal amended to name the
pitfall explicitly; design-record item 6 records the partial reversal of the "no pty" rejection
(presentation-only; gameplay in the timer-driven mode stays ungraded); a `staircase_interactive`
counter-example (setraw + bare `\n`) pins the defect. Verified against the evidence workspaces:
the two staircase cells now fail phase 8 with a diagnosis naming `tty.setraw` and the fix
options; grok's cell passes all nine phases; the golden (curses interactive) passes.

**Matrix impact (not yet re-run — grading surface changed after both waves):** at least the two
verified cells would flip PASS→FAIL, so recorded pass@1 for both matrices is stale pending a
fresh run against the nine-phase probe. Presentation-quality of the other passing cells
(qwen ×3, opus seeds 1–2, sol seeds 0/2, deepseek seed 2, minimax seed 2, grok seeds 0–1) is
unmeasured until then. First nine-phase-native cell: a fresh `openai/gpt-5.6-sol` seed-0 run
**passed all nine phases** (`evals/results/20260712T110513Z.jsonl`; 11 turns, 88 s) — its new
implementation uses `tty.setcbreak` *and* writes `\r\n` line endings, both remedies the amended
goal names. All result rows cited by this note are committed (`.gitignore` whitelist), so the
reported verdicts are auditable without re-running paid cells.
