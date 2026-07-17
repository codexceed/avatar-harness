"""The wall-clock budget pauses while a run is blocked on a human approval.

The budget (`max_wall_clock_seconds`) bounds the *agent's own work*, not the reviewer's
think-time. A tier-3 call that sits at the approval gate for minutes must not consume the
run's wall-clock: the runner credits the elapsed wait to the deadline (`_approval_wait_seconds`)
so a slow human never starves the run into a spurious `incomplete` (the dogfood failure where a
162s approval ate 27% of a 600s budget). A scoped auto-approve returns instantly, so it credits
≈0 — the pause is *only* for real human wait.

Determinism: `avatar.runner.time` is swapped for a controllable fake clock, and the approval
driver advances it past the budget *before* resolving — reproducing a long human wait without
sleeping. Only `monotonic` is patched on the runner's `time`; asyncio keeps its own clock.
"""

import asyncio
from types import SimpleNamespace

from conftest import ScriptedModel

import avatar.runner as runner_mod
from avatar.config import HarnessConfig
from avatar.context import ContextBuilder
from avatar.deps import CancellationToken, RunDeps
from avatar.events import Emitter
from avatar.model_client import FinalAnswer, ModelClient, ModelDecision, ToolCall
from avatar.runner import AgentRunner
from avatar.session import Session
from avatar.state import TaskState
from avatar.tools.base import ToolRegistry
from avatar.tools.commands import run_command
from avatar.tools.filesystem import read_file
from avatar.verifier import Verifier
from avatar.workspace import Workspace

_RAN = "python -c \"open('ran','w').write('1')\""  # a harmless tier-3 command with a visible effect


class _FakeClock:
    """A hand-advanced monotonic clock, so a 'long human wait' costs no real time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _session(tmp_path, config: HarnessConfig) -> Session:
    """A session whose scripted model runs one tier-3 command, then finalizes."""
    reg = ToolRegistry()
    reg.register(run_command)
    reg.register(read_file)
    decisions = [
        ModelDecision(action=ToolCall(name="run_command", input={"command": _RAN})),
        ModelDecision(action=FinalAnswer(answer="done")),
    ]
    runner = AgentRunner(
        model_client=ScriptedModel(decisions),
        registry=reg,
        deps=RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken()),
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=Emitter(),
        config=config,
    )
    # run_command isn't advertised in `investigating`; start an edit task already in `editing`.
    return Session(runner, TaskState(goal="run it", task_kind="edit", phase="editing"))


async def _drive(session: Session, clock: _FakeClock, *, wait: float):
    """Run `session`; when the human is prompted, advance the clock by `wait`, then allow."""
    stream = session.events()  # subscribe before the run starts
    run_task = asyncio.create_task(session.run())
    requested = []
    async for ev in stream:
        if ev.type == "approval_requested":
            requested.append(ev)
            clock.advance(wait)  # the reviewer "thinks" for `wait` seconds
            await session.resolve_approval(ev.approval_id, allow=True)
    return await run_task, requested


async def test_human_approval_wait_does_not_consume_wall_clock(tmp_path, monkeypatch):
    # A 100s human wait on a 10s budget must NOT starve the run: the clock pauses while blocked,
    # so the loop proceeds to the second turn instead of dying at the budget check. Without the
    # fix the loop would exit right after the approval (final_answer never reached, credit 0).
    clock = _FakeClock()
    monkeypatch.setattr(runner_mod, "time", SimpleNamespace(monotonic=clock.monotonic))
    config = HarnessConfig(max_wall_clock_seconds=10)  # far shorter than the human wait

    session = _session(tmp_path, config)
    state, requested = await _drive(session, clock, wait=100.0)  # human takes 10x the budget

    assert len(requested) == 1  # exactly one human prompt fired
    assert (tmp_path / "ran").exists()  # the approved command actually ran
    assert state.final_answer == "done"  # the loop reached turn 2 — NOT cut off after the approval
    # the credited pause equals the human wait exactly (the deadline slid forward by that much)
    assert session.runner._approval_wait_seconds == 100.0


async def test_credit_covers_only_the_human_wait_not_agent_work(tmp_path, monkeypatch):
    # The credit extends the deadline by the human wait ONLY — the agent's own generation time
    # still counts. Budget 10s; the model "spends" 12s generating turn 1, the human waits 5s
    # (credit +5 → effective deadline 15s). At turn 2 the clock reads 17s > 15s → budget trips.
    clock = _FakeClock()

    class _SlowGenThenGate(ModelClient):
        """Turn 1 burns 12s of agent time, then issues the gated command; never reaches turn 2."""

        def decide(self, context):
            clock.advance(12.0)  # the model's OWN generation time — counts against the budget
            return ModelDecision(action=ToolCall(name="run_command", input={"command": _RAN}))

    monkeypatch.setattr(runner_mod, "time", SimpleNamespace(monotonic=clock.monotonic))
    config = HarnessConfig(max_wall_clock_seconds=10)
    reg = ToolRegistry()
    reg.register(run_command)
    reg.register(read_file)
    runner = AgentRunner(
        model_client=_SlowGenThenGate(),
        registry=reg,
        deps=RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken()),
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=Emitter(),
        config=config,
    )
    session = Session(runner, TaskState(goal="run it", task_kind="edit", phase="editing"))

    state, requested = await _drive(session, clock, wait=5.0)  # human waits 5s → 5s credit

    assert len(requested) == 1
    assert (tmp_path / "ran").exists()  # the command was approved and ran before the budget tripped
    assert state.final_answer is None  # turn 2 never reached — the budget stopped the run
    assert state.outcome == "incomplete"
    assert runner._approval_wait_seconds == 5.0  # credit is the human wait, NOT the 12s of gen time
