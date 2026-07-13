# `tetris-playable`: probe hardening, spec v2, and three graded runs

- **Date:** 2026-07-12
- **Status:** measured (runs 1 and 2 have runner-written result rows; run 3 was terminated
  before the runner's summary step, so its verdicts are **probe replays over the kept
  workspaces** — grading-identical, but no `evals/results` stamp exists for it)
- **Raw artifacts:** `eval_run_20260712T140147Z/` (matrix 1), `eval_run_20260712T171109Z/`
  (single-seed diagnostic), `eval_run_20260712T192911Z/` (matrix 2, killed at 20/21)
- **Result rows:** `evals/results/20260712T140147Z.jsonl` (+summary),
  `evals/results/20260712T171109Z.jsonl` (+summary); none for `192911Z`
- **Grading surface at time of writing:** rebuilt `evals/probes/tetris_playable_smoke.py` and
  restructured `evals/tasks/tetris-playable.toml` (spec v2), both **uncommitted** on
  `feat/tetris-tui-eval-task`
- **Reproduce (matrix 2):** `make eval TASKS=tetris-playable SEEDS=3 CONCURRENCY=4 NO_CLEANUP=1
  MODELS="openai/gpt-5.6-sol,openai/gpt-5.6-terra,x-ai/grok-4.5,minimax/minimax-m3,deepseek/deepseek-v4-pro,qwen/qwen3.7-max,moonshotai/kimi-k2.6"`
- **Replay a verdict:** `cd <cell dir> && python <repo>/evals/probes/tetris_playable_smoke.py tetris.py`

## 1. Matrix 1 (`140147Z`, spec v1, original probe): reported 0.19, ground truth 0.52

7 models x 3 seeds. The probe reported pass@1 = 0.19 (4/21). Manual audits (maintainer played
the kept games in a real terminal) plus per-cell probe replays attributed the gap to **four
false-rejection classes** in the probe, each resolved by an explicit ruling:

| Class | Cells hit | Ruling |
| --- | --- | --- |
| `GAME OVER` searched only after the last sentinel | 6 | goal pins "before a successful exit", not a position — search the full output |
| all four falling cells assumed visible | 3+ | hidden rows above the field are legitimate (guideline spawns, upward kicks) |
| single-press interactive check | 1 (opus: buffered-stdin latency) | a human presses again — retry up to 3x before declaring a no-op |
| streaming failure opaquely gated gameplay | 3 (grok x2, gpt-oss) | **option-3 hybrid** (below) |

Ground truth after audits: **11/21 = 0.52** (deepseek 2/3, opus 2/3, terra 2/3, grok 3/3
gameplay, minimax 1/3, sol 0/3, gpt-oss 0/3). Genuine failure modes: raw-mode staircase
(6 cells), stdin slurped to EOF (3), a shipped `SyntaxError`, empty workspaces (models unable
to recover from the declare-before-edit nudge), one interactive input bug.

## 2. The option-3 hybrid probe (rebuilt the same day)

The streaming trade-off (a game can be human-playable yet deadlock a held-open-pipe driver)
was resolved by decomposing gameplay from transport:

- **Replay-prefix driver.** The pinned determinism ("same seed + keys -> same frames") lets
  adaptive phases relaunch the game with the full key history instead of conversing over a
  held-open pipe — so stdin-slurping games are graded on identical frames. Decision-free key
  stretches are batched into single launches (the whole 7-bag phase is one run).
- **Dedicated transport phase.** One live held-open-pipe check verifies the goal's streaming
  sentence and reports on its own `transport (streaming)` line; `_STREAMING_GATES = False`
  keeps it non-gating (a one-line policy).
- Hidden-row tolerance (1-4 visible cells; 0 mid-rotation), full-output `GAME OVER` search,
  and interactive key-retry per the rulings above.

**Validation:** probe replayed over all 21 matrix-1 workspaces — verdicts agreed with the
human audits on every audited cell; validity tests green (golden + rule variants + tolerated
variants pass; movement/rotation/bag/clear/gravity/static/canned/README counter-examples
still flip; `slurps_stdin` moved to "passes with transport FAILED reported").

