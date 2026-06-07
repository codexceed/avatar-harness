"""Lifecycle event emitter — observation only (§13).

Synchronous, fire-and-forget. Subscribers react to what happened; they can
*never* block or redirect the loop. ``emit`` returns ``None`` by design: there
is no value a caller could branch on. This is the deliberate, narrow line that
keeps control out of the emitter — permission and context are awaited control
hooks elsewhere, not subscribers here.
"""

import contextlib
from collections.abc import Callable

Event = dict[str, object]  # {"type": str, ...payload}
Subscriber = Callable[[Event], None]


class Emitter:
    """Fans lifecycle events out to observation-only subscribers (§13)."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> None:
        """Register a subscriber to receive every subsequent event."""
        self._subscribers.append(subscriber)

    def emit(self, event_type: str, **payload: object) -> None:
        """Build the event and deliver it to every subscriber, isolating failures."""
        event: Event = {"type": event_type, **payload}
        for subscriber in self._subscribers:
            # A faulty subscriber must never break emission to others or the loop.
            with contextlib.suppress(Exception):
                subscriber(event)
