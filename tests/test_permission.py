from avatar_harness.permission import PermissionPolicy, ToolPermission
from avatar_harness.tools.commands import run_tests
from avatar_harness.tools.edit import apply_patch
from pydantic import BaseModel

from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolDefinition, ToolResult
from avatar_harness.tools.filesystem import read_file
from avatar_harness.workspace import Workspace


def _noop_handler(args: BaseModel, deps: object) -> ToolResult:
    return ToolResult(tool_name="noop", success=True)


class _Empty(BaseModel):
    pass


def _tier3_tool() -> ToolDefinition:
    """A synthetic high-tier action (e.g. file deletion) — no such tool ships in the MVP."""
    return ToolDefinition(
        name="delete_tree",
        description="dangerous",
        input_model=_Empty,
        handler=_noop_handler,
        phases=frozenset({"editing"}),
        permission_tier=3,
    )


def _state() -> TaskState:
    return TaskState(goal="x", task_kind="edit")


def test_tier0_reads_allowed(git_repo):
    perm = PermissionPolicy().check(read_file, {"path": "calc.py"}, _state(), Workspace(git_repo))
    assert perm.blocked is False


def test_apply_patch_allowed_when_paths_validate(git_repo):
    diff = "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-x\n+y\n"
    perm = PermissionPolicy().check(apply_patch, {"diff": diff}, _state(), Workspace(git_repo))
    assert perm.blocked is False


def test_apply_patch_blocked_when_path_escapes(git_repo):
    diff = "--- a/../evil.py\n+++ b/../evil.py\n@@ -0,0 +1 @@\n+pwned\n"
    perm = PermissionPolicy().check(apply_patch, {"diff": diff}, _state(), Workspace(git_repo))
    assert perm.blocked is True
    assert perm.reason  # explains the refusal


def test_tier2_commands_allowed_with_timeout(git_repo):
    perm = PermissionPolicy().check(run_tests, {}, _state(), Workspace(git_repo))
    assert perm.blocked is False


def test_tier3_action_blocked_by_default(git_repo):
    perm = PermissionPolicy().check(_tier3_tool(), {}, _state(), Workspace(git_repo))
    assert perm.blocked is True
    assert perm.reason


def test_gate_returns_control_decision_not_event(git_repo):
    # The gate is an awaited control hook: it hands back a decision the runner acts on
    # (block/redirect), unlike the fire-and-forget emitter. Constructing it needs no
    # emitter, and a block carries a reason the runner can surface.
    perm = PermissionPolicy().check(_tier3_tool(), {}, _state(), Workspace(git_repo))
    assert isinstance(perm, ToolPermission)
    assert perm.blocked and isinstance(perm.reason, str)
