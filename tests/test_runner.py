import subprocess

from conftest import ScriptedModel
from pydantic import BaseModel

from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextBuilder
from avatar_harness.deps import CancellationToken, RunDeps
from avatar_harness.event_types import VerificationPlanFrozen, VerificationStart
from avatar_harness.events import Emitter
from avatar_harness.model_client import (
    AskUser,
    DecisionParseError,
    FinalAnswer,
    ModelClient,
    ModelDecision,
    ToolCall,
)
from avatar_harness.runner import AgentRunner
from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolDefinition, ToolRegistry, ToolResult
from avatar_harness.tools.commands import run_linter, run_tests
from avatar_harness.tools.edit import apply_patch, write_file
from avatar_harness.tools.filesystem import read_file
from avatar_harness.verifier import Verifier
from avatar_harness.workspace import Workspace


def _runner(tmp_path, registry: ToolRegistry, decisions, *, emitter=None, **config_kw) -> AgentRunner:
    config = HarnessConfig(**config_kw)
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    return AgentRunner(
        model_client=ScriptedModel(decisions),
        registry=registry,
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=emitter or Emitter(),
        config=config,
    )


def test_investigate_loop_runs_to_answer_and_verifies(tmp_path, read_registry):
    (tmp_path / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="the handler lives in app.py")),
    ]
    state = TaskState(goal="where is the handler?", task_kind="investigate")
    result = _runner(tmp_path, read_registry, decisions).run(state)
    assert result.outcome == "success"
    assert not result.files_modified
    assert result.final_answer == "the handler lives in app.py"


def test_final_answer_without_evidence_is_rejected(tmp_path, read_registry):
    # Claims done with no inspection — the verifier rejects it; not self-certified.
    decisions = [ModelDecision(action=FinalAnswer(answer="it's probably fine"))]
    state = TaskState(goal="why is it slow?", task_kind="investigate")
    result = _runner(tmp_path, read_registry, decisions).run(state)
    assert result.outcome != "success"
    assert result.outcome == "failed"  # exhausted repair attempts on an unverifiable claim


def test_iteration_budget_yields_incomplete(tmp_path, read_registry):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [ModelDecision(action=ToolCall(name="search_repo", input={"query": "x"}))]
    state = TaskState(goal="look around forever", task_kind="investigate")
    result = _runner(tmp_path, read_registry, decisions, max_iterations=3).run(state)
    assert result.outcome == "incomplete"
    assert result.iterations == 3


def test_ask_user_noninteractive_yields_blocked(tmp_path, read_registry):
    decisions = [ModelDecision(action=AskUser(question="which module did you mean?"))]
    state = TaskState(goal="ambiguous request", task_kind="investigate")
    result = _runner(tmp_path, read_registry, decisions, interactive=False).run(state)
    assert result.outcome == "blocked"


def test_runner_emits_model_decisions(tmp_path, read_registry):
    # The trajectory must capture the model's voice (thought + chosen action),
    # not just tool names — otherwise the logs are inscrutable.
    (tmp_path / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(
            thought_summary="check app.py",
            action=ToolCall(name="read_file", input={"path": "app.py"}),
            transport="native",
        ),
        ModelDecision(action=FinalAnswer(answer="the handler is in app.py")),
    ]
    events: list = []
    emitter = Emitter()
    emitter.subscribe(events.append)
    _runner(tmp_path, read_registry, decisions, emitter=emitter).run(
        TaskState(goal="where?", task_kind="investigate")
    )
    logged = [e for e in events if e["type"] == "model_decision"]
    assert logged
    assert logged[0]["thought"] == "check app.py"
    assert "read_file" in logged[0]["action"]
    assert logged[0]["transport"] == "native"


class _RaisingModel(ModelClient):
    """A ModelClient whose decisions never parse — exercises recovery (§6)."""

    def decide(self, context: object) -> ModelDecision:
        raise DecisionParseError("garbage output")


def test_malformed_decisions_yield_incomplete(tmp_path, read_registry):
    config = HarnessConfig()
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    runner = AgentRunner(
        model_client=_RaisingModel(),
        registry=read_registry,
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(),
        emitter=Emitter(),
        config=config,
    )
    result = runner.run(TaskState(goal="x", task_kind="investigate"))
    assert result.outcome == "incomplete"  # consecutive failures, never a verified claim
    assert result.consecutive_failures == config.max_consecutive_failures


