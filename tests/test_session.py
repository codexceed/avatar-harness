"""Phase 3.0 foundation — Group 3: the two-plane Session API (§13).

Observation flows OUT via `session.events()` (cannot block or redirect the run);
control flows IN via `resolve_approval()` / `cancel()`. An event may *announce* an
approval need, but only the control method decides it. This is the boundary the
cockpit builds against.
"""

import asyncio
import time

from conftest import ScriptedModel
from pydantic import BaseModel

from avatar.config import HarnessConfig
from avatar.context import ContextBuilder
from avatar.deps import CancellationToken, RunDeps
from avatar.event_types import ApprovalRequested, ApprovalResolved, EventBase
from avatar.events import Emitter
from avatar.model_client import FinalAnswer, ModelDecision, ToolCall
from avatar.runner import AgentRunner
from avatar.session import Session
from avatar.state import TaskState
from avatar.tools.base import ToolDefinition, ToolRegistry, ToolResult
from avatar.tools.filesystem import read_file
from avatar.verifier import Verifier
from avatar.workspace import Workspace


class _Empty(BaseModel):
    pass


def _runner(tmp_path, registry, decisions, *, token=None, **config_kw) -> AgentRunner:
    config = HarnessConfig(**config_kw)
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=token or CancellationToken())
    return AgentRunner(
        model_client=ScriptedModel(decisions),
        registry=registry,
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=Emitter(),
        config=config,
    )


def _read_registry(tmp_path) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(read_file)
    return reg


def _gated_registry(sentinel_dir):
    """A registry whose tier-3 `risky` tool drops a sentinel iff it ever runs."""

    def _risky(args, deps) -> ToolResult:
        (sentinel_dir / "RAN").write_text("ran", encoding="utf-8")
        return ToolResult(tool_name="risky", success=True, summary="did the thing")

    risky = ToolDefinition(
        name="risky",
        description="needs approval",
        input_model=_Empty,
        handler=_risky,
        phases=frozenset({"investigating"}),
        permission_tier=3,
    )
    reg = ToolRegistry()
    reg.register(risky)
    reg.register(read_file)
    return reg


async def test_session_events_yields_typed_stream(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="x is set in app.py")),
    ]
    session = Session(
        _runner(tmp_path, _read_registry(tmp_path), decisions),
        TaskState(goal="where?", task_kind="investigate"),
    )
    collected = []
    run_task = asyncio.create_task(session.run())
    async for ev in session.events():
        collected.append(ev)
    state = await run_task
    assert state.outcome == "success"
    assert collected and all(isinstance(e, EventBase) for e in collected)
    assert any(e.type == "agent_end" for e in collected)  # the stream terminates on agent_end


async def test_two_event_consumers_each_see_full_stream(tmp_path):
    # SDK contract: independent observers (TUI, journal, telemetry, benchmark collector)
    # each see the SAME run, not a competing split of one shared queue.
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="x is set in app.py")),
    ]
    session = Session(
        _runner(tmp_path, _read_registry(tmp_path), decisions),
        TaskState(goal="where?", task_kind="investigate"),
    )
    a, b = [], []
    gen_a, gen_b = session.events(), session.events()  # eager-subscribe both before the run

    async def drain(gen, sink):
        async for ev in gen:
            sink.append(ev)

    run_task = asyncio.create_task(session.run())
    await asyncio.gather(drain(gen_a, a), drain(gen_b, b))
    await run_task
    assert [e.event_id for e in a] == [e.event_id for e in b]  # identical, independent streams
    assert len(a) > 1
    assert a[-1].type == "agent_end" and b[-1].type == "agent_end"


async def test_session_events_subscriber_cannot_alter_control(tmp_path):
    # A raising observer cannot block or change the run outcome (§13 invariant).
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="x is set in app.py")),  # cites the read file → grounded
    ]
    session = Session(
        _runner(tmp_path, _read_registry(tmp_path), decisions),
        TaskState(goal="where?", task_kind="investigate"),
    )
    run_task = asyncio.create_task(session.run())
    try:
        async for _ev in session.events():
            raise RuntimeError("observer blew up")  # simulate a broken subscriber
    except RuntimeError:
        pass
    state = await run_task
    assert state.outcome == "success"  # the run was untouched by the observer's failure


