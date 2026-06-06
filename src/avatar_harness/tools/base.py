"""Tool contracts: result shape, definition, registry, and runtime (§10).

Tools are narrow, typed, and self-describing. The runtime validates every call
(known name, well-formed input) before executing; model-correctable errors come
back as `ToolResult(success=False, error=...)` — recoverable feedback for the
model, never an exception thrown at the loop (§10 retry semantics).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from avatar_harness.deps import RunDeps


class ToolResult(BaseModel):
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


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    phases: frozenset[str]  # phases in which this tool is active
    permission_tier: int = 0


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def active_for_phase(self, phase: str) -> list[ToolDefinition]:
        return [tool for tool in self._tools.values() if phase in tool.phases]


class ToolRuntime:
    def __init__(self, registry: ToolRegistry, deps: RunDeps) -> None:
        self.registry = registry
        self.deps = deps

    def execute(self, name: str, raw_input: dict) -> ToolResult:
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
        return tool.handler(args, self.deps)