# --- Phase 2.5: action ledger -------------------------------------------


def test_runner_records_decision_each_turn(tmp_path, read_registry):
    (tmp_path / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(thought_summary="peek", action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="handler lives in app.py")),
    ]
    state = _runner(tmp_path, read_registry, decisions).run(TaskState(goal="where?", task_kind="investigate"))
    assert len(state.decisions) >= 2  # one record per turn (previously never written)
    assert any("read_file" in d.chosen for d in state.decisions)
    assert any(d.outcome for d in state.decisions)  # each call's outcome is recorded


def test_repeated_identical_tool_call_is_flagged(tmp_path, read_registry):
    # An identical re-issued call is flagged back to the model (anti-loop nudge).
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    same = ToolCall(name="read_file", input={"path": "app.py"})
    decisions = [
        ModelDecision(action=same),
        ModelDecision(action=same),
        ModelDecision(action=FinalAnswer(answer="read app.py, x is set")),
    ]
    state = _runner(tmp_path, read_registry, decisions).run(TaskState(goal="x", task_kind="investigate"))
    assert any(e.kind == "repeat" for e in state.evidence)


# --- Phase 2: permission gate + edit loop -------------------------------

_FIX = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
)


def _edit_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool in (read_file, apply_patch, run_tests, run_linter):
        reg.register(tool)
    return reg


def test_runner_consults_gate_before_execution(git_repo):
    # A tier-3 action whose handler would leave a sentinel if it ever ran.
    class _Empty(BaseModel):
        pass

    def _danger(args, deps) -> ToolResult:
        (deps.workspace.root / "SENTINEL").write_text("ran", encoding="utf-8")
        return ToolResult(tool_name="delete_tree", success=True)

    danger = ToolDefinition(
        name="delete_tree",
        description="dangerous",
        input_model=_Empty,
        handler=_danger,
        phases=frozenset({"investigating"}),
        permission_tier=3,
    )
    reg = _edit_registry()
    reg.register(danger)
    reg.register(read_file)
    decisions = [
        ModelDecision(action=ToolCall(name="delete_tree", input={})),
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer="the bug is in calc.py")),
    ]
    state = TaskState(goal="look at calc.py", task_kind="investigate")
    result = _runner(git_repo, reg, decisions).run(state)
    assert not (git_repo / "SENTINEL").exists()  # blocked → never executed
    assert result.outcome == "success"  # loop continued past the block


def test_edit_task_runs_to_verified_success(git_repo):
    test_cmd = 'python -c "import calc; assert calc.add(2, 3) == 5"'
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _FIX})),
        ModelDecision(action=FinalAnswer(answer="fixed the sign error in calc.py add()")),
    ]
    state = TaskState(goal="fix add()", task_kind="edit")
    result = _runner(git_repo, _edit_registry(), decisions, test_command=test_cmd, lint_command="").run(state)
    assert result.outcome == "success"  # verifier ran the command, not self-certified
    assert "calc.py" in result.files_modified


def test_runner_records_commands_run(git_repo):
    # The verifier runs its own command (§5); the runner must record it in the
    # commands_run ledger so the artifact and logs reflect what actually ran (§7/§14).
    test_cmd = 'python -c "import calc; assert calc.add(2, 3) == 5"'
    decisions = [
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _FIX})),
        ModelDecision(action=FinalAnswer(answer="fixed")),
    ]
    state = TaskState(goal="fix add()", task_kind="edit")
    result = _runner(git_repo, _edit_registry(), decisions, test_command=test_cmd, lint_command="").run(state)
    assert result.outcome == "success"
    assert any(test_cmd in c.command for c in result.commands_run)


def test_bad_patch_leaves_workspace_unchanged_and_loops(git_repo):
    before = Workspace(git_repo).read("calc.py")
    stale = "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-return a * b\n+return a + b\n"
    decisions = [ModelDecision(action=ToolCall(name="apply_patch", input={"diff": stale}))]
    state = TaskState(goal="fix add()", task_kind="edit")
    result = _runner(git_repo, _edit_registry(), decisions, max_consecutive_failures=3).run(state)
    assert Workspace(git_repo, allow_dirty=True).read("calc.py") == before  # nothing written
    assert result.outcome == "incomplete"  # tool errors, not a verification failure


