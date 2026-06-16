"""Run-scoped dependencies handed to tools and the runtime (§8).

Tools receive these explicitly — never via globals — so a run stays
self-contained and replayable. Phase 1 carries the minimum the read tools
need; `state` and `event_log` join when the runner is built.
"""

from dataclasses import dataclass

from avatar.config import HarnessConfig
from avatar.state import PlannedCheck
from avatar.workspace import Workspace


@dataclass
class CancellationToken:
    """Trips to abort an in-flight tool (per-tool timeout, user interrupt — §23)."""

    cancelled: bool = False

    def cancel(self) -> None:
        """Trip the token so an in-flight tool can abort at its next checkpoint."""
        self.cancelled = True


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
