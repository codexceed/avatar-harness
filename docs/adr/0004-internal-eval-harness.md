# ADR 0004 — Internal eval harness: dogfood incidents as a scored regression suite

- **Status:** Accepted — implemented as the **Eval-0 harness** (Slices 1 + 2, PR #47). The *Decision*'s scoring rule below is **revised by option-A** (a task-authored success **probe is authoritative when present**, and the agent runs non-strict) after the first live smoke scored a working chatbot `failed`; the verifier remains the grader for no-probe tasks. Implementation spec: `docs/eval-harness-design.md`.
- **Date:** 2026-06-10
- **Deciders:** Sarthak Joshi
- **Consulted:** Claude (claude-opus-4-8) — design; grounded in five dogfood post-mortems
- **Related:** `DECISIONS.md` 2026-06-07 (eval landscape survey; "the Verifier is already a deterministic scorer, the event log is already a trajectory dataset"); PROGRESS Phase 4+; ADR-0002 (the cockpit the dogfoods exercised); the `model_usage` journaling this ADR motivated (shipped 2026-06-10)

## Context

Five interactive dogfoods produced five distinct, diagnosed failure classes — invisible decision-parse deaths (`events/0ad6c3fe…`), patch-dialect budget burn (`041fde1e…`), session-dirt crash (`2110f1e1…`), silent context truncation (`63bced3f…`), and mode misrouting (`04849a5a…`) — each fixed, none *guarded*. Today a regression in any of them would only be found by another manual dogfood. Separately, model choice (runner at $1.75–$25/M output; classifier at $0.05/M) is being argued from third-party benchmarks rather than measured on our own tasks.

The 2026-06-07 survey already settled the architecture insight: no platform purchase — the **Verifier is the scorer** and the **journal is the dataset**. What was missing is the harness around them, and (until 2026-06-10) token-usage capture, which has now shipped (`model_usage` events; `TaskState.prompt_tokens`/`completion_tokens`).

## Decision (proposed)

### Eval-0 — the minimal harness

- **Task specs** in `evals/tasks/*.yaml`: `id`, `goal` (or a list, for multi-turn), `task_kind` (or `auto` to exercise routing), fixture files (seeded into a fresh scratch git repo per run), a `success_probe` shell command, budgets, and the source incident it guards.
- **Seed set — one task per dogfood incident:**

  | Task | Guards | Source journal |
  |---|---|---|
  | `create-chatbot` | file creation end-to-end (write_file, native tool-calling) | `0ad6c3fe`, `041fde1e` |
  | `enrich-chatbot` (multi-turn, "Now make the UI richer…") | mode routing, context budgets, modify-existing | `63bced3f`, `04849a5a` |
  | `modify-existing` | apply_patch format / dialect regression (the Begin-Patch tripwire) | `041fde1e` |
  | `investigate-question` | grounded-answer contract, no unintended diff | the original live dogfood |
  | `secret-safety` (fixture contains `.env`) | denylist — zero secret bytes in journal/state | `ff24fa3c` (Phase 2.5) |
  | `session-dirt` (two goals, first edits) | multi-turn §15 | `2110f1e1` |

- **Runner** (`evals/run.py`, `make eval`): per task — mkdtemp scratch git repo from the fixture → `Harness` **strict** mode (`conversational=False`) with a journal → score = **verifier pass AND probe exit 0** → one JSONL row in `evals/results/<ts>-<model>.jsonl` + a console table. Scratch repos also end the dogfood-in-own-repo pathologies (staged-artifact sweeps, "the UI" ambiguity, ghost-file chases).
- **Metrics:** pass@1 (per task + overall) · iterations-to-terminal · wall-clock · **tokens + $/solve** (summed from `model_usage` journal events × the model's price) · a failure-mode histogram classified mechanically from the journal (budget-exhausted / verification-failed / blocked / `decision_error` count / loop score = max repeated-read count).
- **Not in CI.** Costs money, needs keys; manual/nightly. A 2-task `eval-smoke` on the cheapest model may join CI later.

### Eval-1 — the model matrix

`make eval MODELS="…"` → a pass-rate × $/solve grid. Standing matrix: `openai/gpt-5.3-codex` (incumbent baseline, $1.75/$14) · `google/gemini-3.5-flash` ($1.50/$9; top Terminal-Bench model cheaper than codex) · `anthropic/claude-haiku-4.5` ($1/$5) · `deepseek/deepseek-v4-pro` ($0.435/$0.87, the value wildcard). The grid — not third-party leaderboards — picks the default `AVATAR_MODEL`.

### Eval-2 — deferred

Tracer (Langfuse/OTel-GenAI conventions), external comparability (SWE-bench Verified · Terminal-Bench · Aider polyglot), baseline-regression diffing in CI. Per the 2026-06-07 entry, these wait until the internal harness shows real friction.

## Alternatives considered

- **Adopt an eval framework (Inspect, promptfoo, DeepEval):** rejected for Eval-0 — the verifier + journal already provide scoring and trajectories; a framework adds a dependency and a second task format for no marginal signal at 6 tasks. Revisit at Eval-2 scale.
- **Score with an LLM judge:** rejected — the §12 verifier is deterministic and already the product's definition of success; an LLM judge would *reduce* fidelity for these tasks.
- **Run evals in CI:** rejected for now — spend and secrets in the gate; nightly/manual keeps the gate hermetic.

## Consequences

- Every past incident becomes a permanent, cheap regression check; a future fix lands with its eval task the way it lands with its unit tests.
- Model selection becomes an empirical, repeatable decision (`AVATAR_MODEL` chosen by grid, revisited when prices/models shift).
- The journal schema becomes load-bearing for scoring (the failure-mode classifier reads it) — event-type changes must stay backward-readable (`schema_version` already exists).
- Until implemented, the six incidents remain guarded only by unit tests at the component level, not end-to-end.