async def test_resolve_approval_unblocks_gated_call(tmp_path):
    # A tier-3 call suspends the run; resolve_approval(allow) lets it execute.
    decisions = [
        ModelDecision(action=ToolCall(name="risky", input={})),
        ModelDecision(action=FinalAnswer(answer="did the risky thing")),
    ]
    session = Session(
        _runner(tmp_path, _gated_registry(tmp_path), decisions),
        TaskState(goal="do it", task_kind="investigate"),
    )
    run_task = asyncio.create_task(session.run())
    async for ev in session.events():
        if isinstance(ev, ApprovalRequested):
            await session.resolve_approval(ev.approval_id, allow=True)
    await run_task
    assert (tmp_path / "RAN").exists()  # the gated tool ran after approval


async def test_cancel_records_feedback_and_stops(tmp_path):
    # session.cancel() trips the token → add_feedback → incomplete.
    def _slow(args, deps) -> ToolResult:
        time.sleep(0.2)
        return ToolResult(tool_name="slow", success=True, summary="slept")

    slow = ToolDefinition(
        name="slow",
        description="slow",
        input_model=_Empty,
        handler=_slow,
        phases=frozenset({"investigating"}),
    )
    reg = ToolRegistry()
    reg.register(slow)
    decisions = [
        ModelDecision(action=ToolCall(name="slow", input={})),
        ModelDecision(action=FinalAnswer(answer="done")),
    ]
    session = Session(_runner(tmp_path, reg, decisions), TaskState(goal="x", task_kind="investigate"))
    run_task = asyncio.create_task(session.run())
    await asyncio.sleep(0.05)
    await session.cancel("user pressed esc")
    state = await run_task
    assert state.outcome == "incomplete"
    assert any(e.kind == "cancelled" for e in state.evidence)


async def test_unattended_session_denies_ask_without_blocking(tmp_path):
    # Batch/unattended (eval, autonomous wrappers): a tier-3 `ask` is auto-denied immediately —
    # no human is present, so the run must terminate instead of blocking forever (the 51-min
    # eval deadlock: a denylist refusal carried `ask=True`, and `request_approval` awaited a
    # human `resolve_approval` that never came).
    decisions = [
        ModelDecision(action=ToolCall(name="risky", input={})),
        ModelDecision(action=FinalAnswer(answer="done")),
    ]
    session = Session(
        _runner(tmp_path, _gated_registry(tmp_path), decisions),
        TaskState(goal="do it", task_kind="investigate"),
        unattended=True,
    )
    # No resolve_approval is ever called; the run must still reach a terminal outcome.
    state = await asyncio.wait_for(session.run(), timeout=5)
    assert state.outcome is not None  # terminal, not hung on the gate
    assert not (tmp_path / "RAN").exists()  # the gated tool was denied, never ran


async def test_unattended_auto_deny_is_observable(tmp_path):
    # The auto-deny is not silent: the gated attempt is still announced (ApprovalRequested) and
    # its disposition recorded (ApprovalResolved allowed=False via="auto"), so the journal and
    # the eval failure-classifier can see it (invariant #5).
    decisions = [
        ModelDecision(action=ToolCall(name="risky", input={})),
        ModelDecision(action=FinalAnswer(answer="done")),
    ]
    session = Session(
        _runner(tmp_path, _gated_registry(tmp_path), decisions),
        TaskState(goal="do it", task_kind="investigate"),
        unattended=True,
    )
    events = []
    runner_task = asyncio.create_task(session.run())
    async for ev in session.events():
        events.append(ev)
    await asyncio.wait_for(runner_task, timeout=5)
    assert any(isinstance(e, ApprovalRequested) for e in events)  # the gated attempt is announced
    resolved = [e for e in events if isinstance(e, ApprovalResolved)]
    assert resolved and resolved[-1].allowed is False and resolved[-1].via == "auto"


async def test_unattended_auto_approves_scoped_amendment(tmp_path):
    # ADR-0039: with amendment_policy="approve", an unattended run self-ratifies an
    # `alter_verification` amendment — but NOTHING else (scoped by tool NAME, not tier).
    session = Session(
        _runner(tmp_path, _gated_registry(tmp_path), []),
        TaskState(goal="x", task_kind="edit"),
        unattended=True,
        amendment_policy="approve",
    )
    assert await session.request_approval("a1", "alter_verification", "gated", {}) is True
    # run_command is ALSO tier 3 — it must still auto-deny; the approve policy is name-scoped.
    assert await session.request_approval("a2", "run_command", "gated", {}) is False


async def test_unattended_denies_amendment_by_default(tmp_path):
    # Default policy "deny" preserves ADR-0016 exactly: even `alter_verification` auto-denies.
    session = Session(
        _runner(tmp_path, _gated_registry(tmp_path), []),
        TaskState(goal="x", task_kind="edit"),
        unattended=True,
    )
    assert await session.request_approval("a1", "alter_verification", "gated", {}) is False


