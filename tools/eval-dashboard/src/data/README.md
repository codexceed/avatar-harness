# Sample data

`results.sample.jsonl` is a **committed sample** — a verbatim copy of
`evals/results/20260705T151224Z.jsonl` (72 rows = 4 models x 6 tasks x 3 seeds),
copied here because `evals/results/` is gitignored and the prototype must be
self-contained and regenerable-from-JSONL. `results.jsonl.js` is the data loader
that emits it (or the `EVAL_RESULTS` target) as `data/results.jsonl`.

It is exactly the deterministic JSONL the eval runner emits: one row per
`(model, task, seed)`, with fields `task, model, seed, solved, outcome,
iterations, prompt_tokens, completion_tokens, probe_exit, probe_role,
failure_mode, workspace` (schema: `evals/result.py::ResultRow`).

## Point the dashboard at a different run

Pass the target via the `EVAL_RESULTS` argument (from `tools/eval-dashboard/`):

```sh
EVAL_RESULTS=../../evals/results/<stamp>.jsonl npm run build:results   # or dev:results
```

The `results.jsonl.js` loader reads it (unset → this sample) and emits
`data/results.jsonl`, which `src/index.md` loads via `FileAttachment`. No code
changes needed. Alternatively, overwrite `results.sample.jsonl` to change the
default. Full docs: the repo-root `tools/eval-dashboard/README.md`.
