"""Model decision protocol: constrained, validated decisions (§6).

The model returns one of three actions, never arbitrary prose. The harness
validates every decision before acting; a malformed decision is a *recoverable*
error fed back to the model, never executed and never fatal.

`parse_decision` is the pure validation boundary (no network), so it — and the
fakes that stand in for a real client in tests — are trivially testable.
"""

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, Field, ValidationError

from avatar.config import HarnessConfig
from avatar.context import ContextPacket, ToolSummary


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
    # Which transport produced this decision: "native" (provider function-calling),
    # "json_fallback" (native asked, endpoint ignored tools= → legacy parse), or "json"
    # (native disabled). Harness-owned (like usage); journaled so a silent native↔JSON
    # flip — a run-to-run consistency hazard with different prompts — stays visible.
    transport: str = ""


# Cap on the raw-reply excerpt kept per malformed attempt (journal/evidence detail).
# An edit-bearing attempt gets a higher cap: a failed `str_replace`/`write_file` is only
# useful to retry WITH its payload, and the flat cap cut real edits mid-body, so the model
# retried blind and re-emitted the same error (loop-determinism hardening).
_RAW_EXCERPT_CAP = 2000
_PATCH_EXCERPT_CAP = 12000
# Markers that say a raw reply is carrying a large edit payload (kept whole in the excerpt).
_PATCH_MARKERS = ('"old_string"', '"new_string"', '"content"', "str_replace", "write_file")


def _excerpt(raw: str, *, patch: bool = False) -> str:
    """Cap a raw malformed-attempt excerpt, marking any cut loudly (never silent).

    Args:
        raw: The raw model reply to excerpt.
        patch: Whether the attempt carries a patch (a higher cap, so the diff survives).

    Returns:
        `raw` whole if within the cap, else a truncated, explicitly-marked excerpt.
    """
    cap = _PATCH_EXCERPT_CAP if patch else _RAW_EXCERPT_CAP
    if len(raw) <= cap:
        return raw
    return raw[:cap] + f"\n… [truncated: {cap}/{len(raw)} chars shown]"


def _carries_patch(raw: str) -> bool:
    """Whether a raw reply looks like it carries a patch (content-mode excerpt sizing).

    Args:
        raw: The raw model reply to inspect.

    Returns:
        `True` if `raw` contains a patch marker, else `False`.
    """
    return any(marker in raw for marker in _PATCH_MARKERS)


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
    # `retry_trace`/`usage`/`transport` are harness-owned channels: a model emitting the
    # fields must not plant fake history, bill itself kindly, or impersonate a transport.
    decision.retry_trace = []
    decision.usage = None
    decision.transport = ""
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

    async def adecide(self, context: ContextPacket) -> ModelDecision:
        """Async, cancellable variant of `decide` — the runner's call site (ADR-0024).

        The default runs the synchronous `decide` in a worker thread, so every existing
        sync `ModelClient` keeps working unchanged — but a mid-call cancel cannot abort
        the in-flight request (the thread runs on to completion). A client with a native
        async transport (e.g. `OpenAIModelClient`) overrides this so that cancelling the
        awaiting task raises `CancelledError` at the `await` and aborts the request at the
        socket, leaving nothing running.

        Args:
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.
        """
        return await asyncio.to_thread(self.decide, context)


