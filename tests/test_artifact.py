from avatar_harness.artifact import ArtifactManager

from avatar_harness.state import CheckResult, CommandRecord, TaskState, VerifierResult
from avatar_harness.workspace import Workspace

_FIX = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
)


def _solved_state() -> TaskState:
    state = TaskState(goal="fix add()", task_kind="edit", outcome="success")
    state.files_modified = {"calc.py"}
    state.commands_run = [CommandRecord(step=1, command="pytest -q", exit_code=0, summary="ok")]
    state.verifier_results = [
        VerifierResult(
            passed=True,
            summary="edit verified",
            checks=[CheckResult(name="tests", kind="required", status="pass", evidence="exit=0")],
        )
    ]
    return state


def test_artifact_status_is_state_outcome_verbatim(git_repo):
    ws = Workspace(git_repo)
    for outcome in ("success", "incomplete", "blocked", "failed"):
        state = TaskState(goal="x", task_kind="edit", outcome=outcome)
        artifact = ArtifactManager().build(state, ws)
        assert artifact.status == outcome  # never re-derived (§14)


def test_artifact_lists_files_commands_verification_and_diff_ref(git_repo):
    ws = Workspace(git_repo)
    ws.apply_patch(_FIX)
    artifact = ArtifactManager().build(_solved_state(), ws)
    assert "calc.py" in artifact.files_changed
    assert any("pytest" in c for c in artifact.commands_run)
    assert any("verified" in v for v in artifact.verification)
    assert "a + b" in artifact.diff_ref  # the uncommitted diff is the deliverable (§15)
    rendered = ArtifactManager().render(artifact)
    assert "success" in rendered
    assert "calc.py" in rendered
