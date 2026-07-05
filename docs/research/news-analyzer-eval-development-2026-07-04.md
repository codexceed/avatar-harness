# news-analyzer task development — probe escaping bug, gpt-oss-120b as the discriminating cell

**Date:** 2026-07-05 (runs executed 2026-07-04; workspace re-scoring 2026-07-05)
**Status:** measured — development runs behind PR #97 (the `news-analyzer` task + `news_app_smoke.py`
probe + the `MATRIX_MODELS` swap `z-ai/glm-5.1` → `openai/gpt-oss-120b`).
**Artifacts:** `evals/results/20260704T122421Z.jsonl`, `20260704T211656Z.jsonl`,
`20260704T232723Z.jsonl` (+ `.summary.json` each); frozen scratch repos under
`eval_run_20260704T211656Z/` and `eval_run_20260704T232723Z/` (repo root, git-ignored).
**Reproduce (final surface):** `make eval TASKS=news-analyzer MODELS="minimax/minimax-m3,openai/gpt-oss-120b,openai/gpt-5.3-codex,z-ai/glm-5.2" SEEDS=2 NO_CLEANUP=1` (temperature 0.7 default).
**Re-score a frozen app (deterministic, zero model spend):** `cd eval_run_<stamp>/<cell> && rm -f news.db && uv run python <repo>/evals/probes/news_app_smoke.py app.py`

## Why these runs

Three task-scoped runs were executed on 2026-07-04 while developing the `news-analyzer` task
(PR #97): a single-cell smoke, a 4-model × 5-seed matrix on the then-current draft of the
task+probe, and a 4-model × 2-seed run on the final grading surface. They are the empirical
basis for two decisions in that PR: the probe's `html.unescape` design (`_ui_request`) and the
`MATRIX_MODELS` swap. This note records what the artifacts actually support — the runs were
development iterations, and (as the re-scoring below shows) the task contract itself evolved
between runs, so only run 3 measures the shipped grading surface.

## Run timeline

| # | Stamp | Surface | Cells | pass@1 | Headline |
| --- | --- | --- | --- | --- | --- |
| 1 | `20260704T122421Z` | draft | glm-5.2 × 1 seed | 1.00 | Task achievable end-to-end within budget (50 iters / 600 s) |
| 2 | `20260704T211656Z` | draft (pre-`unescape`, pre-key-gating) | 4 models × 5 seeds | 0.15 | Probe construct-validity bug: correct apps punished for HTML-escaping |
| 3 | `20260704T232723Z` | **final** (as shipped in PR #97) | 4 models × 2 seeds | 0.75 | gpt-oss-120b 0/2, all other models 2/2 |

Models in runs 2–3: `minimax/minimax-m3`, `openai/gpt-oss-120b`, `openai/gpt-5.3-codex`,
`z-ai/glm-5.2`. Temperature 0.7 throughout.

## Run 2 — the escaping construct-validity bug (measured), its size (author-observed)

16 of 19 scoreable cells `probe_failed` (plus one gpt-oss `harness_error`: TransportError, empty
model reply). The load-bearing measured fact: **14 of the 16 failed cells had harness
`outcome=success`** — the verifier accepted the app, the probe rejected it. Even gpt-5.3-codex
went 1/5. The dominant cause observed during development: the draft probe's plain-substring
content checks failed on apps that *correctly* `html.escape` their rendered output (apostrophe →
`&#x27;`), punishing exactly the well-behaved apps. This motivated `_ui_request`'s
`html.unescape` (probe L216–225).

*Measured vs observed:* the run-level artifact shows the 16 probe failures and the
verifier/probe disagreement; the **attribution split** (~12 of 16 escaping-specific, cited in the
probe comment as "12/16 cells") is an author-observed dev-time figure and is **not recoverable
from the artifacts** — see the re-scoring finding below. The draft probe that graded run 2 was
never committed.

## Re-scoring (2026-07-05): run 2 predates the final contract; run 3 reproduces exactly

The probe is deterministic and free (local stubs, no model calls), so all 28 frozen apps from
runs 2–3 were re-scored with the **final** probe (fresh `news.db` per cell, as at original
grading).

- **Run 2 (20/20 fail, uniformly at the first check):** every app — including the 3 originally
  solved — fails `_docs_check` with `NEWS_API_KEY` undocumented, across all models and seeds.
  Uniform failure at one check means the run-2 **goal text predates the news-API key-gating
  requirement** (the `apikey` parameter + the stub's 401 gate were added to the contract between
  runs 2 and 3). Consequence: run-2 cells are **not comparable** to the shipped grading surface
  and must not be used as a baseline; run 2's evidentiary value is the verifier/probe
  disagreement and the escaping discovery, nothing finer.
- **Run 3 (8/8 identical to the recorded verdicts):** 6 pass, gpt-oss-120b fails both seeds, with
  reasons now pinned:
  - seed 0 — `display step failed: summary+sentiment … not rendered on the home page`: analyses
    complete and store, but the UI round-trip (home page must render stored analyses) is missing.
  - seed 1 — `analyze form submit … failed (status 500)`: the app calls the **removed pre-1.0
    `openai.ChatCompletion` API** — stale-training-data API usage that a verifier-only score
    would miss until exercised.

## The `MATRIX_MODELS` swap: rationale and caveats

**Measured:** on the final surface gpt-oss-120b is 0/2 with `outcome=success` both times — it
builds an app that self-certifies but fails the functional gauntlet — while the other three
models are 2/2. Its failure modes (missing UI round-trip; legacy SDK call) are exactly the
human-facing-app failure classes the task was built to catch, and both are deterministic re-score
reproducible. glm-5.1 (swapped out) offered no discriminating signal on the suite.

**Caveats, stated plainly:**
1. The final-surface evidence is **2 seeds**. Run 2's gpt-oss 0/4-scoreable is consistent but was
   graded on the draft contract, so it corroborates weakly. A 5-seed task-scoped run
   (`make eval TASKS=news-analyzer SEEDS=5`) would firm this up cheaply.
2. gpt-oss-120b produced run 2's only **`harness_error`** (TransportError: empty model reply,
   0 chars) — an operational-reliability flag on a newly pinned matrix model; watch for
   recurrence in the first full-matrix baseline.
3. **Baseline pairing:** `evals.diff` McNemar pairs only shared (model, task, seed) keys. Post-swap,
   comparisons against pre-swap baselines silently lose all glm-5.1 rows, and `news-analyzer`
   rows have no pre-PR counterpart. The first post-merge `make eval-matrix` run establishes the
   new baseline; diff against anything earlier only on the three carried-over models.

## Interpretation vs measurement

*Measured:* run-level pass rates above; the 14-cell verifier-pass/probe-fail disagreement in
run 2; run 3's exact deterministic reproduction and gpt-oss's two failure reasons; run 2's
uniform final-probe docs-check failure (contract drift). *Interpretation:* the functional probe
catches real, otherwise-invisible failure classes (self-certifying apps, dead UI round-trips,
stale SDK usage); gpt-oss-120b is the suite's discriminating hard cell and worth its matrix
slot despite the thin seed count; run 2 should never be cited for anything beyond the escaping
discovery and the verifier-gap demonstration.
