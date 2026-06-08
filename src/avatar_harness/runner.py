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
from avatar_harness.permission import PermissionPolicy
from avatar_harness.state import CommandRecord, DecisionRecord, TaskState
from avatar_harness.tools.base import ToolRegistry, ToolResult, ToolRuntime
from avatar_harness.verifier import Verifier
from avatar_harness.workspace import Workspace


def _action_brief(action: ToolCall | FinalAnswer | AskUser) -> str:
    """A one-line description of an action, for the trajectory log.

    Args:
        action: The model's chosen action.

    Returns:
        A one-line brief of `action`.
    """
    if isinstance(action, ToolCall):
        return f"{action.name}({action.input})"
    if isinstance(action, FinalAnswer):
        return action.answer
    return action.question


class AgentRunner:
    """The bounded loop that owns all state mutation and ends on verification (§5, §8).

    Collaborators are injected explicitly so a run is self-contained and replayable;
    the runner orchestrates them but mutates `TaskState` itself.

    Args:
        model_client: Proposes each turn's `ModelDecision`.
        registry: The active `ToolRegistry`.
        deps: Run-scoped `RunDeps` (workspace, etc.).
        context_builder: Assembles the per-iteration context packet (§9).
        verifier: Disposes of "done" on external evidence (§12).
        emitter: Observation-only event emitter (§13).
        config: Budgets and harness settings.
        policy: The before-tool-call control gate (§11); defaults to the standard tier policy.
    """

    def __init__(  # noqa: PLR0913 — keyword-only dependency injection of the run's collaborators
        self,
        *,
        model_client: ModelClient,
        registry: ToolRegistry,
        deps: RunDeps,
        context_builder: ContextBuilder,
        verifier: Verifier,
        emitter: Emitter,
        config: HarnessConfig,
        policy: PermissionPolicy | None = None,
    ) -> None:
        self.model_client = model_client
        self.registry = registry
        self.deps = deps
        self.context_builder = context_builder
        self.verifier = verifier
        self.emitter = emitter
        self.config = config
        # The before-tool-call control gate (§11); defaults to the standard tier policy,
        # threaded with the configured sensitive-path denylist (§11, Phase 2.5).
        self.policy = policy or PermissionPolicy(config.sensitive_path_globs)

    def run(self, state: TaskState) -> TaskState:
        """Drive the loop to a terminal outcome and return the final state (§5).

        Args:
            state: The task state to drive; mutated in place.

        Returns:
            The final `TaskState` with a terminal `outcome`.
        """
        ws = self.deps.workspace
        runtime = ToolRuntime(self.registry, self.deps)
        self.emitter.emit("agent_start", goal=state.goal, task_id=state.task_id)

        while not state.terminal and self._within_budget(state):
            state.iterations += 1
            self.emitter.emit("turn_start", task_id=state.task_id, iteration=state.iterations)
            context = self.context_builder.build(state, ws, self.registry)
            try:
                decision = self.model_client.decide(context)
            except DecisionParseError as exc:
                # A malformed decision is model-correctable: feed it back, don't crash (§6).
                state.latest_error = str(exc)
                state.add_feedback(f"invalid decision: {exc}", kind="decision_error")
                state.consecutive_failures += 1
                self.emitter.emit("decision_error", error=str(exc))
                self.emitter.emit("turn_end", task_id=state.task_id)
                continue

            action = decision.action
            brief = _action_brief(action)
            self.emitter.emit(
                "model_decision",
                thought=decision.thought_summary,
                action_type=action.type,
                action=brief,
            )
            # Record every turn's decision so the context can show the agent its own
            # action history (§7/§9, Phase 2.5); `outcome` is filled in once known.
            record = DecisionRecord(step=state.iterations, rationale=decision.thought_summary, chosen=brief)
            state.decisions.append(record)

            if isinstance(action, ToolCall):
                self._run_tool_call(state, runtime, ws, action, record)
            elif isinstance(action, FinalAnswer):
                state.final_answer = action.answer
                self._verify(state, ws)
                record.outcome = "verified" if state.outcome == "success" else "verification rejected"
            elif isinstance(action, AskUser):
                # Interactive answering is Phase 3; for now any ask blocks the run.
                state.open_questions.append(action.question)
                state.block(reason=f"needs input: {action.question}")
                record.outcome = "blocked (needs input)"

            self.emitter.emit("turn_end", task_id=state.task_id)

        self._record_commands(state, ws)
        if not state.terminal:
            state.outcome = self._exit_reason(state)
        self.emitter.emit("agent_end", outcome=state.outcome, task_id=state.task_id)
        return state

    def _record_commands(self, state: TaskState, ws: Workspace) -> None:
        """Mirror the workspace command log into `state.commands_run` (§7).

        Every command — the model's `run_tests`/`run_linter` and the verifier's own
        runs — flows through `ws.run`, so this single sync captures them all.

        Args:
            state: The task state whose `commands_run` ledger is rebuilt.
            ws: The workspace whose command log is the source of truth.
        """
        state.commands_run = [
            CommandRecord(
                step=i,
                command=out.command,
                exit_code=out.exit_code,
                summary="timed out" if out.timed_out else f"exit={out.exit_code}",
            )
            for i, out in enumerate(ws.command_log, start=1)
        ]

    def _run_tool_call(
        self,
        state: TaskState,
        runtime: ToolRuntime,
        ws: Workspace,
        action: ToolCall,
        record: DecisionRecord,
    ) -> None:
        """Gate, execute, and record one tool call; mutate `state`/`record` in place (§5, §11).

        Args:
            state: The task state to mutate (evidence, files, failure counters).
            runtime: The tool runtime that validates and dispatches the call.
            ws: The run-scoped workspace, for the permission gate's path checks.
            action: The model's tool-call action.
            record: This turn's decision record, whose `outcome` is filled in.
        """
        # Anti-loop nudge: an identical earlier call is flagged back as evidence so the
        # model stops re-issuing it (the dogfood replayed turns 1-5 at turn 9; §9).
        if any(d.chosen == record.chosen for d in state.decisions[:-1]):
            state.add_feedback(
                f"'{record.chosen}' repeats an earlier call — try a different approach or finalize.",
                kind="repeat",
            )
        tool = self.registry.get(action.name)
        # Consult the control gate before execution (§11). A block redirects the loop —
        # the action never runs — and the reason is fed back as evidence.
        permission = self.policy.check(tool, action.input, state, ws) if tool is not None else None
        if permission is not None and permission.blocked:
            record.outcome = f"blocked: {permission.reason}"
            state.latest_error = permission.reason
            state.add_feedback(permission.reason, kind="permission_blocked")
            self.emitter.emit("permission_blocked", tool=action.name, reason=permission.reason)
            return
        result = runtime.execute(action.name, action.input)
        self._apply_tool_result(state, result)
        record.outcome = result.summary if result.success else (result.error or "failed")
        self.emitter.emit(
            "tool_execution_end",
            tool=action.name,
            input=action.input,
            success=result.success,
            summary=result.summary,
            content=result.content if result.success else (result.error or ""),
        )

    # --- state mutation (runner-owned) -----------------------------------

    def _apply_tool_result(self, state: TaskState, result: ToolResult) -> None:
        if result.success:
            state.files_read |= set(result.files_read)
            state.files_modified |= set(result.files_changed)
            # Record the tool's CONTENT (not just the summary) so the next context
            # surfaces it — otherwise the model is blind to what it found.
            state.add_feedback(
                result.summary or f"{result.tool_name} ok",
                kind="tool_result",
                detail=result.content or None,
            )
            state.consecutive_failures = 0
        else:
            state.latest_error = result.error
            state.add_feedback(result.error or f"{result.tool_name} failed", kind="tool_error")
            state.consecutive_failures += 1

    def _verify(self, state: TaskState, ws: Workspace) -> None:
        self.emitter.emit("verification_start")
        report = self.verifier.verify(state, ws)
        self.emitter.emit(
            "verification_end",
            passed=report.passed,
            summary=report.summary,
            next_action=report.recommended_next_action,
        )
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
