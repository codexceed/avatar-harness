"""Phase 3.0 foundation — Group 2: the async core (`arun()`).

`arun()` is the real loop; sync `run()` wraps it via `asyncio.run()`. The async
core must reach loop parity with the sync path, offload sync tool/model bodies so
the event loop stays responsive, and publish typed events in order through a sink.
"""

import asyncio
import time

import pytest
from conftest import ScriptedModel
from pydantic import BaseModel

from avatar.config import HarnessConfig
from avatar.context import ContextBuilder
from avatar.deps import CancellationToken, RunDeps
from avatar.event_types import EventBase
from avatar.events import Emitter
from avatar.model_client import (
    DecisionParseError,
    DecisionRetryNote,
    DecisionUsage,
    FinalAnswer,
    ModelClient,
    ModelDecision,
    ToolCall,
)
from avatar.runner import AgentRunner
from avatar.session import EventBus
from avatar.state import TaskState
from avatar.tools.base import ToolDefinition, ToolRegistry, ToolResult
from avatar.tools.filesystem import read_file
from avatar.tools.search import search_repo
from avatar.verifier import Verifier
from avatar.workspace import Workspace


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


async def test_arun_cancellation_propagates_out_of_the_loop(tmp_path):
    # ADR-0024: a cancel during the (async) model call must unwind arun at once. The narrow
    # `except DecisionParseError` must not swallow CancelledError (a BaseException), and no
    # outer handler may trap it — so cancelling the task raises straight out of arun.
    started = asyncio.Event()

    class _Blocking(ModelClient):
        def decide(self, context):  # the runner uses adecide; decide must never be hit here
            raise AssertionError("decide() should not be called when adecide is overridden")

        async def adecide(self, context):
            started.set()
            await asyncio.sleep(30)  # a slow in-flight model call
            raise AssertionError("the call should have been cancelled")  # pragma: no cover

    config = HarnessConfig()
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    runner = AgentRunner(
        model_client=_Blocking(),
        registry=_read_registry(tmp_path),
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=Emitter(),
        config=config,
    )

    task = asyncio.create_task(runner.arun(TaskState(goal="g", task_kind="investigate")))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


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


async def test_recovered_parse_retries_recorded_and_published(tmp_path):
    # A decision that needed in-client retries arrives annotated (`retry_trace`); the
    # runner must surface each failed attempt as state evidence (so the model remembers
    # its own failed patches next turn) AND as a typed `decision_error` event (so the
    # journal shows the struggle). Closes the dogfood gap: 24 turns of invisible failure.
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    annotated = ModelDecision(
        action=FinalAnswer(answer="done"),
        retry_trace=[
            DecisionRetryNote(error="not valid JSON: truncated", raw='{"action": {"type": "apply_pat')
        ],
    )
    bus = EventBus(session_id="sess")
    runner = _runner(tmp_path, _read_registry(tmp_path), [annotated], event_sink=bus)
    state = await runner.arun(TaskState(goal="g", task_kind="investigate"))

    retries = [e for e in state.evidence if e.kind == "decision_error"]
    assert retries and "not valid JSON" in retries[0].summary  # the model will see this
    assert "apply_pat" in (retries[0].detail or "")  # raw attempt retained for debugging
    published = [e for e in bus.history if e.type == "decision_error"]
    assert published and published[0].recovered is True
    assert "not valid JSON" in published[0].error


async def test_exhausted_parse_failure_publishes_typed_event(tmp_path):
    # When every in-client attempt is malformed, the runner already records feedback —
    # but the typed stream (the journal) said nothing. The lost turn must be journaled.
    class _AlwaysMalformed(ModelClient):
        def decide(self, context):
            raise DecisionParseError("no valid decision after retries")

    bus = EventBus(session_id="sess")
    config = HarnessConfig(max_iterations=2)
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    runner = AgentRunner(
        model_client=_AlwaysMalformed(),
        registry=_read_registry(tmp_path),
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=Emitter(),
        config=config,
        event_sink=bus,
    )
    await runner.arun(TaskState(goal="g", task_kind="investigate"))

    published = [e for e in bus.history if e.type == "decision_error"]
    assert published and all(e.recovered is False for e in published)


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