def test_repair_budget_exhaustion_yields_failed(git_repo):
    failing = 'python -c "import sys; sys.exit(1)"'
    decisions = [
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _FIX})),
        ModelDecision(action=FinalAnswer(answer="I believe this is fixed")),
    ]
    state = TaskState(goal="fix add()", task_kind="edit")
    result = _runner(git_repo, _edit_registry(), decisions, test_command=failing, max_repair_attempts=2).run(
        state
    )
    assert result.outcome == "failed"  # exhausted repair attempts on a rejected claim
    assert result.repair_failures == 2


# --- Phase 2.6 Lane A: phase advance/enforce + budgets + cancellation ----

# A new-file hunk: creates `greeter.py` from nothing — no read precedes it (pure creation).
_NEW_FILE = '--- /dev/null\n+++ b/greeter.py\n@@ -0,0 +1,2 @@\n+def greet():\n+    return "hi"\n'


def _runner_with_token(
    tmp_path, registry: ToolRegistry, decisions, token: CancellationToken, *, emitter=None, **config_kw
) -> AgentRunner:
    """Build a runner exposing a caller-supplied cancellation token (for cancel tests)."""
    config = HarnessConfig(**config_kw)
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=token)
    return AgentRunner(
        model_client=ScriptedModel(decisions),
        registry=registry,
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=emitter or Emitter(),
        config=config,
    )


def test_phase_advances_to_editing_on_first_edit_intent(git_repo):
    # An edit task starts in `investigating`; the first apply_patch is the edit intent
    # that advances the phase to `editing` — not a read-counter.
    decisions = [ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _FIX}))]
    state = TaskState(goal="fix add()", task_kind="edit")
    assert state.phase == "investigating"
    _runner(git_repo, _edit_registry(), decisions, lint_command="", max_iterations=1).run(state)
    assert state.phase in {"editing", "verifying"}  # advanced past investigating on the edit


def test_pure_creation_from_bare_workspace_succeeds(git_repo):
    # A new-file hunk applies with ZERO reads — the creation case a `>=1 read` trigger kills.
    test_cmd = "python -c \"import greeter; assert greeter.greet() == 'hi'\""
    decisions = [
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _NEW_FILE})),
        ModelDecision(action=FinalAnswer(answer="created greeter.py with greet()")),
    ]
    state = TaskState(goal="add a greeter", task_kind="edit")
    result = _runner(git_repo, _edit_registry(), decisions, test_command=test_cmd, lint_command="").run(state)
    assert result.outcome == "success"
    assert not result.files_read  # never forced to read
    assert "greeter.py" in result.files_modified


def test_pure_creation_via_write_file_succeeds(git_repo):
    # ADR-0003 B: file creation as plain content — phase advance (tier-1 edit intent),
    # staging into the diff, and the §12 edit gate all behave exactly as for apply_patch.
    test_cmd = "python -c \"import greeter; assert greeter.greet() == 'hi'\""
    decisions = [
        ModelDecision(
            action=ToolCall(
                name="write_file",
                input={"path": "greeter.py", "content": "def greet():\n    return 'hi'\n"},
            )
        ),
        ModelDecision(action=FinalAnswer(answer="created greeter.py with greet()")),
    ]
    reg = _edit_registry()
    reg.register(write_file)
    state = TaskState(goal="add a greeter", task_kind="edit")
    result = _runner(git_repo, reg, decisions, test_command=test_cmd, lint_command="").run(state)
    assert result.outcome == "success"  # verified: the diff exists and the test ran green
    assert "greeter.py" in result.files_modified
    assert state.phase in {"editing", "verifying"}  # tier-1 write advanced the phase


def test_modify_without_read_fails_stale_then_recovers(git_repo):
    # Inspect-before-edit EMERGES from clean-apply: a stale modify-hunk fails (model-correctable),
    # then a correct patch (after a read) applies and verifies. No read-counter is consulted.
    test_cmd = 'python -c "import calc; assert calc.add(2, 3) == 5"'
    stale = "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-return a * b\n+return a + b\n"
    decisions = [
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": stale})),  # fails stale
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),  # then inspects
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _FIX})),  # correct patch
        ModelDecision(action=FinalAnswer(answer="fixed the sign error in calc.py add()")),
    ]
    state = TaskState(goal="fix add()", task_kind="edit")
    result = _runner(git_repo, _edit_registry(), decisions, test_command=test_cmd, lint_command="").run(state)
    assert result.outcome == "success"
    assert "calc.py" in result.files_modified


