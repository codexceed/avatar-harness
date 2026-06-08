"""Model decision protocol: constrained, validated decisions (§6).

The model returns one of three actions, never arbitrary prose. The harness
validates every decision before acting; a malformed decision is a *recoverable*
error fed back to the model, never executed and never fatal.

`parse_decision` is the pure validation boundary (no network), so it — and the
fakes that stand in for a real client in tests — are trivially testable.
"""

import json
from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextPacket, ToolSummary


class ToolCall(BaseModel):
    """Decision to invoke a named tool with validated input (§6)."""

    type: Literal["tool_call"] = "tool_call"
    name: str  # must match a tool active for the current phase
    input: dict = Field(default_factory=dict)  # validated against the tool's input_schema


class FinalAnswer(BaseModel):
    """Decision claiming the task is complete — a proposal for the verifier (§6, §12)."""

    type: Literal["final_answer"] = "final_answer"
    answer: str  # claims completion; subject to verification (§12)


class AskUser(BaseModel):
    """Decision to ask the user a question (blocks in a non-interactive run) (§6)."""

    type: Literal["ask_user"] = "ask_user"
    question: str


class ModelDecision(BaseModel):
    """One validated model decision: a thought plus exactly one action (§6)."""

    thought_summary: str = ""  # for logging/context only — never control flow
    action: ToolCall | FinalAnswer | AskUser = Field(discriminator="type")


class DecisionParseError(Exception):
    """Malformed model output — recoverable; fed back to the model (§6), never fatal."""


def parse_decision(raw: str) -> ModelDecision:
    """Validate raw model output into a `ModelDecision`, or raise a recoverable error.

    Args:
        raw: The raw model output to validate.

    Returns:
        The validated `ModelDecision`.

    Raises:
        DecisionParseError: If `raw` is not valid JSON or not a valid decision.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DecisionParseError(f"not valid JSON: {exc}") from exc
    try:
        return ModelDecision.model_validate(data)
    except ValidationError as exc:
        raise DecisionParseError(f"invalid decision: {exc.errors(include_url=False)}") from exc


class ModelClient(ABC):
    """Anything that turns a context packet into a validated decision (§6).

    The real implementation calls an OpenAI-compatible endpoint and runs the
    result through `parse_decision`; tests substitute a scripted fake.
    """

    @abstractmethod
    def decide(self, context: ContextPacket) -> ModelDecision:
        """Turn a context packet into a validated decision for the current turn.

        Args:
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.
        """
        ...


_SYSTEM_TEMPLATE = """You are the reasoning core of a coding-agent harness on a READ-ONLY \
investigation task. Return EXACTLY ONE JSON object per turn and nothing else.

Decision schema:
  {{"thought_summary": "<brief reasoning>", "action": <action>}}
where <action> is exactly one of:
  {{"type": "tool_call", "name": "<tool name>", "input": {{...}}}}
  {{"type": "final_answer", "answer": "<answer citing files/lines you actually read>"}}
  {{"type": "ask_user", "question": "<question>"}}

Rules:
- You begin with no files; discover the repo incrementally using tools.
- Your final answer MUST cite concrete evidence (paths you actually read).
- Call only the tools listed below, with input matching their schema.

Available tools:
{tools}"""


def _format_tools(tools: list[ToolSummary]) -> str:
    lines = []
    for tool in tools:
        props = json.dumps(tool.input_schema.get("properties", {}))
        lines.append(f"- {tool.name}: {tool.description} | input properties: {props}")
    return "\n".join(lines)


def build_messages(context: ContextPacket) -> list[dict[str, str]]:
    """Assemble the system + user messages for one decision (§9 packet → prompt).

    Args:
        context: The assembled context packet.

    Returns:
        The system + user messages for one decision.
    """
    parts = [f"Goal: {context.goal}"]
    if context.constraints:
        parts.append("Constraints: " + "; ".join(context.constraints))
    parts.append(f"Phase: {context.phase}")
    if context.files_read:
        parts.append("Files read: " + ", ".join(context.files_read))
    if context.prior_actions:
        parts.append(
            "Actions so far (do NOT repeat these):\n" + "\n".join(f"- {a}" for a in context.prior_actions)
        )
    if context.recent_evidence:
        parts.append("Recent evidence:\n" + "\n".join(f"- {e}" for e in context.recent_evidence))
    if context.latest_error:
        parts.append(f"Latest error: {context.latest_error}")
    parts.append("Respond with your next action as a single JSON object.")
    return [
        {"role": "system", "content": _SYSTEM_TEMPLATE.format(tools=_format_tools(context.allowed_tools))},
        {"role": "user", "content": "\n".join(parts)},
    ]


class OpenAIModelClient(ModelClient):
    """Calls an OpenAI-compatible endpoint and validates the reply (§6, §18).

    A malformed reply is fed back to the model for a bounded number of retries
    before surfacing as a `DecisionParseError` (which the runner treats as a
    recoverable, model-correctable error).

    Args:
        config: The harness configuration.
        client: An injected OpenAI-compatible client, or `None` to construct one.
        max_parse_retries: Number of retries on malformed model output.
    """

    def __init__(self, config: HarnessConfig, client: Any = None, max_parse_retries: int = 2) -> None:
        self.config = config
        self.max_parse_retries = max_parse_retries
        if client is None:
            from openai import OpenAI  # noqa: PLC0415 — lazy: only needed when no client is injected

            # api_key=None lets the OpenAI client fall back to OPENAI_API_KEY in the env.
            client = OpenAI(api_key=config.api_key, base_url=config.base_url)
        self.client = client

    def decide(self, context: ContextPacket) -> ModelDecision:
        """Call the endpoint and validate the reply, retrying on malformed output (§6).

        Args:
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.

        Raises:
            DecisionParseError: If every attempt yields malformed output.
        """
        messages = build_messages(context)
        last_error: DecisionParseError | None = None
        for _ in range(self.max_parse_retries + 1):
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw = response.choices[0].message.content or ""
            try:
                return parse_decision(raw)
            except DecisionParseError as exc:
                last_error = exc
                retry = f"That was not a valid decision ({exc}). Reply with one valid JSON decision."
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": retry},
                ]
        # The loop only exits without returning via the except branch, which always
        # sets last_error; the fallback keeps this total without an (O-stripped) assert.
        message = str(last_error) if last_error else "model returned no valid decision"
        raise DecisionParseError(message) from last_error
