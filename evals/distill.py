"""Journal events → a compact trajectory digest (deterministic, no model).

A run's journal can reach hundreds of MB (the 875 MB blowup), so an agent cannot ingest it.
`distill` makes a **single streaming pass** — it accumulates only bounded counters and a capped
action sample, discarding each event (especially `tool_end.content`) immediately — so peak memory
is bounded by the digest, not the journal. The digest keeps the shape an analysis subagent needs
— ordered actions plus repeat/failure/error counts and the token curve — at KB scale. See
ADR-0024 / the design doc §4.

``python -m evals.distill <results>.jsonl`` writes a sibling ``<results>.digests.jsonl``.
"""

import argparse
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from evals.journal_read import row_events
from evals.result import ResultRow, load_results

_ACTION_CAP = 100  # store at most this many action strings (repeat counts span the whole run)
_ACTION_CHARS = 120  # truncate each stored action to this many chars


class TrajectoryDigest(BaseModel):
    """The compact, agent-ingestible summary of one run's trajectory."""

    task: str
    model: str
    seed: int
    outcome: str | None
    iterations: int
    actions: list[str] = Field(default_factory=list)
    tool_calls: int = 0
    tool_failures: int = 0
    repeated_action_max: int = 0  # max times any single action recurred (oscillation signal)
    decision_errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def to_jsonl(self) -> str:
        """Serialize to a single JSONL line.

        Returns:
            A one-line JSON string (no trailing newline).
        """
        return self.model_dump_json()


def distill(row: ResultRow, events: Iterable[dict] | None = None) -> TrajectoryDigest:
    """Distill one run to a compact digest in a single streaming pass.

    Iterates `events` exactly once (so it can consume a one-shot generator straight off the
    journal), keeping only bounded state: per-action counts, a capped/truncated action sample,
    and call/failure/error tallies. Bulky `tool_end` payloads are never retained.

    Args:
        row: The scored result row (carries outcome, iterations, tokens).
        events: The run's journal events (any iterable); when absent, the digest is row-only.

    Returns:
        A `TrajectoryDigest`.
    """
    counts: Counter[str] = Counter()
    sample: list[str] = []
    n_actions = tool_calls = tool_failures = decision_errors = 0
    for event in events or ():
        etype = event.get("type")
        if etype == "model_decision" and event.get("action"):
            action = str(event["action"])
            counts[action] += 1
            n_actions += 1
            if len(sample) < _ACTION_CAP:
                sample.append(action[:_ACTION_CHARS])
        elif etype == "tool_end":
            tool_calls += 1
            if event.get("success") is False:
                tool_failures += 1
        elif etype == "decision_error":
            decision_errors += 1
    if n_actions > _ACTION_CAP:
        sample.append(f"… [{n_actions - _ACTION_CAP} more actions omitted]")
    return TrajectoryDigest(
        task=row.task,
        model=row.model,
        seed=row.seed,
        outcome=row.outcome,
        iterations=row.iterations,
        actions=sample,
        tool_calls=tool_calls,
        tool_failures=tool_failures,
        repeated_action_max=max(counts.values()) if counts else 0,
        decision_errors=decision_errors,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
    )


def distill_results(
    rows: Sequence[ResultRow],
    events_for: Callable[[ResultRow], Iterable[dict] | None] | None = None,
) -> list[TrajectoryDigest]:
    """Distill every row, resolving each row's journal events via `events_for`.

    Args:
        rows: The result rows.
        events_for: Resolver of a row's journal events (the runner streams them from the row's
            scratch repo); `None` distills row-only.

    Returns:
        One digest per row, in order.
    """
    return [distill(r, events_for(r) if events_for else None) for r in rows]


def main(argv: list[str] | None = None) -> int:
    """Distill a results file's journals to a sibling ``.digests.jsonl``.

    Args:
        argv: CLI args (a results JSONL path); `None` uses ``sys.argv``.

    Returns:
        Process exit code (0).
    """
    parser = argparse.ArgumentParser(
        prog="evals.distill", description="Distill run journals to compact digests."
    )
    parser.add_argument(
        "results", help="a results JSONL file (digests stream each row's scratch-repo journal)"
    )
    args = parser.parse_args(argv)
    digests = distill_results(load_results(Path(args.results)), events_for=row_events)
    out = Path(args.results).with_suffix(".digests.jsonl")
    out.write_text("".join(d.to_jsonl() + "\n" for d in digests), encoding="utf-8")
    print(f"wrote {len(digests)} digests -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
