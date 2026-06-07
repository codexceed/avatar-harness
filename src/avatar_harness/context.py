"""ContextBuilder — the compact per-turn working packet (§9).

Assembles only what the model needs *this* turn: goal, phase, recent evidence
(summaries, most-recent-first-budgeted), and the tools allowed for the current
phase. The model discovers the repo incrementally through tools; it never
receives the whole repository by default.
"""

from pydantic import BaseModel, Field

from avatar_harness.state import Evidence, TaskState
from avatar_harness.tools.base import ToolRegistry
from avatar_harness.workspace import Workspace


class ToolSummary(BaseModel):
    """A tool's name, description, and input schema, as shown to the model."""

    name: str
    description: str
    input_schema: dict = Field(default_factory=dict)


class ContextPacket(BaseModel):
    """The compact, per-turn working set assembled for one model decision (§9)."""

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
    """Builds the per-turn `ContextPacket` from `TaskState` under a fixed budget (§9)."""

    def __init__(self, max_evidence: int = 5, max_detail_chars: int = 1500) -> None:
        self.max_evidence = max_evidence
        self.max_detail_chars = max_detail_chars

    def _render_evidence(self, evidence: Evidence) -> str:
        """Render one evidence item for the packet.

        Includes the item's detail (the tool's content) so the model can see what
        a tool actually found, truncated to the per-item detail budget.
        """
        if evidence.detail:
            return f"{evidence.summary}\n{evidence.detail[: self.max_detail_chars]}"
        return evidence.summary

    def build(self, state: TaskState, ws: Workspace, registry: ToolRegistry) -> ContextPacket:
        """Assemble the working packet for the current turn from `state` (§9)."""
        return ContextPacket(
            goal=state.goal,
            constraints=list(state.constraints),
            phase=state.phase,
            plan=list(state.current_plan),
            files_read=sorted(state.files_read),
            files_modified=sorted(state.files_modified),
            recent_evidence=[self._render_evidence(e) for e in state.evidence[-self.max_evidence :]],
            allowed_tools=[
                ToolSummary(
                    name=t.name,
                    description=t.description,
                    input_schema=t.input_model.model_json_schema(),
                )
                for t in registry.active_for_phase(state.phase)
            ],
            latest_error=state.latest_error,
            has_uncommitted_changes=bool(ws.diff()),
        )
