"""Model decision protocol: constrained, validated decisions (§6).

The model returns one of three actions, never arbitrary prose. The harness
validates every decision before acting; a malformed decision is a *recoverable*
error fed back to the model, never executed and never fatal.

`parse_decision` is the pure validation boundary (no network), so it — and the
fakes that stand in for a real client in tests — are trivially testable.
"""

import json
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError


class ToolCall(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    name: str  # must match a tool active for the current phase
    input: dict = Field(default_factory=dict)  # validated against the tool's input_schema


class FinalAnswer(BaseModel):
    type: Literal["final_answer"] = "final_answer"
    answer: str  # claims completion; subject to verification (§12)


class AskUser(BaseModel):
    type: Literal["ask_user"] = "ask_user"
    question: str


class ModelDecision(BaseModel):
    thought_summary: str = ""  # for logging/context only — never control flow
    action: ToolCall | FinalAnswer | AskUser = Field(discriminator="type")


class DecisionParseError(Exception):
    """Malformed model output — recoverable; fed back to the model (§6), never fatal."""


def parse_decision(raw: str) -> ModelDecision:
    """Validate raw model output into a `ModelDecision`, or raise a recoverable error."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DecisionParseError(f"not valid JSON: {exc}") from exc
    try:
        return ModelDecision.model_validate(data)
    except ValidationError as exc:
        raise DecisionParseError(f"invalid decision: {exc.errors(include_url=False)}") from exc


class ModelClient(Protocol):
    """Anything that turns a context packet into a validated decision (§6).

    The real implementation calls an OpenAI-compatible endpoint and runs the
    result through `parse_decision`; tests substitute a scripted fake.
    """

    def decide(self, context: object) -> ModelDecision: ...
