"""ArtifactManager — the final task summary (§14).

The artifact reports, never re-derives: `status` is exactly `state.outcome`, and
the rest is read off `TaskState` plus the workspace diff (the deliverable — the
harness never commits, §15). Keeping `status` a copy of `outcome` is what lets a
caller distinguish "give me more budget" (`incomplete`) from "this can't be
verified" (`failed`) from "I need input" (`blocked`).
"""

from pydantic import BaseModel, Field

from avatar_harness.state import TaskState
from avatar_harness.workspace import Workspace


class Artifact(BaseModel):
    """The structured final summary of one task (§14)."""

    status: str  # exactly state.outcome — never re-derived
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    commands_run: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    diff_ref: str = ""  # the uncommitted working-tree diff (the deliverable)


class ArtifactManager:
    """Builds and renders the terminal artifact from `TaskState` (§14)."""

    def build(self, state: TaskState, ws: Workspace) -> Artifact:
        """Assemble the artifact for a terminal `state`; `status` mirrors `outcome`.

        Args:
            state: The terminal task state to report from.
            ws: The workspace, queried for the deliverable diff.

        Returns:
            The assembled `Artifact`.
        """
        return Artifact(
            status=str(state.outcome),
            summary=state.final_answer or state.goal,
            files_changed=sorted(state.files_modified),
            commands_run=[c.command for c in state.commands_run],
            verification=[v.summary for v in state.verifier_results],
            diff_ref=ws.diff(),
        )

    def render(self, artifact: Artifact) -> str:
        """Render the artifact as the plain-text summary block (§14).

        Args:
            artifact: The artifact to render.

        Returns:
            The plain-text summary block.
        """
        lines = [f"Status: {artifact.status}"]
        if artifact.files_changed:
            lines.append("Changed files:")
            lines.extend(f"  - {path}" for path in artifact.files_changed)
        if artifact.verification:
            lines.append("Verification:")
            lines.extend(f"  - {item}" for item in artifact.verification)
        if artifact.commands_run:
            lines.append("Commands:")
            lines.extend(f"  - {cmd}" for cmd in artifact.commands_run)
        if artifact.summary:
            # The summary is the answer/change note — print it in full as a trailing
            # block (an investigate answer can be long markdown), not a list item.
            lines.append(f"\n{artifact.summary}")
        return "\n".join(lines)
