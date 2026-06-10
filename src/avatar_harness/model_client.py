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


class DecisionRetryNote(BaseModel):
    """One malformed in-client attempt: what was wrong, and a capped raw excerpt."""

    error: str
    raw: str = ""


class DecisionUsage(BaseModel):
    """Provider-reported token usage for one decision (all in-client attempts summed)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0


class ModelDecision(BaseModel):
    """One validated model decision: a thought plus exactly one action (§6).

    `retry_trace` is a **harness-owned diagnostics channel**: the model client annotates
    the decision with any malformed attempts it recovered from in-client, so the runner
    can record them as evidence and journal them (invariant #5). It is never accepted
    from raw model output — `parse_decision` clears it.
    """

    thought_summary: str = ""  # for logging/context only — never control flow
    action: ToolCall | FinalAnswer | AskUser = Field(discriminator="type")
    retry_trace: list[DecisionRetryNote] = Field(default_factory=list)
    usage: DecisionUsage | None = None  # harness-owned, like retry_trace; set by the client


# Cap on the raw-reply excerpt kept per malformed attempt (journal/evidence detail).
_RAW_EXCERPT_CAP = 2000


class DecisionParseError(Exception):
    """Malformed model output — recoverable; fed back to the model (§6), never fatal.

    Carries `usage` when the client exhausts its in-client retries, so a lost turn is
    still billed — the expensive failure mode is exactly the one that must not be
    undercounted (PR-#31 review).

    Args:
        message: The parse-failure description fed back to the model.
        usage: Tokens spent across the failed attempts, or `None` if unreported.
    """

    def __init__(self, message: str = "", usage: "DecisionUsage | None" = None) -> None:
        super().__init__(message)
        self.usage = usage


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
        decision = ModelDecision.model_validate(data)
    except ValidationError as exc:
        raise DecisionParseError(f"invalid decision: {exc.errors(include_url=False)}") from exc
    # `retry_trace`/`usage` are harness-owned channels: a model emitting the fields
    # must not plant fake history or bill itself kindly.
    decision.retry_trace = []
    decision.usage = None
    return decision


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


# Kind-AWARE framing: one mission line per `task_kind`, injected into the template.
# Capability is still gated by tool *exposure* per phase (§10/§21) — the mission only
# orients the model. An edit task is never framed READ-ONLY (that would forbid the very
# `apply_patch` it must call); an investigate task is explicitly told not to edit.
_KIND_FRAMING = {
    "investigate": (
        "Your mission: ANSWER the question WITHOUT editing the repo. Inspect with read "
        "tools and cite the concrete evidence (paths/lines) you actually read."
    ),
    "edit": (
        "Your mission: make a WORKING code change. Inspect what you will modify, then "
        "apply a patch; an external verifier will run real tests/lint on your diff."
    ),
    "test_only": (
        "Your mission: ADD or change tests that capture the intended behavior. The new "
        "tests must run and pass."
    ),
}

_SYSTEM_TEMPLATE = """You are the reasoning core of a coding-agent harness. Return EXACTLY \
ONE JSON object per turn and nothing else.

{mission}

Decision schema:
  {{"thought_summary": "<brief reasoning>", "action": <action>}}
where <action> is exactly one of:
  {{"type": "tool_call", "name": "<tool name>", "input": {{...}}}}
  {{"type": "final_answer", "answer": "<answer citing concrete evidence>"}}
  {{"type": "ask_user", "question": "<question>"}}

Rules:
- You begin with no files; discover the repo incrementally using tools.
- Your final answer MUST cite concrete evidence (paths/lines you actually inspected).
- Call only the tools listed below, with input matching their schema.

Available tools:
{tools}"""


# Native-transport twin of _SYSTEM_TEMPLATE (ADR-0003 A): the provider carries the tool
# schemas and the call envelope, so the prompt must not demand a hand-written JSON
# object — that instruction is exactly what conflicted with tool-calling.
_SYSTEM_TEMPLATE_NATIVE = """You are the reasoning core of a coding-agent harness. Take exactly \
ONE action per turn by calling exactly one of the provided tools.

{mission}

Rules:
- You begin with no files; discover the repo incrementally using the tools.
- When the task is complete, call final_answer — the answer MUST cite concrete evidence \
(paths/lines you actually inspected). Completion is verified externally; never claim work \
you did not do.
- If you are blocked on something only the user can answer, call ask_user."""


def _format_tools(tools: list[ToolSummary]) -> str:
    lines = []
    for tool in tools:
        props = json.dumps(tool.input_schema.get("properties", {}))
        lines.append(f"- {tool.name}: {tool.description} | input properties: {props}")
    return "\n".join(lines)


def build_messages(context: ContextPacket, *, native_tools: bool = False) -> list[dict[str, str]]:
    """Assemble the system + user messages for one decision (§9 packet → prompt).

    Args:
        context: The assembled context packet.
        native_tools: `True` for the native tool-calling transport (ADR-0003 A) — the
            provider carries the tool schemas, so the prompt drops the JSON-envelope
            contract and the prose tool list; `False` keeps the legacy protocol verbatim.

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
    mission = _KIND_FRAMING.get(context.task_kind, _KIND_FRAMING["investigate"])
    if native_tools:
        parts.append("Take your next action now (one tool call).")
        system = _SYSTEM_TEMPLATE_NATIVE.format(mission=mission)
    else:
        parts.append("Respond with your next action as a single JSON object.")
        system = _SYSTEM_TEMPLATE.format(mission=mission, tools=_format_tools(context.allowed_tools))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(parts)},
    ]


