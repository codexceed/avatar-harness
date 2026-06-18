"""Failure-mode classification — bucket a non-solved run mechanically, no model.

Base buckets come from the `ResultRow` alone, so they survive workspace cleanup. When the
run's journal events are supplied, an ``incomplete`` run is refined into ``loop_oscillation``
or ``decision_error`` — distinctions only the trajectory reveals.
"""

from collections import Counter
from collections.abc import Sequence

from evals.result import ResultRow

_LOOP_THRESHOLD = 3  # the same action chosen this many times in a run reads as oscillation
_DECISION_ERROR_THRESHOLD = 3  # this many malformed decisions reads as a decision-format problem


def classify(row: ResultRow, events: Sequence[dict] | None = None) -> str:
    """Bucket a run by failure mode (or ``"solved"``).

    Args:
        row: The scored result row.
        events: The run's journal events (optional). When present, an ``incomplete`` run is
            refined into ``loop_oscillation`` / ``decision_error``.

    Returns:
        One of: ``solved``, ``verification_failed``, ``budget_exhausted``, ``loop_oscillation``,
        ``decision_error``, ``blocked``, ``guard_violation``, ``probe_failed``, ``harness_error``,
        ``unknown``.
    """
    if row.solved:
        return "solved"
    outcome = row.outcome or ""
    if outcome.startswith("error"):  # the eval runner caught an exception (e.g. a provider 400)
        return "harness_error"
    # A failed probe is surfaced *before* the outcome dispatch, regardless of outcome — a guard
    # violation (e.g. a secret leaked) must never be hidden under `budget_exhausted` just because
    # the run also ran out of iterations (the Eval-0 leak that 2-of-3 hid behind, ADR-0020/0021).
    if row.probe_exit not in (None, 0):
        # A guard probe (no-leak) failing means the bad thing happened; a success probe failing
        # means the produced code doesn't work — distinct signals, distinct buckets.
        return "guard_violation" if row.probe_role == "guard" else "probe_failed"
    if outcome == "incomplete":
        return _refine_incomplete(events)
    return {"blocked": "blocked", "failed": "verification_failed"}.get(outcome, "unknown")


def _refine_incomplete(events: Sequence[dict] | None) -> str:
    """Refine an ``incomplete`` run into a specific bucket using the journal, if available.

    Args:
        events: The run's journal events, or `None`.

    Returns:
        ``loop_oscillation``, ``decision_error``, or the base ``budget_exhausted``.
    """
    if events:
        actions = [e.get("action") for e in events if e.get("type") == "model_decision" and e.get("action")]
        if actions and max(Counter(actions).values()) >= _LOOP_THRESHOLD:
            return "loop_oscillation"
        if sum(1 for e in events if e.get("type") == "decision_error") >= _DECISION_ERROR_THRESHOLD:
            return "decision_error"
    return "budget_exhausted"


def resolve_failure_mode(row: ResultRow) -> str:
    """The row's bucket, preferring the value persisted at scoring time (ADR-0025).

    The bucket is computed once, when the run is scored and its journal is still live, and
    stored on the row (``failure_mode``). Consumers read it back through here so they all see
    the same journal-refined value — never re-classifying row-only and silently disagreeing
    once the journal is gone. Rows written before the field existed have it empty; for those we
    fall back to a row-only `classify` (the only path that can still reach the legacy data).

    Args:
        row: The scored result row.

    Returns:
        The persisted bucket if present, else a row-only classification.
    """
    return row.failure_mode or classify(row)


def failure_histogram(rows: Sequence[ResultRow]) -> dict[str, int]:
    """Count failure modes across the non-solved rows, reading the persisted bucket.

    Args:
        rows: The result rows (each carrying its scoring-time `failure_mode`).

    Returns:
        A bucket → count mapping over the rows that did not solve (solved runs excluded).
    """
    return dict(Counter(resolve_failure_mode(r) for r in rows if not r.solved))
