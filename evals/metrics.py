"""Aggregate metrics — pass@1 (capability) and pass^k (reliability).

pass@1 is the fraction of *runs* solved; pass^k is the fraction of *tasks* solved on
*every* seed — the "works every time" metric the dogfood showed we lack.
"""

from collections import defaultdict
from collections.abc import Sequence

from evals.result import ResultRow


def pass_at_1(rows: Sequence[ResultRow]) -> float:
    """Fraction of runs that were solved.

    Args:
        rows: The result rows.

    Returns:
        The mean solved rate, or 0.0 for an empty input.
    """
    if not rows:
        return 0.0
    return sum(1 for r in rows if r.solved) / len(rows)


def pass_caret_k(rows: Sequence[ResultRow]) -> float:
    """Fraction of tasks whose every seed was solved (reliability, not capability).

    Args:
        rows: The result rows across tasks and seeds.

    Returns:
        The fraction of tasks solved on all their seeds, or 0.0 for an empty input.
    """
    by_task: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_task[r.task].append(r.solved)
    if not by_task:
        return 0.0
    return sum(1 for solved in by_task.values() if all(solved)) / len(by_task)