def test_phase_changed_emitted_on_transition(git_repo):
    test_cmd = 'python -c "import calc; assert calc.add(2, 3) == 5"'
    decisions = [
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _FIX})),
        ModelDecision(action=FinalAnswer(answer="fixed the sign error in calc.py add()")),
    ]
    events: list = []
    emitter = Emitter()
    emitter.subscribe(events.append)
    state = TaskState(goal="fix add()", task_kind="edit")
    _runner(
        git_repo, _edit_registry(), decisions, test_command=test_cmd, lint_command="", emitter=emitter
    ).run(state)
    changes = [e for e in events if e["type"] == "phase_changed"]
    assert changes  # at least one transition emitted
    assert {e["new"] for e in changes} >= {"editing"}  # advanced into editing
    assert any(e["old"] == "investigating" and e["new"] == "editing" for e in changes)


def test_out_of_phase_tool_call_is_model_correctable(git_repo):
    # run_tests is active only in `editing`/`verifying`; calling it while still
    # investigating is WORKFLOW feedback (model-correctable), not a crash or block.
    decisions = [
        ModelDecision(action=ToolCall(name="run_tests", input={})),  # out of phase
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer="the bug is in calc.py")),
    ]
    state = TaskState(goal="inspect calc.py", task_kind="investigate")
    result = _runner(git_repo, _edit_registry(), decisions).run(state)
    assert any(e.kind == "out_of_phase" for e in result.evidence)  # fed back, not fatal
    assert result.outcome == "success"  # loop continued past the out-of-phase call


def test_repair_falls_back_to_editing(git_repo):
    # A failed verification returns the agent from `verifying` to `editing` for repair.
    failing = 'python -c "import sys; sys.exit(1)"'
    decisions = [
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _FIX})),
        ModelDecision(action=FinalAnswer(answer="I believe this is fixed")),
    ]
    events: list = []
    emitter = Emitter()
    emitter.subscribe(events.append)
    state = TaskState(goal="fix add()", task_kind="edit")
    _runner(
        git_repo,
        _edit_registry(),
        decisions,
        test_command=failing,
        lint_command="",
        max_repair_attempts=2,
        emitter=emitter,
    ).run(state)
    assert any(
        e["old"] == "verifying" and e["new"] == "editing" for e in events if e["type"] == "phase_changed"
    )


def test_wall_clock_budget_yields_incomplete(git_repo):
    # A zero wall-clock budget trips the bound before any turn runs → incomplete.
    decisions = [ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"}))]
    state = TaskState(goal="look around", task_kind="investigate")
    result = _runner(git_repo, _edit_registry(), decisions, max_wall_clock_seconds=0).run(state)
    assert result.outcome == "incomplete"
    assert result.iterations == 0  # the wall-clock bound short-circuits, not iteration exhaustion


def test_context_budget_yields_incomplete(git_repo):
    # A tiny context-token budget is exceeded immediately → incomplete (not failed).
    decisions = [ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"}))]
    state = TaskState(goal="look around", task_kind="investigate")
    result = _runner(git_repo, _edit_registry(), decisions, max_context_tokens=0).run(state)
    assert result.outcome == "incomplete"
    assert result.iterations == 0  # the context bound short-circuits, not iteration exhaustion


def test_cancellation_observed_yields_incomplete(git_repo):
    # A pre-tripped cancellation token stops the loop with an `incomplete` outcome.
    decisions = [ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"}))]
    token = CancellationToken(cancelled=True)
    state = TaskState(goal="look around", task_kind="investigate")
    result = _runner_with_token(git_repo, _edit_registry(), decisions, token).run(state)
    assert result.outcome == "incomplete"
    assert result.iterations == 0  # cancellation observed before any turn ran


def test_cancellation_records_feedback(git_repo):
    # Cancellation is recorded as feedback so the trajectory shows why the run stopped.
    decisions = [ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"}))]
    token = CancellationToken(cancelled=True)
    state = TaskState(goal="look around", task_kind="investigate")
    result = _runner_with_token(git_repo, _edit_registry(), decisions, token).run(state)
    assert any(e.kind == "cancelled" for e in result.evidence)


# --- ADR-0005: transient edits in investigate tasks (net-zero-diff relaxation) ----

_PROBE = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n+    print('probe')\n     return a - b\n"
)
_UNPROBE = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,3 +1,2 @@\n def add(a, b):\n-    print('probe')\n     return a - b\n"
)


def test_investigate_transient_edit_round_trip_verifies(git_repo):
    # The ADR-0005 happy path end-to-end: instrument (apply_patch) -> observe -> revert ->
    # final_answer. The gate admits the tier-1 calls, the phase never advances (the
    # edit-intent bootstrap stays edit-kinds-only), and the unchanged net-zero-diff
    # contract passes because the tree matches the pinned baseline at verification.
    decisions = [
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _PROBE})),
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _UNPROBE})),
        ModelDecision(action=FinalAnswer(answer="probed calc.py: add() subtracts; probe reverted")),
    ]
    events: list = []
    emitter = Emitter()
    emitter.subscribe(events.append)
    state = TaskState(goal="why does add() return the wrong sum?", task_kind="investigate")
    result = _runner(git_repo, _edit_registry(), decisions, emitter=emitter).run(state)
    assert result.outcome == "success"
    assert Workspace(git_repo, allow_dirty=True).diff() == ""  # net-zero at the end
    assert "calc.py" in result.files_modified  # the transient writes are still on the ledger
    # No `investigating -> editing` advance rode the tier-1 calls (investigate flow unchanged).
    assert not any(
        e["old"] == "investigating" and e["new"] == "editing" for e in events if e["type"] == "phase_changed"
    )


