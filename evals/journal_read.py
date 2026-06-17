"""The one streaming reader for run journals — yields events line by line (bounded memory).

A run's journal can reach hundreds of MB (the 875 MB blowup), so consumers must stream it, not
materialize it. Both the eval runner and the distiller read journals through here, so the read
logic lives in exactly one place.
"""

import json
from collections.abc import Iterator
from pathlib import Path

from evals.result import ResultRow


def iter_events(path: Path) -> Iterator[dict]:
    """Stream a journal file's events one line at a time.

    Args:
        path: The journal JSONL file.

    Yields:
        Each non-blank line parsed as a dict, in file order — never the whole file at once.
    """
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def row_events(row: ResultRow) -> Iterator[dict]:
    """Stream a result row's journal events from its scratch repo.

    Args:
        row: The result row (its ``workspace`` points at the scratch repo).

    Yields:
        Each journal event in order; nothing when the workspace or journal is absent.
    """
    if not row.workspace:
        return
    journal = Path(row.workspace) / "journal.jsonl"
    if journal.exists():
        yield from iter_events(journal)
