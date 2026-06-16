"""Session — the two-plane interactive boundary over one task run (ADR-0001/0002, Phase 3.0).

Observation flows OUT via `events()` (an async stream that can never block or
redirect the run); control flows IN via `resolve_approval()` and `cancel()`. An
event may *announce* that approval is needed, but only the control method decides
it (§13). The cockpit binds to exactly this surface; the engine stays unchanged.

The `EventBus` fan-out + the privileged `JsonlEventJournal` live in `bus.py`/`journal.py`
(Lane 1); this module owns the session boundary and approval/grant control.
"""

import asyncio
import shlex
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

from pydantic import BaseModel

from avatar.bus import EventBus
from avatar.event_types import (
    ApprovalRequested,
    ApprovalResolved,
    HarnessEvent,
)
from avatar.journal import JsonlEventJournal
from avatar.runner import AgentRunner
from avatar.state import TaskState

# Grants never auto-allow tier-4+ (ADR-0002): destructive/external stays human-gated.
_GRANT_MAX_TIER = 4


def _grant_prefix(tool: str, tool_input: dict) -> str:
    """The grant key for a call: a command's program (`argv[0]`), else the tool name.

    For a command tool the standing-approval unit is the *program* — approving
    `pytest -q` grants `pytest …`, not every command (mirrors `Bash(pytest:*)`). A blank
    or unparseable command yields `""`, which `ApprovalGrant.matches` never matches, so a
    "remember" on it stores nothing global. A non-command tier-3 tool grants on its name.

    Args:
        tool: The tool name awaiting approval.
        tool_input: The proposed call arguments.

    Returns:
        The program prefix to grant/match on (possibly `""`).
    """
    if "command" in tool_input:
        try:
            tokens = shlex.split(str(tool_input["command"]))
        except ValueError:
            tokens = []
        return tokens[0] if tokens else ""
    return tool


class ApprovalGrant(BaseModel):
    """A session-scoped standing approval: auto-allow one tool's calls sharing a program.

    Stored when a human approves a tier-3 call with `[a] always`. Scoped to a `(tool,
    prefix, tier)` triple — never global (an empty `prefix` matches nothing) and never
    tier-4 (destructive actions always re-prompt).
    """

    tool: str
    prefix: str  # the command program (argv[0]); a non-command tool grants on its name
    tier: int

    def matches(self, tool: str, program: str, tier: int) -> bool:
        """Whether this grant auto-allows a call to `tool`/`program` at `tier`.

        Args:
            tool: The tool name of the incoming call.
            program: The incoming call's program prefix (see `_grant_prefix`).
            tier: The incoming call's permission tier.

        Returns:
            True iff the grant covers the call (same tool + program, at or below the
            granted tier, and below the tier-4 ceiling). A blank prefix never matches.
        """
        if tier >= _GRANT_MAX_TIER:
            return False
        return self.tool == tool and self.tier >= tier and bool(self.prefix) and self.prefix == program


@dataclass
class _Pending:
    """An in-flight approval awaiting the control plane (retained so a grant can derive)."""

    future: "asyncio.Future[bool]"
    tool: str
    program: str
    tier: int


