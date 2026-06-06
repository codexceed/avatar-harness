"""ContextBuilder — the compact per-turn working packet (§9).

Assembles only what the model needs *this* turn: goal, phase, recent evidence
(summaries, most-recent-first-budgeted), and the tools allowed for the current
phase. The model discovers the repo incrementally through tools; it never
receives the whole repository by default.
"""

from pydantic import BaseModel, Field

from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolRegistry
from avatar_harness.workspace import Workspace


class ToolSummary(BaseModel):
    name: str
    description: str


class ContextPacket(BaseModel):
    goal: str
    constraints: list[str] = Field(default_factory=list)
    phase: str
    plan: list[str] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    recent_evidence: list[str] = Field(default_factory=list)
    allowed_tools: list[ToolSummary] = Field(default_factory=list)
    latest_error: str | None = None
    has_uncommitted_changes: bool = False


class ContextBuilder:
    def __init__(self, max_evidence: int = 5) -> None:
        self.max_evidence = max_evidence

    def build(self, state: TaskState, ws: Workspace, registry: ToolRegistry) -> ContextPacket:
        return ContextPacket(
            goal=state.goal,
            constraints=list(state.constraints),
            phase=state.phase,
            plan=list(state.current_plan),
            files_read=sorted(state.files_read),
            files_modified=sorted(state.files_modified),
            recent_evidence=[e.summary for e in state.evidence[-self.max_evidence :]],
            allowed_tools=[
                ToolSummary(name=t.name, description=t.description)
                for t in registry.active_for_phase(state.phase)
            ],
            latest_error=state.latest_error,
            has_uncommitted_changes=bool(ws.diff()),
        )
