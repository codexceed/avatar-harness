# `tetris-playable` × `z-ai/glm-5.2` seed 0

- **Date:** 2026-07-12
- **Status:** measured — one development cell, no capability generalization
- **Result row:** `evals/results/20260712T132907Z.jsonl`
- **Summary:** `evals/results/20260712T132907Z.summary.json`
- **Raw artifact:** `eval_run_20260712T132907Z/z-ai-glm-5.2__tetris-playable__seed0___a1lswk4/`
- **Journal:** `eval_run_20260712T132907Z/z-ai-glm-5.2__tetris-playable__seed0___a1lswk4/journal.jsonl`
- **Reproduce:** `make eval TASKS=tetris-playable SEEDS=1 MODELS=z-ai/glm-5.2 NO_CLEANUP=1`

## Measured result

The cell did **not solve** the task: `solved=false`, `probe_exit=1`, and
`failure_mode="probe_failed"`. The harness run itself reached `outcome="success"` after 23
iterations and 715.93 seconds, using 197,773 prompt tokens and 51,224 completion tokens. The
single-cell aggregate is therefore pass@1 = 0.00; one seed is not a model-level estimate.

The declared verification contract passed before the held-out success probe ran. Manual replay
of the same probe against the kept workspace passed README, boot/determinism, movement, and
rotation, then failed at the 7-bag phase:

```text
probe: 7-bag: no flushed frame arrived after a key; input may be buffered until EOF
```

The generated `tetris.py` implements scripted input as `data = sys.stdin.buffer.read()` and only
processes the buffered bytes after EOF. That satisfies batch invocations which close stdin, but
violates the task's explicit streaming requirement: each recognized key must produce and flush a
frame before the next key is read. The live 7-bag driver deliberately holds stdin open between
keys, so the game deadlocks at that boundary and the probe rejects it.

## Interpretation

This is a genuine task failure, not evidence of an over-prescribed gameplay ruleset: the failure
is at the minimal black-box transport seam, before the probe evaluates bag order, line clearing,
score values, or interactive terminal behavior. It also demonstrates why the external probe adds
signal beyond model-authored verification: all declared checks passed while the human-like
streaming interaction did not.
