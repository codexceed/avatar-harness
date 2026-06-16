"""avatar-harness: a minimal, ground-up coding-agent harness.

A bounded, verifiable loop around an LLM: the model proposes actions, the harness
owns execution/state/permissions/logging, and the loop terminates on external
verification — never on a text reply. See `HARNESS_DESIGN.md` for the design.

The curated public surface (`__all__`) is the stable importable API. `Harness`
is the one-call entry point; the rest are the seams a downstream user composes
against (tools, model client, workspace, state, decisions) — all extensible at
the edges (Principle A).

    from avatar import Harness
    state = Harness.from_env().run("explain the retry loop")

For an interactive UI or autonomous wrapper, the **two-plane async surface** is
the one to build on — observation out (`Session.events()` → typed `HarnessEvent`s),
control in (`Session.resolve_approval()` / `Session.cancel()`):

    session = Harness.from_env().session("fix the bug", task_kind="edit")
    run_task = asyncio.create_task(session.run())
    async for event in session.events():
        render(event)
        if isinstance(event, ApprovalRequested):
            await session.resolve_approval(event.approval_id, allow=ask_user(event))
    state = await run_task
"""

from avatar.bus import EventBus
from avatar.config import HarnessConfig
from avatar.deps import RunDeps
from avatar.event_types import (
    AgentEnd,
    AgentStart,
    ApprovalController,
    ApprovalRequested,
    ApprovalResolved,
    CancellationObserved,
    DecisionError,
    EventBase,
    EventSink,
    HarnessEvent,
    ModelDecisionEvent,
    ModelUpdate,
    PhaseChanged,
    ToolEnd,
    ToolStart,
    TurnEnd,
    TurnStart,
    VerificationEnd,
    VerificationPlanFrozen,
    VerificationStart,
    dump_event,
    load_events,
    parse_event,
)
from avatar.harness import Harness
from avatar.journal import JsonlEventJournal, resolve_log_path, update_latest_pointer
from avatar.model_client import (
    AskUser,
    FinalAnswer,
    ModelClient,
    ModelDecision,
    ToolCall,
)
from avatar.planner import VerificationPlanner
from avatar.session import ApprovalGrant, Session
from avatar.session_state import ReplSession, SessionState, Turn
from avatar.state import PlannedCheck, TaskState
from avatar.tools.base import ToolDefinition, ToolRegistry, ToolResult
from avatar.workspace import DirtyWorkspaceError, Workspace

__version__ = "1.2.0"  # x-release-please-version

__all__ = [  # noqa: RUF022 — grouped by role, not alphabetized: the grouping is the SDK map
    # --- core entry points & state ---
    "Harness",
    "HarnessConfig",
    "TaskState",
    "RunDeps",
    "Workspace",
    "DirtyWorkspaceError",
    # --- verification-plan resolution (ADR-0007) ---
    "VerificationPlanner",
    "PlannedCheck",
    # --- model decisions ---
    "ModelClient",
    "ModelDecision",
    "ToolCall",
    "FinalAnswer",
    "AskUser",
    # --- tools ---
    "ToolDefinition",
    "ToolRegistry",
    "ToolResult",
    # --- two-plane async surface (Phase 3.0) ---
    "Session",
    "EventBus",
    "JsonlEventJournal",
    "resolve_log_path",
    "update_latest_pointer",
    "EventSink",
    "ApprovalController",
    "ApprovalGrant",
    # --- multi-turn session scope (Phase 3.1 Lane 2a) ---
    "ReplSession",
    "SessionState",
    "Turn",
    # --- typed lifecycle events ---
    "HarnessEvent",
    "EventBase",
    "AgentStart",
    "AgentEnd",
    "TurnStart",
    "TurnEnd",
    "PhaseChanged",
    "ModelDecisionEvent",
    "DecisionError",
    "ModelUpdate",
    "ToolStart",
    "ToolEnd",
    "ApprovalRequested",
    "ApprovalResolved",
    "VerificationPlanFrozen",
    "VerificationStart",
    "VerificationEnd",
    "CancellationObserved",
    "parse_event",
    "dump_event",
    "load_events",
]