async def test_unattended_auto_approve_is_observable(tmp_path):
    # The scoped auto-approve is recorded ApprovalResolved(allowed=True, via="auto") — visible to
    # the journal and the eval classifier (invariant #5), never a silent goalpost move.
    session = Session(
        _runner(tmp_path, _gated_registry(tmp_path), []),
        TaskState(goal="x", task_kind="edit"),
        unattended=True,
        amendment_policy="approve",
    )
    queue = session.bus.subscribe()
    await session.request_approval("a1", "alter_verification", "gated", {})
    seen: list[EventBase] = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    resolved = [e for e in seen if isinstance(e, ApprovalResolved)]
    assert resolved and resolved[-1].allowed is True and resolved[-1].via == "auto"


async def test_auto_denied_call_is_model_correctable(tmp_path):
    # The auto-deny is fed back as recoverable feedback (not a system failure, not a terminate):
    # the loop advances past the denied call to the next decision.
    decisions = [
        ModelDecision(action=ToolCall(name="risky", input={})),
        ModelDecision(action=FinalAnswer(answer="done")),
    ]
    session = Session(
        _runner(tmp_path, _gated_registry(tmp_path), decisions),
        TaskState(goal="do it", task_kind="investigate"),
        unattended=True,
    )
    state = await asyncio.wait_for(session.run(), timeout=5)
    assert any(e.kind == "permission_blocked" for e in state.evidence)  # recorded as feedback
    assert state.iterations >= 2  # advanced past the denied call to the final-answer turn


async def test_attended_session_still_awaits_human(tmp_path):
    # The default (attended) path is unchanged by the new flag: an `ask` blocks until a human
    # resolves it, then the tool runs. Guards against the unattended branch leaking into the REPL.
    decisions = [
        ModelDecision(action=ToolCall(name="risky", input={})),
        ModelDecision(action=FinalAnswer(answer="done")),
    ]
    session = Session(
        _runner(tmp_path, _gated_registry(tmp_path), decisions),
        TaskState(goal="do it", task_kind="investigate"),
        unattended=False,  # explicit default
    )
    runner_task = asyncio.create_task(session.run())
    approval_id = None
    async for ev in session.events():
        if isinstance(ev, ApprovalRequested):
            approval_id = ev.approval_id
            break
    await asyncio.sleep(0.05)
    assert approval_id is not None and not runner_task.done()  # blocked awaiting the human
    await session.resolve_approval(approval_id, allow=True)
    await asyncio.wait_for(runner_task, timeout=5)
    assert (tmp_path / "RAN").exists()  # ran only after the human allowed it


async def test_pending_approval_times_out_to_deny(tmp_path):
    # Defense-in-depth backstop: an unresolved approval trips to deny after `approval_timeout`,
    # so even a present-but-silent controller can't hang a run forever (the wall-clock budget
    # can't save a run blocked *inside* the awaited gate).
    decisions = [
        ModelDecision(action=ToolCall(name="risky", input={})),
        ModelDecision(action=FinalAnswer(answer="done")),
    ]
    session = Session(
        _runner(tmp_path, _gated_registry(tmp_path), decisions),
        TaskState(goal="do it", task_kind="investigate"),
        approval_timeout=0.2,  # nobody resolves; the backstop denies
    )
    state = await asyncio.wait_for(session.run(), timeout=5)
    assert state.outcome is not None  # terminal, not hung
    assert not (tmp_path / "RAN").exists()  # the timed-out approval denied the call


async def test_approval_announced_by_event_not_decided_by_it(tmp_path):
    # The event ANNOUNCES the need; only the control method decides. Ignoring the
    # event leaves the run blocked and the tool un-run.
    decisions = [
        ModelDecision(action=ToolCall(name="risky", input={})),
        ModelDecision(action=FinalAnswer(answer="did it")),
    ]
    session = Session(
        _runner(tmp_path, _gated_registry(tmp_path), decisions),
        TaskState(goal="do it", task_kind="investigate"),
    )
    run_task = asyncio.create_task(session.run())
    approval_id = None
    async for ev in session.events():
        if isinstance(ev, ApprovalRequested):
            approval_id = ev.approval_id
            break
    await asyncio.sleep(0.05)
    assert approval_id is not None  # the need was announced as an event
    assert not run_task.done()  # blocked awaiting control — the event did not decide it
    assert not (tmp_path / "RAN").exists()
    await session.resolve_approval(approval_id, allow=False)  # cleanup: deny the real pending call
    await run_task
