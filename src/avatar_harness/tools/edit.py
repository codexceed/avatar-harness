"""Editing tools: str_replace + write_file + delete_file (§10, tier 1, ADR-0015).

The mutating tools. `str_replace` makes a targeted, string-anchored edit to an existing
file — `old_string` → `new_string`, no line numbers, no diff; a miss comes back as a
model-correctable `ToolResult`, never an exception thrown at the loop (§10). `write_file`
(ADR-0003 B) is the plain-content transport for creation and explicit whole-file rewrites,
and `delete_file` removes one. Modification is anchored on the exact existing text (the
read-before-edit staleness proof), which replaced the line-numbered unified diff models
could not produce reliably (ADR-0015).
"""

from pydantic import BaseModel

from avatar_harness.deps import RunDeps
from avatar_harness.tools.base import ToolDefinition, ToolResult
from avatar_harness.workspace import (
    AmbiguousMatchError,
    EmptyAnchorError,
    MatchNotFoundError,
    PathOutsideWorkspaceError,
    ReplaceError,
    SensitivePathError,
)

# Editing is active only once the agent has moved into the editing phase (§21).
_EDIT_PHASES = frozenset({"editing"})


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
                f"file exists: {exc} — modify existing content with str_replace, "
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
        "existing file deliberately). For a targeted change to existing content, use str_replace."
    ),
    input_model=WriteFileInput,
    handler=_write_file,
    phases=_EDIT_PHASES,
    permission_tier=1,
    # The target file is the gate's path policy input (confinement + denylist, §11).
    paths=lambda args: (args.path,),
)


class StrReplaceInput(BaseModel):
    """Input for `str_replace`: an exact-text find/replace in one existing file (ADR-0015)."""

    path: str
    old_string: str
    new_string: str
    replace_all: bool = False


# Replace failures map 1:1 to a model-correctable message (§10): widen / re-read the anchor,
# or fall back to write_file — never a system-level retry.
_REPLACE_INTERNAL = (PathOutsideWorkspaceError, SensitivePathError)


def _str_replace_precheck(args: StrReplaceInput) -> str | None:
    """Reject a degenerate anchor before touching the workspace.

    Args:
        args: The `str_replace` tool input.

    Returns:
        A model-correctable rejection message, or `None` when the anchor is well-formed.
    """
    # The empty-anchor contract lives at the `Workspace.replace` chokepoint (so a direct SDK
    # caller is guarded too); here we only catch the no-op the chokepoint would silently allow.
    if args.old_string == args.new_string:
        return "old_string and new_string are identical — there is no change to make"
    return None


def _replace_error(args: StrReplaceInput, exc: Exception) -> str:
    """Map a `Workspace.replace` failure to its model-facing message (ADR-0015 error contract).

    Args:
        args: The `str_replace` tool input (for the path in the message).
        exc: The exception raised by `Workspace.replace`.

    Returns:
        The model-correctable error text for the `ToolResult`.
    """
    if isinstance(exc, PathOutsideWorkspaceError | SensitivePathError):
        label = (
            "path outside workspace"
            if isinstance(exc, PathOutsideWorkspaceError)
            else "sensitive path refused"
        )
        return f"{label}: {exc}"  # system refusal, not a retry
    if isinstance(exc, FileNotFoundError):
        return f"file does not exist: {args.path} — create it with write_file"
    if isinstance(exc, EmptyAnchorError):
        return "old_string must be non-empty: provide the exact existing text to replace"
    if isinstance(exc, AmbiguousMatchError):
        return (
            f"old_string matches {exc.count} locations in {args.path} — extend it with surrounding "
            "lines until it uniquely identifies ONE location (or set replace_all=true to change "
            "every occurrence)"
        )
    if isinstance(exc, MatchNotFoundError):
        return (  # the anchor doesn't match current content (staleness signal, §10)
            f"old_string not found in {args.path} — it must match the current file text exactly, "
            "whitespace included; re-read the file and copy the exact text"
        )
    return f"could not edit {args.path}: {exc}"  # any other ReplaceError (keeps §10 correctable)


def _str_replace(args: StrReplaceInput, deps: RunDeps) -> ToolResult:
    rejection = _str_replace_precheck(args)
    if rejection is not None:
        return ToolResult(tool_name="str_replace", success=False, error=rejection)
    try:
        rel = deps.workspace.replace(
            args.path, args.old_string, args.new_string, replace_all=args.replace_all
        )
    except (*_REPLACE_INTERNAL, FileNotFoundError, ReplaceError) as exc:
        # Catch the ReplaceError BASE, not just today's subclasses: a future subclass must not
        # escape to ToolRuntime's generic handler as a non-correctable system error (§10).
        return ToolResult(tool_name="str_replace", success=False, error=_replace_error(args, exc))
    return ToolResult(
        tool_name="str_replace",
        success=True,
        content=f"replaced text in {rel}",
        summary=f"edited {rel}",
        files_changed=[rel],
    )


str_replace = ToolDefinition(
    name="str_replace",
    description=(
        "Make a targeted edit to an existing file by exact text match — no line numbers, no diff. "
        "old_string must match the current file text exactly (whitespace included) and identify "
        "ONE location (or set replace_all=true to change every occurrence). To create a file or "
        "rewrite it wholesale, use write_file instead."
    ),
    input_model=StrReplaceInput,
    handler=_str_replace,
    phases=_EDIT_PHASES,
    permission_tier=1,
    # The target file is the gate's path policy input (confinement + denylist, §11).
    paths=lambda args: (args.path,),
)


class DeleteFileInput(BaseModel):
    """Input for `delete_file`: remove one existing workspace file (ADR-0015)."""

    path: str


def _delete_file(args: DeleteFileInput, deps: RunDeps) -> ToolResult:
    try:
        rel = deps.workspace.remove(args.path)
    except PathOutsideWorkspaceError as exc:
        # A path escape is a system-level refusal, not a retry — surface it.
        return ToolResult(tool_name="delete_file", success=False, error=f"path outside workspace: {exc}")
    except SensitivePathError as exc:
        # A denylisted target is refused at the workspace (defense in depth behind the gate).
        return ToolResult(tool_name="delete_file", success=False, error=f"sensitive path refused: {exc}")
    except FileNotFoundError:
        # Model-correctable: nothing to delete (already gone or wrong path).
        return ToolResult(tool_name="delete_file", success=False, error=f"file does not exist: {args.path}")
    return ToolResult(
        tool_name="delete_file",
        success=True,
        content=f"deleted {rel}",
        summary=f"deleted {rel}",
        files_changed=[rel],
    )


delete_file = ToolDefinition(
    name="delete_file",
    description=(
        "Delete an existing file from the workspace. Use this to remove a file (including "
        "tidying up a scratch file you created). Editing existing content is str_replace; "
        "creating or rewriting a file is write_file."
    ),
    input_model=DeleteFileInput,
    handler=_delete_file,
    phases=_EDIT_PHASES,
    permission_tier=1,
    # The target file is the gate's path policy input (confinement + denylist, §11).
    paths=lambda args: (args.path,),
)
