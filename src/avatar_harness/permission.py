"""PermissionPolicy — the before-tool-call control gate (§11).

An *awaited control hook*, not an observation event (§13): the runner calls it
before every execution and acts on the `ToolPermission` it returns (allow /
block / ask). It can block and redirect control flow; the event emitter cannot.
Keeping it a direct call — never an emitter subscriber — is the whole point.

Tiers (§11): 0 reads (allow) · 1 apply_patch (allow iff every target path
resolves inside the workspace) · 2 commands (allow) · 3+ destructive / external
(blocked by default in the non-interactive MVP; `ask` lands with the Phase 3 REPL).
"""

from collections.abc import Sequence
from fnmatch import fnmatch
from pathlib import PurePosixPath

from pydantic import BaseModel, ValidationError

from avatar_harness.config import DEFAULT_SENSITIVE_PATH_GLOBS
from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolDefinition
from avatar_harness.workspace import Workspace

_ASK_TIER = 3  # tier at and above which an action is gated (ask/block).
_EDIT_TIER = 1  # the mutation tier (apply_patch) — blocked for read-only investigate tasks.


def _path_is_sensitive(rel_path: str, globs: Sequence[str]) -> bool:
    """Whether `rel_path` matches any denylist glob (§11, Phase 2.5).

    A pattern *without* a slash matches any single path component (gitignore-style
    "match anywhere" — so ``.env`` hits ``a/b/.env`` and ``.ssh`` hits ``.ssh/id_rsa``).
    A pattern *with* a slash is matched against the whole relative path.

    Args:
        rel_path: The workspace-relative path to test.
        globs: The denylist patterns.

    Returns:
        `True` if any pattern matches, else `False`.
    """
    parts = PurePosixPath(rel_path).parts
    for glob in globs:
        if "/" in glob:
            if fnmatch(rel_path, glob):
                return True
        elif any(fnmatch(part, glob) for part in parts):
            return True
    return False


class ToolPermission(BaseModel):
    """The gate's decision for one tool call — the runner acts on this (§11)."""

    blocked: bool
    reason: str = ""
    ask: bool = False  # the action needs human approval; non-interactive runs treat this as blocked


class PermissionPolicy:
    """Evaluates a tool call against the §11 tier table before it runs.

    Args:
        sensitive_path_globs: The denylist enforced over every tool's declared paths.
            Defaults to the built-in set (secure by default); the runner threads the
            configured `HarnessConfig.sensitive_path_globs` through.
    """

    def __init__(self, sensitive_path_globs: Sequence[str] | None = None) -> None:
        self._sensitive = list(
            DEFAULT_SENSITIVE_PATH_GLOBS if sensitive_path_globs is None else sensitive_path_globs
        )

    def check(
        self,
        tool: ToolDefinition,
        raw_input: dict,
        state: TaskState,
        ws: Workspace,
    ) -> ToolPermission:
        """Return the control decision for `tool` with `raw_input` (allow / block / ask).

        Args:
            tool: The tool definition, carrying its `permission_tier` and declared paths.
            raw_input: The proposed tool arguments.
            state: The current task state; its `task_kind` gates mutation.
            ws: The run-scoped workspace, used for path confinement.

        Returns:
            The `ToolPermission` the runner acts on.
        """
        tier = tool.permission_tier
        if tier >= _ASK_TIER:
            # Destructive / external actions are gated; no auto-approval path in the MVP.
            return ToolPermission(
                blocked=True,
                ask=True,
                reason=f"{tool.name!r} is tier {tier} (destructive/external) — blocked pending approval",
            )
        if state.task_kind == "investigate" and tier == _EDIT_TIER:
            # A read-only kind may never mutate — prevention at the gate, not just the
            # verifier catching the diff afterward (tier 1 is the editing capability).
            return ToolPermission(
                blocked=True,
                reason=f"investigate tasks cannot modify files; {tool.name!r} is not permitted",
            )
        # Path policy over the tool's *declared* paths — one place for confinement AND the
        # sensitive-path denylist, so neither can drift per tool (subsumes apply_patch's
        # old special-case: its targets are now just declared paths).
        return self._check_paths(self._declared_paths(tool, raw_input), ws)

    def _declared_paths(self, tool: ToolDefinition, raw_input: dict) -> list[str]:
        """The tool's self-declared filesystem paths for `raw_input`, or `[]` if invalid.

        Validation failures need no path verdict — the runtime rejects the call next.

        Args:
            tool: The tool whose `paths` extractor is consulted.
            raw_input: The unvalidated call arguments.

        Returns:
            The declared workspace paths, or `[]` when the input does not validate.
        """
        try:
            args = tool.input_model.model_validate(raw_input)
        except ValidationError:
            return []
        return list(tool.paths(args))

    def _check_paths(self, paths: list[str], ws: Workspace) -> ToolPermission:
        outside = sorted(p for p in paths if not ws.contains(p))
        if outside:
            return ToolPermission(blocked=True, reason=f"path(s) resolve outside the workspace: {outside}")
        sensitive = sorted(p for p in paths if _path_is_sensitive(p, self._sensitive))
        if sensitive:
            # Treated like a tier-3 gate: blocked now, an `ask` once the REPL lands (Phase 3).
            return ToolPermission(
                blocked=True, ask=True, reason=f"sensitive path(s) refused by the denylist: {sensitive}"
            )
        return ToolPermission(blocked=False)
