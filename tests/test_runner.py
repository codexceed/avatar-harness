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
from avatar_harness.tools.base import ToolRegistry
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


def _runner(tmp_path, registry: ToolRegistry, decisions, **config_kw) -> AgentRunner:
    config = HarnessConfig(**config_kw)
    deps = RunDeps(
        workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken()
    )
    return AgentRunner(
        model_client=ScriptedModel(decisions),
        registry=registry,
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(),
        emitter=Emitter(),
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


class _RaisingModel:
    """A ModelClient whose decisions never parse — exercises recovery (§6)."""

    def decide(self, context: object) -> ModelDecision:
        raise DecisionParseError("garbage output")


def test_malformed_decisions_yield_incomplete(tmp_path, read_registry):
    config = HarnessConfig()
    deps = RunDeps(
        workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken()
    )
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
