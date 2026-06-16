"""EventLog — a JSONL subscriber to the emitter (§13).

Writes one JSON object per event, append-only, for replay, debugging, audit,
and eval data. It is a plain subscriber: it observes, never influences.
"""

import json
from pathlib import Path

from pydantic import BaseModel

from avatar.events import Event


class EventLog:
    """An append-only JSONL subscriber: one timestamped record per event (§13).

    Accepts both the sync `Emitter`'s raw-dict events and typed `HarnessEvent`
    models (Phase 3.0) — a typed event is serialized via `model_dump_json`, so the
    same log can be reloaded by `event_types.load_events`.

    Args:
        path: JSONL file the events are appended to.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: Event | BaseModel) -> None:
        """Append `event` to the log as one JSON line, preserving its emitted `ts`.

        Args:
            event: The event to persist verbatim — a raw-dict event or a typed model.
        """
        # The Emitter already stamped `ts` at emission; persist the event verbatim so
        # the log reflects when the event happened, not when it was flushed to disk.
        line = event.model_dump_json() if isinstance(event, BaseModel) else json.dumps(event)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
