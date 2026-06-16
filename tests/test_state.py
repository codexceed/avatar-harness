from avatar.state import TaskState


def test_taskstate_roundtrips():
    state = TaskState(goal="fix the auth test", constraints=["no new deps"])
    state.files_read.add("auth/session.py")
    state.add_feedback("found the failing assertion")

    restored = TaskState.model_validate_json(state.model_dump_json())
    assert restored == state


def test_terminal_property():
    state = TaskState(goal="anything")
    assert state.terminal is False
    state.outcome = "success"
    assert state.terminal is True


def test_add_feedback_appends_evidence():
    state = TaskState(goal="anything")
    state.add_feedback("first")
    first_snapshot = state.evidence[0].model_copy(deep=True)
    state.add_feedback("second")

    assert len(state.evidence) == 2
    assert state.evidence[0] == first_snapshot  # earlier evidence is not mutated
    assert state.evidence[1].summary == "second"