def test_investigate_leftover_diff_fails_then_repair_by_revert_succeeds(git_repo):
    # An investigation that LEAVES its instrumentation fails verification with the
    # legible no_unintended_diff reason, and the existing repair loop engages: the
    # model reverts, finalizes again, and the unchanged contract passes.
    decisions = [
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _PROBE})),
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer="probed calc.py: add() subtracts")),  # forgot to revert
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _UNPROBE})),  # repair: revert
        ModelDecision(action=FinalAnswer(answer="probed calc.py: add() subtracts; probe reverted")),
    ]
    state = TaskState(goal="why does add() return the wrong sum?", task_kind="investigate")
    result = _runner(git_repo, _edit_registry(), decisions).run(state)
    assert result.outcome == "success"
    assert result.repair_failures == 1  # exactly one rejection before the revert repaired it
    first = result.verifier_results[0]
    assert first.passed is False
    assert "no_unintended_diff" in first.summary  # the legible reason the model repairs from
    assert Workspace(git_repo, allow_dirty=True).diff() == ""


def test_investigate_write_file_probe_then_delete_verifies(git_repo):
    # The write_file creation path of the ADR-0005 round trip (the apply_patch path is
    # covered above): a scratch probe file is created (staged, so it is visible in the
    # diff), the repo is observed, then the probe is deleted via an apply_patch deletion
    # hunk — the tree nets to zero diff vs the pinned baseline and verification passes.
    delete_probe = "--- a/probe.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-print('probe')\n"
    reg = ToolRegistry()
    for tool in (read_file, apply_patch, write_file):
        reg.register(tool)
    decisions = [
        ModelDecision(
            action=ToolCall(name="write_file", input={"path": "probe.py", "content": "print('probe')\n"})
        ),
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": delete_probe})),
        ModelDecision(action=FinalAnswer(answer="probed: calc.py subtracts in add(); probe deleted")),
    ]
    state = TaskState(goal="why does add() return the wrong sum?", task_kind="investigate")
    result = _runner(git_repo, reg, decisions).run(state)
    assert result.outcome == "success"
    assert not (git_repo / "probe.py").exists()  # the probe is genuinely gone (tree AND index)
    assert Workspace(git_repo, allow_dirty=True).diff() == ""  # net-zero at the end
    assert "probe.py" in result.files_modified  # the transient creation stayed on the ledger


# --- ADR-0007: verification-plan freeze at the phase boundary --------------


def _commit_makefile(git_repo, recipe: str = 'python -c "import calc; assert calc.add(2, 3) == 5"'):
    """Commit a Makefile declaring a `test` target, so detection has a contract."""
    (git_repo / "Makefile").write_text(f"test:\n\t{recipe}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(git_repo), "add", "Makefile"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(git_repo), "commit", "-q", "-m", "add Makefile"], check=True, capture_output=True
    )