def build_tool_schemas(context: ContextPacket) -> list[dict]:
    """The function schemas for one decision: the advertised tools + the decision actions.

    Each phase-admitted tool rides its real pydantic `input_schema`; `final_answer` and
    `ask_user` become functions too, so every §6 decision shape is a structured call the
    provider validates — never a hand-escaped JSON envelope (ADR-0003 A).

    Args:
        context: The assembled context packet (its `allowed_tools` are advertised).

    Returns:
        OpenAI-style `tools=` entries.
    """
    schemas = [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema or {"type": "object", "properties": {}},
            },
        }
        for tool in context.allowed_tools
    ]
    schemas.append(
        {
            "type": "function",
            "function": {
                "name": "final_answer",
                "description": (
                    "Claim the task is complete. The answer must cite concrete evidence "
                    "(paths/lines actually inspected); completion is verified externally."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                },
            },
        }
    )
    schemas.append(
        {
            "type": "function",
            "function": {
                "name": "ask_user",
                "description": "Ask the user a question you are blocked on (blocks in batch runs).",
                "parameters": {
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                },
            },
        }
    )
    return schemas


def _decision_from_tool_call(call: Any, thought: str) -> ModelDecision:
    """Map one provider tool call onto the §6 decision union, or raise a recoverable error.

    Args:
        call: The provider tool call (`.function.name` / `.function.arguments`).
        thought: Prose the model emitted alongside the call (its `thought_summary`).

    Returns:
        The validated decision.

    Raises:
        DecisionParseError: If the arguments are not a valid JSON object for the action.
    """
    name = call.function.name
    raw_args = call.function.arguments or "{}"
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        raise DecisionParseError(f"tool arguments not valid JSON: {exc}") from exc
    if not isinstance(args, dict):
        raise DecisionParseError("tool arguments must be a JSON object")
    try:
        action: ToolCall | FinalAnswer | AskUser
        if name == "final_answer":
            action = FinalAnswer.model_validate(args)
        elif name == "ask_user":
            action = AskUser.model_validate(args)
        else:
            action = ToolCall(name=name, input=args)
    except ValidationError as exc:
        raise DecisionParseError(f"invalid decision: {exc.errors(include_url=False)}") from exc
    return ModelDecision(thought_summary=thought, action=action)


