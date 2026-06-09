"""ReplaySession — a fixed event stream standing in for a live `Session` (Phase 3.1 Lane 2b).

The cockpit consumes a session purely through `events()` (observation) + `resolve_approval`
/ `cancel` (control). `ReplaySession` satisfies that surface from a *fixed list* of events,
so the UI can be driven deterministically with no model or engine — the basis for the
headless `Pilot` tests, and a future `--replay <journal>` viewer. It carries no Textual
import, so it stays usable wherever events are available.
"""

from collections.abc import AsyncIterator, Sequence

from avatar_harness.event_types import HarnessEvent


class ReplaySession:
    """A session-shaped object that replays a pre-recorded event list (no engine).

    Args:
        events: The events to replay, in order, through `events()` (any sequence — a list
            of a single concrete event type is accepted via the covariant `Sequence`).
    """

    def __init__(self, events: Sequence[HarnessEvent]) -> None:
        self._events = list(events)

    async def events(self) -> AsyncIterator[HarnessEvent]:
        """Yield the recorded events in order, then end the stream.

        Yields:
            Each recorded `HarnessEvent`.
        """
        for event in self._events:
            yield event

    async def resolve_approval(self, approval_id: str, *, allow: bool, remember: bool = False) -> None:
        """No-op control plane: a replay has no live gate to resolve.

        Args:
            approval_id: Ignored — present to match the control surface.
            allow: Ignored.
            remember: Ignored.
        """

    async def cancel(self, reason: str = "cancelled") -> None:
        """No-op control plane: a replay cannot be cancelled.

        Args:
            reason: Ignored — present to match the control surface.
        """
