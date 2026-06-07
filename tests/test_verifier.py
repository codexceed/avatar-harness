from avatar_harness.config import HarnessConfig
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


# --- edit gate (§12) ----------------------------------------------------

_FIX = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
)

_PASS = 'python -c "import sys; sys.exit(0)"'
_FAIL = 'python -c "import sys; sys.exit(1)"'
_NO_TESTS = 'python -c "import sys; sys.exit(5)"'  # pytest convention: no tests collected


def _edit(ws: Workspace, diff: str = _FIX) -> TaskState:
    """Apply `diff` to the workspace and return an `edit` state that reflects it."""
    changed = ws.apply_patch(diff)
    return TaskState(goal="fix the bug", task_kind="edit", files_modified=set(changed))


def test_edit_gate_passes_with_diff_and_passing_tests(git_repo):
    ws = Workspace(git_repo)
    state = _edit(ws)
    report = Verifier(HarnessConfig(test_command=_PASS, lint_command="")).verify(state, ws)
    assert report.passed


def test_edit_gate_fails_with_no_diff(git_repo):
    ws = Workspace(git_repo)
    state = TaskState(goal="fix the bug", task_kind="edit")  # nothing changed
    report = Verifier(HarnessConfig(test_command=_PASS)).verify(state, ws)
    assert report.passed is False
    assert any(c.name == "diff_present" and c.status == "fail" for c in report.checks)


def test_edit_gate_fails_on_failing_tests(git_repo):
    ws = Workspace(git_repo)
    state = _edit(ws)
    report = Verifier(HarnessConfig(test_command=_FAIL)).verify(state, ws)
    assert report.passed is False


def test_edit_gate_passes_on_clean_lint_when_no_test_target(git_repo):
    ws = Workspace(git_repo)
    state = _edit(ws)
    # Tests skip for an ALLOWED reason (none in repo); clean lint is the positive signal.
    report = Verifier(HarnessConfig(test_command=_NO_TESTS, lint_command=_PASS)).verify(state, ws)
    assert report.passed


def test_edit_gate_fails_on_disallowed_skip(git_repo):
    ws = Workspace(git_repo)
    state = _edit(ws)
    # No test command configured is NOT an allowed skip for an edit (criterion 2),
    # even though lint passes and would otherwise supply a positive signal.
    report = Verifier(HarnessConfig(test_command="", lint_command=_PASS)).verify(state, ws)
    assert report.passed is False
    assert any(c.name == "tests" and c.status == "skip" for c in report.checks)


def test_edit_gate_flags_placeholder_or_secret(git_repo):
    ws = Workspace(git_repo)
    leak = (
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def add(a, b):\n"
        '+    api_key = "AKIAIOSFODNN7EXAMPLE"\n'
        "     return a - b\n"
    )
    state = _edit(ws, leak)
    report = Verifier(HarnessConfig(test_command=_PASS)).verify(state, ws)
    assert report.passed is False
    assert any(c.name == "no_secrets" and c.status == "fail" for c in report.checks)


# --- test_only gate (§12) -----------------------------------------------

_ADD_TEST = "--- /dev/null\n+++ b/tests/test_calc.py\n@@ -0,0 +1,2 @@\n+def test_add():\n+    assert True\n"


def test_test_only_gate_passes_when_new_tests_added_and_pass(git_repo):
    ws = Workspace(git_repo)
    changed = ws.apply_patch(_ADD_TEST)
    state = TaskState(goal="add tests", task_kind="test_only", files_modified=set(changed))
    report = Verifier(HarnessConfig(test_command=_PASS)).verify(state, ws)
    assert report.passed


def test_test_only_gate_fails_when_no_tests_changed(git_repo):
    ws = Workspace(git_repo)
    state = _edit(ws)  # changed calc.py only — not a test file
    state.task_kind = "test_only"
    report = Verifier(HarnessConfig(test_command=_PASS)).verify(state, ws)
    assert report.passed is False
    assert any(c.name == "tests_changed" and c.status == "fail" for c in report.checks)


def test_verifier_never_passes_on_zero_positive_signal(git_repo):
    ws = Workspace(git_repo)
    state = _edit(ws)
    # Diff present, no secrets, but BOTH checks skip for allowed reasons → no positive
    # external signal exists, so the gate must not pass (criterion 3).
    report = Verifier(HarnessConfig(test_command=_NO_TESTS, lint_command="")).verify(state, ws)
    assert report.passed is False
