"""The per-run result row and its JSONL serialization."""

import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel


class ResultRow(BaseModel):
    """One scored eval run — the unit appended to ``evals/results/<ts>.jsonl``."""

    task: str
    model: str
    seed: int
    solved: bool
    # The two signals `solved` composes, split out so gaming is measurable (ADR-0040). Under
    # auto-approved self-amendment (ADR-0039) a model's own contract passing certifies nothing, so
    # the honest grade is the held-out oracle, not the self-report. `self_reported_success` is the
    # model's own claim (it reached `final_answer` / its own contract passed); `held_out_passed` is
    # the independent hidden oracle's verdict (the success probe, or the verifier when no probe).
    # `gamed` = self_reported ∧ ¬held_out (see `metrics.gamed_rate`). Both default False so rows
    # written before the split load cleanly.
    self_reported_success: bool = False
    held_out_passed: bool = False
    outcome: str | None
    iterations: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Agent-loop wall-clock in seconds (the `session.run()` span, matching the journal event span) —
    # the latency cost axis, distinct from token/$ cost. `None` on rows written before the field
    # existed (or on error rows where the loop did not complete); consumers median over the present
    # ones. Excludes provision/probe overhead so it reflects the model's own latency, not the harness.
    wall_clock_seconds: float | None = None
    probe_exit: int | None = None
    # The declared probe's role (ADR-0020), carried so the failure classifier can distinguish a
    # guard violation (e.g. a secret leaked) from an ordinary success-probe failure (code broken).
    probe_role: str = "success"
    workspace: str | None = None  # the scratch repo this ran in (for inspecting the agent's output)
    # The journal-refined failure bucket (`evals.classify.classify`), computed once at scoring time
    # while the run's journal is still live — so every downstream consumer (histogram, clusterer,
    # report) reads one consistent value instead of re-deriving it and silently dropping to the
    # row-only tier once the scratch journal is cleaned up (ADR-0025). Empty on rows written before
    # the field existed; `evals.classify.resolve_failure_mode` falls back to a row-only classify then.
    failure_mode: str = ""

    def to_jsonl(self) -> str:
        """Serialize to a single JSONL line.

        Returns:
            A one-line JSON string (no trailing newline).
        """
        return self.model_dump_json()


def load_results(path: Path) -> list[ResultRow]:
    """Load result rows from a JSONL file (the inverse of `to_jsonl`).

    The cross-run reader the aggregator/regression-diff builds on — `main()` only ever held
    the current run's rows in memory; this reads a persisted `evals/results/<ts>.jsonl` back.

    Args:
        path: The JSONL results file.

    Returns:
        The rows, in file order (blank lines skipped).
    """
    rows: list[ResultRow] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(ResultRow.model_validate(json.loads(line)))
    return rows


def write_results(rows: Sequence[ResultRow], path: Path) -> None:
    """Write rows as JSONL to `path` (one row per line).

    Args:
        rows: The result rows.
        path: The destination file.
    """
    Path(path).write_text("".join(r.to_jsonl() + "\n" for r in rows), encoding="utf-8")
