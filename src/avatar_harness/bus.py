"""EventBus — bounded, non-blocking fan-out + one privileged journal (ADR-0001, Phase 3.1).

Stamps the global order (`event_id`) on every event, appends it losslessly to the
privileged `JsonlEventJournal` (if wired), then fans it out to each subscriber's
**bounded** queue. The bound is *soft*: only droppable `*_update` events are shed when a
subscriber is at its cap; lifecycle/control events are always enqueued (they may exceed
the cap). A slow or broken subscriber can therefore never stall publishing or its peers,
and a subscriber's drops show up as `event_id` gaps — never silent reordering. The
journal stays lossless regardless of what any subscriber sheds.

This was the foundation's simple unbounded fan-out (lived in `session.py`); Lane 1 grew
it into its own module behind the same `events()` / `EventSink` API.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from avatar_harness.event_types import HarnessEvent
from avatar_harness.journal import JsonlEventJournal

# Generous default cap: a normal run never drops; the cap only matters under a flood of
# streamed updates feeding a subscriber that can't keep up.
_DEFAULT_MAX_QUEUE = 1024

# Only streamed `*_update` deltas are sheddable; everything else is lifecycle/control and
# must reach every subscriber (the renderer can tolerate missing deltas, not missing a
# phase change or an approval prompt).
_DROPPABLE_TYPES = frozenset({"model_update"})


@dataclass
class _Subscription:
    """One observer's bounded queue and its soft cap."""

    queue: asyncio.Queue
    max_queue: int


class EventBus:
    """Stamps, journals, and **fans events to every subscriber's bounded queue** (§13).

    Each `subscribe()` gets its own queue, so independent observers — a TUI, a telemetry
    exporter, a benchmark collector — each see the same stream rather than competing for
    one. Fan-out is non-blocking: a full subscriber sheds droppable updates instead of
    blocking the publisher. A late subscriber sees only events published after it
    subscribed; the `journal` (and the in-memory `history`) is the lossless record.

    Args:
        session_id: Stamped on every event so a stream/journal groups back to its run.
        journal: The privileged lossless sink; every published event is appended to it
            in order before fan-out. `None` disables on-disk journaling (the foundation
            behavior — in-memory `history` is still kept).
    """

    def __init__(self, session_id: str, *, journal: JsonlEventJournal | None = None) -> None:
        self.session_id = session_id
        self.journal = journal
        self.history: list[HarnessEvent] = []
        self._subscribers: list[_Subscription] = []
        self._next_id = 0
        self._closed = False

    def subscribe(self, *, max_queue: int | None = None) -> "asyncio.Queue":
        """Register an independent consumer and return its bounded queue.

        Args:
            max_queue: The soft cap for this consumer (droppable updates are shed above
                it). Defaults to a generous value that never drops a normal-size stream.

        Returns:
            A queue that will receive every event published from now on (subject to the
            drop policy for `*_update` events), then a `None` close sentinel. An
            already-closed bus returns a queue holding only the sentinel.
        """
        queue: asyncio.Queue = asyncio.Queue()
        if self._closed:
            queue.put_nowait(None)
        else:
            self._subscribers.append(_Subscription(queue=queue, max_queue=max_queue or _DEFAULT_MAX_QUEUE))
        return queue

    def publish_nowait(self, draft: HarnessEvent) -> HarnessEvent:
        """Stamp `draft`, journal it losslessly, and fan it out (never blocks, §13).

        Args:
            draft: The event to publish; mutated in place with the ordering keys.

        Returns:
            The stamped event.
        """
        self._next_id += 1
        draft.event_id = self._next_id
        draft.session_id = self.session_id
        draft.ts = datetime.now(UTC)
        self.history.append(draft)
        if self.journal is not None:
            self.journal.append(draft)  # lossless commit before any (lossy) fan-out
        droppable = draft.type in _DROPPABLE_TYPES
        for sub in self._subscribers:
            if droppable and sub.queue.qsize() >= sub.max_queue:
                continue  # shed under pressure — visible later as an event_id gap
            sub.queue.put_nowait(draft)
        return draft

    async def emit(self, draft: HarnessEvent) -> HarnessEvent:
        """Awaitable publish — the frozen async interface (delegates to `publish_nowait`).

        Args:
            draft: The event to publish.

        Returns:
            The stamped event.
        """
        return self.publish_nowait(draft)

    def close(self) -> None:
        """Signal end-of-stream to every subscriber and close the journal (idempotent)."""
        self._closed = True
        for sub in self._subscribers:
            sub.queue.put_nowait(None)
        if self.journal is not None:
            self.journal.close()
