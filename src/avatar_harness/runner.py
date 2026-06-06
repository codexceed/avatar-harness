"""AgentRunner — the bounded loop that terminates on verification (§5).

The runner owns *all* `TaskState` mutation (§8); tools and the verifier are
pure-ish workers. Phase 1 covers the read-only path: tier-0 tools, no permission
gate (every tool is tier 0), and the minimal `investigate` verifier. The loop is
deliberately a near-verbatim transcription of the §5 pseudocode.
"""

from typing import Literal

from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextBuilder
from avatar_harness.deps import RunDeps
from avatar_harness.events import Emitter
from avatar_harness.model_client import (
    AskUser,
    DecisionParseError,
    FinalAnswer,
    ModelClient,
    ToolCall,
)
from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolRegistry, ToolResult, ToolRuntime
from avatar_harness.verifier import Verifier
from avatar_harness.workspace import Workspace


class AgentRunner:
    def __init__(
        self,
        *,
        model_client: ModelClient,
        registry: ToolRegistry,
        deps: RunDeps,
        context_builder: ContextBuilder,
        verifier: Verifier,
        emitter: Emitter,
        config: HarnessConfig,
    ) -> None:
        self.model_client = model_client
        self.registry = registry
        self.deps = deps
        self.context_builder = context_builder
        self.verifier = verifier
        self.emitter = emitter
        self.config = config

    def run(self, state: TaskState) -> TaskState:
        ws = self.deps.workspace
        runtime = ToolRuntime(self.registry, self.deps)
        self.emitter.emit("agent_start", goal=state.goal, task_id=state.task_id)

        while not state.terminal and self._within_budget(state):
            state.iterations += 1
            self.emitter.emit("turn_start", task_id=state.task_id, iteration=state.iterations)
            context = self.context_builder.build(state, ws, self.registry)
            try:
                action = self.model_client.decide(context).action
            except DecisionParseError as exc:
                # A malformed decision is model-correctable: feed it back, don't crash (§6).
                state.latest_error = str(exc)
                state.add_feedback(f"invalid decision: {exc}", kind="decision_error")
                state.consecutive_failures += 1
                self.emitter.emit("turn_end", task_id=state.task_id)
                continue

            if isinstance(action, ToolCall):
                result = runtime.execute(action.name, action.input)
                self._apply_tool_result(state, result)
                self.emitter.emit("tool_execution_end", tool=action.name, success=result.success)
            elif isinstance(action, FinalAnswer):
                state.final_answer = action.answer
                self._verify(state, ws)
            elif isinstance(action, AskUser):
                # Interactive answering is Phase 3; for now any ask blocks the run.
                state.open_questions.append(action.question)
                state.block(reason=f"needs input: {action.question}")

            self.emitter.emit("turn_end", task_id=state.task_id)

        if not state.terminal:
            state.outcome = self._exit_reason(state)
        self.emitter.emit("agent_end", outcome=state.outcome, task_id=state.task_id)
        return state

    # --- state mutation (runner-owned) -----------------------------------

    def _apply_tool_result(self, state: TaskState, result: ToolResult) -> None:
        if result.success:
            state.files_read |= set(result.files_read)
            state.files_modified |= set(result.files_changed)
            state.add_feedback(result.summary or f"{result.tool_name} ok", kind="tool_result")
            state.consecutive_failures = 0
        else:
            state.latest_error = result.error
            state.add_feedback(result.error or f"{result.tool_name} failed", kind="tool_error")
            state.consecutive_failures += 1

    def _verify(self, state: TaskState, ws: Workspace) -> None:
        self.emitter.emit("verification_start")
        report = self.verifier.verify(state, ws)
        self.emitter.emit("verification_end", passed=report.passed)
        state.verifier_results.append(report)
        if report.passed:
            state.outcome = "success"
        else:
            state.repair_failures += 1
            state.add_feedback(report.summary, kind="verification")
            if report.recommended_next_action:
                state.add_feedback(report.recommended_next_action, kind="repair_hint")

    # --- bounding (§5) ---------------------------------------------------

    def _within_budget(self, state: TaskState) -> bool:
        return (
            state.iterations < self.config.max_iterations
            and state.consecutive_failures < self.config.max_consecutive_failures
            and state.repair_failures < self.config.max_repair_attempts
        )

    def _exit_reason(self, state: TaskState) -> Literal["failed", "incomplete"]:
        if state.repair_failures >= self.config.max_repair_attempts:
            return "failed"
        return "incomplete"
