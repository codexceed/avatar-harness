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
    task_kind: str = "investigate"  # lets the model adapter frame the prompt per kind (§7)
    plan: list[str] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    prior_actions: list[str] = Field(default_factory=list)  # the agent's own action history (§9)
    recent_evidence: list[str] = Field(default_factory=list)
    allowed_tools: list[ToolSummary] = Field(default_factory=list)
    latest_error: str | None = None
    has_uncommitted_changes: bool = False


# Verifier feedback — its latest output is pinned verbatim through compaction (§9, §12).
_VERIFIER_KINDS = frozenset({"verification", "repair_hint"})


class ContextBuilder:
    """Builds the per-turn `ContextPacket` from `TaskState` under a fixed budget (§9).

    Evidence is compacted *less-lossily* (Phase 2.5): recent items keep full detail
    until a char budget is spent, older items degrade to their summary line (not
    dropped), the latest verifier output is pinned verbatim, and adjacent duplicates
    collapse to one `... (xN)` line. Action history is cheap (one line each) so it is
    kept on a longer horizon than evidence.

    Args:
        detail_char_budget: Total chars of evidence *detail* shown verbatim before
            older items degrade to summary-only.
        max_detail_chars: Per-item detail truncation budget.
        max_evidence_lines: Hard cap on rendered evidence lines (newest kept).
        max_actions: Max prior-action lines to surface.
    """

    def __init__(
        self,
        detail_char_budget: int = 6000,
        max_detail_chars: int = 1500,
        max_evidence_lines: int = 40,
        max_actions: int = 25,
    ) -> None:
        self.detail_char_budget = detail_char_budget
        self.max_detail_chars = max_detail_chars
        self.max_evidence_lines = max_evidence_lines
        self.max_actions = max_actions

    def _render_action(self, decision: object) -> str:
        chosen = getattr(decision, "chosen", "")
        outcome = getattr(decision, "outcome", "")
        return f"{chosen} → {outcome}" if outcome else chosen

    def _compact_evidence(self, evidence: list[Evidence]) -> list[str]:
        """Render evidence newest-first under the detail budget, then reverse (§9).

        Collapses adjacent duplicates, spends `detail_char_budget` on the most recent
        items (showing detail), degrades older items to summary-only, and pins the
        latest verifier item's detail verbatim regardless of budget.

        Args:
            evidence: The full evidence list (oldest-first).

        Returns:
            Rendered evidence lines, oldest-first.
        """
        collapsed: list[list] = []  # [Evidence, count]
        for item in evidence:
            prev = collapsed[-1][0] if collapsed else None
            if prev is not None and prev.summary == item.summary and prev.detail == item.detail:
                collapsed[-1][1] += 1
            else:
                collapsed.append([item, 1])

        lines_newest_first: list[str] = []
        spent = 0
        pinned_verifier = False
        for item, count in reversed(collapsed):
            if len(lines_newest_first) >= self.max_evidence_lines:
                break
            suffix = f" ... (x{count})" if count > 1 else ""
            pin = item.kind in _VERIFIER_KINDS and not pinned_verifier
            if item.detail and (spent < self.detail_char_budget or pin):
                detail = item.detail[: self.max_detail_chars]
                spent += len(detail)
                if item.kind in _VERIFIER_KINDS:
                    pinned_verifier = True
                lines_newest_first.append(f"{item.summary}{suffix}\n{detail}")
            else:
                lines_newest_first.append(f"{item.summary}{suffix}")
        return list(reversed(lines_newest_first))

    def build(self, state: TaskState, ws: Workspace, registry: ToolRegistry) -> ContextPacket:
        """Assemble the working packet for the current turn from `state` (§9).

        Args:
            state: The task state, source of truth for the packet.
            ws: The run-scoped `Workspace` handle.
            registry: The `ToolRegistry`, for phase-active tools.

        Returns:
            The `ContextPacket` for this turn.
        """
        return ContextPacket(
            goal=state.goal,
            constraints=list(state.constraints),
            phase=state.phase,
            task_kind=state.task_kind,
            plan=list(state.current_plan),
            files_read=sorted(state.files_read),
            files_modified=sorted(state.files_modified),
            prior_actions=[self._render_action(d) for d in state.decisions[-self.max_actions :]],
            recent_evidence=self._compact_evidence(state.evidence),
            allowed_tools=[
                ToolSummary(
                    name=t.name,
                    description=t.description,
                    input_schema=t.input_model.model_json_schema(),
                )
                for t in registry.admitted_for(state.phase, state.task_kind)
            ],
            latest_error=state.latest_error,
            has_uncommitted_changes=bool(ws.diff()),
        )