class Session:
    """A live, interruptible task run exposing the two-plane API (§13, §23).

    Args:
        runner: The `AgentRunner` to drive (its `event_sink`/`approval_controller`
            are wired to this session for the duration of `run`).
        state: The task state to execute.
        session_id: Stable id stamped on events; generated if omitted.
        journal: The privileged write-ahead sink for this run's events; `None` (default)
            keeps the in-memory `history` only. `run()` closes it when the run ends.
        grants: The standing-approval list to consult and append to. Pass the multi-turn
            `SessionState.grants` (by reference) so a `[a] always` granted in one task
            persists to later tasks in the conversation; omit for a fresh per-run list.
        unattended: When `True` (batch/eval/autonomous wrappers), an `ask` is auto-denied
            immediately rather than awaiting a human — no `resolve_approval` will ever come,
            so awaiting one deadlocks the run. The deny is still announced + recorded
            (`ApprovalResolved(via="auto")`). `False` (default) keeps the interactive path.
        approval_timeout: A backstop, in seconds, on a *blocking* (attended) approval: if no
            `resolve_approval` arrives within it, the call is auto-denied so a run can't hang
            forever inside the gate (the wall-clock budget can't preempt an awaited approval).
            `None` (default) waits indefinitely — the right shape for a human at a REPL.
    """

    def __init__(  # noqa: PLR0913 — keyword-only DI of the run's collaborators + approval-mode seams
        self,
        runner: AgentRunner,
        state: TaskState,
        *,
        session_id: str | None = None,
        journal: JsonlEventJournal | None = None,
        grants: list[ApprovalGrant] | None = None,
        unattended: bool = False,
        approval_timeout: float | None = None,
    ) -> None:
        self.runner = runner
        self.state = state
        self.session_id = session_id or uuid4().hex
        self.bus = EventBus(self.session_id, journal=journal)
        self._pending: dict[str, _Pending] = {}
        # Shared by reference with the session scope when seeded, so grants persist across tasks.
        self._grants: list[ApprovalGrant] = grants if grants is not None else []
        self._unattended = unattended
        self._approval_timeout = approval_timeout
        self.cancel_reason: str | None = None  # set by cancel(); the loop records its own feedback

    def events(self) -> AsyncIterator[HarnessEvent]:
        """The observation plane: an independent async stream of typed events (§13).

        Subscribes *eagerly* (at call time, not first iteration), so a consumer created
        before `run()` never misses early events. Each call yields a fresh, independent
        stream — many observers can watch the same run concurrently.

        Returns:
            An async iterator over this run's `HarnessEvent`s, ending on `agent_end`.
        """
        return self._drain(self.bus.subscribe())

    async def _drain(self, queue: "asyncio.Queue") -> AsyncIterator[HarnessEvent]:
        """Yield events from one subscriber queue until the close sentinel.

        Args:
            queue: This consumer's queue, from `EventBus.subscribe`.

        Yields:
            Each event for this consumer, in publish order.
        """
        while True:
            item = await queue.get()
            if item is None:
                return
            yield item

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

        Called by the runner on a tier-3 `ask`. Emits `ApprovalRequested`, then disposes of
        it per mode: an attended session blocks *this run only* until `resolve_approval`
        completes the future (decision never returns through the event stream, §13); an
        `unattended` session auto-denies immediately (no human will come); and an
        `approval_timeout` denies a blocking wait that no human answers in time. Every deny
        is recorded as `ApprovalResolved(via="auto")`.

        A standing `ApprovalGrant` from an earlier `[a] always` short-circuits the human:
        the call is auto-allowed and recorded as `ApprovalResolved(via="grant")` with **no**
        `ApprovalRequested` (that event means "a human must decide"; a grant skips the human).

        Args:
            approval_id: Correlates the announcement with its resolution.
            tool: The tool name awaiting approval.
            reason: The gate's reason, shown to the human.
            tool_input: The proposed call arguments.

        Returns:
            True iff the call was allowed.
        """
        program = _grant_prefix(tool, tool_input)
        tier = self._tier_of(tool)
        if any(grant.matches(tool, program, tier) for grant in self._grants):
            self.bus.publish_nowait(
                ApprovalResolved(
                    task_id=self.state.task_id, approval_id=approval_id, allowed=True, via="grant"
                )
            )
            return True
        # Announce on every disposition (attended, unattended, or timeout). On the unattended
        # path no human will answer, so `ApprovalRequested` reads here as "a gate was hit and
        # disposed" rather than "a human must decide" — it exists for observability/journaling,
        # and the immediately-following `ApprovalResolved(via="auto")` records the verdict.
        self.bus.publish_nowait(
            ApprovalRequested(
                task_id=self.state.task_id,
                approval_id=approval_id,
                tool=tool,
                reason=reason,
                input=tool_input,
            )
        )
        # Unattended (batch/eval/autonomous): no human will resolve this — deny now rather than
        # deadlock awaiting a `resolve_approval` that never comes. The deny stays observable.
        if self._unattended:
            self._auto_deny(approval_id)
            return False
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = _Pending(future=future, tool=tool, program=program, tier=tier)
        try:
            allowed = await asyncio.wait_for(future, self._approval_timeout)
        except TimeoutError:
            # Backstop: the human never answered within `approval_timeout` — deny so the run
            # can reach a terminal outcome instead of hanging inside the gate.
            self._pending.pop(approval_id, None)
            self._auto_deny(approval_id)
            return False
        self.bus.publish_nowait(
            ApprovalResolved(
                task_id=self.state.task_id, approval_id=approval_id, allowed=allowed, via="human"
            )
        )
        return allowed

    def _auto_deny(self, approval_id: str) -> None:
        """Record a no-human auto-deny (unattended disposition or timeout backstop).

        Args:
            approval_id: The pending approval being denied without a human decision.
        """
        self.bus.publish_nowait(
            ApprovalResolved(task_id=self.state.task_id, approval_id=approval_id, allowed=False, via="auto")
        )

    def _tier_of(self, tool: str) -> int:
        """The permission tier of `tool`, or the tier-4 ceiling if it is unknown.

        Looked up from the runner's registry so the grant logic stays out of the gate.
        An unknown tool defaults to the ceiling, so a grant can never auto-allow it.

        Args:
            tool: The tool name to look up.

        Returns:
            The tool's `permission_tier`, or `_GRANT_MAX_TIER` when not registered.
        """
        definition = self.runner.registry.get(tool)
        return _GRANT_MAX_TIER if definition is None else definition.permission_tier

    async def resolve_approval(self, approval_id: str, *, allow: bool, remember: bool = False) -> None:
        """Control plane: resolve a pending approval (the decision the event announced).

        Tolerant of an unknown id (resolves the sole pending request if there is exactly
        one) so a caller that didn't capture the id can still unblock the run. With
        `allow` and `remember` both set (the `[a] always` choice), stores a session-scoped
        `ApprovalGrant` for the call's program prefix so matching calls auto-allow later.
        `remember` is ignored on a denial (there is no "always deny") and on a blank prefix.

        Args:
            approval_id: The id from the `ApprovalRequested` event.
            allow: Whether to permit the gated call.
            remember: Whether to store a standing grant (only meaningful with `allow`).
        """
        pending = self._pending.get(approval_id)
        if pending is None and len(self._pending) == 1:
            pending = next(iter(self._pending.values()))
        if pending is None or pending.future.done():
            return
        if allow and remember and pending.program and pending.tier < _GRANT_MAX_TIER:
            self._grants.append(ApprovalGrant(tool=pending.tool, prefix=pending.program, tier=pending.tier))
        pending.future.set_result(allow)

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
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_result(False)
