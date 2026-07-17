"""Tool contracts: result shape, definition, registry, and runtime (§10).

Tools are narrow, typed, and self-describing. The runtime validates every call
(known name, well-formed input) before executing; model-correctable errors come
back as `ToolResult(success=False, error=...)` — recoverable feedback for the
model, never an exception thrown at the loop (§10 retry semantics).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from avatar.deps import RunDeps


class ToolResult(BaseModel):
    """The typed outcome of one tool call; the model only ever sees `content` (§10)."""

    tool_name: str
    success: bool
    content: str = ""  # what the model MAY see
    summary: str = ""  # one-line; feeds context budgeting
    error: str | None = None  # set when success is False (model-correctable)
    files_read: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    terminate: bool = False  # "ready for verification", NOT "stop now"


# Handlers take the validated input model (concrete subtype) + run deps.
ToolHandler = Callable[[Any, RunDeps], ToolResult]


# The mutating tier (str_replace/write_file). A tier-1 call is the model's *edit intent*: it
# advances the phase to `editing` and is reachable from `investigating` on edit-shaped
# tasks (the bootstrap exception that avoids a deadlock on pure-creation, §2.6).
EDIT_INTENT_TIER = 1

# Task kinds whose contract permits mutation, so the edit-intent bootstrap applies (§7).
EDIT_KINDS = frozenset({"edit", "test_only"})

# The mid-run `investigate → edit` escalation tool (ADR-0048). Kind-gated, not just
# phase-gated: an `edit` task also *starts* in `investigating`, and an escalated task has
# already become `edit` — so a phase-only rule would advertise "escalate this INVESTIGATION"
# to tasks that cannot escalate, and `_escalate_to_edit`'s guard would no-op after the human
# paid a tier-3 approval, telling the model an escalation happened when none did (PR #114
# review). Offering it only on a live investigation keeps the affordance honest.
ESCALATION_TOOL = "switch_to_editing"


@dataclass(frozen=True)
class ToolDefinition:
    """A registered tool: its schema, handler, the phases it is active in, and tier (§10).

    `paths` self-declares which of the tool's *validated* inputs are filesystem
    paths (§11, Phase 2.5). The permission gate runs confinement + the sensitive-path
    denylist over them centrally, so the policy can't drift across tools. The default
    is a pass-through (no paths) — only path-bearing tools override it.
    """

    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    phases: frozenset[str]  # phases in which this tool is active
    permission_tier: int = 0
    paths: Callable[[Any], Sequence[str]] = field(default=lambda _args: ())


def is_edit_intent(task_kind: str, tool: ToolDefinition) -> bool:
    """Whether `tool` is the model's edit intent: the mutating tier on an edit-shaped task.

    Deliberately edit-kinds-only: an investigate task's transient edits (ADR-0005) are
    admitted by `admits_transient_edit` instead, so they never ride this bootstrap and
    never advance the phase.

    Args:
        task_kind: The task's kind (only `edit`/`test_only` permit mutation, §7).
        tool: The resolved tool definition.

    Returns:
        True when `tool` is the mutating tool (tier 1) and the task kind permits edits.
    """
    return tool.permission_tier == EDIT_INTENT_TIER and task_kind in EDIT_KINDS


def admits_transient_edit(task_kind: str, tool: ToolDefinition) -> bool:
    """Whether `tool` is admitted as a transient edit in an investigate task (ADR-0005).

    Investigation sometimes *instruments*: add a debug print, run, observe, revert.
    Tier-1 mutation is therefore legal in `investigate` tasks — the enforcement point is
    the verifier's unchanged net-zero-diff contract (`no_unintended_diff`: the tree must
    match the pinned baseline at verification), detection where prevention used to be.
    An explicit rule, distinct from `is_edit_intent`, so the edit-intent phase bootstrap
    stays edit-kinds-only and investigate's phase flow is unchanged.

    Args:
        task_kind: The task's kind; only `investigate` rides this rule.
        tool: The resolved tool definition.

    Returns:
        True when `tool` is the mutating tier (1) and the task is an investigation.
    """
    return tool.permission_tier == EDIT_INTENT_TIER and task_kind == "investigate"


def phase_admits_tool(phase: str, task_kind: str, tool: ToolDefinition) -> bool:
    """Whether `tool` may run *and* be advertised in `phase` for `task_kind` (§2.6).

    The single source of truth shared by the runner's gate (what may execute) and the
    `ContextBuilder` (what the model is told it may call) — keeping them in lockstep so
    the model never loops blind on a tool the runner would have admitted. True when the
    tool is active in the phase, or it is the edit-intent tool reachable from
    `investigating` via the bootstrap exception, or it is a transient edit in an
    investigate task (ADR-0005).

    Args:
        phase: The current control phase.
        task_kind: The task's kind, gating the edit-intent bootstrap and the
            transient-edit rule.
        tool: The resolved tool definition.

    Returns:
        True if `phase` is in the tool's phases, or `tool` is an edit-intent tool on an
        edit-shaped task (the bootstrap that surfaces `str_replace` from `investigating`),
        or `tool` is tier-1 on an investigate task (transient instrumentation, ADR-0005).
        The escalation tool is the one *narrowing* rule: investigate-only (ADR-0048).
    """
    if tool.name == ESCALATION_TOOL:
        # Only a live investigation can escalate. An escalation flips the kind to `edit`, so
        # this same rule also stops offering it once escalated — no separate "already escalated"
        # check is needed, and the gate refuses a stray call instead of false-succeeding.
        return task_kind == "investigate" and phase in tool.phases
    return phase in tool.phases or is_edit_intent(task_kind, tool) or admits_transient_edit(task_kind, tool)


class ToolRegistry:
    """The set of available tools, queryable by name and by active phase (§10)."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        """Add (or replace) a tool definition by name.

        Args:
            tool: The tool definition to register, keyed by its `name`.
        """
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        """Return the tool registered under `name`, or None if unknown.

        Args:
            name: The tool name to look up.

        Returns:
            The matching `ToolDefinition`, or None if no tool is registered under `name`.
        """
        return self._tools.get(name)

    def active_for_phase(self, phase: str) -> list[ToolDefinition]:
        """Return the tools enabled in the given phase (§10/§21 capability groups).

        Args:
            phase: The phase to filter active tools by.

        Returns:
            The tool definitions whose `phases` include `phase`.
        """
        return [tool for tool in self._tools.values() if phase in tool.phases]

    def admitted_for(self, phase: str, task_kind: str) -> list[ToolDefinition]:
        """Return the tools the runner will admit in `phase` for `task_kind` (§2.6).

        Like `active_for_phase`, but also includes the edit-intent bootstrap, so the
        model is advertised *exactly* what the runner's gate would let it execute —
        notably `str_replace` from `investigating` on an edit task.

        Args:
            phase: The phase to filter by.
            task_kind: The task's kind, gating the edit-intent bootstrap.

        Returns:
            The tool definitions admitted in `phase` for `task_kind`.
        """
        return [tool for tool in self._tools.values() if phase_admits_tool(phase, task_kind, tool)]


class ToolRuntime:
    """Validates and dispatches tool calls; never raises into the loop (§10).

    Args:
        registry: The registry to resolve tool calls against.
        deps: The run-scoped `RunDeps` passed to every handler.
    """

    def __init__(self, registry: ToolRegistry, deps: RunDeps) -> None:
        self.registry = registry
        self.deps = deps

    def execute(self, name: str, raw_input: dict) -> ToolResult:
        """Resolve, validate, and run a tool call; errors return as a failed `ToolResult`.

        Args:
            name: The registered name of the tool to invoke.
            raw_input: The unvalidated call arguments, validated against the tool's input model.

        Returns:
            The handler's `ToolResult`, or a failed one for an unknown name, invalid input,
            or a handler that raised (isolated so a buggy tool never crashes the run).
        """
        tool = self.registry.get(name)
        if tool is None:
            return ToolResult(tool_name=name, success=False, error=f"unknown tool: {name!r}")
        try:
            args = tool.input_model.model_validate(raw_input)
        except ValidationError as exc:
            return ToolResult(
                tool_name=name,
                success=False,
                error=f"invalid input for {name!r}: {exc.errors(include_url=False)}",
            )
        try:
            return tool.handler(args, self.deps)
        except Exception as exc:  # isolate any tool crash; never raise into the loop
            # A buggy/third-party handler must not crash the run: surface it as a failed
            # result. Naming the exception type marks it as a systemic failure to be
            # surfaced (not a model-correctable error to auto-retry — §10 retry semantics).
            return ToolResult(
                tool_name=name,
                success=False,
                error=f"tool {name!r} raised {type(exc).__name__}: {exc}",
            )
