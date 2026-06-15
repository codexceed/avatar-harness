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
    outcome: str | None
    iterations: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    probe_exit: int | None = None
    # The declared probe's role (ADR-0018), carried so the failure classifier can distinguish a
    # guard violation (e.g. a secret leaked) from an ordinary success-probe failure (code broken).
    probe_role: str = "success"
    workspace: str | None = None  # the scratch repo this ran in (for inspecting the agent's output)

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
