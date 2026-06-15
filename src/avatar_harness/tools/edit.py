"""Editing tools: str_replace + write_file (+ legacy apply_patch) (§10, tier 1, ADR-0015).

The mutating tools. `str_replace` makes a targeted, string-anchored edit to an existing
file — `old_string` → `new_string`, no line numbers, no diff; a miss is a model-correctable
`ToolResult`, never an exception thrown at the loop (§10). `write_file` (ADR-0003 B) is the
plain-content transport for creation and explicit whole-file rewrites. `apply_patch` (the
legacy unified-diff editor) is retained only through the ADR-0015 migration and removed in
phase 2; modification anchors on the exact existing text, not a line-numbered diff.
"""

import re

from pydantic import BaseModel

from avatar_harness.deps import RunDeps
from avatar_harness.tools.base import ToolDefinition, ToolResult
from avatar_harness.workspace import (
    AmbiguousMatchError,
    EmptyAnchorError,
    MatchNotFoundError,
    PatchError,
    PathOutsideWorkspaceError,
    ReplaceError,
    SensitivePathError,
    _parse_patch_targets,
)

# Editing is active only once the agent has moved into the editing phase (§21).
_EDIT_PHASES = frozenset({"editing"})

# Markers of OpenAI's in-house "apply_patch" dialect, which their models are heavily
# trained to emit. We don't accept it (rule of three: translate only if better guidance
# doesn't stop the bleeding) — but a recognized dialect must come back as an error that
# TEACHES the expected format, not a generic "no file targets found" that burned a whole
# dogfood budget on blind retries.
_BEGIN_PATCH_MARKERS = ("*** Begin Patch", "*** Update File:", "*** Add File:", "*** Delete File:")

_DIALECT_GUIDANCE = (
    "unsupported patch dialect: this looks like the '*** Begin Patch' format. apply_patch "
    "takes a unified git diff — '--- a/<path>' and '+++ b/<path>' headers with '@@' hunks, "
    "exactly as `git diff` prints (use '/dev/null' for a created or deleted file). Re-send "
    "the SAME change as a unified diff, or use write_file with overwrite=true to rewrite "
    "the whole file."
)

# A well-formed unified-diff hunk header carries line ranges: '@@ -<start>,<count> +<start>,
# <count> @@' (the count is optional for a single line). A diff that names targets but emits a
# bare '@@' is what makes `git apply` answer with the opaque "No valid patches in input" — and a
# model that can't see *why* re-sends the same broken hunk until the failure cap (dogfood: 5 blind
# retries → incomplete). Catch it deterministically and TEACH the fix, like the dialect guard.
_HUNK_HEADER_RE = re.compile(r"(?m)^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")

_MALFORMED_HUNK_GUIDANCE = (
    "malformed unified diff: each hunk header must carry line ranges — "
    "'@@ -<start>,<count> +<start>,<count> @@' (e.g. '@@ -1,5 +1,7 @@'), not a bare '@@'. "
    "Without ranges, git rejects the whole patch ('No valid patches in input'). Re-send with "
    "real '@@' line numbers, or — for a large rewrite — use write_file with overwrite=true."
)


class ApplyPatchInput(BaseModel):
    """Input for `apply_patch`: a unified diff that may span multiple files."""

    diff: str


def _apply_patch(args: ApplyPatchInput, deps: RunDeps) -> ToolResult:
    if any(marker in args.diff for marker in _BEGIN_PATCH_MARKERS):
        return ToolResult(tool_name="apply_patch", success=False, error=_DIALECT_GUIDANCE)
    # A diff that names targets but has no well-formed hunk header is the bare-'@@' case git
    # rejects opaquely; teach the fix instead of letting the model retry the same broken hunk.
    if _parse_patch_targets(args.diff) and not _HUNK_HEADER_RE.search(args.diff):
        return ToolResult(tool_name="apply_patch", success=False, error=_MALFORMED_HUNK_GUIDANCE)
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
    description=(
        "Apply a unified git diff to the workspace atomically (may span multiple files). "
        "Format: '--- a/<path>' / '+++ b/<path>' headers with '@@' hunks, exactly as "
        "`git diff` prints; use '/dev/null' for created or deleted files. Other patch "
        "dialects (e.g. '*** Begin Patch') are not accepted."
    ),
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
