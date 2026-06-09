"""Lane C — the public API surface and the `Harness` facade (Phase 2.6).

The facade performs the default wiring the CLI used to hardcode, with every
collaborator overridable. These tests pin (a) the curated import surface,
(b) an end-to-end `from_env` investigate run, (c) that each injected seam is
actually used, and (d) that the CLI delegates to the facade rather than
re-deriving the wiring.
"""

import asyncio
import subprocess
import sys
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel

import avatar_harness
from avatar_harness import cli
from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextPacket
from avatar_harness.events import Emitter
from avatar_harness.harness import Harness
from avatar_harness.model_client import (
    FinalAnswer,
    ModelClient,
    ModelDecision,
    ToolCall,
)
from avatar_harness.permission import PermissionPolicy, ToolPermission
from avatar_harness.state import TaskState
from avatar_harness.tools.base import ToolDefinition, ToolRegistry, ToolResult
from avatar_harness.verifier import Verifier
from avatar_harness.workspace import Workspace


class _OneShotModel(ModelClient):
    """A ModelClient that answers immediately — enough to exercise facade wiring."""

    def decide(self, context: ContextPacket) -> ModelDecision:
        return ModelDecision(action=FinalAnswer(answer="done"))


def _read_then_answer() -> ModelClient:
    """A scripted model: read a real file (cites evidence), then answer."""

    class _Scripted(ModelClient):
        def __init__(self) -> None:
            self._calls = 0

        def decide(self, context: ContextPacket) -> ModelDecision:
            self._calls += 1
            if self._calls == 1:
                return ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"}))
            return ModelDecision(action=FinalAnswer(answer="calc.py defines add(), which subtracts"))

    return _Scripted()


_STABLE_SURFACE = (
    "Harness",
    "HarnessConfig",
    "TaskState",
    "ToolDefinition",
    "ToolResult",
    "ToolRegistry",
    "RunDeps",
    "ModelClient",
    "Workspace",
    "ModelDecision",
    "ToolCall",
    "FinalAnswer",
    "AskUser",
)

# The async / two-plane surface a third-party UI or autonomous wrapper builds against
# (Phase 3.0). Exported deliberately so consumers never deep-import internals.
_ASYNC_SURFACE = (
    "Session",
    "EventBus",
    "JsonlEventJournal",
    "ReplSession",
    "SessionState",
    "Turn",
    "HarnessEvent",
    "EventBase",
    "EventSink",
    "ApprovalController",
    "ApprovalGrant",
    "AgentStart",
    "AgentEnd",
    "PhaseChanged",
    "ModelUpdate",
    "ToolStart",
    "ToolEnd",
    "ApprovalRequested",
    "ApprovalResolved",
    "VerificationStart",
    "VerificationEnd",
    "parse_event",
    "load_events",
)


def test_public_api_exports_stable_surface():
    # The curated surface a downstream user imports — pinning it guards against
    # accidental churn of the public contract: every name is both importable from
    # the top-level package and listed in `__all__`.
    for name in (*_STABLE_SURFACE, *_ASYNC_SURFACE):
        assert name in avatar_harness.__all__
        assert getattr(avatar_harness, name, None) is not None


def test_core_imports_without_textual():
    # The TUI cockpit is behind the optional [textual] extra: importing the core
    # package must not pull in textual (or the tui package). Checked in a fresh
    # interpreter so it is independent of whatever other tests have imported.
    code = (
        "import avatar_harness, sys; "
        "assert 'textual' not in sys.modules, sorted(m for m in sys.modules if 'textual' in m); "
        "assert 'avatar_harness.tui' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


async def test_harness_arun_runs_investigate_end_to_end(git_repo):
    # The async entry point: `await harness.arun(...)` drives the same loop as run().
    harness = Harness(config=HarnessConfig(workspace_root=str(git_repo)), model=_read_then_answer())
    state = await harness.arun("explain add()", task_kind="investigate")
    assert state.outcome == "success"
    assert "calc.py" in state.files_read


async def test_harness_session_streams_to_completion(git_repo):
    # The two-plane SDK shape: harness.session(...) → events() out, run() drives it.
    harness = Harness(config=HarnessConfig(workspace_root=str(git_repo)), model=_read_then_answer())
    session = harness.session("explain add()", task_kind="investigate")
    seen: list[str] = []
    run_task = asyncio.create_task(session.run())
    async for ev in session.events():
        seen.append(ev.type)
    state = await run_task
    assert state.outcome == "success"
    assert seen[-1] == "agent_end"  # the stream ran to completion


def test_harness_from_env_runs_investigate_end_to_end(git_repo, monkeypatch):
    # `Harness.from_env()` builds HarnessConfig from the environment + defaults and
    # runs a real investigate loop end-to-end (fake model so no network).
    monkeypatch.setenv("AVATAR_WORKSPACE_ROOT", str(git_repo))
    harness = Harness.from_env(model=_read_then_answer())
    state = harness.run("explain add()", task_kind="investigate")
    assert isinstance(state, TaskState)
    assert state.outcome == "success"
    assert "calc.py" in state.files_read


def test_harness_overrides_each_seam(git_repo):
    # Each injected collaborator is actually used by the run: a custom registry,
    # verifier, and policy all leave an observable mark.
    used = {"verifier": False, "policy": False}

    class _RecordingVerifier(Verifier):
        def verify(self, state: TaskState, ws: Workspace) -> Any:
            used["verifier"] = True
            return super().verify(state, ws)

    class _BlockingPolicy(PermissionPolicy):
        def check(self, tool: ToolDefinition, raw_input: dict, state: TaskState, ws: Workspace) -> Any:
            used["policy"] = True
            return ToolPermission(blocked=True, reason="blocked by injected policy")

    class _PingInput(BaseModel):
        pass

    def _ping_handler(args: _PingInput, deps: Any) -> ToolResult:
        return ToolResult(tool_name="ping", success=True, content="pong")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="ping",
            description="ping",
            input_model=_PingInput,
            handler=_ping_handler,
            phases=frozenset({"investigating", "editing", "verifying"}),
        )
    )

    class _PingThenAnswer(ModelClient):
        def __init__(self) -> None:
            self._calls = 0

        def decide(self, context: ContextPacket) -> ModelDecision:
            self._calls += 1
            if self._calls == 1:
                return ModelDecision(action=ToolCall(name="ping", input={}))
            return ModelDecision(action=FinalAnswer(answer="done"))

    config = HarnessConfig(workspace_root=str(git_repo))
    harness = Harness(
        config=config,
        model=_PingThenAnswer(),
        tools=registry,
        verifier=_RecordingVerifier(config),
        policy=_BlockingPolicy(),
    )
    state = harness.run("do a thing", task_kind="investigate")

    assert used["verifier"]  # the injected verifier ran
    assert used["policy"]  # the injected policy gated the tool call
    # The blocking policy refused the injected registry's tool — observable proof
    # both the tools and policy seams were wired through the facade.
    assert any("blocked by injected policy" in ev.summary for ev in state.evidence)


def test_cli_delegates_to_harness_facade(git_repo):
    # The CLI is a thin caller of the facade — `run_agent` constructs a `Harness`
    # and calls `.run()`, rather than re-deriving the wiring inline.
    with patch.object(Harness, "run", autospec=True) as mock_run:
        mock_run.return_value = TaskState(goal="x")
        config = HarnessConfig(workspace_root=str(git_repo))
        cli.run_agent("explain add()", config=config, emitter=Emitter(), model_client=_OneShotModel())

    assert mock_run.called
