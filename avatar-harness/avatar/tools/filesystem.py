"""Read-only filesystem tools: read_file, list_files (§10, tier 0)."""

from pydantic import BaseModel, field_validator

from avatar.deps import RunDeps
from avatar.tools.base import ToolDefinition, ToolResult
from avatar.workspace import PathOutsideWorkspaceError, SensitivePathError

# Repo inspection is available in every phase ("always", §21 capability groups).
_READ_PHASES = frozenset({"investigating", "editing", "verifying"})

# Cap on the number of paths shown to the model; a directory match can expand to
# thousands, which would blow the context (the full count is kept in the summary).
_LIST_CAP = 1000

# A line range is exactly `[start, end]` — two 1-indexed line numbers (ADR-0019).
_LINE_RANGE_LEN = 2


class ReadFileInput(BaseModel):
    """Input for `read_file`: a workspace path and optional 1-indexed line range.

    `line_range` is a `list[int]` (`[start, end]`), not a `tuple` — a tuple renders to
    JSON Schema as `prefixItems` *without* an `items` key, which Gemini's request validator
    rejects (ADR-0019). A plain list emits a provider-agnostic `{"type": "array", "items":
    {"type": "integer"}}`; the exactly-two / ordering contract is enforced by the validator.
    """

    path: str
    line_range: list[int] | None = None

    @field_validator("line_range")
    @classmethod
    def _validate_line_range(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        if len(value) != _LINE_RANGE_LEN:
            raise ValueError("line_range must be [start, end] — two 1-indexed line numbers")
        start, end = value
        if start < 1 or end < start:
            raise ValueError("line_range must satisfy 1 <= start <= end")
        return value


def _read_file(args: ReadFileInput, deps: RunDeps) -> ToolResult:
    line_range = (args.line_range[0], args.line_range[1]) if args.line_range else None
    try:
        content = deps.workspace.read(args.path, line_range)
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
    description=(
        "List files in the workspace matching a glob pattern. Hidden (dot-prefixed) "
        "entries are skipped unless the pattern names one (e.g. '.github/**/*')."
    ),
    input_model=ListFilesInput,
    handler=_list_files,
    phases=_READ_PHASES,
)
