"""PermissionPolicy — the before-tool-call control gate (§11).

An *awaited control hook*, not an observation event (§13): the runner calls it
before every execution and acts on the `ToolPermission` it returns (allow /
block / ask). It can block and redirect control flow; the event emitter cannot.
Keeping it a direct call — never an emitter subscriber — is the whole point.

Tiers (§11): 0 reads (allow) · 1 apply_patch (allow iff every target path
resolves inside the workspace) · 2 commands (allow) · 3+ destructive / external
(blocked by default in the non-interactive MVP; `ask` lands with the Phase 3 REPL).
"""

from pydantic import BaseModel

from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolDefinition
from avatar_harness.workspace import Workspace, _parse_patch_targets

_ASK_TIER = 3  # tier at and above which an action is gated (ask/block).
_EDIT_TIER = 1  # the mutation tier (apply_patch) — blocked for read-only investigate tasks.


class ToolPermission(BaseModel):
    """The gate's decision for one tool call — the runner acts on this (§11)."""

    blocked: bool
    reason: str = ""
    ask: bool = False  # the action needs human approval; non-interactive runs treat this as blocked


class PermissionPolicy:
    """Evaluates a tool call against the §11 tier table before it runs."""

    def check(
        self,
        tool: ToolDefinition,
        raw_input: dict,
        state: TaskState,
        ws: Workspace,
    ) -> ToolPermission:
        """Return the control decision for `tool` with `raw_input` (allow / block / ask).

        Args:
            tool: The tool definition, carrying its `permission_tier`.
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
        if tool.name == "apply_patch":
            return self._check_patch_paths(raw_input, ws)
        # Tiers 0 and 2 are allowed (commands carry their own timeout, §11).
        return ToolPermission(blocked=False)

    def _check_patch_paths(self, raw_input: dict, ws: Workspace) -> ToolPermission:
        diff = raw_input.get("diff", "") if isinstance(raw_input, dict) else ""
        outside = sorted(p for p in _parse_patch_targets(diff) if not ws.contains(p))
        if outside:
            return ToolPermission(
                blocked=True, reason=f"patch targets resolve outside the workspace: {outside}"
            )
        return ToolPermission(blocked=False)
