from pydantic import BaseModel

from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextBuilder
from avatar_harness.deps import CancellationToken, RunDeps
from avatar_harness.events import Emitter
from avatar_harness.model_client import (
    AskUser,
    DecisionParseError,
    FinalAnswer,
    ModelDecision,
    ToolCall,
)
from avatar_harness.runner import AgentRunner
from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolDefinition, ToolRegistry, ToolResult
from avatar_harness.tools.commands import run_linter, run_tests
from avatar_harness.tools.edit import apply_patch
from avatar_harness.tools.filesystem import read_file
from avatar_harness.verifier import Verifier
from avatar_harness.workspace import Workspace


class ScriptedModel:
    """A ModelClient that replays pre-built decisions; repeats the last when exhausted."""

    def __init__(self, decisions: list[ModelDecision]) -> None:
        self._decisions = decisions
        self._i = 0

    def decide(self, context: object) -> ModelDecision:
        decision = self._decisions[min(self._i, len(self._decisions) - 1)]
        self._i += 1
        return decision


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
            thought_summary="check app.py", action=ToolCall(name="read_file", input={"path": "app.py"})
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


class _RaisingModel:
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
