from avatar_harness.state import TaskState
from avatar_harness.verifier import Verifier
from avatar_harness.workspace import Workspace


def _investigate_state(**kwargs) -> TaskState:
    return TaskState(goal="why is login slow?", task_kind="investigate", **kwargs)


def test_investigate_gate_passes_with_cited_evidence(tmp_path):
    state = _investigate_state(
        files_read={"auth/session.py"},
        final_answer="the slowdown is in auth/session.py, the expiry check on line 12",
    )
    report = Verifier().verify(state, Workspace(tmp_path))
    assert report.passed


def test_investigate_gate_fails_on_zero_evidence(tmp_path):
    # An answer with no inspected files / commands / evidence cannot pass.
    state = _investigate_state(final_answer="I think it's probably fine.")
    report = Verifier().verify(state, Workspace(tmp_path))
    assert report.passed is False
    assert report.recommended_next_action  # repair direction is provided


def test_investigate_gate_fails_on_unintended_diff(tmp_path):
    # Grounded answer, but the task modified a file — an investigate must not.
    state = _investigate_state(
        files_read={"auth/session.py"},
        files_modified={"auth/session.py"},
        final_answer="auth/session.py looked wrong so I changed it",
    )
    report = Verifier().verify(state, Workspace(tmp_path))
    assert report.passed is False
    assert any(c.name == "no_unintended_diff" and c.status == "fail" for c in report.checks)
