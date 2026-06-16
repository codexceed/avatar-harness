from pydantic import BaseModel

from avatar.permission import PermissionPolicy, ToolPermission
from avatar.state import TaskState
from avatar.tools.base import ToolDefinition, ToolResult
from avatar.tools.commands import run_tests
from avatar.tools.edit import str_replace, write_file
from avatar.tools.filesystem import read_file
from avatar.workspace import Workspace


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


def test_str_replace_allowed_when_paths_validate(git_repo):
    raw = {"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"}
    perm = PermissionPolicy().check(str_replace, raw, _state(), Workspace(git_repo))
    assert perm.blocked is False


def test_str_replace_blocked_when_path_escapes(git_repo):
    raw = {"path": "../escape.py", "old_string": "a", "new_string": "b"}
    perm = PermissionPolicy().check(str_replace, raw, _state(), Workspace(git_repo))
    assert perm.blocked is True
    assert perm.reason  # explains the refusal


def test_investigate_can_str_replace(git_repo):
    # ADR-0005: tier-1 mutation is legal in an investigate task — prevention at the
    # gate is traded for detection at the verifier, whose net-zero-diff contract
    # (`no_unintended_diff`) is the enforcement point for transient instrumentation.
    raw = {"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"}
    state = TaskState(goal="why is it slow?", task_kind="investigate")
    perm = PermissionPolicy().check(str_replace, raw, state, Workspace(git_repo))
    assert perm.blocked is False


def test_investigate_can_write_file(git_repo):
    # The other tier-1 tool rides the same ADR-0005 relaxation: a scratch probe
    # script is legal as long as the tree nets to zero diff at verification.
    state = TaskState(goal="why is it slow?", task_kind="investigate")
    raw = {"path": "probe.py", "content": "print('probe')\n"}
    perm = PermissionPolicy().check(write_file, raw, state, Workspace(git_repo))
    assert perm.blocked is False


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
