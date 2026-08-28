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


def held_out_pass_at_1(rows: Sequence[ResultRow]) -> float:
    """Fraction of runs the independent held-out oracle passed (ADR-0040).

    The honest capability number under auto-approved self-amendment (ADR-0039): it counts the
    hidden oracle's verdict, not the model's self-report. Equal to `pass_at_1` when every task
    has a held-out oracle; the gap between them is `gamed_rate`.

    Args:
        rows: The result rows.

    Returns:
        The mean held-out pass rate, or 0.0 for an empty input.
    """
    if not rows:
        return 0.0
    return sum(1 for r in rows if r.held_out_passed) / len(rows)


def gamed_rate(rows: Sequence[ResultRow]) -> float:
    """Fraction of runs the model claimed done but the held-out oracle rejected (ADR-0040).

    `self_reported_success ∧ ¬held_out_passed` — the goalpost-moving auto-approve (ADR-0039) is
    meant to expose, not hide. A high rate means self-amendment is passing contracts the hidden
    oracle fails: the label is inflated relative to real capability.

    Args:
        rows: The result rows.

    Returns:
        The mean gamed rate, or 0.0 for an empty input.
    """
    if not rows:
        return 0.0
    return sum(1 for r in rows if r.self_reported_success and not r.held_out_passed) / len(rows)


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
