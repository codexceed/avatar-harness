"""Read-only filesystem tools: read_file, list_files (§10, tier 0)."""

from pydantic import BaseModel

from avatar_harness.deps import RunDeps
from avatar_harness.tools.base import ToolDefinition, ToolResult
from avatar_harness.workspace import PathOutsideWorkspace

# Repo inspection is available in every phase ("always", §21 capability groups).
_READ_PHASES = frozenset({"investigating", "editing", "verifying"})


class ReadFileInput(BaseModel):
    path: str
    line_range: tuple[int, int] | None = None


def _read_file(args: ReadFileInput, deps: RunDeps) -> ToolResult:
    try:
        content = deps.workspace.read(args.path, args.line_range)
    except FileNotFoundError:
        return ToolResult(tool_name="read_file", success=False, error=f"file not found: {args.path}")
    except PathOutsideWorkspace:
        return ToolResult(
            tool_name="read_file", success=False, error=f"path outside workspace: {args.path}"
        )
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
)


class ListFilesInput(BaseModel):
    glob: str = "**/*"


def _list_files(args: ListFilesInput, deps: RunDeps) -> ToolResult:
    files = deps.workspace.list_files(args.glob)
    return ToolResult(
        tool_name="list_files",
        success=True,
        content="\n".join(files),
        summary=f"{len(files)} file(s) matching {args.glob!r}",
    )


list_files = ToolDefinition(
    name="list_files",
    description="List files in the workspace matching a glob pattern.",
    input_model=ListFilesInput,
    handler=_list_files,
    phases=_READ_PHASES,
)
