# `ecommerce-portal` — first baseline (seed 1) + probe-fairness validation

**Date:** 2026-07-05
**Status:** measured — task introduction + construct-validity check for the new probe (ADR-0035)
**Artifact:** `evals/results/20260705T125921Z.jsonl` (+ `.summary.json`); journals kept under
`eval_run_20260705T125921Z/` (`--no-cleanup`).
**Reproduce:** `make eval-matrix SEEDS=1` (≡ `--models "minimax/minimax-m3,openai/gpt-oss-120b,openai/gpt-5.3-codex,z-ai/glm-5.2" --seeds 1 --concurrency 8 --no-cleanup`). A single cell: `make eval TASKS="ecommerce-portal" SEEDS=1 MODELS="…"`.

## Why this run

Introduce `ecommerce-portal` (the suite's first concurrency/ACID task, ADR-0035) and check that
its functional probe discriminates on the intended property — atomic reservation under contention,
a concurrent retrying order pipeline, cache/stock consistency, and UI responsiveness under sustained
load — rather than on incidental behaviour. One seed, all four tracked models, run alongside the
existing five tasks so the new task's cost and cell behaviour sit in context.

## Result — the new task splits the field 2/4

Per-model pass@1 across all six tasks (n=6 each), and the `ecommerce-portal` cell in particular:

| model | overall pass@1 (6 tasks) | ecommerce-portal | disposition on the ecommerce cell |
| --- | --- | --- | --- |
| minimax/minimax-m3 | 1.00 | **pass** | solved; 8 peak concurrent payments |
| z-ai/glm-5.2 | 1.00 | **pass** | solved; 30 peak concurrent payments |
| openai/gpt-5.3-codex | 0.83 | **fail** | probe_failed — reached `outcome=success`, but the app wedges under load |
| openai/gpt-oss-120b | 0.50 | **fail** | probe_failed — app wedges under load |

Overall pass@1 = 0.83 (n=24). The other two non-passing cells in the run are unrelated to this task:
gpt-oss-120b `investigate-question` (`loop_oscillation`) and gpt-oss-120b `news-analyzer`
(`harness_error` — a `TransportError: empty model reply`, the known minimax/gpt-oss NUL-ish class).

The **gpt-5.3-codex** cell is the headline: the agent self-reported `final_answer`/`success`, but
the harness-owned probe caught that the delivered app does not actually satisfy the load
requirement — a clean demonstration of the "done is a proposal the verifier disposes of" invariant.

## Construct validity — the failures are real, not probe artifacts

Both failing apps failed at the same phase (H, responsiveness under sustained load) with the same
surface symptom (`/api/orders` stops returning a JSON array during the 30-order settle). Two strong
models failing identically warranted ruling out a probe bug before trusting the signal. Investigation
(`--no-cleanup` journals + isolated repros against each app):

1. **Isolated, the load phase is satisfiable.** Replaying only phase H against a freshly launched
   codex app settled all 30 orders with zero poll failures. So the phase's *requirement* is not
   impossibly strict.
2. **In the full run, the apps genuinely wedge.** At the point of failure a direct `GET /` times out
   at 15 s **and** a 30 s retry also fails for both apps — total unresponsiveness, not a transient
   blip. The wedge is triggered by *accumulated* multi-phase load (it does not appear in the
   isolated phase), and it is **deterministic**: codex failed 3/3 repeated full runs at the same
   barrier. This is exactly the "UI must stay responsive under high load" requirement being violated
   in the strongest possible way.
3. **The passing apps prove the bar is clearable.** The golden reference app, minimax, and glm all
   stay responsive through the full run (glm sustains the full 30 concurrent payments). The
   discriminating property — staying responsive under sustained concurrent order load — is real
   engineering (WAL + adequate worker/connection headroom + no lock/thread accumulation), not luck.

One probe-hygiene fix fell out of the investigation (commit `e0719e7`): the settle **barrier**
(`_await_terminal`) originally aborted on a *single* transient poll timeout, conflating
connection-burst pressure with a stalled pipeline. It now retries a failed poll until the deadline
and paces requests, so only a genuine stall (orders never terminal) or a persistently broken
`/api/orders` body fails the barrier. This did not change any cell's outcome (both wedging apps still
fail deterministically; golden/minimax/glm still pass) — it removes a latent false-negative for a
future app that is genuinely responsive but momentarily slow.

## Caveats

- **One seed.** This is an introduction/validation baseline, not a reliability estimate; pass^k here
  equals pass@1 by construction. A full `make eval-matrix` (5 seeds) is the next step before treating
  these per-model numbers as a reference, and is the run ADR-0035's grader-surface change rides
  through `python -m evals.validate` (frozen assets).
- **Known construct-validity limit (documented in ADR-0035):** the search cache is verified by the
  `X-Cache` header plus stock-consistency invariants; a fake cache that recomputes and sets
  `X-Cache: hit` while honouring the sellout invariant would pass. The hard requirement
  (zero-stock items never surfaced, even on a warmed query) is fully checked.
- **Cost/latency:** a wedging app runs its probe to the 90 s settle-barrier deadline before failing,
  so failing cells are the slow ones; the task's `probe_timeout_seconds = 360` accommodates this.
