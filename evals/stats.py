"""Statistical rigor for eval results — clustered CIs and paired regression testing.

Two things keep eval numbers honest (docs/eval-harness-design.md §8):
- **Clustered 95% CI** for pass@1 — seeds within a task are correlated, so the CI clusters by
  task (naive per-run SEs understate uncertainty). With a single task it degrades to the binomial
  SE (you can't estimate between-cluster variance from one cluster).
- **Paired McNemar** — to tell a real regression from sampling noise, compare two runs *paired* by
  (model, task, seed) and test the discordant pairs with the exact two-sided sign test (stdlib only;
  no SciPy).
"""

import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from typing import NamedTuple

from evals.result import ResultRow

_Z_95 = 1.96
_MIN_CLUSTERS = 2  # need ≥2 tasks to estimate between-cluster variance; else fall back to binomial


class CI(NamedTuple):
    """A point estimate with a confidence interval and its standard error."""

    mean: float
    lo: float
    hi: float
    se: float


class McNemarResult(NamedTuple):
    """The paired discordant counts and exact two-sided p-value of a regression test."""

    regressions: int  # passed in baseline, failed in candidate
    improvements: int  # failed in baseline, passed in candidate
    n_pairs: int
    p_value: float


def mean_ci(rows: Sequence[ResultRow], z: float = _Z_95) -> CI:
    """pass@1 with a task-clustered confidence interval.

    Args:
        rows: The result rows.
        z: The z-multiplier (default 1.96 for ~95%).

    Returns:
        A `CI` over the solved rate; SE clustered by task when ≥2 tasks are present, else the
        binomial SE. Bounds are capped to [0, 1].
    """
    if not rows:
        return CI(0.0, 0.0, 0.0, 0.0)
    n = len(rows)
    mean = sum(1 for r in rows if r.solved) / n
    by_task: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_task[r.task].append(1.0 if r.solved else 0.0)
    cluster_means = [statistics.fmean(v) for v in by_task.values()]
    if len(cluster_means) >= _MIN_CLUSTERS:  # cluster-level SE: each task's pass-rate is one obs
        se = statistics.stdev(cluster_means) / math.sqrt(len(cluster_means))
    else:  # one cluster — fall back to the binomial SE (understates if seeds are correlated)
        se = math.sqrt(mean * (1 - mean) / n)
    return CI(mean, max(0.0, mean - z * se), min(1.0, mean + z * se), se)


def mcnemar(baseline: Sequence[ResultRow], candidate: Sequence[ResultRow]) -> McNemarResult:
    """Paired regression test between two runs, by the exact two-sided sign test.

    Rows are paired by ``(model, task, seed)``; only keys present in both runs count. Pairing
    cancels the large between-task variance, so the test reflects within-pair change.

    Args:
        baseline: The reference run's rows.
        candidate: The new run's rows.

    Returns:
        The discordant counts (`regressions`, `improvements`), number of paired runs, and the
        exact two-sided McNemar p-value.
    """
    base = {_key(r): r.solved for r in baseline}
    cand = {_key(r): r.solved for r in candidate}
    shared = base.keys() & cand.keys()
    regressions = sum(1 for k in shared if base[k] and not cand[k])
    improvements = sum(1 for k in shared if not base[k] and cand[k])
    return McNemarResult(regressions, improvements, len(shared), _sign_test_p(regressions, improvements))


def _key(row: ResultRow) -> tuple[str, str, int]:
    """The pairing key for a row: ``(model, task, seed)``.

    Args:
        row: The result row.

    Returns:
        The (model, task, seed) tuple.
    """
    return (row.model, row.task, row.seed)


def _sign_test_p(b: int, c: int) -> float:
    """Exact two-sided sign-test (McNemar) p-value over discordant counts.

    Args:
        b: Discordant count in one direction (regressions).
        c: Discordant count in the other (improvements).

    Returns:
        The exact two-sided p-value under Binomial(b+c, 0.5); 1.0 when there are no discordant pairs.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) * (0.5**n)
    return min(1.0, 2 * tail)
