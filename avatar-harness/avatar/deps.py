"""Run-scoped dependencies handed to tools and the runtime (§8).

Passed explicitly — never via globals — so a run stays self-contained and replayable.
"""

import asyncio
from dataclasses import dataclass, field

from avatar.config import HarnessConfig
from avatar.state import PlannedCheck
from avatar.workspace import Workspace


@dataclass
class CancellationToken:
    """Trips to abort an in-flight tool (per-tool timeout, user interrupt — §23).

    `cancelled` is the source of truth (polled at loop checkpoints). `event()` exposes the
    same trip as an `asyncio.Event` so the runner can *race* a cancel against an in-flight
    model call (ADR-0029 R5) — true mid-call abort, not just between-turn polling. The Event is
    created lazily inside the running loop, so a no-loop construction (and `CancellationToken(
    cancelled=True)` in tests) still works.
    """

    cancelled: bool = False
    # Lazy, loop-bound mirror of `cancelled`; non-init so equality/construction ignore it.
    _event: "asyncio.Event | None" = field(default=None, init=False, compare=False, repr=False)

    def cancel(self) -> None:
        """Trip the token so an in-flight tool or model call can abort at its next checkpoint."""
        self.cancelled = True
        if self._event is not None:
            self._event.set()

    def event(self) -> asyncio.Event:
        """Return the loop-bound cancel Event, creating it on first call (must be inside a loop).

        Returns:
            An `asyncio.Event` set iff the token is cancelled — already set if `cancel()` ran first.
        """
        if self._event is None:
            self._event = asyncio.Event()
            if self.cancelled:
                self._event.set()
        return self._event


@dataclass
class RunDeps:
    """The run-scoped dependencies handed explicitly to tools and the runtime (§8).

    `verification_plan` mirrors the plan the runner froze onto `TaskState`
    (ADR-0007), so `run_tests`/`run_linter` can ride the resolved contract when no
    config override exists — the model exercises the same rubric the verifier grades.
    """

    workspace: Workspace
    config: HarnessConfig
    cancellation: CancellationToken
    verification_plan: list[PlannedCheck] | None = None
