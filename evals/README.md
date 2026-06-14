# `evals/` — the Eval-0 harness

A small, self-contained tool that **measures the agent harness** by running it on fixed tasks
and scoring the results deterministically. It is intentionally an **independent entity** from the
agent harness in `src/avatar_harness/`: it imports only the public `Harness` facade, ships no
runtime code into the package, and can be reasoned about (and run) on its own.

The design rationale lives in [`../docs/eval-harness-design.md`](../docs/eval-harness-design.md)
(ADR-0004, ADR-0011, ADR-0012).

---

## What it does

For each task it: provisions a **fresh, clean scratch git repo**, runs the agent on the task,
then **scores the result deterministically — no LLM judge**:

- If the task declares a **success probe**, the probe is authoritative (`solved = probe exits 0`)
  and the agent runs **non-strict** (it delivers its best; we grade it blind).
- If there is no probe, the harness's own **`Verifier`** decides (e.g. an `investigate` task's
  grounded-answer gate).

It reports **pass@1** (capability) and **pass^k** (reliability — all *k* seeds pass) per model.

---

## Quick start

```bash
# Single model (defaults to AVATAR_MODEL from your .env), 3 seeds:
make eval

# A model matrix (the sonnet-class trio), 3 seeds each:
make eval MODELS="openai/gpt-5.1,anthropic/claude-sonnet-4-6,google/gemini-3.1-pro-preview" SEEDS=3

# make passthroughs: MODELS=, SEEDS=, TEMPERATURE=, WORKSPACE=, NO_CLEANUP=1 (keep output)
make eval MODELS="openai/gpt-5.1" SEEDS=1 NO_CLEANUP=1

# Or invoke the module directly:
uv run python -m evals.run --models "openai/gpt-5.1" --seeds 1 --no-cleanup
```

**Requirements:** `AVATAR_API_KEY` (+ `AVATAR_BASE_URL`, default OpenRouter) in `.env` — the same
credentials the agent harness uses. Runs cost real API spend (the agent's model calls); the probe
does **not** (it mocks the network — see *Probes*).

### Flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--models a,b,c` | `AVATAR_MODEL` | Comma-separated model ids to run as a matrix. |
| `--seeds N` | `3` | Repetitions per task (see *Seeds & temperature*). |
| `--temperature T` | `0.7` | Sampling temperature; `>0` makes each seed an independent draw. Pass `0` for a deterministic run. |
| `--workspace PATH` | `./eval_run_<timestamp>` | Where scratch repos are created. |
| `--no-cleanup` | (cleanup on) | Keep the run workspace for inspection instead of deleting it. |

### Seeds & temperature

A **seed** here is one **repetition** of the same task — running N is how we measure *reliability*,
not just capability:

- **pass@1** (capability) — averaged over seeds: *can* it do this?
- **pass^k** (reliability) — *all k* seeds pass: does it work *every time*?

For seeds to be meaningful they must be **independent samples**, which needs **`temperature > 0`**
(eval default `0.7`). At `--temperature 0` the runs are as identical as the provider allows, so
`pass^k` then reflects only *provider noise*, not the model's behavior. (The first baseline was run
at temp 0 — read those 3/3s as "consistent under provider noise," not behavioral reliability.)

---

## Run workspace & cleanup

Each run uses a **run workspace** that holds one scratch git repo per `(model, task, seed)`,
named `…/<model>__<task>__seedN__<rand>/` so it's easy to find the agent's output.

- **Default:** an auto-generated `eval_run_<timestamp>/` in the current directory, **deleted on
  exit** (tidy). Pass `--no-cleanup` to keep it.
- **`--workspace PATH`:** use `PATH` instead. Cleanup only ever removes what the runner *created* —
  a pre-existing directory and its contents are never deleted (only the per-run scratch repos under
  it are, and only when cleanup is on).
- Each result row records its scratch repo path in `workspace`, so you can map a row → its files.

```bash
# Keep outputs to inspect what the agent wrote:
uv run python -m evals.run --models openai/gpt-5.1 --seeds 1 --no-cleanup
#   -> run workspace kept: /…/eval_run_20260614T….  ls it for chatbot.py, journal.jsonl, etc.
```

