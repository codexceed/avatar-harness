"""EventLog — a JSONL subscriber to the emitter (§13).

Writes one JSON object per event, append-only, for replay, debugging, audit,
and eval data. It is a plain subscriber: it observes, never influences.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from avatar_harness.events import Event


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: Event) -> None:
        # Stamp each record with a UTC timestamp at the persistence boundary, so the
        # JSONL is replayable and ordered in wall-clock time. `ts` leads each line.
        record = {"ts": datetime.now(UTC).isoformat(), **event}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
