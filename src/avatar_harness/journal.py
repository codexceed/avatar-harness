"""JsonlEventJournal — the privileged, lossless write-ahead sink (ADR-0001, Phase 3.1).

Unlike an observation subscriber (bounded, droppable), the journal is *part of the
engine*: every published event is appended in global order and **flushed per event**,
so a crash mid-run leaves a durable, replayable prefix. It is the substrate 3.3's
semantics-aware resume will replay (`load_events`); Lane 1 builds the record, not the
replay engine.

It keeps the file handle open for the run (append mode) and flushes on every write —
the write-ahead property — rather than reopening per event like the sync-`Emitter`
`EventLog`. The two are deliberately distinct: `EventLog` is an emitter *subscriber*;
this is the engine's durable commit log for the typed `HarnessEvent` stream.
"""

from pathlib import Path
from typing import TextIO

from avatar_harness.event_types import HarnessEvent, dump_event


class JsonlEventJournal:
    """An append-only, per-event-flushed JSONL record of the typed event stream.

    Args:
        path: JSONL file the events are appended to (parent dirs are created).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: TextIO | None = self.path.open("a", encoding="utf-8")

    def append(self, event: HarnessEvent) -> None:
        """Append `event` as one JSON line and flush it to disk (write-ahead).

        Args:
            event: The stamped event to persist (must carry its ordering keys).
        """
        if self._handle is None:
            self._handle = self.path.open("a", encoding="utf-8")
        self._handle.write(dump_event(event) + "\n")
        self._handle.flush()  # durable immediately — a crash leaves a usable prefix

    def close(self) -> None:
        """Close the journal file handle (idempotent)."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None
