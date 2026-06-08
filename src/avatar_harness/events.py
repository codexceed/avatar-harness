"""Lifecycle event emitter — observation only (§13).

Synchronous, fire-and-forget. Subscribers react to what happened; they can
*never* block or redirect the loop. ``emit`` returns ``None`` by design: there
is no value a caller could branch on. This is the deliberate, narrow line that
keeps control out of the emitter — permission and context are awaited control
hooks elsewhere, not subscribers here.
"""

import contextlib
from collections.abc import Callable
from datetime import UTC, datetime

Event = dict[str, object]  # {"type": str, "ts": str, ...payload}
Subscriber = Callable[[Event], None]


class Emitter:
    """Fans lifecycle events out to observation-only subscribers (§13).

    Args:
        session_id: Stamped on every emitted event to group a run's events. A run is
            one process invocation; in a future REPL it is the long-lived process, so
            grouping is intentional rather than incidental. `None` omits the key.
    """

    def __init__(self, session_id: str | None = None) -> None:
        self._subscribers: list[Subscriber] = []
        self._session_id = session_id

    def subscribe(self, subscriber: Subscriber) -> None:
        """Register a subscriber to receive every subsequent event.

        Args:
            subscriber: Callable invoked with each emitted event.
        """
        self._subscribers.append(subscriber)

    def emit(self, event_type: str, **payload: object) -> None:
        """Build the event and deliver it to every subscriber, isolating failures.

        Args:
            event_type: The event's `type` tag.
            **payload: Extra event fields merged after `type` and `ts`.
        """
        # Stamp the event once, at emission, so every subscriber (console + log) sees
        # the same wall-clock time the thing actually happened — not a per-sink re-stamp.
        # `type`, `ts`, and (when set) `session_id` lead each event as its grouping keys.
        event: Event = {"type": event_type, "ts": datetime.now(UTC).isoformat()}
        if self._session_id is not None:
            event["session_id"] = self._session_id
        event.update(payload)
        for subscriber in self._subscribers:
            # A faulty subscriber must never break emission to others or the loop.
            with contextlib.suppress(Exception):
                subscriber(event)
