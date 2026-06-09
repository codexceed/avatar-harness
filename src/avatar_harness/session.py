"""Session — the two-plane interactive boundary over one task run (ADR-0001/0002, Phase 3.0).

Observation flows OUT via `events()` (an async stream that can never block or
redirect the run); control flows IN via `resolve_approval()` and `cancel()`. An
event may *announce* that approval is needed, but only the control method decides
it (§13). The cockpit binds to exactly this surface; the engine stays unchanged.

`EventBus` is the foundation's deliberately-simple fan-out: one unbounded queue and
a monotonic `event_id` stamp. Lane 1 (ADR-0001) replaces it with bounded
per-subscriber queues + the privileged write-ahead journal, behind this same API.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

from avatar_harness.event_types import (
    ApprovalRequested,
    ApprovalResolved,
    HarnessEvent,
)
from avatar_harness.runner import AgentRunner
from avatar_harness.state import TaskState


class EventBus:
    """Stamps, orders, and fans typed events to a single async consumer (foundation).

    Args:
        session_id: Stamped on every event so a stream/journal groups back to its run.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.history: list[HarnessEvent] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._next_id = 0

    def publish_nowait(self, draft: HarnessEvent) -> HarnessEvent:
        """Stamp `draft` with the next id/session/ts and enqueue it (never blocks, §13).

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
        self._queue.put_nowait(draft)
        return draft

    async def emit(self, draft: HarnessEvent) -> HarnessEvent:
        """Awaitable publish — the frozen async interface (lane 1 adds backpressure).

        Args:
            draft: The event to publish.

        Returns:
            The stamped event.
        """
        return self.publish_nowait(draft)

    def close(self) -> None:
        """Signal end-of-stream so a consuming `stream()` terminates."""
        self._queue.put_nowait(None)

    async def stream(self) -> AsyncIterator[HarnessEvent]:
        """Yield events in publish order until the bus is closed.

        Yields:
            Each stamped event, oldest first, until the close sentinel.
        """
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class Session:
    """A live, interruptible task run exposing the two-plane API (§13, §23).

    Args:
        runner: The `AgentRunner` to drive (its `event_sink`/`approval_controller`
            are wired to this session for the duration of `run`).
        state: The task state to execute.
        session_id: Stable id stamped on events; generated if omitted.
    """

    def __init__(self, runner: AgentRunner, state: TaskState, *, session_id: str | None = None) -> None:
        self.runner = runner
        self.state = state
        self.session_id = session_id or uuid4().hex
        self.bus = EventBus(self.session_id)
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self.cancel_reason: str | None = None  # set by cancel(); the loop records its own feedback

    def events(self) -> AsyncIterator[HarnessEvent]:
        """The observation plane: an async stream of typed events (cannot alter control).

        Returns:
            An async iterator over this run's `HarnessEvent`s, ending on `agent_end`.
        """
        return self.bus.stream()

    async def run(self) -> TaskState:
        """Drive the run with this session as the event sink + approval controller.

        Returns:
            The terminal `TaskState`.
        """
        self.runner.event_sink = self.bus
        self.runner.approval_controller = self
        try:
            return await self.runner.arun(self.state)
        finally:
            self.bus.close()  # terminate any open events() stream

    async def request_approval(self, approval_id: str, tool: str, reason: str, tool_input: dict) -> bool:
        """Announce a gated call (observation) and await the human's decision (control).

        Called by the runner on a tier-3 `ask`. Emits `ApprovalRequested`, then blocks
        *this run only* until `resolve_approval` completes the future — the decision
        never returns through the event stream (§13).

        Args:
            approval_id: Correlates the announcement with its resolution.
            tool: The tool name awaiting approval.
            reason: The gate's reason, shown to the human.
            tool_input: The proposed call arguments.

        Returns:
            True iff the call was allowed.
        """
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = future
        self.bus.publish_nowait(
            ApprovalRequested(
                task_id=self.state.task_id,
                approval_id=approval_id,
                tool=tool,
                reason=reason,
                input=tool_input,
            )
        )
        allowed = await future
        self.bus.publish_nowait(
            ApprovalResolved(task_id=self.state.task_id, approval_id=approval_id, allowed=allowed)
        )
        return allowed

    async def resolve_approval(self, approval_id: str, *, allow: bool) -> None:
        """Control plane: resolve a pending approval (the decision the event announced).

        Tolerant of an unknown id (resolves the sole pending request if there is exactly
        one) so a caller that didn't capture the id can still unblock the run.

        Args:
            approval_id: The id from the `ApprovalRequested` event.
            allow: Whether to permit the gated call.
        """
        future = self._pending.get(approval_id)
        if future is None and len(self._pending) == 1:
            future = next(iter(self._pending.values()))
        if future is not None and not future.done():
            future.set_result(allow)

    async def cancel(self, reason: str = "cancelled") -> None:
        """Control plane: trip the cancellation token; the loop observes it at the next turn.

        Also denies any in-flight approval so a run blocked on the gate can reach the
        cancellation checkpoint rather than hanging.

        Args:
            reason: Why the run is being cancelled (retained for display; the loop records
                its own cancellation feedback when it observes the token).
        """
        self.cancel_reason = reason
        self.runner.deps.cancellation.cancel()
        for future in self._pending.values():
            if not future.done():
                future.set_result(False)