# Kind-AWARE framing: one mission line per `task_kind`, injected into the template.
# Capability is still gated by tool *exposure* per phase (§10/§21) — the mission only
# orients the model. An edit task is never framed READ-ONLY (that would forbid the very
# `str_replace` it must call); an investigate task may instrument transiently (ADR-0005)
# but is told the tree must net to zero diff when it answers.
_KIND_FRAMING = {
    "investigate": (
        "Your mission: ANSWER the question. Inspect with read tools and cite the concrete "
        "evidence (paths/lines) you actually read. You may instrument transiently (a debug "
        "print, a scratch probe), but the repo must be unchanged when you answer — revert "
        "any instrumentation first."
    ),
    "edit": (
        "Your mission: make a WORKING code change. Inspect what you will modify, then edit "
        "with str_replace (or write_file to create or rewrite a file); an external verifier "
        "will run real tests/lint on your diff."
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
    # Prior goals/replies ride as REAL chat turns between the system message and the working
    # packet (ADR-0017) — the model under-weighted them flattened into "Recent evidence".
    conversation = [{"role": turn.role, "content": turn.content} for turn in context.conversation]
    return [
        {"role": "system", "content": system},
        *conversation,
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
        client: An injected sync OpenAI-compatible client, or `None` to build one lazily on
            first use — so construction needs no credentials; the optional `openai`
            extra and an API key are required only when `decide()` is first called.
        max_parse_retries: Number of retries on malformed model output.
        async_client: An injected async OpenAI-compatible client (`AsyncOpenAI`), or `None`
            to build one lazily on first `adecide()` — the runner's cancellable call site
            (ADR-0024). Separate from `client` so the two transports stay independent.
    """

    def __init__(
        self,
        config: HarnessConfig,
        client: Any = None,
        max_parse_retries: int = 2,
        async_client: Any = None,
    ) -> None:
        self.config = config
        self.max_parse_retries = max_parse_retries
        # Built lazily on first decide()/adecide() (see _ensure_client / _ensure_async_client):
        # credentials are an inference-time concern, so a Harness with the default model is
        # constructible without an API key (and without the `openai` extra installed). The sync
        # and async clients are separate handles — the runner uses the async one (ADR-0024); the
        # sync `decide` remains for non-async callers and is injectable for tests.
        self._client = client
        self._async_client = async_client

    _IMPORT_HINT = (
        "OpenAIModelClient requires the optional 'openai' extra. "
        "Install it with `pip install avatar-harness[openai]` (or `uv sync --extra openai`), "
        "or inject a `client` / use a custom ModelClient instead."
    )

    def _timeout_kwargs(self) -> dict:
        """The `timeout=` kwarg for client construction — empty when unset (ADR-0024).

        Passing `timeout=None` to the SDK would mean *no* timeout (httpx), so an unset
        `request_timeout` must omit the kwarg entirely to keep the SDK's own default.

        Returns:
            `{"timeout": <seconds>}` when `config.request_timeout` is set, else `{}`.
        """
        return {} if self.config.request_timeout is None else {"timeout": self.config.request_timeout}

    def _ensure_client(self) -> Any:
        """Return the sync OpenAI-compatible client, constructing it on first use.

        Returns:
            The injected client, or one constructed from `config` on first call.

        Raises:
            ImportError: If no client was injected and the optional `openai` extra is not installed.
        """
        if self._client is None:
            try:
                from openai import OpenAI  # noqa: PLC0415 — lazy: `openai` is an optional extra
            except ImportError as exc:  # openai not installed — it is an optional extra
                raise ImportError(self._IMPORT_HINT) from exc
            # api_key=None lets the OpenAI client fall back to OPENAI_API_KEY in the env.
            self._client = OpenAI(
                api_key=self.config.api_key, base_url=self.config.base_url, **self._timeout_kwargs()
            )
        return self._client

    def _ensure_async_client(self) -> Any:
        """Return the async OpenAI-compatible client, constructing it on first use (ADR-0024).

        `AsyncOpenAI` is backed by `httpx.AsyncClient`, so cancelling the awaiting task
        aborts the in-flight request at the socket.

        Returns:
            The injected async client, or one constructed from `config` on first call.

        Raises:
            ImportError: If none was injected and the optional `openai` extra is not installed.
        """
        if self._async_client is None:
            try:
                from openai import AsyncOpenAI  # noqa: PLC0415 — lazy: `openai` is an optional extra
            except ImportError as exc:  # openai not installed — it is an optional extra
                raise ImportError(self._IMPORT_HINT) from exc
            self._async_client = AsyncOpenAI(
                api_key=self.config.api_key, base_url=self.config.base_url, **self._timeout_kwargs()
            )
        return self._async_client

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

    async def adecide(self, context: ContextPacket) -> ModelDecision:
        """Async, cancellable decision over the same transport as `decide` (ADR-0024).

        Cancelling the awaiting task raises `CancelledError` at the in-flight `await`,
        aborting the request at the socket — nothing is left running.

        Args:
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.
        """
        client = self._ensure_async_client()
        if self.config.native_tool_calls:
            return await self._adecide_native(client, context)
        return await self._adecide_json(client, context)

    # --- transport loops: sync and async drivers share the per-response step helpers, so the
    #     parse/retry/usage logic lives once; only the (awaited) `create` call differs (ADR-0024).

    def _native_request(self, messages: list[dict], tools: list) -> dict:
        """The `chat.completions.create` kwargs for one native-transport attempt.

        Args:
            messages: The conversation so far (grows by a pair on each retry).
            tools: The tool schemas offered to the model.

        Returns:
            The keyword arguments for the create call.
        """
        return {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "temperature": self.config.temperature,
        }

    def _step_native(
        self,
        response: Any,
        messages: list[dict],
        trace: list[DecisionRetryNote],
        tally: _UsageTally,
    ) -> tuple[ModelDecision | None, list[dict], DecisionParseError | None]:
        """Process one native-transport response (pure; no I/O).

        The reply's first tool call maps onto the §6 union (`final_answer`/`ask_user` are
        functions too). A content-only reply — an endpoint that ignored `tools=` — falls
        back to the legacy `parse_decision` path, so "OpenAI-compatible" stays compatible.
        A malformed attempt is annotated onto `trace` and returned as `decision=None` with
        the retry conversation (valid §18 pairing) folded into the returned `messages`.

        Args:
            response: The provider response for this attempt.
            messages: The conversation that produced it.
            trace: The retry-note accumulator (appended in place on a malformed attempt).
            tally: The token-usage accumulator (added to in place).

        Returns:
            `(decision, messages, error)`: a validated decision (then `error` is `None`),
            or `(None, retry_messages, error)` to retry.
        """
        tally.add(response)
        message = response.choices[0].message
        calls = getattr(message, "tool_calls", None)
        if calls:
            call = calls[0]  # the protocol is exactly one action per turn (§6)
            try:
                decision = _decision_from_tool_call(call, thought=message.content or "")
            except DecisionParseError as exc:
                raw = f"{call.function.name}({call.function.arguments or ''})"
                is_patch = call.function.name in ("str_replace", "write_file")
                trace.append(DecisionRetryNote(error=str(exc), raw=_excerpt(raw, patch=is_patch)))
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
                return None, messages, exc
            decision.retry_trace = trace
            decision.usage = tally.total()
            decision.transport = "native"
            return decision, messages, None
        raw = message.content or ""
        try:
            decision = parse_decision(raw)  # endpoint ignored tools= — legacy fallback
        except DecisionParseError as exc:
            trace.append(DecisionRetryNote(error=str(exc), raw=_excerpt(raw, patch=_carries_patch(raw))))
            retry = (
                f"That was not a valid decision ({exc}). Call one of the provided tools "
                "— re-send the SAME intended action, do not switch to a different one."
            )
            messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {"role": "user", "content": retry},
            ]
            return None, messages, exc
        decision.retry_trace = trace
        decision.usage = tally.total()
        decision.transport = "json_fallback"  # native asked, endpoint answered in prose
        return decision, messages, None

    def _raise_no_decision(self, last_error: DecisionParseError | None, tally: _UsageTally) -> NoReturn:
        """Raise the terminal error when every attempt yielded malformed output.

        Args:
            last_error: The most recent parse error, if any.
            tally: The token-usage accumulator (its sum is attached for billing).

        Raises:
            DecisionParseError: Always — wrapping the last parse error with the token tally.
        """
        text = str(last_error) if last_error else "model returned no valid decision"
        raise DecisionParseError(text, usage=tally.total()) from last_error

    def _decide_native(self, client: Any, context: ContextPacket) -> ModelDecision:
        """One decision over the native tool-calling transport — sync driver (ADR-0003 A).

        Args:
            client: The OpenAI-compatible client.
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.
        """
        messages: list[dict] = list(build_messages(context, native_tools=True))
        tools = build_tool_schemas(context)
        trace: list[DecisionRetryNote] = []
        tally = _UsageTally()  # every attempt costs tokens; the decision reports the sum
        last_error: DecisionParseError | None = None
        for _ in range(self.max_parse_retries + 1):
            response = client.chat.completions.create(**self._native_request(messages, tools))
            decision, messages, last_error = self._step_native(response, messages, trace, tally)
            if decision is not None:
                return decision
        return self._raise_no_decision(last_error, tally)  # NoReturn — always raises

    async def _adecide_native(self, client: Any, context: ContextPacket) -> ModelDecision:
        """One decision over the native tool-calling transport — async driver (ADR-0024).

        Args:
            client: The async OpenAI-compatible client.
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.
        """
        messages: list[dict] = list(build_messages(context, native_tools=True))
        tools = build_tool_schemas(context)
        trace: list[DecisionRetryNote] = []
        tally = _UsageTally()
        last_error: DecisionParseError | None = None
        for _ in range(self.max_parse_retries + 1):
            response = await client.chat.completions.create(**self._native_request(messages, tools))
            decision, messages, last_error = self._step_native(response, messages, trace, tally)
            if decision is not None:
                return decision
        return self._raise_no_decision(last_error, tally)  # NoReturn — always raises

    def _json_request(self, messages: list[dict]) -> dict:
        """The `chat.completions.create` kwargs for one JSON-transport attempt.

        Args:
            messages: The conversation so far (grows by a pair on each retry).

        Returns:
            The keyword arguments for the create call.
        """
        return {
            "model": self.config.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": self.config.temperature,
        }

    def _step_json(
        self,
        response: Any,
        messages: list[dict],
        trace: list[DecisionRetryNote],
        tally: _UsageTally,
    ) -> tuple[ModelDecision | None, list[dict], DecisionParseError | None]:
        """Process one JSON-transport response (pure; no I/O).

        A malformed attempt is annotated onto `trace` (not swallowed — the runner records
        each note as evidence and journals it, so a truncated-patch attempt stays visible)
        and returned as `decision=None` with the retry conversation folded into `messages`.

        Args:
            response: The provider response for this attempt.
            messages: The conversation that produced it.
            trace: The retry-note accumulator (appended in place on a malformed attempt).
            tally: The token-usage accumulator (added to in place).

        Returns:
            `(decision, messages, error)`: a validated decision (then `error` is `None`),
            or `(None, retry_messages, error)` to retry.
        """
        tally.add(response)
        raw = response.choices[0].message.content or ""
        try:
            decision = parse_decision(raw)
        except DecisionParseError as exc:
            trace.append(DecisionRetryNote(error=str(exc), raw=_excerpt(raw, patch=_carries_patch(raw))))
            retry = (
                f"That was not a valid decision ({exc}). Re-send the SAME intended "
                "action as one valid JSON decision — do not switch to a different action."
            )
            messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {"role": "user", "content": retry},
            ]
            return None, messages, exc
        decision.retry_trace = trace
        decision.usage = tally.total()
        decision.transport = "json"  # native disabled (the legacy escape hatch)
        return decision, messages, None

    def _decide_json(self, client: Any, context: ContextPacket) -> ModelDecision:
        """One decision over the legacy single-JSON-object protocol — sync driver.

        Args:
            client: The OpenAI-compatible client.
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.
        """
        messages: list[dict] = list(build_messages(context))
        trace: list[DecisionRetryNote] = []
        tally = _UsageTally()
        last_error: DecisionParseError | None = None
        for _ in range(self.max_parse_retries + 1):
            response = client.chat.completions.create(**self._json_request(messages))
            decision, messages, last_error = self._step_json(response, messages, trace, tally)
            if decision is not None:
                return decision
        return self._raise_no_decision(last_error, tally)  # NoReturn — always raises

    async def _adecide_json(self, client: Any, context: ContextPacket) -> ModelDecision:
        """One decision over the legacy single-JSON-object protocol — async driver (ADR-0024).

        Args:
            client: The async OpenAI-compatible client.
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.
        """
        messages: list[dict] = list(build_messages(context))
        trace: list[DecisionRetryNote] = []
        tally = _UsageTally()
        last_error: DecisionParseError | None = None
        for _ in range(self.max_parse_retries + 1):
            response = await client.chat.completions.create(**self._json_request(messages))
            decision, messages, last_error = self._step_json(response, messages, trace, tally)
            if decision is not None:
                return decision
        return self._raise_no_decision(last_error, tally)  # NoReturn — always raises
