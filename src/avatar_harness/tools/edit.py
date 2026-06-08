"""Editing tool: apply_patch (§10, tier 1).

The only mutating tool in the MVP. It hands a unified diff to the `Workspace`,
which applies it atomically; a failed apply (stale context) comes back as a
model-correctable `ToolResult`, never an exception thrown at the loop (§10).
"""

from pydantic import BaseModel

from avatar_harness.deps import RunDeps
from avatar_harness.tools.base import ToolDefinition, ToolResult
from avatar_harness.workspace import PatchError, PathOutsideWorkspaceError, _parse_patch_targets

# Editing is active only once the agent has moved into the editing phase (§21).
_EDIT_PHASES = frozenset({"editing"})


class ApplyPatchInput(BaseModel):
    """Input for `apply_patch`: a unified diff that may span multiple files."""

    diff: str


def _apply_patch(args: ApplyPatchInput, deps: RunDeps) -> ToolResult:
    try:
        changed = deps.workspace.apply_patch(args.diff)
    except PathOutsideWorkspaceError as exc:
        # A path escape is a system-level refusal, not a stale-context retry — surface it.
        return ToolResult(tool_name="apply_patch", success=False, error=f"path outside workspace: {exc}")
    except PatchError as exc:
        # Stale context is model-correctable: the model re-reads and retries (§10).
        return ToolResult(tool_name="apply_patch", success=False, error=f"patch did not apply: {exc}")
    return ToolResult(
        tool_name="apply_patch",
        success=True,
        content="applied patch to: " + ", ".join(changed),
        summary=f"patched {len(changed)} file(s)",
        files_changed=changed,
    )


apply_patch = ToolDefinition(
    name="apply_patch",
    description="Apply a unified diff (may span multiple files) to the workspace atomically.",
    input_model=ApplyPatchInput,
    handler=_apply_patch,
    phases=_EDIT_PHASES,
    permission_tier=1,
    # The diff's target files are the gate's path policy inputs (confinement + denylist, §11).
    paths=lambda args: tuple(sorted(_parse_patch_targets(args.diff))),
)
