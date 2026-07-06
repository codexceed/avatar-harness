# ADR 0036 — `ecommerce-portal`: a concurrency/ACID eval task scored by schedule-invariant assertions

- **Status:** Accepted — implemented 2026-07-05 (PR #99); construct validity confirmed by the 2026-07-05 7-model landscape run (see *Validation* below).
- **Date:** 2026-07-05
- **Deciders:** Sarthak Joshi
- **Related:** ADR-0004 (eval harness — the probe is the deterministic grader); ADR-0020 (probe roles — this is a `success` probe); the `news-analyzer` task (7cf1e6e) established the hermetic-adaptation pattern (probe-owned stubs, pinned server-rendered UI, golden + counter-example probe validation) that this task extends to concurrency.

## Context

The suite had no task exercising the failure modes that dominate real backend work: races on shared state, atomic multi-step writes under contention, background pipelines with retries, and cache/state coherence. The requested scenario — a high-concurrency ecommerce portal (carted demand exceeding stock, graceful denial, free cancellations restocking against live purchase traffic, search caching consistent with inventory, a UI that stays responsive under order load, and an order processor that takes 3–15 s with a ~5 % transient failure rate) — is intrinsically nondeterministic, while the harness's scoring contract (ADR-0004) is *deterministic, no LLM judge*. The design problem is squaring those.

## Decision

Add `evals/tasks/ecommerce-portal.toml` + `evals/fixtures/ecommerce-portal/products.json` + `evals/probes/shop_portal_smoke.py`, with these load-bearing choices:

1. **The environment's randomness lives in the probe's stub, not the app.** The "3–15 s, ~5 % failure" processor is a *narrative contract about the environment*: the app must call `PAYMENT_API_URL` per order and design for slowness + transient 503s. The probe's stub *enacts a deterministic schedule within that contract*, keyed on the `user_id` echoed in the pinned payment payload (`retry-*` → 503 on each order's first attempt; `doomed-*` → always 503; `slow-<i>` → a 3–9 s hold). An app-internal `sleep(random())` would be unverifiable, slow to score, and flaky.
2. **Every assertion is schedule-invariant.** Concurrent waves assert only interleaving-independent facts: aggregate counts (stock 5 + 20 racing checkouts ⇒ exactly 5 orders), conservation ledgers (net completed units + stock == units returned), never-negative stock (a polling monitor during waves), and definitive per-request outcomes (order XOR legible denial naming the product). "Which user won" is deliberately unpinned. Where a race legitimately leaves a range (the cancel/restock storm), a **sequential settle-then-drain** step buys the range back down to an exact final state — so the run ends in one precomputable ledger (inventories, `units_sold` 56, `revenue` 1137, `orders_by_status`), verified via the API and re-verified after a restart.
3. **Final state is read through the pinned API surface (`/api/products`, `/api/orders`, `/api/metrics`), never by opening `shop.db`.** Direct DB reads would force pinning a schema (over-constraining design, brittle grading); the restart check already proves API state ≡ durable state. `/api/metrics` doubles as an ACID check: the app's own aggregation must reconcile with its order rows after all races.
4. **Concurrency-hostile pins that make the task real:** SQLite as the store (so `database is locked` under a threaded server is the intended difficulty, not an accident); checkout must return < 2 s regardless of processor latency (forces a background pipeline); payment calls must overlap (stub-observed peak in-flight ≥ 2 kills single-sequential-worker designs); a warmed cache (pinned `X-Cache: hit|miss` header) must drop a product the moment stock hits zero and restore it on restock.
5. **"Fair UX" is pinned as *definitive outcomes*, not FIFO** — ordering fairness under concurrency is unverifiable; hangs, crashes, silent drops, and oversells are what the probe rejects.
6. **Enabler: per-task `probe_timeout_seconds`** (`TaskSpec` field, default 120 — the prior hard-coded cap), because a gauntlet that waits out real background processing needs ~2 min against a good app; this task sets 360.

