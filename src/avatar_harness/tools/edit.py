"""Editing tools: apply_patch + write_file (§10, tier 1).

The mutating tools. `apply_patch` hands a unified diff to the `Workspace`, which
applies it atomically; a failed apply (stale context) comes back as a
model-correctable `ToolResult`, never an exception thrown at the loop (§10).
`write_file` (ADR-0003 B) is the plain-content transport for the no-anchor case —
file *creation* — where a unified diff is pure fragility; modification stays
diff-anchored (an existing target is refused toward `apply_patch` unless
`overwrite` is explicit).
"""

from pydantic import BaseModel

from avatar_harness.deps import RunDeps
from avatar_harness.tools.base import ToolDefinition, ToolResult
from avatar_harness.workspace import (
    PatchError,
    PathOutsideWorkspaceError,
    SensitivePathError,
    _parse_patch_targets,
)

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
    except SensitivePathError as exc:
        # A patch targeting a denylisted file is refused at the workspace (defense in depth).
        return ToolResult(tool_name="apply_patch", success=False, error=f"sensitive path refused: {exc}")
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


class WriteFileInput(BaseModel):
    """Input for `write_file`: create a file with full content (overwrite only if explicit)."""

    path: str
    content: str
    overwrite: bool = False


def _write_file(args: WriteFileInput, deps: RunDeps) -> ToolResult:
    try:
        rel = deps.workspace.write_file(args.path, args.content, overwrite=args.overwrite)
    except PathOutsideWorkspaceError as exc:
        # A path escape is a system-level refusal, not a retry — surface it.
        return ToolResult(tool_name="write_file", success=False, error=f"path outside workspace: {exc}")
    except SensitivePathError as exc:
        # A denylisted target is refused at the workspace (defense in depth behind the gate).
        return ToolResult(tool_name="write_file", success=False, error=f"sensitive path refused: {exc}")
    except FileExistsError as exc:
        # Model-correctable: modification stays diff-anchored (clean-apply staleness, §10).
        return ToolResult(
            tool_name="write_file",
            success=False,
            error=(
                f"file exists: {exc} — modify existing content with apply_patch, "
                "or pass overwrite=true to replace it deliberately"
            ),
        )
    return ToolResult(
        tool_name="write_file",
        success=True,
        content=f"wrote {rel} ({len(args.content)} chars)",
        summary=f"wrote {rel}",
        files_changed=[rel],
    )


write_file = ToolDefinition(
    name="write_file",
    description=(
        "Create a new file with the given full content (set overwrite=true to replace an "
        "existing file deliberately). For modifying existing content, use apply_patch."
    ),
    input_model=WriteFileInput,
    handler=_write_file,
    phases=_EDIT_PHASES,
    permission_tier=1,
    # The target file is the gate's path policy input (confinement + denylist, §11).
    paths=lambda args: (args.path,),
)
