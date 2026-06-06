from avatar_harness.context import ContextBuilder
from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolDefinition
from avatar_harness.tools.filesystem import read_file
from avatar_harness.workspace import Workspace


def test_context_contains_goal_phase_and_recent_evidence(tmp_path, read_registry):
    state = TaskState(goal="explain the loop", task_kind="investigate")
    state.add_feedback("found the loop in runner.py")
    packet = ContextBuilder().build(state, Workspace(tmp_path), read_registry)
    assert packet.goal == "explain the loop"
    assert packet.phase == "investigating"
    assert any("runner.py" in line for line in packet.recent_evidence)


def test_context_omits_out_of_phase_tools(tmp_path, read_registry):
    read_registry.register(
        ToolDefinition(
            name="apply_patch",
            description="edit-only",
            input_model=read_file.input_model,
            handler=read_file.handler,
            phases=frozenset({"editing"}),
        )
    )
    state = TaskState(goal="x", task_kind="investigate")  # phase = investigating
    packet = ContextBuilder().build(state, Workspace(tmp_path), read_registry)
    names = {t.name for t in packet.allowed_tools}
    assert "read_file" in names
    assert "apply_patch" not in names
