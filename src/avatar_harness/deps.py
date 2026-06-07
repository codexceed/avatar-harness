"""Run-scoped dependencies handed to tools and the runtime (§8).

Tools receive these explicitly — never via globals — so a run stays
self-contained and replayable. Phase 1 carries the minimum the read tools
need; `state` and `event_log` join when the runner is built.
"""

from dataclasses import dataclass

from avatar_harness.config import HarnessConfig
from avatar_harness.workspace import Workspace


@dataclass
class CancellationToken:
    """Trips to abort an in-flight tool (per-tool timeout, user interrupt — §23)."""

    cancelled: bool = False

    def cancel(self) -> None:
        """Trip the token so an in-flight tool can abort at its next checkpoint."""
        self.cancelled = True


@dataclass
class RunDeps:
    """The run-scoped dependencies handed explicitly to tools and the runtime (§8)."""

    workspace: Workspace
    config: HarnessConfig
    cancellation: CancellationToken
