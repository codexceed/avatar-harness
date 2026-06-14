"""Eval-0 — a model-agnostic, deterministic-verifier eval harness (docs/eval-harness-design.md).

Dev tooling deliberately outside the shipped ``src/avatar_harness`` package: it drives the
public ``Harness`` facade, scores with the harness's own ``Verifier`` plus a success probe,
and reports pass@1 / pass^k across a model matrix.
"""