def _assistant_call_message(message: Any, call: Any) -> dict:
    """Re-encode the assistant's tool-call turn for the retry conversation (§18 pairing).

    Only the call being answered is included, so the appended `role="tool"` reply keeps
    the history LLM-valid (every tool call answered by a matching `tool_call_id`).

    Args:
        message: The provider reply message carrying the call.
        call: The tool call being retried.

    Returns:
        The assistant message dict for the retry transcript.
    """
    return {
        "role": "assistant",
        "content": getattr(message, "content", None),
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments or ""},
            }
        ],
    }


class _UsageTally:
    """Accumulates provider-reported usage across a decision's in-client attempts."""

    def __init__(self) -> None:
        self._prompt = 0
        self._completion = 0
        self._seen = False

    def add(self, response: Any) -> None:
        """Fold one response's `usage` into the tally (absent usage is tolerated).

        Args:
            response: The provider reply, possibly carrying a `usage` object.
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self._prompt += int(getattr(usage, "prompt_tokens", 0) or 0)
        self._completion += int(getattr(usage, "completion_tokens", 0) or 0)
        self._seen = True

    def total(self) -> DecisionUsage | None:
        """The summed usage, or `None` when the endpoint never reported any.

        Returns:
            The tally as a `DecisionUsage`, or `None`.
        """
        if not self._seen:
            return None
        return DecisionUsage(prompt_tokens=self._prompt, completion_tokens=self._completion)


class OpenAIModelClient(ModelClient):
    """Calls an OpenAI-compatible endpoint and validates the reply (§6, §18).

    A malformed reply is fed back to the model for a bounded number of retries
    before surfacing as a `DecisionParseError` (which the runner treats as a
    recoverable, model-correctable error).

    Args:
        config: The harness configuration.
        client: An injected OpenAI-compatible client, or `None` to build one lazily on
            first use — so construction needs no credentials; the optional `openai`
            extra and an API key are required only when `decide()` is first called.
        max_parse_retries: Number of retries on malformed model output.
    """

    def __init__(self, config: HarnessConfig, client: Any = None, max_parse_retries: int = 2) -> None:
        self.config = config
        self.max_parse_retries = max_parse_retries
        # Built lazily on first decide() (see _ensure_client): credentials are an
        # inference-time concern, so a Harness with the default model is constructible
        # without an API key (and without the `openai` extra installed).
        self._client = client

    def _ensure_client(self) -> Any:
        """Return the OpenAI-compatible client, constructing it on first use.

        Returns:
            The injected client, or one constructed from `config` on first call.

        Raises:
            ImportError: If no client was injected and the optional `openai` extra is not installed.
        """
        if self._client is None:
            try:
                from openai import OpenAI  # noqa: PLC0415 — lazy: `openai` is an optional extra
            except ImportError as exc:  # openai not installed — it is an optional extra
                raise ImportError(
                    "OpenAIModelClient requires the optional 'openai' extra. "
                    "Install it with `pip install avatar-harness[openai]` (or `uv sync --extra openai`), "
                    "or inject a `client` / use a custom ModelClient instead."
                ) from exc
            # api_key=None lets the OpenAI client fall back to OPENAI_API_KEY in the env.
            self._client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url)
        return self._client

    def decide(self, context: ContextPacket) -> ModelDecision:
        """Call the endpoint and validate the reply, retrying on malformed output (§6).

        The default transport is native provider tool-calling (ADR-0003 A) — the
        provider owns the call envelope, so a large patch can't die in hand-escaping;
        `config.native_tool_calls=False` restores the legacy single-JSON-object protocol.
        Either path raises `DecisionParseError` when every attempt is malformed.

        Args:
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.
        """
        client = self._ensure_client()
        if self.config.native_tool_calls:
            return self._decide_native(client, context)
        return self._decide_json(client, context)

    def _decide_native(self, client: Any, context: ContextPacket) -> ModelDecision:
        """One decision over the native tool-calling transport (ADR-0003 A).

        The reply's first tool call maps onto the §6 union (`final_answer`/`ask_user`
        are functions too). A content-only reply — an endpoint that ignored `tools=` —
        falls back to the legacy `parse_decision` path, so "OpenAI-compatible" stays
        compatible. Malformed attempts are retried in-conversation with valid §18
        pairing (the call answered by a `role="tool"` message) and annotated onto the
        decision's `retry_trace`.

        Args:
            client: The OpenAI-compatible client.
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.

        Raises:
            DecisionParseError: If every attempt yields malformed output.
        """
        messages: list[dict] = list(build_messages(context, native_tools=True))
        tools = build_tool_schemas(context)
        last_error: DecisionParseError | None = None
        trace: list[DecisionRetryNote] = []
        tally = _UsageTally()  # every attempt costs tokens; the decision reports the sum
        for _ in range(self.max_parse_retries + 1):
            response = client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                tools=tools,
                temperature=0,
            )
            tally.add(response)
            message = response.choices[0].message
            calls = getattr(message, "tool_calls", None)
            if calls:
                call = calls[0]  # the protocol is exactly one action per turn (§6)
                try:
                    decision = _decision_from_tool_call(call, thought=message.content or "")
                except DecisionParseError as exc:
                    last_error = exc
                    raw = f"{call.function.name}({call.function.arguments or ''})"
                    trace.append(DecisionRetryNote(error=str(exc), raw=raw[:_RAW_EXCERPT_CAP]))
                    messages = [
                        *messages,
                        _assistant_call_message(message, call),
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": (
                                f"Invalid arguments ({exc}). Re-send the SAME intended "
                                "action with valid arguments."
                            ),
                        },
                    ]
                    continue
                decision.retry_trace = trace
                decision.usage = tally.total()
                return decision
            raw = message.content or ""
            try:
                decision = parse_decision(raw)  # endpoint ignored tools= — legacy fallback
            except DecisionParseError as exc:
                last_error = exc
                trace.append(DecisionRetryNote(error=str(exc), raw=raw[:_RAW_EXCERPT_CAP]))
                retry = (
                    f"That was not a valid decision ({exc}). Call one of the provided tools "
                    "— re-send the SAME intended action, do not switch to a different one."
                )
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": retry},
                ]
                continue
            decision.retry_trace = trace
            decision.usage = tally.total()
            return decision
        text = str(last_error) if last_error else "model returned no valid decision"
        raise DecisionParseError(text, usage=tally.total()) from last_error

    def _decide_json(self, client: Any, context: ContextPacket) -> ModelDecision:
        """One decision over the legacy single-JSON-object protocol (the escape hatch).

        Args:
            client: The OpenAI-compatible client.
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.

        Raises:
            DecisionParseError: If every attempt yields malformed output.
        """
        messages = build_messages(context)
        last_error: DecisionParseError | None = None
        trace: list[DecisionRetryNote] = []
        tally = _UsageTally()
        for _ in range(self.max_parse_retries + 1):
            response = client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0,
            )
            tally.add(response)
            raw = response.choices[0].message.content or ""
            try:
                decision = parse_decision(raw)
            except DecisionParseError as exc:
                last_error = exc
                # Annotate, don't swallow: the runner records each note as evidence and
                # journals it, so a failed (e.g. truncated-patch) attempt stays visible.
                trace.append(DecisionRetryNote(error=str(exc), raw=raw[:_RAW_EXCERPT_CAP]))
                retry = (
                    f"That was not a valid decision ({exc}). Re-send the SAME intended "
                    "action as one valid JSON decision — do not switch to a different action."
                )
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": retry},
                ]
            else:
                decision.retry_trace = trace
                decision.usage = tally.total()
                return decision
        # The loop only exits without returning via the except branch, which always
        # sets last_error; the fallback keeps this total without an (O-stripped) assert.
        message = str(last_error) if last_error else "model returned no valid decision"
        raise DecisionParseError(message, usage=tally.total()) from last_error
