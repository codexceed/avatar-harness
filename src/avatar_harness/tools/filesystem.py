"""Read-only filesystem tools: read_file, list_files (§10, tier 0)."""

from pydantic import BaseModel

from avatar_harness.deps import RunDeps
from avatar_harness.tools.base import ToolDefinition, ToolResult
from avatar_harness.workspace import PathOutsideWorkspaceError, SensitivePathError

# Repo inspection is available in every phase ("always", §21 capability groups).
_READ_PHASES = frozenset({"investigating", "editing", "verifying"})

# Cap on the number of paths shown to the model; a directory match can expand to
# thousands, which would blow the context (the full count is kept in the summary).
_LIST_CAP = 1000


class ReadFileInput(BaseModel):
    """Input for `read_file`: a workspace path and optional 1-indexed line range."""

    path: str
    line_range: tuple[int, int] | None = None


def _read_file(args: ReadFileInput, deps: RunDeps) -> ToolResult:
    try:
        content = deps.workspace.read(args.path, args.line_range)
    except FileNotFoundError:
        return ToolResult(tool_name="read_file", success=False, error=f"file not found: {args.path}")
    except PathOutsideWorkspaceError:
        return ToolResult(tool_name="read_file", success=False, error=f"path outside workspace: {args.path}")
    except SensitivePathError as exc:
        # Defense in depth: refused even without the gate (and on the symlink-resolved target).
        return ToolResult(tool_name="read_file", success=False, error=f"sensitive path refused: {exc}")
    return ToolResult(
        tool_name="read_file",
        success=True,
        content=content,
        summary=f"read {args.path}",
        files_read=[args.path],
    )


read_file = ToolDefinition(
    name="read_file",
    description="Read a bounded file or line range from the workspace.",
    input_model=ReadFileInput,
    handler=_read_file,
    phases=_READ_PHASES,
    paths=lambda args: (args.path,),  # self-declared for the gate's path policy (§11)
)


class ListFilesInput(BaseModel):
    """Input for `list_files`: a glob pattern (defaults to the whole tree)."""

    glob: str = "**/*"


def _list_files(args: ListFilesInput, deps: RunDeps) -> ToolResult:
    files = deps.workspace.list_files(args.glob)
    content = "\n".join(files[:_LIST_CAP])
    if len(files) > _LIST_CAP:
        content += f"\n… (+{len(files) - _LIST_CAP} more)"
    return ToolResult(
        tool_name="list_files",
        success=True,
        content=content,
        summary=f"{len(files)} file(s) matching {args.glob!r}",  # full count, even when capped
    )


list_files = ToolDefinition(
    name="list_files",
    description="List files in the workspace matching a glob pattern.",
    input_model=ListFilesInput,
    handler=_list_files,
    phases=_READ_PHASES,
)