Probe validity is established the `news-analyzer` way, in `tests/test_evals.py`: a golden stdlib portal passes, and four surgical counter-examples flip it — non-atomic reservation (oversells/negative stock), no retry (transient 503 ends `failed`), never-invalidated cache (sold-out product served from a `hit`), synchronous checkout (blocks for the processor hold).

## Rejected alternatives

- **App-generated randomness** (the literal "randomly 3–15 s, 5 % failure" in the app): unverifiable distribution, nondeterministic scoring, 20× slower probes.
- **Exact per-user outcome scripts** ("user 7 wins the race"): pins scheduler behavior no correct app can guarantee; flaky by construction, and flakiness poisons pass^k / McNemar.
- **Reading `shop.db` directly for the final ledger:** requires a pinned schema; grades implementation, not behavior.
- **Verifying the cache by timing or DB-read counts:** unobservable hermetically. Known construct-validity limit: a fake cache that recomputes and sets `X-Cache: hit` passes the header check while honoring consistency — accepted, since the *hard* requirement (zero-stock items never surfaced, even on hot queries) is fully checked.
- **Cookie/session auth:** a pinned `user_id` field is the hermetic stand-in; auth is not what this task measures.
- **Cancelling mid-`processing` orders:** a genuinely interesting cancel-vs-payment race, but not deterministically scoreable; scope-cut to cancelling `completed` orders.

## Consequences

- The suite gains its heaviest task (budgets: 60 iterations / 900 s wall; probe budget 360 s) and its first concurrency signal.
- `probe_timeout_seconds` is new grading-surface schema; like all grader changes it rides the frozen-asset validation path (ADR-0024).
- The probe's counter-example tests add ~45 s to `make test`.
- The exact-ledger design means any future edit to the fixture catalog or probe phase plan must recompute the expected constants together (they are cross-derived; the probe docstring carries the phase→ledger map).

## Validation (2026-07-05 landscape run)

The task shipped (PR #99) and was exercised in the first wide capability×reliability sweep — 7 models × 6 tasks × 5 seeds (`docs/research/llm-landscape-2026-07-05.md`; artifact `evals/results/20260705T173314Z.jsonl`, heatmap `docs/research/assets/20260705T173314Z-solved-heatmap.svg`). The data vindicates the load-bearing choices:

- **It is the suite's sole hard discriminator.** `pass@1 = 34 %` (12/35), against 63–100 % for the other five tasks — the only task nobody solves reliably. Best is **3/5** (`gpt-5.3-codex`, `deepseek-v4-pro` — the joint leaders); the mid-tier lands the occasional seed (`minimax` 2/5, `glm` 2/5, `gemma`/`qwen` 1/5) and `gpt-oss-120b` scores **0/5** while burning the full 60-iteration budget on two seeds. This is exactly the frontier-separating signal the task was added to provide.
- **It discriminates on capability, not grader noise.** Of the 23 non-solved runs, only **4** are `harness_error` — transport flakes concentrated on the two weakest models (`qwen` 3, `gemma` 1), never the probe. The other 19 are genuine capability failures: the schedule-invariant assertions (Decision §2) rejecting real oversells / broken retries / stale sold-out caches / synchronous checkouts, or the app exhausting the wall/iteration budget still-broken. The "randomness lives in the probe's deterministic stub" design (Decision §1) held — no flakiness is attributable to the task itself.
- **The heavy budgets are binding, not over-provisioned.** Every model that engages the task deeply — `deepseek`, `minimax`, `glm` — runs to the 900 s wall on nearly every seed, and one solved `qwen` run took 51 of 60 iterations. The 60-iter / 900 s-wall / 360 s-probe budgets (and the new `probe_timeout_seconds`, Decision §6) were a true enabler: a correct app scores comfortably within them (`gpt-5.3-codex` solves in 146–213 s), while a wrong one exhausts them.
- **A known construct limit, now observed.** For the wall-bound models, wall-clock — not a probe verdict — is the terminator, so their `pass@1` cannot separate "wrong design" from "correct-but-too-slow." `codex`'s sub-4-minute solves show a correct async pipeline finishes well under budget; scoring a wall-halted app not-solved is still valid (it is genuinely not-yet-correct at halt), but future budget tuning should watch this boundary.
