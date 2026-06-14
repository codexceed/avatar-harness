"""Regression-diff between two result files — `python -m evals.diff base.jsonl cand.jsonl`.

Loads two persisted runs and reports, per model and overall: pass@1 with clustered 95% CIs, the
delta, and the paired-McNemar verdict (real change vs. sampling noise). Read-only; no spend.
"""

import argparse
from pathlib import Path

from evals.result import ResultRow, load_results
from evals.stats import mcnemar, mean_ci

_ALPHA = 0.05


def _verdict(base: list[ResultRow], cand: list[ResultRow]) -> str:
    """One line comparing two runs: pass@1 ± CI on each side, delta, and the McNemar verdict.

    Args:
        base: The baseline run's rows.
        cand: The candidate run's rows.

    Returns:
        A formatted summary line.
    """
    b, c = mean_ci(base), mean_ci(cand)
    mc = mcnemar(base, cand)
    if mc.p_value < _ALPHA and mc.regressions != mc.improvements:
        tag = "REGRESSION" if mc.regressions > mc.improvements else "IMPROVEMENT"
    else:
        tag = "no significant change"
    return (
        f"pass@1 {b.mean:.2f} [{b.lo:.2f},{b.hi:.2f}] -> {c.mean:.2f} [{c.lo:.2f},{c.hi:.2f}]  "
        f"(Δ{c.mean - b.mean:+.2f})  "
        f"reg={mc.regressions} imp={mc.improvements} n={mc.n_pairs} p={mc.p_value:.3f}  ->  {tag}"
    )


def main(argv: list[str] | None = None) -> int:
    """Compare two result files and print per-model + overall regression verdicts.

    Args:
        argv: ``[baseline.jsonl, candidate.jsonl]``; `None` uses ``sys.argv``.

    Returns:
        0 always (a report, not a gate).
    """
    parser = argparse.ArgumentParser(prog="evals.diff", description="Regression-diff two result files.")
    parser.add_argument("baseline", help="baseline results JSONL")
    parser.add_argument("candidate", help="candidate results JSONL")
    args = parser.parse_args(argv)

    base = load_results(Path(args.baseline))
    cand = load_results(Path(args.candidate))
    models = sorted({r.model for r in base} | {r.model for r in cand})
    for model in models:
        bm = [r for r in base if r.model == model]
        cm = [r for r in cand if r.model == model]
        print(f"{model}:  {_verdict(bm, cm)}")
    print(f"overall:  {_verdict(base, cand)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