class _SinkStub:
    """A typed-event sink stub: records every published draft."""

    def __init__(self) -> None:
        self.events = []

    def publish_nowait(self, draft):
        self.events.append(draft)
        return draft

    async def emit(self, draft):
        self.events.append(draft)
        return draft


def test_runner_freezes_detected_plan_at_editing_transition(git_repo):
    # With no config override, the planner detects the repo's declared contract
    # (the Makefile test target) and the runner freezes it at the
    # investigating → editing boundary; the verifier then executes the frozen plan.
    _commit_makefile(git_repo)
    decisions = [
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _FIX})),
        ModelDecision(action=FinalAnswer(answer="fixed the sign error")),
    ]
    state = TaskState(goal="fix add()", task_kind="edit")
    result = _runner(git_repo, _edit_registry(), decisions, test_command="", lint_command="").run(state)
    assert result.outcome == "success"
    assert result.verification_plan is not None
    by_kind = {c.kind: c for c in result.verification_plan}
    assert by_kind["test"].command == "make test"
    assert by_kind["test"].provenance == "Makefile:test"
    assert any("make test" in c.command for c in result.commands_run)  # the verifier ran the frozen plan


def test_runner_journals_frozen_plan_as_typed_event(git_repo):
    # The frozen plan is journaled BEFORE verification — every run's rubric is auditable.
    _commit_makefile(git_repo)
    config = HarnessConfig(test_command="", lint_command="")
    deps = RunDeps(workspace=Workspace(git_repo), config=config, cancellation=CancellationToken())
    sink = _SinkStub()
    runner = AgentRunner(
        model_client=ScriptedModel(
            [
                ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _FIX})),
                ModelDecision(action=FinalAnswer(answer="fixed")),
            ]
        ),
        registry=_edit_registry(),
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=Emitter(),
        config=config,
        event_sink=sink,
    )
    runner.run(TaskState(goal="fix add()", task_kind="edit"))
    types = [type(e) for e in sink.events]
    assert VerificationPlanFrozen in types
    assert types.index(VerificationPlanFrozen) < types.index(VerificationStart)
    frozen = next(e for e in sink.events if isinstance(e, VerificationPlanFrozen))
    assert [c.command for c in frozen.checks] == ["make test"]


def test_runner_empty_plan_fails_verification_legibly(git_repo):
    # Nothing resolves (no config, no artifacts) → an empty plan freezes and the
    # edit fails verification with a pointer to declaring a contract — never a crash,
    # never an invented Python default.
    decisions = [
        ModelDecision(action=ToolCall(name="apply_patch", input={"diff": _FIX})),
        ModelDecision(action=FinalAnswer(answer="fixed")),
    ]
    state = TaskState(goal="fix add()", task_kind="edit")
    result = _runner(
        git_repo, _edit_registry(), decisions, test_command="", lint_command="", max_repair_attempts=1
    ).run(state)
    assert result.outcome == "failed"
    assert result.verification_plan == []
    assert any("no verification contract" in e.summary for e in result.evidence)


def test_runner_leaves_plan_unfrozen_for_investigate(git_repo):
    # Investigate verification is evidence-shaped, not command-shaped — no plan needed.
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer="the bug is the minus in calc.py")),
    ]
    state = TaskState(goal="what is wrong with add()?", task_kind="investigate")
    result = _runner(git_repo, _edit_registry(), decisions).run(state)
    assert result.outcome == "success"
    assert result.verification_plan is None


# --- canonical repeat detection (loop-determinism hardening) --------------------------------


def test_repeat_detected_despite_input_key_reorder(tmp_path, read_registry):
    # The anti-loop nudge must compare ACTIONS, not dict-insertion-order string forms:
    # the same call with reordered input keys is the same call.
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    first = ToolCall(name="read_file", input={"path": "app.py", "line_range": [1, 1]})
    reordered = ToolCall(name="read_file", input={"line_range": [1, 1], "path": "app.py"})
    decisions = [
        ModelDecision(action=first),
        ModelDecision(action=reordered),
        ModelDecision(action=FinalAnswer(answer="read app.py, x is set")),
    ]
    state = _runner(tmp_path, read_registry, decisions).run(TaskState(goal="x", task_kind="investigate"))
    assert any(e.kind == "repeat" for e in state.evidence)