async def test_usage_accumulates_into_state_and_journal(tmp_path):
    """Per-turn usage lands on `TaskState` totals and as a typed `model_usage` event.

    The eval harness (ADR-0004) sums journal usage for $/solve; the runner — the one
    mutator — does the accumulation, mirroring how `retry_trace` notes become evidence.
    """
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(
            action=ToolCall(name="read_file", input={"path": "app.py"}),
            usage=DecisionUsage(prompt_tokens=1000, completion_tokens=20),
        ),
        ModelDecision(
            action=FinalAnswer(answer="x is set in app.py"),  # cited → investigate gate passes
            usage=DecisionUsage(prompt_tokens=1500, completion_tokens=30),
        ),
    ]
    bus = EventBus(session_id="sess")
    runner = _runner(tmp_path, _read_registry(tmp_path), decisions, event_sink=bus)
    state = await runner.arun(TaskState(goal="g", task_kind="investigate"))

    assert state.prompt_tokens == 2500 and state.completion_tokens == 50
    usage_events = [e for e in bus.history if e.type == "model_usage"]
    assert [(e.prompt_tokens, e.completion_tokens) for e in usage_events] == [(1000, 20), (1500, 30)]


async def test_lost_turn_usage_still_recorded(tmp_path):
    """A turn that dies in parse retries is still billed into state + the journal."""

    class _MalformedWithUsage(ModelClient):
        def decide(self, context):
            raise DecisionParseError(
                "no valid decision", usage=DecisionUsage(prompt_tokens=900, completion_tokens=12)
            )

    bus = EventBus(session_id="sess")
    config = HarnessConfig(max_iterations=1)
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    runner = AgentRunner(
        model_client=_MalformedWithUsage(),
        registry=_read_registry(tmp_path),
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=Emitter(),
        config=config,
        event_sink=bus,
    )
    state = await runner.arun(TaskState(goal="g", task_kind="investigate"))
    assert state.prompt_tokens == 900 and state.completion_tokens == 12
    assert any(e.type == "model_usage" for e in bus.history)


async def test_usage_reaches_legacy_emitter(tmp_path):
    """Batch runs (legacy Emitter → JSONL EventLog) record usage too — observability
    must not depend on which path the run took (PR-#31 review)."""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(
            action=FinalAnswer(answer="x is set in app.py"),
            usage=DecisionUsage(prompt_tokens=500, completion_tokens=10),
        ),
    ]
    seen: list[dict] = []
    emitter = Emitter()
    emitter.subscribe(seen.append)
    config = HarnessConfig()
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    runner = AgentRunner(
        model_client=ScriptedModel(decisions),
        registry=_read_registry(tmp_path),
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=emitter,
        config=config,
    )
    await runner.arun(TaskState(goal="g", task_kind="investigate"))
    usage_events = [e for e in seen if e.get("type") == "model_usage"]
    assert usage_events and usage_events[0]["prompt_tokens"] == 500


async def test_model_decision_event_carries_transport(tmp_path):
    # The transport that produced each decision is journaled (loop-determinism hardening):
    # a silent native->JSON flip is a run-to-run consistency hazard, so it must be visible
    # in the flight recorder.
    decision = ModelDecision(action=FinalAnswer(answer="done"), transport="native")
    bus = EventBus(session_id="sess")
    runner = _runner(tmp_path, _read_registry(tmp_path), [decision], event_sink=bus)
    await runner.arun(TaskState(goal="g", task_kind="investigate"))
    published = [e for e in bus.history if e.type == "model_decision"]
    assert published and published[0].transport == "native"
