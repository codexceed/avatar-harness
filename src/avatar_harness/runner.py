"""AgentRunner — the bounded loop that terminates on verification (§5).

The runner owns *all* `TaskState` mutation (§8); tools and the verifier are
pure-ish workers. Phase 1 covers the read-only path: tier-0 tools, no permission
gate (every tool is tier 0), and the minimal `investigate` verifier. The loop is
deliberately a near-verbatim transcription of the §5 pseudocode.
"""

import asyncio
import json
import time
from typing import Literal
from uuid import uuid4

from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextBuilder, ContextPacket
from avatar_harness.deps import RunDeps
from avatar_harness.event_types import (
    AgentEnd,
    AgentStart,
    ApprovalController,
    CancellationObserved,
    DecisionError,
    EventSink,
    HarnessEvent,
    ModelDecisionEvent,
    ModelUsage,
    PhaseChanged,
    ToolEnd,
    ToolStart,
    TurnEnd,
    TurnStart,
    VerificationEnd,
    VerificationPlanFrozen,
    VerificationStart,
)
from avatar_harness.events import Emitter
from avatar_harness.model_client import (
    AskUser,
    DecisionParseError,
    DecisionUsage,
    FinalAnswer,
    ModelClient,
    ToolCall,
)
from avatar_harness.permission import PermissionPolicy
from avatar_harness.planner import VerificationPlanner
from avatar_harness.state import CommandRecord, DecisionRecord, TaskState
from avatar_harness.tools.base import (
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    ToolRuntime,
    is_edit_intent,
    phase_admits_tool,
)
from avatar_harness.verifier import Verifier
from avatar_harness.workspace import Workspace

# Rough chars-per-token estimate for the context-budget bound; the harness has no
# tokenizer dependency, and an over-estimate fails safe (stops earlier, not later).
_CHARS_PER_TOKEN = 4


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


