"""Typed lifecycle events — the `HarnessEvent` discriminated union (ADR-0001/0002, Phase 3.0).

The closed, versioned union the engine *emits* and the cockpit *renders*. Unlike
the sync `Emitter`'s raw dicts (`events.py`, kept for back-compat + the CLI), these
are exhaustively matchable and carry a `schema_version` + global `event_id`, so the
journal can round-trip them verbatim and a renderer can switch on `type` safely.

`event_id` / `session_id` / `ts` are *stamped by the bus at publish* (see
`session.EventBus`); a freshly built event carries placeholder defaults until then.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, TypeAdapter

from avatar_harness.state import PlannedCheck

SCHEMA_VERSION = 1


class EventBase(BaseModel):
    """Fields common to every lifecycle event — the journal's ordering/versioning keys.

    The `type` discriminator is declared on each concrete event (not here): it is
    per-event by nature, and a shared mutable base field can't be narrowed to a
    `Literal` soundly. `HarnessEvent` is the discriminated union over the concretes.
    """

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: int = 0  # global total order, assigned by the bus at publish
    session_id: str = ""  # stamped by the bus
    task_id: str | None = None
    turn: int | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentStart(EventBase):
    """A run has begun on `goal`."""

    type: Literal["agent_start"] = "agent_start"
    goal: str = ""


class AgentEnd(EventBase):
    """A run has settled to a terminal `outcome` (the stream's end)."""

    type: Literal["agent_end"] = "agent_end"
    outcome: str | None = None


class TurnStart(EventBase):
    """A new loop iteration (`iteration`) has begun."""

    type: Literal["turn_start"] = "turn_start"
    iteration: int = 0


class TurnEnd(EventBase):
    """The current loop iteration has finished."""

    type: Literal["turn_end"] = "turn_end"


class PhaseChanged(EventBase):
    """The control phase advanced from `old` to `new` (§7)."""

    type: Literal["phase_changed"] = "phase_changed"
    old: str
    new: str


class ModelDecisionEvent(EventBase):
    """The model chose an action this turn (thought + a one-line brief)."""

    type: Literal["model_decision"] = "model_decision"
    thought: str = ""
    action_type: str = ""
    action: str = ""


class ModelUpdate(EventBase):
    """A streamed model-output delta — display only; never private chain-of-thought (ADR-0001 D6)."""

    type: Literal["model_update"] = "model_update"
    delta: str = ""
    channel: Literal["display"] = "display"


class ToolStart(EventBase):
    """A tool call (`call_id`) is about to execute."""

    type: Literal["tool_start"] = "tool_start"
    call_id: str = ""
    tool: str
    input: dict = Field(default_factory=dict)


class ToolEnd(EventBase):
    """A tool call finished — `success` plus its summary/content (§10)."""

    type: Literal["tool_end"] = "tool_end"
    call_id: str = ""
    tool: str
    success: bool
    summary: str = ""
    content: str = ""


class ApprovalRequested(EventBase):
    """A gated (tier-3 `ask`) call awaits a human decision — announce only (§13)."""

    type: Literal["approval_requested"] = "approval_requested"
    approval_id: str
    tool: str
    reason: str = ""
    input: dict = Field(default_factory=dict)


class ApprovalResolved(EventBase):
    """A pending approval (`approval_id`) was decided via the control plane.

    `via` records who decided: `"human"` answered a prompt, `"grant"` was auto-allowed
    by a session-scoped `ApprovalGrant` with no prompt (still observable, invariant #5).
    """

    type: Literal["approval_resolved"] = "approval_resolved"
    approval_id: str
    allowed: bool
    via: Literal["human", "grant"] = "human"


class ModelUsage(EventBase):
    """Provider-reported token usage for one turn (in-client retries summed).

    The journal's per-turn cost record — the eval harness (ADR-0004) sums these for
    tokens/$ per solved task; without them cost is unmeasurable (invariant #5).
    """

    type: Literal["model_usage"] = "model_usage"
    prompt_tokens: int = 0
    completion_tokens: int = 0


class DecisionError(EventBase):
    """A malformed model reply — either recovered by an in-client retry or a lost turn (§6).

    Closes the observability gap where a failed decision attempt (e.g. a truncated
    `apply_patch` emission) left no trace: every malformed attempt is journaled with
    its error and a capped excerpt of the raw reply, so a struggling run is legible
    live and debuggable after the fact (invariant #5).
    """

    type: Literal["decision_error"] = "decision_error"
    error: str = ""
    raw: str = ""  # capped excerpt of the malformed reply
    recovered: bool = True  # True: a later in-client attempt parsed; False: the turn was lost


class VerificationPlanFrozen(EventBase):
    """The per-session verification plan was resolved and frozen (ADR-0007).

    Journaled at the investigating → editing boundary, before any verification:
    each check carries its command and provenance, so every run's rubric — and
    where each check came from — is auditable. An empty `checks` records that
    nothing was discovered (the verifier will fail legibly).
    """

    type: Literal["verification_plan_frozen"] = "verification_plan_frozen"
    checks: list[PlannedCheck] = Field(default_factory=list)


class VerificationStart(EventBase):
    """The harness-owned verifier has begun (§12)."""

    type: Literal["verification_start"] = "verification_start"


class VerificationEnd(EventBase):
    """The verifier returned a verdict — `passed` plus its summary (§12)."""

    type: Literal["verification_end"] = "verification_end"
    passed: bool
    summary: str = ""


class CancellationObserved(EventBase):
    """The loop observed a tripped cancellation token and is stopping (§8)."""

    type: Literal["cancellation_observed"] = "cancellation_observed"
    reason: str = ""


HarnessEvent = Annotated[
    AgentStart
    | AgentEnd
    | TurnStart
    | TurnEnd
    | PhaseChanged
    | ModelDecisionEvent
    | ModelUpdate
    | ToolStart
    | ToolEnd
    | ApprovalRequested
    | ApprovalResolved
    | DecisionError
    | ModelUsage
    | VerificationPlanFrozen
    | VerificationStart
    | VerificationEnd
    | CancellationObserved,
    Field(discriminator="type"),
]

_ADAPTER: TypeAdapter[HarnessEvent] = TypeAdapter(HarnessEvent)


@runtime_checkable
class EventSink(Protocol):
    """Where the runner publishes typed events (the bus stamps `event_id`/`session_id`/`ts`).

    Foundation publishing is fire-and-forget via `publish_nowait` onto an unbounded
    queue; the awaited `emit` is the frozen async interface lane 1 fills in with
    bounded, backpressured fan-out. Both must keep `event_id` monotonic.
    """

    def publish_nowait(self, draft: "HarnessEvent") -> "HarnessEvent":
        """Stamp and enqueue `draft` without blocking.

        Args:
            draft: The event to publish.

        Returns:
            The stamped event.
        """
        ...

    async def emit(self, draft: "HarnessEvent") -> "HarnessEvent":
        """Awaitable publish — the interface lane 1 fills in with backpressure.

        Args:
            draft: The event to publish.

        Returns:
            The stamped event.
        """
        ...


@runtime_checkable
class ApprovalController(Protocol):
    """The awaited control hook the runner consults for a gated (tier-3 `ask`) call.

    The controller *announces* the need (an `ApprovalRequested` event) and then blocks
    this run only until a control method resolves it — the §13 two-plane boundary.
    """

    async def request_approval(self, approval_id: str, tool: str, reason: str, tool_input: dict) -> bool:
        """Announce the gated call and block this run until a control method resolves it.

        Args:
            approval_id: Correlates the announcement with its resolution.
            tool: The tool name awaiting approval.
            reason: The gate's reason, shown to the human.
            tool_input: The proposed call arguments.

        Returns:
            True iff the call was allowed.
        """
        ...


def parse_event(data: dict | str | bytes) -> HarnessEvent:
    """Validate a dict / JSON string into the right `HarnessEvent` variant.

    Raises `pydantic.ValidationError` if `type` is unknown (the union is closed) or
    fields are invalid.

    Args:
        data: A mapping or JSON text carrying a known `type` discriminator.

    Returns:
        The validated event.
    """
    if isinstance(data, (str, bytes)):
        return _ADAPTER.validate_json(data)
    return _ADAPTER.validate_python(data)


def dump_event(event: HarnessEvent) -> str:
    """Serialize an event to one JSON line for the journal.

    Args:
        event: The event to serialize.

    Returns:
        Its compact JSON representation.
    """
    return event.model_dump_json()


def load_events(path: Path) -> list[HarnessEvent]:
    """Reload a JSONL journal back into typed events, in file order.

    Args:
        path: The JSONL journal written via `dump_event` / a typed `EventLog`.

    Returns:
        The events, one per non-blank line, validated through the union.
    """
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [parse_event(line) for line in lines if line.strip()]