The single-seed diagnostic (`171109Z`) immediately caught two bugs in the rebuild itself,
both fixed and re-validated: a vertical I rotated at spawn can sit *wholly* above the field
(zero visible cells), and skipping partially hidden orientations blinded the packing planner
to exactly the hole-free vertical placements (fix: soft-drop each piece to depth >= 3 before
enumerating rotations — landing is depth-invariant). Lesson: with a first-failure-exit probe,
a defect at phase N censors all later phases; verdicts can only be corrected by full replay.

## 3. Spec v2 (maintainer rulings)

`tetris-playable.toml` restructured into labeled two-mode sections with three tightenings:

1. **Board glyphs pinned in both modes** (`.`/`@`/`#`, `|`-bordered) — an interactive
   renderer using e.g. `[]` blocks is now out of spec (one opus cell exercised this freedom;
   the game was verified playable, and the ruling chose pinning over a glyph-agnostic probe).
2. **Streaming sentence upgraded to the emphatic tui-grade wording** ("do not read stdin to
   EOF first: the frame answering a key must be written and flushed before the next key is
   read") — removing the wording asymmetry behind grok's 3/3-vs-1/3 split across the two
   tetris tasks.
3. **"No-ops included"** added to the frame cadence (the replay driver indexes frames by key).

The freedom clause is explicit: bag order/implementation, spawn orientations, rotation system
and kicks, scoring values, terminal implementation.

## 4. Matrix 2 (`192911Z`, spec v2, final probe): salvaged pass@1 = 0.75

Run killed externally at 20/21 agents (exit 143); one kimi cell died mid-run (`turn_start`)
and is excluded as ungraded. Verdicts below are probe replays over the kept workspaces.

| Model | Score | Notes |
| --- | --- | --- |
| openai/gpt-5.6-terra | 3/3 | transport ok on all |
| qwen/qwen3.7-max | 3/3 | first outing; also 3/3 on `tetris-tui` |
| minimax/minimax-m3 | 3/3 | up from 1/3 on spec v1 |
| openai/gpt-5.6-sol | 2/3 | seed 0 staircases (repeat offender) |
| x-ai/grok-4.5 | 2/3 | seed 1: interactive arrows never register (cbreak input-loop bug, survives 3 retries) |
| deepseek/deepseek-v4-pro | 2/3 | seed 0 is a **false rejection**: fully hidden spawn (0 visible cells at boot; piece appears on first soft drop) — working game, spawn visibility unpinned in the goal as run |
| moonshotai/kimi-k2.6 | 0/2 | two cells never wrote `tetris.py` (declare-before-edit protocol failure, gpt-oss profile); third cell killed |

**pass@1 = 15/20 = 0.75 as graded (16/20 = 0.80 counting the deepseek false rejection).**

### Headline finding: precise spec wording moved transport compliance to 100%

Zero stdin-slurpers among 16 shipped games under the emphatic streaming sentence, vs 3
slurping cells (grok 2/3 among them) under the terse wording — replicating the `tetris-tui`
spec-sensitivity result in the opposite direction. Raw-mode staircases fell from 6 cells to 1
(same-day model variance not controlled). Both directions support the standing ruling: state
observable requirements precisely; vague wording measures prompt-reading luck, not capability.

### Interpretation cautions

- Matrix 1 vs matrix 2 compare different specs, probes, and model sets — directional only.
- Cost fields for qwen/kimi are absent (`evals/pricing.json` not extended).
- Several passing cells ended `outcome=incomplete` (budget exhausted after a working build) —
  gameplay verdicts are unaffected (probe grades the artifact).

## 5. Open items

1. **Spawn-visibility seam** (deepseek seed 0): either one goal sentence ("a newly spawned
   piece must be at least partly visible") or a boot-check tolerance. Pinning matches the
   glyph ruling's simplicity stance and keeps the probe unchanged.
2. **kimi-k2.6 / gpt-oss-120b cannot operate the declaration gate** (0 shipped files across
   5 cells) — harness-protocol signal worth its own investigation, not Tetris signal.
3. Probe + spec v2 + test updates are uncommitted; a citable baseline wants them landed and
   a clean matrix run (`192911Z` has no runner stamp).
