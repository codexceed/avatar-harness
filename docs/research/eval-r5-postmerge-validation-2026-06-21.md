# R5 post-merge validation — no regression after the main merge + PR #89 review fixes

**Date:** 2026-06-21
**Status:** measured — validation run, no action required
**Artifact:** `evals/results/20260621T214930Z.jsonl` (+ `.summary.json`); journals kept under
`eval_run_20260621T214930Z/` (`--no-cleanup`).
**Reproduce:** `make eval-matrix` (≡ `--models "minimax/minimax-m3,z-ai/glm-5.1,openai/gpt-5.3-codex,z-ai/glm-5.2" --seeds 5 --concurrency 8 --no-cleanup`).

## Why this run

Confirm that the `feat/r5-streaming-idle-timeout` line — after merging `main` (ADR-0030 async
client + cockpit) and applying the PR #89 review fixes (wall-clock mid-call bound, `stream.close()`
suppression, symmetric future draining, the cleanups) — did not regress the harness. R5 (ADR-0029)
streaming runs under concurrency 8, the same condition that triggered the original minimax NUL
incident (`docs/research/failure-modes.md` **A9**), so this is the load case that matters.

## Result — no regression

Paired **McNemar** vs the pre-merge R5 baseline `20260621T171455Z` (identical model set, seeds=5,
temp=0.7, concurrency=8), via `make eval-diff`:

| model | baseline pass@1 | candidate pass@1 | Δ | reg/imp | McNemar p | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| minimax/minimax-m3 | 1.00 | 0.95 | −0.05 | 1/0 | 1.000 | no significant change |
| openai/gpt-5.3-codex | 0.75 | 0.70 | −0.05 | 1/0 | 1.000 | no significant change |
| z-ai/glm-5.1 | 0.75 | 0.75 | +0.00 | 0/0 | 1.000 | no significant change |
| z-ai/glm-5.2 | 0.75 | 0.75 | +0.00 | 0/0 | 1.000 | no significant change |
| **overall** | **0.81** | **0.79** | **−0.03** | 2/0 | **0.500** | **no significant change** |

The two regressed cells are sampling noise (overall p=0.50, well above any threshold). pass^k at k=5:
minimax 0.75, glm-5.1 0.75, gpt-5.3-codex 0.50, glm-5.2 0.75.

## The signal that matters: zero transport-layer failures

Across all **80 rows**:
- **0** `transport_error` / `harness_error` rows, **0** `error: …` outcomes, **0** `iterations==0`
  rows (the transport-hang/NUL signature R5/ADR-0028 was built to kill — absent).
- Failure spectrum is entirely **model behavior**: `budget_exhausted` 7, `blocked` 5,
  `loop_oscillation` 3, `probe_failed` 2 (outcomes: success 65, incomplete 10, blocked 5).
- minimax's single miss is `create-chatbot` seed 3 → `success`/`probe_failed` (it concluded cleanly;
  the chatbot just didn't round-trip a completion) — a functional-output miss, not a transport fault.

## Streaming was genuinely exercised

Scanning all 80 journals: **525 model decisions, every one tagged `transport="native_stream"`**, and
**0 `streaming_fallback`** events. So the R5 streaming path was live for the whole matrix and no
provider rejected streaming — the capability fallback never fired (consistent with the earlier
491/491 observation, now at larger scale).

## secret-safety (the strict guard task) — model-capability spread, not a harness issue

minimax 5/5 `success`; glm-5.1 5/5 `incomplete` (give-up); glm-5.2 5/5 `incomplete`;
gpt-5.3-codex 5/5 `blocked` (permission gate). A guard-probe task (ADR-0020): give-up/blocked
correctly do **not** score solved. This is a behavioral spread across models, unchanged in character
from prior runs — not a regression.

## Interpretation vs measurement

*Measured:* no statistically significant pass@1 change; zero transport failures; streaming live
end-to-end. *Interpretation:* the merge + review fixes are safe to ship — they preserve the R5
reliability win (minimax at 0.95/concurrency-8 with no hangs) within sampling noise. Caveat: the
*recovery* machinery (transport retry / idle timeout firing on a real hang) is still unexercised by
this run — no hang occurred to trigger it — so this validates "no regression," not "the recovery
path works in production" (that still wants fault injection; see A9).
