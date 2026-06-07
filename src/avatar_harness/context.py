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
    name: str
    description: str
    input_schema: dict = Field(default_factory=dict)


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
    def __init__(self, max_evidence: int = 5, max_detail_chars: int = 1500) -> None:
        self.max_evidence = max_evidence
        self.max_detail_chars = max_detail_chars

    def _render_evidence(self, evidence: Evidence) -> str:
        """Render one evidence item, including its detail (tool content) so the
        model can actually see what a tool found — truncated to the detail budget."""
        if evidence.detail:
            return f"{evidence.summary}\n{evidence.detail[: self.max_detail_chars]}"
        return evidence.summary

    def build(self, state: TaskState, ws: Workspace, registry: ToolRegistry) -> ContextPacket:
        return ContextPacket(
            goal=state.goal,
            constraints=list(state.constraints),
            phase=state.phase,
            plan=list(state.current_plan),
            files_read=sorted(state.files_read),
            files_modified=sorted(state.files_modified),
            recent_evidence=[
                self._render_evidence(e) for e in state.evidence[-self.max_evidence :]
            ],
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
