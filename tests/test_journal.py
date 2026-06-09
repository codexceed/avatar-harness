"""Phase 3.1 Lane 1 — the privileged write-ahead `JsonlEventJournal` (ADR-0001).

Unlike a subscriber, the journal is *part of the engine*: a lossless, ordered, on-disk
record of every published event, flushed per event (write-ahead) so a crash mid-run
leaves a durable prefix. It is the substrate 3.3's semantics-aware resume will replay;
Lane 1 builds the record, not the replay engine. The journal round-trips through the
typed `HarnessEvent` union (`load_events`).
"""

import asyncio

from avatar_harness.bus import EventBus
from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextBuilder
from avatar_harness.deps import CancellationToken, RunDeps
from avatar_harness.event_types import (
    AgentEnd,
    AgentStart,
    PhaseChanged,
    load_events,
)
from avatar_harness.events import Emitter
from avatar_harness.journal import JsonlEventJournal
from avatar_harness.model_client import FinalAnswer, ModelClient, ModelDecision, ToolCall
from avatar_harness.runner import AgentRunner
from avatar_harness.session import Session
from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolRegistry
from avatar_harness.tools.filesystem import read_file
from avatar_harness.verifier import Verifier
from avatar_harness.workspace import Workspace


class ScriptedModel(ModelClient):
    """Replays pre-built decisions; repeats the last when exhausted."""

    def __init__(self, decisions: list[ModelDecision]) -> None:
        self._decisions = decisions
        self._i = 0

    def decide(self, context: object) -> ModelDecision:
        decision = self._decisions[min(self._i, len(self._decisions) - 1)]
        self._i += 1
        return decision


def _runner(tmp_path, decisions) -> AgentRunner:
    config = HarnessConfig()
    reg = ToolRegistry()
    reg.register(read_file)
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    return AgentRunner(
        model_client=ScriptedModel(decisions),
        registry=reg,
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=Emitter(),
        config=config,
    )


def test_journal_appends_in_order_and_round_trips(tmp_path):
    journal = JsonlEventJournal(tmp_path / "j.jsonl")
    bus = EventBus("s1", journal=journal)
    bus.publish_nowait(AgentStart(goal="g"))
    bus.publish_nowait(PhaseChanged(old="investigating", new="editing"))
    bus.publish_nowait(AgentEnd(outcome="success"))
    journal.close()
    reloaded = load_events(journal.path)
    assert [e.type for e in reloaded] == ["agent_start", "phase_changed", "agent_end"]
    phase = reloaded[1]
    assert isinstance(phase, PhaseChanged)  # narrow the union for the typed field check
    assert phase.old == "investigating" and phase.new == "editing"  # typed round-trip


def test_journal_writes_ahead_each_event_flushed(tmp_path):
    # Each event is durable on disk immediately after publish — not buffered until close.
    journal = JsonlEventJournal(tmp_path / "j.jsonl")
    bus = EventBus("s1", journal=journal)
    bus.publish_nowait(AgentStart(goal="g"))
    assert [e.type for e in load_events(journal.path)] == ["agent_start"]  # readable mid-stream
    bus.publish_nowait(AgentEnd(outcome="success"))
    assert [e.type for e in load_events(journal.path)] == ["agent_start", "agent_end"]
    journal.close()


async def test_session_journal_records_full_run(tmp_path):
    # Integration: a Session given a journal persists the complete typed stream of a run,
    # reloadable via load_events, with contiguous ids (lossless).
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="x is set in app.py")),
    ]
    journal_path = tmp_path / "run.jsonl"
    session = Session(
        _runner(tmp_path, decisions),
        TaskState(goal="where?", task_kind="investigate"),
        journal=JsonlEventJournal(journal_path),
    )
    run_task = asyncio.create_task(session.run())
    async for _ev in session.events():
        pass
    state = await run_task
    assert state.outcome == "success"
    reloaded = load_events(journal_path)
    types = [e.type for e in reloaded]
    assert types[0] == "agent_start" and types[-1] == "agent_end"
    ids = [e.event_id for e in reloaded]
    assert ids == list(range(1, len(ids) + 1))  # lossless, contiguous order