> A run that dies on the first model call (`iterations: 0`, e.g. a transient provider error) writes
> nothing — its scratch repo is empty.

---

## Results & metrics

Every run appends one **JSONL** file to `evals/results/<timestamp>.jsonl` (git-ignored, **persists
across cleanup** — only scratch repos are cleaned). One row per `(model, task, seed)`:

```json
{"task":"create-chatbot","model":"openai/gpt-5.1","seed":0,"solved":true,
 "outcome":"success","iterations":5,"prompt_tokens":9990,"completion_tokens":1924,
 "probe_exit":0,"workspace":"/…/eval_run_…/openai-gpt-5.1__create-chatbot__seed0__ab12"}
```

The runner prints a per-model summary (`pass@1`, `pass^k`) and a **failure-mode histogram** over the
non-solved runs (`verification_failed`, `budget_exhausted`, `loop_oscillation`, `decision_error`,
`blocked`, `probe_failed`, `harness_error`). A bad model slug or run error becomes an
`outcome: "error: …"` row (`harness_error`) and the matrix continues.

> **Cross-run reading:** `evals.result.load_results(path)` reads a `<ts>.jsonl` back into
> `ResultRow`s (the inverse of `to_jsonl`). The run summary still aggregates only the current run's
> rows; a **regression-diff vs. a previous baseline** (built on `load_results`) is the next Slice-2
> step. Inspect past runs directly with `cat evals/results/*.jsonl`.

---

## Task specs

Tasks are **TOML** files in `evals/tasks/` (stdlib `tomllib` — zero extra deps).

```toml
id = "create-chatbot"                 # required
goal = "Create a runnable …"          # required: the prompt the agent receives
task_kind = "edit"                    # edit | investigate | test_only
fixture = "empty"                     # "empty" (bare repo) or a dir name under evals/fixtures/
success_probe = "python evals/probes/chatbot_smoke.py chatbot.py"   # deterministic grader

[budgets]                             # override harness budgets for this task
max_iterations = 30
max_wall_clock_seconds = 300

[env]                                 # runtime env for the program under test (you set it;
OPENAI_API_KEY = "sk-eval-dummy"      # never shown to the agent), e.g. so os.environ[…] won't crash

# Optional, for later slices (SWE-bench partition + ADR-0011 integrity):
#   fail_to_pass = []   pass_to_pass = []   oracle = []   hidden = []
```

### Adding a task

1. Drop a `<id>.toml` in `evals/tasks/`.
2. If it needs functional grading, add a probe under `evals/probes/` and point `success_probe` at it
   (the probe runs with the scratch repo as cwd; reference its script path as `evals/probes/…`).
3. Run `make eval` — the new task is picked up automatically.

---

## Probes

A **probe** is a deterministic, post-run check that the agent's output actually works. It runs
*after* the agent finishes, in the scratch repo, and exits 0 (solved) or non-zero.

The `create-chatbot` probe (`probes/chatbot_smoke.py`) is **functional**: it swaps the `openai`
module for a mock that records calls, runs the agent's `chatbot.py`, and passes only if a
chat-completions call actually fired (a turn round-tripped) — stricter than "it imports a client."
It mocks at the **library** level today; the wire-level alternative (a fake OpenAI-compatible
server) is recorded as a deferred decision in **ADR-0012**.

---

## Layout

```text
evals/
  README.md          this file
  spec.py            TaskSpec + TOML loader
  provision.py       fresh clean scratch git repo per run
  score.py           is_solved (verifier/probe) + run_probe
  result.py          ResultRow (+ JSONL load/write)
  metrics.py         pass@1, pass^k
  classify.py        failure-mode bucketing + histogram
  run.py             run_task + the `make eval` matrix driver (main)
  tasks/*.toml       task specs
  probes/*.py        deterministic success probes
  fixtures/<name>/   starter repo trees (optional; "empty" = bare repo)
  results/*.jsonl    run outputs (git-ignored)
```

## Relationship to the agent harness

`evals/` is **dev/eval tooling, not shipped code.** It lives outside `src/avatar_harness/`, is not
type-checked as part of the package (only lint-gated by ruff), and depends on the harness solely
through the public facade (`Harness(config=…).session(...)`). Deleting `evals/` would not affect the
shipped harness in any way.
