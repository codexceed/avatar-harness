"""EventLog — a JSONL subscriber to the emitter (§13).

Writes one JSON object per event, append-only, for replay, debugging, audit,
and eval data. It is a plain subscriber: it observes, never influences.
"""

import json
from pathlib import Path

from avatar_harness.events import Event


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: Event) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
