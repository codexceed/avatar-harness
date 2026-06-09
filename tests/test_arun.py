"""Phase 3.0 foundation — Group 2: the async core (`arun()`).

`arun()` is the real loop; sync `run()` wraps it via `asyncio.run()`. The async
core must reach loop parity with the sync path, offload sync tool/model bodies so
the event loop stays responsive, and publish typed events in order through a sink.
"""

import asyncio
import time

from pydantic import BaseModel

from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextBuilder
from avatar_harness.deps import CancellationToken, RunDeps
from avatar_harness.event_types import EventBase
from avatar_harness.events import Emitter
from avatar_harness.model_client import FinalAnswer, ModelClient, ModelDecision, ToolCall
from avatar_harness.runner import AgentRunner
from avatar_harness.session import EventBus
from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolDefinition, ToolRegistry, ToolResult
from avatar_harness.tools.filesystem import read_file
from avatar_harness.tools.search import search_repo
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


def _runner(tmp_path, registry, decisions, *, event_sink=None, token=None, **config_kw) -> AgentRunner:
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
        event_sink=event_sink,
    )


def _read_registry(tmp_path) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in (read_file, search_repo):
        reg.register(tool)
    return reg


async def test_arun_drives_loop_to_terminal_outcome(tmp_path):
    (tmp_path / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="the handler lives in app.py")),
    ]
    runner = _runner(tmp_path, _read_registry(tmp_path), decisions)
    state = await runner.arun(TaskState(goal="where?", task_kind="investigate"))
    assert state.outcome == "success"
    assert state.final_answer == "the handler lives in app.py"


def test_run_wraps_arun_via_asyncio_run(tmp_path):
    # Sync run() delegates to arun() — proven by spying on arun, and the result matches.
    (tmp_path / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="handler in app.py")),
    ]
    runner = _runner(tmp_path, _read_registry(tmp_path), decisions)
    called = {}
    original = runner.arun

    async def spy(state):
        called["yes"] = True
        return await original(state)

    runner.arun = spy
    result = runner.run(TaskState(goal="where?", task_kind="investigate"))
    assert called.get("yes")  # run() drove arun()
    assert result.outcome == "success"


async def test_sync_tool_body_does_not_block_loop(tmp_path):
    # A slow SYNC tool body is offloaded (to_thread): a concurrent task keeps ticking
    # while it runs. If the loop blocked on the sync body, the ticker would not advance.
    class _Empty(BaseModel):
        pass

    def _slow(args, deps) -> ToolResult:
        time.sleep(0.3)  # deliberately a BLOCKING sync body — the thing under test
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
    runner = _runner(tmp_path, reg, decisions)
    ticks = 0
    task = asyncio.create_task(runner.arun(TaskState(goal="x", task_kind="investigate")))
    while not task.done():
        await asyncio.sleep(0.01)
        ticks += 1
    await task
    assert ticks >= 5  # the loop stayed responsive across the 0.3s sync body


async def test_cancellation_observed_during_arun(tmp_path):
    decisions = [ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"}))]
    token = CancellationToken(cancelled=True)
    runner = _runner(tmp_path, _read_registry(tmp_path), decisions, token=token)
    state = await runner.arun(TaskState(goal="x", task_kind="investigate"))
    assert state.outcome == "incomplete"
    assert state.iterations == 0  # observed before any turn ran


async def test_arun_emits_typed_events_with_monotonic_ids(tmp_path):
    (tmp_path / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="in app.py")),
    ]
    bus = EventBus(session_id="sess")
    runner = _runner(tmp_path, _read_registry(tmp_path), decisions, event_sink=bus)
    await runner.arun(TaskState(goal="where?", task_kind="investigate"))
    assert bus.history  # typed events were published
    assert all(isinstance(e, EventBase) for e in bus.history)
    ids = [e.event_id for e in bus.history]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)  # strictly increasing, unique
    assert {e.type for e in bus.history} >= {"agent_start", "agent_end"}
