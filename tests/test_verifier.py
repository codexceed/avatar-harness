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


def test_investigate_gate_fails_on_unintended_diff(git_repo):
    # Grounded answer, but the working tree still differs from the pinned baseline —
    # an investigation that LEAVES a change fails its contract, legibly, with repair
    # direction pointing at the fix: revert (ADR-0005).
    ws = Workspace(git_repo)
    ws.apply_patch(
        "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n"
        "+    print('probe')\n     return a - b\n"
    )
    state = _investigate_state(
        files_read={"calc.py"},
        files_modified={"calc.py"},
        final_answer="calc.py subtracts instead of adding",
    )
    report = Verifier().verify(state, ws)
    assert report.passed is False
    assert any(c.name == "no_unintended_diff" and c.status == "fail" for c in report.checks)
    assert "no_unintended_diff" in report.summary  # the legible reason
    assert "revert" in (report.recommended_next_action or "")  # repair-by-revert direction


def test_investigate_gate_passes_after_net_zero_revert(git_repo):
    # ADR-0005's key observation: the rule is "no diff at the END", not "no writes ever".
    # A task that instrumented and then reverted has a non-empty files_modified ledger but
    # a tree that nets to zero diff vs the pinned baseline — the contract is satisfied.
    state = _investigate_state(
        files_read={"calc.py"},
        files_modified={"calc.py"},  # the transient instrumentation, already reverted
        final_answer="instrumented calc.py briefly: the bug is the '-' in add()",
    )
    report = Verifier().verify(state, Workspace(git_repo))  # clean tree == pinned baseline
    assert report.passed
    assert any(c.name == "no_unintended_diff" and c.status == "pass" for c in report.checks)


def test_investigate_gate_fails_on_secret_in_leftover_diff(git_repo):
    # The always-on secret/placeholder diff guard (§12) applies to every kind that can
    # write — including investigate now that ADR-0005 admits tier-1 tools.
    ws = Workspace(git_repo)
    ws.apply_patch(
        "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n"
        "+    key = 'AKIA123'\n     return a - b\n"
    )
    state = _investigate_state(
        files_read={"calc.py"},
        files_modified={"calc.py"},
        final_answer="calc.py holds the key logic",
    )
    report = Verifier().verify(state, ws)
    assert report.passed is False
    assert any(c.name == "no_secrets" and c.status == "fail" for c in report.checks)


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


def test_edit_gate_flags_secret_in_created_file(git_repo):
    # The secret guard must see brand-new files too, not just modified tracked ones.
    ws = Workspace(git_repo)
    leak = '--- /dev/null\n+++ b/cfg.py\n@@ -0,0 +1 @@\n+API = "AKIAIOSFODNN7EXAMPLE"\n'
    changed = ws.apply_patch(leak)
    state = TaskState(goal="add config", task_kind="edit", files_modified=set(changed))
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