def _action_key(action: ToolCall | FinalAnswer | AskUser) -> str:
    """A canonical, order-independent identity for an action, for repeat detection.

    Unlike the human-readable `_action_brief`, a tool call's input is serialized with
    sorted keys, so the model's JSON key order can't defeat the anti-loop nudge — the
    same call with reordered inputs is the same call.

    Args:
        action: The model's chosen action.

    Returns:
        A canonical key for `action`.
    """
    if isinstance(action, ToolCall):
        return f"{action.name}({json.dumps(action.input, sort_keys=True)})"
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
        planner: Resolves the per-session verification plan (ADR-0007); defaults to a
            `VerificationPlanner(config)`.
        event_sink: Optional typed-event sink (a `Session`); absent on the batch/sync path.
        approval_controller: Optional awaited gate for tier-3 `ask` calls (a `Session`).
        conversational: Verification authority (§23.5). `False` (default) is the strict §12
            gate; `True` runs the verifier as advisory and delivers without repairing.
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
        planner: VerificationPlanner | None = None,
        event_sink: EventSink | None = None,
        approval_controller: ApprovalController | None = None,
        conversational: bool = False,
    ) -> None:
        self.model_client = model_client
        self.registry = registry
        self.deps = deps
        self.context_builder = context_builder
        self.verifier = verifier
        self.emitter = emitter
        self.config = config
        # Verification authority (§23.5): strict (`False`, default) runs the §12 gate that
        # sets `outcome` and drives the repair loop; conversational (`True`) runs + reports
        # the verifier as advisory and delivers the reply without repairing (human is authority).
        self.conversational = conversational
        # The before-tool-call control gate (§11); defaults to the standard tier policy,
        # threaded with the configured sensitive-path denylist (§11, Phase 2.5).
        self.policy = policy or PermissionPolicy(config.sensitive_path_globs)
        # Resolves the per-session verification plan (ADR-0007): config override →
        # deterministic detection → cited LLM proposal. The runner freezes the result
        # onto TaskState at the investigating → editing boundary.
        self.planner = planner or VerificationPlanner(config)
        # Phase 3.0 two-plane wiring (both optional; absent on the batch/sync path):
        # `event_sink` receives typed `HarnessEvent`s; `approval_controller` is the
        # awaited gate for tier-3 `ask` calls. A `Session` supplies both.
        self.event_sink = event_sink
        self.approval_controller = approval_controller

    def _record_usage(self, state: TaskState, usage: DecisionUsage | None) -> None:
        """Accumulate provider-reported usage into state and record it per turn.

        The one mutator does the accumulation. Emitted on BOTH channels: the typed
        bus (Session/journal) and the legacy emitter (the batch CLI's JSONL log) — a
        run's cost must be observable on whichever path it ran (PR-#31 review).

        Args:
            state: The live task state to accumulate into.
            usage: The turn's summed usage, or `None` when the endpoint reported none.
        """
        if usage is None:
            return
        state.prompt_tokens += usage.prompt_tokens
        state.completion_tokens += usage.completion_tokens
        self.emitter.emit(
            "model_usage", prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens
        )
        self._publish(
            ModelUsage(
                task_id=state.task_id,
                turn=state.iterations,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
            )
        )

    def _publish(self, draft: HarnessEvent) -> None:
        """Publish a typed event to the sink, if one is wired (fire-and-forget, §13).

        No-op on the batch/sync path (no sink). Sync `put_nowait` onto an unbounded
        queue, so observation never blocks the loop; lane 1 adds bounded backpressure.

        Args:
            draft: The typed event to publish; the sink stamps `event_id`/`session_id`/`ts`.
        """
        if self.event_sink is not None:
            self.event_sink.publish_nowait(draft)

    def run(self, state: TaskState) -> TaskState:
        """Drive the loop to a terminal outcome synchronously (§5).

        The thin sync wrapper over the async core: `arun()` *is* the loop, `run()`
        wraps it via `asyncio.run()` for batch/library callers. Behavior is identical
        to the async path with no sink/controller wired.

        Args:
            state: The task state to drive; mutated in place.

        Returns:
            The final `TaskState` with a terminal `outcome`.
        """
        return asyncio.run(self.arun(state))

    async def arun(self, state: TaskState) -> TaskState:  # noqa: PLR0915 — deliberate near-verbatim §5 transcription
        """Drive the loop to a terminal outcome asynchronously — the real loop (§5).

        A near-verbatim async transcription of the §5 pseudocode: blocking model/tool/
        verifier bodies are offloaded with `asyncio.to_thread` so the event loop stays
        responsive (spinners, keystrokes, ESC), and typed lifecycle events are published
        to the sink as the run progresses. With no sink/controller wired it matches `run()`.

        Args:
            state: The task state to drive; mutated in place.

        Returns:
            The final `TaskState` with a terminal `outcome`.
        """
        ws = self.deps.workspace
        runtime = ToolRuntime(self.registry, self.deps)
        deadline = time.monotonic() + self.config.max_wall_clock_seconds
        self.emitter.emit("agent_start", goal=state.goal, task_id=state.task_id)
        self._publish(AgentStart(task_id=state.task_id, goal=state.goal))

        while not state.terminal and self._within_budget(state, deadline):
            if self.deps.cancellation.cancelled:
                self._stop_incomplete(state, "run cancelled", kind="cancelled")
                self._publish(CancellationObserved(task_id=state.task_id, reason="run cancelled"))
                break
            context = self.context_builder.build(state, ws, self.registry)
            if self._context_over_budget(context):
                self._stop_incomplete(state, "context budget exceeded", kind="budget")
                break
            state.iterations += 1
            self.emitter.emit("turn_start", task_id=state.task_id, iteration=state.iterations)
            self._publish(TurnStart(task_id=state.task_id, turn=state.iterations, iteration=state.iterations))
            try:
                decision = await asyncio.to_thread(self.model_client.decide, context)
            except DecisionParseError as exc:
                # A malformed decision is model-correctable: feed it back, don't crash (§6).
                # The lost turn still cost tokens — bill them (the client sums attempts).
                self._record_usage(state, getattr(exc, "usage", None))
                state.latest_error = str(exc)
                state.add_feedback(f"invalid decision: {exc}", kind="decision_error")
                state.consecutive_failures += 1
                self.emitter.emit("decision_error", error=str(exc))
                self._publish(
                    DecisionError(
                        task_id=state.task_id, turn=state.iterations, error=str(exc), recovered=False
                    )
                )
                self.emitter.emit("turn_end", task_id=state.task_id)
                self._publish(TurnEnd(task_id=state.task_id, turn=state.iterations))
                continue

            # Malformed attempts the client recovered from in-flight: record each as
            # evidence (the model sees its own failed attempts next turn) and journal it
            # (a struggling run is legible, invariant #5) — then clear the channel.
            for note in decision.retry_trace:
                state.add_feedback(
                    f"malformed decision attempt (recovered): {note.error}",
                    detail=note.raw,
                    kind="decision_error",
                )
                self.emitter.emit("decision_error", error=note.error, recovered=True)
                self._publish(
                    DecisionError(
                        task_id=state.task_id,
                        turn=state.iterations,
                        error=note.error,
                        raw=note.raw,
                        recovered=True,
                    )
                )

            self._record_usage(state, decision.usage)

            action = decision.action
            brief = _action_brief(action)
            self.emitter.emit(
                "model_decision",
                thought=decision.thought_summary,
                action_type=action.type,
                action=brief,
                transport=decision.transport,
            )
            self._publish(
                ModelDecisionEvent(
                    task_id=state.task_id,
                    turn=state.iterations,
                    thought=decision.thought_summary,
                    action_type=action.type,
                    action=brief,
                    transport=decision.transport,
                )
            )
            # Record every turn's decision so the context can show the agent its own
            # action history (§7/§9, Phase 2.5); `outcome` is filled in once known.
            record = DecisionRecord(
                step=state.iterations,
                rationale=decision.thought_summary,
                chosen=brief,
                key=_action_key(action),
            )
            state.decisions.append(record)

            if isinstance(action, ToolCall):
                await self._arun_tool_call(state, runtime, ws, action, record)
            elif isinstance(action, FinalAnswer):
                state.final_answer = action.answer
                await self._averify(state, ws)
                record.outcome = "verified" if state.outcome == "success" else "verification rejected"
            elif isinstance(action, AskUser):
                # Interactive answering rides the control plane (resolve via the session);
                # with no controller wired, any ask blocks the run as before.
                state.open_questions.append(action.question)
                state.block(reason=f"needs input: {action.question}")
                record.outcome = "blocked (needs input)"

            self.emitter.emit("turn_end", task_id=state.task_id)
            self._publish(TurnEnd(task_id=state.task_id, turn=state.iterations))

        self._record_commands(state, ws)
        if not state.terminal:
            state.outcome = self._exit_reason(state)
        self.emitter.emit("agent_end", outcome=state.outcome, task_id=state.task_id)
        self._publish(AgentEnd(task_id=state.task_id, outcome=state.outcome))
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

    def _set_phase(self, state: TaskState, new: Literal["investigating", "editing", "verifying"]) -> None:
        """Advance the control phase and announce the transition (§7, §13).

        Phase is capability-exposure, not security; this only mutates `state.phase`
        and emits an observation event — the security boundary is the permission
        gate + workspace chokepoint + `task_kind` gate.

        Args:
            state: The task state whose phase is advanced.
            new: The phase to move into.
        """
        if state.phase == new:
            return
        old = state.phase
        state.phase = new
        self.emitter.emit("phase_changed", old=old, new=new, task_id=state.task_id)
        self._publish(PhaseChanged(task_id=state.task_id, old=old, new=new))

    def _freeze_plan(self, state: TaskState, ws: Workspace) -> None:
        """Resolve and freeze the verification plan at the phase boundary (ADR-0007).

        Idempotent and runner-owned: resolution happens once, during `investigating`,
        and the result freezes onto `TaskState` before editing begins — the freeze is
        the authority transfer away from the model (it may pick among the frozen
        checks, never author one). The frozen plan is journaled as a typed event so
        every run's rubric is auditable. `investigate` tasks are command-free (§12)
        and never freeze a plan.

        Args:
            state: The task state the plan freezes onto.
            ws: The run-scoped workspace whose artifacts the planner reads.
        """
        if state.verification_plan is not None or state.task_kind == "investigate":
            return
        plan = self.planner.resolve(ws)
        state.freeze_verification_plan(plan)
        self.deps.verification_plan = plan  # mirrored so run_tests/run_linter ride the contract
        rubric = "; ".join(f"{c.name}: `{c.command}` [{c.provenance}]" for c in plan) or "none discovered"
        state.add_feedback(f"verification plan frozen: {rubric}", kind="verification_plan")
        self.emitter.emit("verification_plan_frozen", checks=[c.model_dump() for c in plan])
        self._publish(VerificationPlanFrozen(task_id=state.task_id, turn=state.iterations, checks=plan))

    def _stop_incomplete(self, state: TaskState, reason: str, *, kind: str) -> None:
        """Record a stop reason and end the run as `incomplete` (budgets/cancellation, §5).

        Args:
            state: The task state to terminate.
            reason: The human-readable stop reason, surfaced as feedback.
            kind: The evidence kind (`cancelled` or `budget`).
        """
        state.add_feedback(reason, kind=kind)
        state.latest_error = reason
        state.outcome = "incomplete"

    async def _arun_tool_call(
        self,
        state: TaskState,
        runtime: ToolRuntime,
        ws: Workspace,
        action: ToolCall,
        record: DecisionRecord,
    ) -> None:
        """Gate, execute, and record one tool call; mutate `state`/`record` in place (§5, §11).

        The async loop's sole tool-call path: same phase/gate/anti-loop logic for every
        run, plus (1) a tier-3 `ask` is routed to the awaited `approval_controller` when
        one is wired — suspending *this run only* until a control method resolves it — and
        (2) the tool body runs in `asyncio.to_thread` so the loop stays responsive. With no
        controller (the batch path), `ask` stays blocked exactly as the gate decided.

        Args:
            state: The task state to mutate (evidence, files, failure counters).
            runtime: The tool runtime that validates and dispatches the call.
            ws: The run-scoped workspace, for the permission gate's path checks.
            action: The model's tool-call action.
            record: This turn's decision record, whose `outcome` is filled in.
        """
        if any(d.key == record.key for d in state.decisions[:-1]):
            state.add_feedback(
                f"'{record.chosen}' repeats an earlier call — try a different approach or finalize.",
                kind="repeat",
            )
        tool = self.registry.get(action.name)
        if tool is not None and not self._phase_admits(state, tool):
            record.outcome = "out of phase"
            msg = f"'{action.name}' is not available in the {state.phase} phase."
            state.latest_error = msg
            state.add_feedback(msg, kind="out_of_phase")
            self.emitter.emit("out_of_phase", tool=action.name, phase=state.phase)
            return
        if tool is not None and self._is_edit_intent(state, tool) and state.phase == "investigating":
            # The investigating → editing boundary is the plan's freeze point (ADR-0007).
            self._freeze_plan(state, ws)
            self._set_phase(state, "editing")
        permission = self.policy.check(tool, action.input, state, ws) if tool is not None else None
        if permission is not None and permission.blocked and not await self._approved(action, permission):
            record.outcome = f"blocked: {permission.reason}"
            state.latest_error = permission.reason
            state.add_feedback(permission.reason, kind="permission_blocked")
            self.emitter.emit("permission_blocked", tool=action.name, reason=permission.reason)
            return
        call_id = uuid4().hex
        self._publish(ToolStart(task_id=state.task_id, call_id=call_id, tool=action.name, input=action.input))
        result = await asyncio.to_thread(runtime.execute, action.name, action.input)
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
        self._publish(
            ToolEnd(
                task_id=state.task_id,
                call_id=call_id,
                tool=action.name,
                success=result.success,
                summary=result.summary,
                content=result.content if result.success else (result.error or ""),
            )
        )

    async def _approved(self, action: ToolCall, permission: object) -> bool:
        """Whether a *gated* (`ask`) call is approved by the awaited controller (§11, §13).

        Only an `ask` permission with a wired `approval_controller` reaches the human;
        any other block (or no controller — the batch path) stays denied. Returns False
        for non-`ask` blocks so the caller records the original block reason.

        Args:
            action: The tool-call awaiting a verdict.
            permission: The gate's `ToolPermission` (its `ask`/`reason` drive the prompt).

        Returns:
            True iff the controller explicitly allows the call.
        """
        ask = getattr(permission, "ask", False)
        if not ask or self.approval_controller is None:
            return False
        approval_id = uuid4().hex
        return await self.approval_controller.request_approval(
            approval_id, action.name, getattr(permission, "reason", ""), action.input
        )

    async def _averify(self, state: TaskState, ws: Workspace) -> None:
        """Run the verifier off-thread, set the outcome, and publish typed events (§5, §12).

        Args:
            state: The task state to verify and mutate (outcome / repair counters).
            ws: The run-scoped workspace the verifier inspects.
        """
        # A run can claim done without ever editing — the plan still freezes before
        # the verifier runs, so the rubric is fixed and journaled in every case.
        self._freeze_plan(state, ws)
        await self._maybe_add_smoke_floor(state, ws)
        self._set_phase(state, "verifying")
        self.emitter.emit("verification_start")
        self._publish(VerificationStart(task_id=state.task_id))
        report = await asyncio.to_thread(self.verifier.verify, state, ws)
        self.emitter.emit(
            "verification_end",
            passed=report.passed,
            summary=report.summary,
            next_action=report.recommended_next_action,
        )
        self._publish(VerificationEnd(task_id=state.task_id, passed=report.passed, summary=report.summary))
        state.verifier_results.append(report)
        if self.conversational:
            # §23.5: the verifier ran + reported (above); in conversational mode it is advisory.
            # Deliver the reply and end the turn — the human is terminal authority. The verdict
            # stays on `verifier_results[-1]` for the cockpit to render; never repair.
            state.outcome = "success"
        elif report.passed:
            state.outcome = "success"
        else:
            state.repair_failures += 1
            state.add_feedback(report.summary, kind="verification")
            if report.recommended_next_action:
                state.add_feedback(report.recommended_next_action, kind="repair_hint")
            self._set_phase(state, "editing")

    async def _maybe_add_smoke_floor(self, state: TaskState, ws: Workspace) -> None:
        """Bind the greenfield smoke floor when tiers 1-3 resolved nothing (ADR-0014).

        The lowest-precedence tier: only when the frozen plan is the *empty* no-contract
        plan AND an `edit` run actually wrote code does the model get to AUTHOR one
        executable smoke check. The harness still runs it (the verifier reads the real
        exit code), so this is author-and-run, never self-certification (§5). Late-bound
        on purpose — the artifact under test did not exist at the freeze boundary. A real
        contract (non-empty plan) is never touched; resolution failure leaves the empty
        plan as-is (the legible no-contract path).

        Args:
            state: The task state whose empty frozen plan may gain the floor.
            ws: The run-scoped workspace the proposal excerpts and the verifier runs in.
        """
        if state.task_kind != "edit" or state.verification_plan != [] or not state.files_modified:
            return
        check = await asyncio.to_thread(self.planner.propose_smoke_check, ws, sorted(state.files_modified))
        if check is None:
            return
        floor = [check]
        state.set_smoke_floor(floor)
        self.deps.verification_plan = floor  # mirror, as _freeze_plan does
        state.add_feedback(
            f"greenfield smoke floor: `{check.command}` [{check.provenance}]", kind="verification_plan"
        )
        # Both channels, exactly as _freeze_plan: the legacy emitter AND the typed sink, so a
        # journaled/cockpit run records the floor as the rubric — not the earlier empty plan.
        self.emitter.emit("verification_plan_frozen", checks=[c.model_dump() for c in floor])
        self._publish(VerificationPlanFrozen(task_id=state.task_id, turn=state.iterations, checks=floor))

    def _is_edit_intent(self, state: TaskState, tool: ToolDefinition) -> bool:
        """Whether a tool call is the model's edit intent (the mutating tier on an edit task).

        Delegates to the shared `is_edit_intent` predicate so the runner's bootstrap and
        the `ContextBuilder`'s advertised tool set never drift apart.

        Args:
            state: The task state (for `task_kind`).
            tool: The resolved tool definition.

        Returns:
            True when `tool` is the mutating tool (tier 1) and the task kind permits edits.
        """
        return is_edit_intent(state.task_kind, tool)

    def _phase_admits(self, state: TaskState, tool: ToolDefinition) -> bool:
        """Whether `tool` may run in the current phase (with the edit-intent bootstrap).

        Delegates to the shared `phase_admits_tool` predicate — the same one the
        `ContextBuilder` advertises through — so what the model is *told* it may call
        matches what the gate will *let* it call.

        Args:
            state: The task state (current `phase` and `task_kind`).
            tool: The resolved tool definition.

        Returns:
            True if the current phase is in the tool's phases, or it is an edit-intent
            tool reachable from `investigating` on an edit-shaped task.
        """
        return phase_admits_tool(state.phase, state.task_kind, tool)

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

    # --- bounding (§5) ---------------------------------------------------

    def _within_budget(self, state: TaskState, deadline: float) -> bool:
        return (
            state.iterations < self.config.max_iterations
            and state.consecutive_failures < self.config.max_consecutive_failures
            and state.repair_failures < self.config.max_repair_attempts
            and time.monotonic() < deadline
        )

    def _context_over_budget(self, context: ContextPacket) -> bool:
        """Whether the assembled context exceeds the configured token budget (§5, §9).

        The harness carries no tokenizer; it estimates from the serialized packet at
        ~`_CHARS_PER_TOKEN` chars/token. An over-estimate fails safe (stops earlier).

        Args:
            context: The per-turn context packet (serialized to size it).

        Returns:
            True when the estimated token count exceeds `config.max_context_tokens`.
        """
        estimated_tokens = len(context.model_dump_json()) // _CHARS_PER_TOKEN
        return estimated_tokens > self.config.max_context_tokens

    def _exit_reason(self, state: TaskState) -> Literal["failed", "incomplete"]:
        if state.repair_failures >= self.config.max_repair_attempts:
            return "failed"
        return "incomplete"
