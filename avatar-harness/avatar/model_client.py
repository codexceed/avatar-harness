"""Model decision protocol: constrained, validated decisions (§6).

The model returns one of three actions, never arbitrary prose. The harness
validates every decision before acting; a malformed decision is a *recoverable*
error fed back to the model, never executed and never fatal.

`parse_decision` is the pure validation boundary (no network), so it — and the
fakes that stand in for a real client in tests — are trivially testable.
"""

import asyncio
import contextlib
import json
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
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
    # Transport-layer failures recovered from before this decision succeeded (ADR-0028 R3/R4):
    # one message per re-issued attempt. Harness-owned like `retry_trace`, but UNLIKE it these are
    # NOT fed back to the model (a dead/NUL provider reply isn't model-correctable, §16) — the
    # runner only journals them as `transport_retry` events so a flaky provider stays visible.
    transport_trace: list[str] = Field(default_factory=list)
    # Set on the turn a session drops streaming→non-streaming (ADR-0029 R5): the capability reason.
    # Harness-owned like `transport_trace`, NOT fed back to the model — the runner journals it as a
    # `streaming_fallback` event so an eval can tell whether streaming was actually exercised.
    streaming_fallback: str = ""
    usage: DecisionUsage | None = None  # harness-owned, like retry_trace; set by the client
    # Which transport produced this decision: "native_stream" (streamed function-calling, R5),
    # "native" (non-streamed function-calling), "json_fallback" (native asked, endpoint ignored
    # tools= → legacy parse), or "json" (native disabled). Harness-owned; journaled so a silent
    # native↔stream↔JSON flip — a run-to-run consistency hazard — stays visible.
    transport: str = ""


# Per-attempt excerpt cap; edits get a higher cap so a failed `str_replace`/`write_file` keeps its
# payload for retry (a flat cap cut edits mid-body → the model retried blind, re-emitting the error).
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


class TransportError(Exception):
    """A model call failed at the *transport* layer — NOT model-correctable (§16, ADR-0028).

    A request timeout, connection reset, or an empty/NUL body (`EmptyResponseError`) means the
    provider returned nothing usable. Unlike `DecisionParseError`, this is **never** fed back to
    the model: it is retried in-client at the transport layer (re-issue the same request with
    backoff), and on exhaustion surfaced to the runner as a system failure. Carries `usage` so
    the billed-but-lost attempts are not undercounted.

    Args:
        message: A short description of the transport failure.
        usage: Tokens spent across the failed attempts, or `None` if unreported.
    """

    def __init__(self, message: str = "", usage: "DecisionUsage | None" = None) -> None:
        super().__init__(message)
        self.usage = usage


class EmptyResponseError(TransportError):
    """The provider returned an empty / whitespace-only / all-NUL body (a 200 with no content).

    The OpenAI SDK does not retry this (it is a *successful* HTTP response), so it must be caught
    explicitly and treated as a transport failure — not routed into the model parse-retry, which
    would re-prompt the model for what is really a dead/stalled provider reply (ADR-0028 R2).
    """


class StreamingUnsupportedError(Exception):
    """The provider can't stream tool-calls — trip the per-instance flag and fall back (ADR-0029 D4).

    Deliberately NOT a `TransportError`: re-issuing the same streaming request would fail
    identically, so it must not flow into the transport-retry. The client catches it once, flips
    `_streaming_unsupported`, and re-issues the SAME request non-streaming for the rest of the
    session. A capability verdict (a streaming-rejection 4xx, or unusable tool-call framing), never
    a transient fault — when in doubt the discrimination defaults to `TransportError` (ADR-0029 D).
    """


def _is_empty_body(raw: str) -> bool:
    """Whether a content body is effectively empty: blank, whitespace-only, or all-NUL.

    Args:
        raw: The model reply's content string.

    Returns:
        ``True`` when nothing remains after stripping NUL bytes and whitespace — the signature of
        the 2026-06-20 provider hang (a NUL-byte body), distinct from a non-empty malformed reply.
    """
    return not raw.replace("\x00", "").strip()


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
    # Harness-owned channels: a model emitting these must not plant fake history or usage.
    decision.retry_trace = []
    decision.transport_trace = []
    decision.streaming_fallback = ""
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
        """Async entry point for one decision (ADR-0029 R5); defaults to offloading sync `decide`.

        `OpenAIModelClient` overrides this with a cancellable streaming path; every fake inherits
        this bridge unchanged — its sync `decide` runs in a worker thread (uncancellable mid-call,
        but fast and deterministic, so the runner's cancel-race still resolves promptly).

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
        "with str_replace (or write_file to create or rewrite a file). An external verifier runs "
        "real tests/lint on your diff — and if this repo has no test/lint setup for the harness to "
        "detect, you MUST first declare how your change will be verified via declare_verification "
        "(executing checks that fail on breakage) BEFORE you edit."
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

    def add_usage(self, usage: "DecisionUsage | None") -> None:
        """Fold an already-summarized `DecisionUsage` into the tally (for cross-attempt sums).

        Args:
            usage: A per-attempt usage total (e.g. from a recovered/exhausted transport attempt),
                or `None` when the endpoint reported none.
        """
        if usage is None:
            return
        self._prompt += usage.prompt_tokens
        self._completion += usage.completion_tokens
        self._seen = True

    def total(self) -> DecisionUsage | None:
        """The summed usage, or `None` when the endpoint never reported any.

        Returns:
            The tally as a `DecisionUsage`, or `None`.
        """
        if not self._seen:
            return None
        return DecisionUsage(prompt_tokens=self._prompt, completion_tokens=self._completion)


def _fold_stream_chunk(chunk: Any, content_parts: list[str], frags: dict[int, dict[str, Any]]) -> Any:
    """Fold one streamed completion chunk into the running reassembly state (ADR-0029 D1).

    Concatenates content deltas and accumulates tool-call deltas by `.index` (id/name captured once,
    arguments string-joined in arrival order). Mutates `content_parts`/`frags` in place; `frags`
    preserves first-seen index order (dict insertion order) for diagnostics on broken framing.

    Args:
        chunk: One streamed completion chunk.
        content_parts: The growing list of content delta pieces.
        frags: Tool-call fragments keyed by `.index` (`id`/`name`/`args`).

    Returns:
        The chunk's `usage` object if it carried one (the usage-only final chunk), else `None`.
    """
    choices = getattr(chunk, "choices", None) or []
    if choices:
        delta = getattr(choices[0], "delta", None)
        if delta is not None:
            if delta.content:
                content_parts.append(delta.content)
            for tc in delta.tool_calls or []:
                if tc.index not in frags:
                    frags[tc.index] = {"id": None, "name": None, "args": []}
                slot = frags[tc.index]  # dict[str, Any] from frags's annotation
                if tc.id:
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if fn.name:
                        slot["name"] = fn.name
                    if fn.arguments:
                        slot["args"].append(fn.arguments)
    return getattr(chunk, "usage", None)


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
        transport_max_retries: Transport-layer retries on a NUL/empty body or a request
            failure (ADR-0028 R3); `None` takes `config.transport_max_retries`.
        sleep: Backoff sleeper, injectable so tests exercise retries without real delay.
        aclient: An injected async OpenAI-compatible client (the ADR-0029 R5 streaming path),
            or `None` to build an `AsyncOpenAI` lazily on first `adecide()`.
        asleep: Async backoff sleeper, injectable so async tests skip real delay.
    """

    def __init__(  # noqa: PLR0913 — parallel sync + async injection slots (client/sleep) plus budgets
        self,
        config: HarnessConfig,
        client: Any = None,
        max_parse_retries: int = 2,
        *,
        transport_max_retries: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        aclient: Any = None,
        asleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.max_parse_retries = max_parse_retries
        # Transport-retry budget (ADR-0028 R3); falls back to config. `sleep`/`asleep` are
        # injectable so tests exercise the backoff path without real delay.
        self.transport_max_retries = (
            transport_max_retries if transport_max_retries is not None else config.transport_max_retries
        )
        self._sleep = sleep
        self._asleep = asleep
        # Built lazily on first decide()/adecide() (see _ensure_client / _aensure_client):
        # credentials are an inference-time concern, so a Harness with the default model is
        # constructible without an API key (and without the `openai` extra installed). The sync and
        # async SDK clients are separate stacks (`OpenAI` vs `AsyncOpenAI`) with independent slots.
        self._client = client
        self._aclient = aclient
        # Runtime, per-instance (NOT config): set once a provider proves it can't stream tool-calls,
        # so the rest of this client's session skips straight to the non-streaming path (ADR-0029 D4).
        self._streaming_unsupported = False

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
            # `timeout` (ADR-0028 R1) bounds a single call well under the wall clock so a hung
            # provider can't eat the whole run; `max_retries=0` because the transport-retry loop
            # owns retries (it also covers the SDK-invisible 200-with-empty-body case).
            self._client = OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                timeout=self.config.request_timeout_seconds,
                max_retries=0,
            )
        return self._client

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter for transport retry `attempt` (0-based).

        Args:
            attempt: The zero-based retry index.

        Returns:
            Seconds to sleep before the next attempt: ``2^attempt`` capped at 20s, plus up to
            1s of jitter to decorrelate a synchronized herd off one provider (ADR-0028 R3).
        """
        return min(2.0**attempt, 20.0) + random.uniform(0.0, 1.0)  # noqa: S311 — jitter, not crypto

    def _transport_retry(self, attempt_once: Callable[[], ModelDecision]) -> ModelDecision:
        """Run one decision attempt, retrying *transport* failures with backoff (ADR-0028 R3).

        `attempt_once` performs a full create+validate over a fresh copy of the messages, so a
        retry re-issues the SAME request — it never re-prompts the model (that is the parse-retry's
        job, and it stays inside `attempt_once`). `DecisionParseError` propagates unretried here.

        Args:
            attempt_once: A thunk that returns a validated decision or raises `TransportError`.

        Returns:
            The validated decision from the first successful attempt. Its `usage` is the sum
            across every attempt (failed ones included), and `transport_trace` lists the
            transport failures recovered from — so a lost-but-billed attempt is never undercounted.

        Raises:
            TransportError: When every attempt fails at the transport layer; its `usage` carries
                the summed cost of all attempts.
        """
        usage = _UsageTally()  # every attempt costs tokens; sum them across the whole loop
        recovered: list[str] = []
        last: TransportError | None = None
        for attempt in range(self.transport_max_retries + 1):
            try:
                decision = attempt_once()
            except TransportError as exc:
                last = exc
                usage.add_usage(exc.usage)
                recovered.append(str(exc))
                if attempt < self.transport_max_retries:
                    self._sleep(self._backoff(attempt))
                continue
            # Success: fold this attempt's usage into the running total and surface the recovered
            # transport failures so the runner can journal them (never fed back to the model).
            usage.add_usage(decision.usage)
            decision.usage = usage.total()
            decision.transport_trace = recovered
            return decision
        # Exhausted: surface the failure as a TransportError (§16) carrying the summed usage. The
        # loop only falls through via the except branch, which always sets `last` (the fallback
        # message is defensive).
        raise TransportError(
            str(last) if last else "model transport failed with no recorded error",
            usage=usage.total(),
        ) from last

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
        # Each transport-retry attempt rebuilds messages from `context` → re-issues the SAME request
        # (ADR-0028 R3); the parse-retry (re-prompting on malformed output) stays inside the attempt.
        if self.config.native_tool_calls:
            return self._transport_retry(lambda: self._decide_native(client, context))
        return self._transport_retry(lambda: self._decide_json(client, context))

    # ---- Async path (ADR-0029 R5): streaming + idle-timeout + mid-call cancellation ----

    def _aensure_client(self) -> Any:
        """Return the async OpenAI-compatible client, constructing it on first use.

        Returns:
            The injected `aclient`, or an `AsyncOpenAI` built from `config` on first call. The
            `timeout` is the non-streaming ceiling (`request_timeout_seconds`); the per-streaming-call
            idle bound is passed per call in `_acreate_stream`, so this one client serves both.

        Raises:
            ImportError: If no client was injected and the optional `openai` extra is not installed.
        """
        if self._aclient is None:
            try:
                from openai import AsyncOpenAI  # noqa: PLC0415 — lazy: `openai` is an optional extra
            except ImportError as exc:  # openai not installed — it is an optional extra
                raise ImportError(
                    "OpenAIModelClient requires the optional 'openai' extra. "
                    "Install it with `pip install avatar-harness[openai]` (or `uv sync --extra openai`), "
                    "or inject an `aclient` / use a custom ModelClient instead."
                ) from exc
            self._aclient = AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                timeout=self.config.request_timeout_seconds,
                max_retries=0,
            )
        return self._aclient

    async def adecide(self, context: ContextPacket) -> ModelDecision:
        """Call the endpoint asynchronously, streaming by default for idle-timeout + cancellation.

        Streams native tool-calls when enabled (ADR-0029 R5) so a stall is caught at the idle
        timeout regardless of generation length; a provider that can't stream trips
        `_streaming_unsupported` and the SAME request is re-issued non-streaming for the rest of the
        session (D4). Either path is cancellable mid-call (the runner races a cancel against it) and
        raises `TransportError`/`DecisionParseError` exactly like the sync `decide`.

        Args:
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.
        """
        client = self._aensure_client()
        stream = self.config.stream_model_calls and self.config.native_tool_calls
        fallback_reason = ""
        if stream and not self._streaming_unsupported:
            try:
                return await self._atransport_retry(lambda: self._adecide_native_stream(client, context))
            except StreamingUnsupportedError as exc:
                # Capability verdict, not a transient fault: flip the flag and fall through to the
                # non-streaming path with the SAME request (never fed back to the model). Record the
                # reason so the runner journals a `streaming_fallback` event (R5 observability).
                self._streaming_unsupported = True
                fallback_reason = str(exc)
        if self.config.native_tool_calls:
            decision = await self._atransport_retry(lambda: self._adecide_native_async(client, context))
        else:
            decision = await self._atransport_retry(lambda: self._adecide_json_async(client, context))
        decision.streaming_fallback = fallback_reason
        return decision

    async def _atransport_retry(self, attempt_once: Callable[[], Awaitable[ModelDecision]]) -> ModelDecision:
        """Async twin of `_transport_retry`: retry transport failures with backoff (ADR-0028 R3).

        Identical backoff/usage-summing/`transport_trace` semantics to the sync loop; only the sleep
        is awaited. Catches `TransportError` ONLY — `StreamingUnsupportedError` (a capability verdict,
        handled in `adecide`), `DecisionParseError`, and `CancelledError` all propagate.

        Args:
            attempt_once: A coroutine factory returning a validated decision or raising `TransportError`.

        Returns:
            The decision from the first successful attempt, its `usage` summed across attempts and
            `transport_trace` listing the transport failures recovered from.

        Raises:
            TransportError: When every attempt fails at the transport layer; carries the summed usage.
        """
        usage = _UsageTally()
        recovered: list[str] = []
        last: TransportError | None = None
        for attempt in range(self.transport_max_retries + 1):
            try:
                decision = await attempt_once()
            except TransportError as exc:
                last = exc
                usage.add_usage(exc.usage)
                recovered.append(str(exc))
                if attempt < self.transport_max_retries:
                    await self._asleep(self._backoff(attempt))
                continue
            usage.add_usage(decision.usage)
            decision.usage = usage.total()
            decision.transport_trace = recovered
            return decision
        raise TransportError(
            str(last) if last else "model transport failed with no recorded error",
            usage=usage.total(),
        ) from last

    async def _acreate(self, client: Any, **kwargs: Any) -> Any:
        """Async twin of `_create`: one `chat.completions.create`, failures → `TransportError`.

        Args:
            client: The async OpenAI-compatible client.
            **kwargs: Call arguments (`messages`, `tools`/`response_format`).

        Returns:
            The raw provider response.

        Raises:
            TransportError: When the underlying call raises (network/timeout/5xx).
        """
        try:
            return await client.chat.completions.create(
                model=self.config.model, temperature=self.config.temperature, **kwargs
            )
        except Exception as exc:  # any create() failure is a transport failure (§16)
            raise TransportError(f"model request failed: {type(exc).__name__}: {exc}") from exc

    async def _acreate_stream(self, client: Any, **kwargs: Any) -> Any:
        """Open a streaming completion with the idle bound as the per-call httpx `read` timeout.

        The idle timeout is passed PER CALL (not on the client), so the same async client serves the
        non-streaming fallback at the looser `request_timeout_seconds` ceiling. A silent socket then
        raises mid-stream (`ReadTimeout`) after `request_idle_timeout_seconds` — the fast stall signal.
        A call failure is classified by `_raise_stream_fault` into `StreamingUnsupportedError`
        (provider rejected streaming) or `TransportError` (any other, transient fault).

        Args:
            client: The async OpenAI-compatible client.
            **kwargs: Call arguments (`messages`, `tools`).

        Returns:
            The async stream of completion chunks.
        """
        # A plain float `timeout` is applied by the SDK as the httpx per-operation timeout — crucially
        # the per-READ timeout, which bounds the gap between streamed chunks (the idle watchdog). Passed
        # per call so the shared client's looser non-streaming ceiling is untouched. No httpx import:
        # keeping httpx an indirect (openai-provided) dep, and a float says exactly "idle bound".
        try:
            return await client.chat.completions.create(
                model=self.config.model,
                temperature=self.config.temperature,
                stream=True,
                stream_options={"include_usage": True},
                timeout=self.config.request_idle_timeout_seconds,
                **kwargs,
            )
        except Exception as exc:
            self._raise_stream_fault(exc)

    def _raise_stream_fault(self, exc: Exception) -> NoReturn:
        """Raise a streaming failure as a capability verdict vs a transient fault (ADR-0029 D).

        Capability → `StreamingUnsupportedError` (flip the flag, fall back): a streaming-rejection
        4xx (400/404/422) whose message names streaming as unsupported. Everything else (idle
        `ReadTimeout`, connection error, 429, 5xx, generic 4xx) → `TransportError` — default to
        transient when in doubt, so a flaky provider is retried rather than wrongly downgraded.
        Callers filter already-classified errors before delegating here, so there is no passthrough.
        `exc` is chained as the raised error's ``__cause__``.

        Args:
            exc: The exception raised while opening or consuming the stream.

        Raises:
            StreamingUnsupportedError: When the provider rejects streaming as unsupported (D4).
            TransportError: For any other (transient) stream fault.
        """
        try:
            from openai import APIStatusError  # noqa: PLC0415 — lazy: `openai` is an optional extra
        except ImportError:
            APIStatusError = ()  # type: ignore[assignment,misc] # noqa: N806 — it is a class
        if isinstance(exc, APIStatusError) and getattr(exc, "status_code", None) in (400, 404, 422):
            text = str(getattr(exc, "message", "") or exc).lower()
            markers = ("not supported", "unsupported", "not allowed", "does not support")
            if "stream" in text and any(m in text for m in markers):
                raise StreamingUnsupportedError(f"provider rejected streaming: {exc}") from exc
        raise TransportError(f"model request failed: {type(exc).__name__}: {exc}") from exc

    async def _areassemble(self, client: Any, **create_kwargs: Any) -> tuple[Any, Any]:
        """Consume a streamed completion into a non-streaming-shaped message + usage (ADR-0029 D1).

        Reassembles per the OpenAI streaming format: concatenate `content` deltas; key tool-call
        deltas strictly on `.index`, capturing `.id`/`.function.name` on the first fragment and
        string-joining `.function.arguments` fragments in arrival order; the usage-only final chunk
        has `choices == []`. Only index 0 is used (one action per turn, §6). The stream is always
        closed in `finally` — load-bearing on cancel, to release the aborted connection.

        Args:
            client: The async OpenAI-compatible client.
            **create_kwargs: `messages`/`tools` for the streaming call.

        Returns:
            A `(message, usage)` pair: `message` has `.content`/`.tool_calls` shaped like a
            non-streaming reply; `usage` is the final chunk's usage object or `None`.

        Raises:
            StreamingUnsupportedError: On unusable framing (no index-0 fragment, or a missing
                name/id) — a capability verdict that triggers the non-streaming fallback (D4).
            TransportError: On an idle `ReadTimeout` or other transient stream fault.
        """
        stream = await self._acreate_stream(client, **create_kwargs)
        content_parts: list[str] = []
        frags: dict[int, dict[str, Any]] = {}
        usage_obj: Any = None
        try:
            async for chunk in stream:
                usage_obj = _fold_stream_chunk(chunk, content_parts, frags) or usage_obj
        except (TransportError, StreamingUnsupportedError):
            raise
        except Exception as exc:  # a mid-stream fault (idle ReadTimeout, reset) is transport-shaped
            self._raise_stream_fault(exc)
        finally:
            # Close best-effort: a close() that raises must NOT mask a propagating CancelledError
            # (BaseException, so suppress(Exception) lets it through) or the real stream fault above.
            with contextlib.suppress(Exception):
                await stream.close()
        content = "".join(content_parts)
        if not frags:
            return SimpleNamespace(content=content, tool_calls=None), usage_obj
        if 0 not in frags:  # broken framing — a capability problem, not a parse one
            raise StreamingUnsupportedError(
                f"streamed tool-call framing missing index 0 (indices={list(frags)})"
            )
        slot = frags[0]
        if not slot["name"] or not slot["id"]:
            raise StreamingUnsupportedError(
                f"streamed tool-call missing name/id (name={slot['name']!r} id={slot['id']!r})"
            )
        call = SimpleNamespace(
            id=slot["id"],
            function=SimpleNamespace(name=slot["name"], arguments="".join(slot["args"]) or "{}"),
        )
        return SimpleNamespace(content=content, tool_calls=[call]), usage_obj

    def _native_decision_from_message(
        self, message: Any, messages: list[dict], trace: list[DecisionRetryNote], tally: _UsageTally
    ) -> tuple[ModelDecision | None, list[dict], DecisionParseError | None]:
        """Empty-check + map a native reply to a decision, or extend `messages` to re-prompt (§6/§18).

        Shared by every native path (sync, async non-streaming, and the reassembled stream — same
        message shape). Sets `.transport`; the caller owns the tally/trace.

        Args:
            message: The reply message (real or reassembled): `.content`, `.tool_calls`.
            messages: The conversation so far.
            trace: The in-client retry trace; a malformed attempt is appended here.
            tally: The running usage tally, billed onto an `EmptyResponseError`.

        Returns:
            `(decision, messages, None)` on success, or `(None, extended, error)` on a malformed
            attempt (conversation extended to re-prompt the SAME action).

        Raises:
            EmptyResponseError: On an empty/NUL body with no tool call — a transport failure, NOT a
                parse retry (re-issued, not re-prompted); a tool call takes precedence (ADR-0028 R2).
        """
        calls = getattr(message, "tool_calls", None)
        raw_content = message.content or ""
        if not calls and _is_empty_body(raw_content):
            raise EmptyResponseError(f"empty model reply ({len(raw_content)} chars)", usage=tally.total())
        if calls:
            call = calls[0]  # one action per turn (§6)
            try:
                decision = _decision_from_tool_call(call, thought=raw_content)
            except DecisionParseError as exc:
                raw = f"{call.function.name}({call.function.arguments or ''})"
                is_patch = call.function.name in ("str_replace", "write_file")
                trace.append(DecisionRetryNote(error=str(exc), raw=_excerpt(raw, patch=is_patch)))
                extended = [
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
                return None, extended, exc
            decision.transport = "native"
            return decision, messages, None
        try:
            decision = parse_decision(raw_content)  # endpoint ignored tools= — legacy fallback
        except DecisionParseError as exc:
            trace.append(
                DecisionRetryNote(
                    error=str(exc), raw=_excerpt(raw_content, patch=_carries_patch(raw_content))
                )
            )
            retry = (
                f"That was not a valid decision ({exc}). Call one of the provided tools "
                "— re-send the SAME intended action, do not switch to a different one."
            )
            extended = [
                *messages,
                {"role": "assistant", "content": raw_content},
                {"role": "user", "content": retry},
            ]
            return None, extended, exc
        decision.transport = "json_fallback"  # native asked, endpoint answered in prose
        return decision, messages, None

    def _json_decision_from_message(
        self, message: Any, messages: list[dict], trace: list[DecisionRetryNote], tally: _UsageTally
    ) -> tuple[ModelDecision | None, list[dict], DecisionParseError | None]:
        """Empty-check + map a legacy-JSON reply to a decision, or extend `messages` to re-prompt (§6).

        Args:
            message: The reply message (`.content` carries the JSON object).
            messages: The conversation so far.
            trace: The in-client retry trace; a malformed attempt is appended here.
            tally: The running usage tally, billed onto an `EmptyResponseError`.

        Returns:
            `(decision, messages, None)` on success, or `(None, extended, error)` on a malformed attempt.

        Raises:
            EmptyResponseError: On an empty/NUL body (a transport failure, ADR-0028 R2).
        """
        raw = message.content or ""
        if _is_empty_body(raw):
            raise EmptyResponseError(f"empty model reply ({len(raw)} chars)", usage=tally.total())
        try:
            decision = parse_decision(raw)
        except DecisionParseError as exc:
            trace.append(DecisionRetryNote(error=str(exc), raw=_excerpt(raw, patch=_carries_patch(raw))))
            retry = (
                f"That was not a valid decision ({exc}). Re-send the SAME intended "
                "action as one valid JSON decision — do not switch to a different action."
            )
            extended = [*messages, {"role": "assistant", "content": raw}, {"role": "user", "content": retry}]
            return None, extended, exc
        decision.transport = "json"  # native disabled (the legacy escape hatch)
        return decision, messages, None

    def _fetch_sync(self, client: Any, messages: list[dict], **create_kwargs: Any) -> tuple[Any, Any]:
        """Issue one sync create; return its reply message and the response (usage carrier).

        Args:
            client: The OpenAI-compatible client.
            messages: The conversation for this attempt.
            **create_kwargs: `tools` or `response_format`.

        Returns:
            A `(message, usage_carrier)` pair for the parse-retry loop.
        """
        response = self._create(client, messages=messages, **create_kwargs)
        return response.choices[0].message, response

    async def _afetch(self, client: Any, messages: list[dict], **create_kwargs: Any) -> tuple[Any, Any]:
        """Async non-streaming twin of `_fetch_sync`.

        Args:
            client: The async OpenAI-compatible client.
            messages: The conversation for this attempt.
            **create_kwargs: `tools` or `response_format`.

        Returns:
            A `(message, usage_carrier)` pair for the parse-retry loop.
        """
        response = await self._acreate(client, messages=messages, **create_kwargs)
        return response.choices[0].message, response

    async def _afetch_stream(
        self, client: Any, messages: list[dict], **create_kwargs: Any
    ) -> tuple[Any, Any]:
        """Stream + reassemble one reply (ADR-0029 R5); return the message and a usage carrier.

        Args:
            client: The async OpenAI-compatible client.
            messages: The conversation for this attempt.
            **create_kwargs: `tools` for the streaming call.

        Returns:
            A `(message, usage_carrier)` pair for the parse-retry loop.
        """
        message, usage_obj = await self._areassemble(client, messages=messages, **create_kwargs)
        return message, SimpleNamespace(usage=usage_obj)

    def _parse_retry(
        self,
        messages: list[dict],
        fetch: Callable[..., Any],
        handle: Callable[..., Any],
        client: Any,
        create_kwargs: dict[str, Any],
    ) -> ModelDecision:
        """Sync parse-retry loop: fetch a reply, map it, re-prompt malformed attempts (§6, ADR-0028 R2).

        Each attempt re-issues the SAME request; `usage` sums across attempts; `handle` raises
        `EmptyResponseError` (transport) on an empty body and re-prompts a malformed one.

        Args:
            messages: The initial conversation (rebuilt on each malformed attempt).
            fetch: `fetch(client, messages, **create_kwargs) -> (message, usage_carrier)`.
            handle: `handle(message, messages, trace, tally) -> (decision|None, messages, error)`.
            client: The OpenAI-compatible client passed to `fetch`.
            create_kwargs: `tools`/`response_format` passed to `fetch`.

        Returns:
            The validated decision (with `retry_trace`/`usage` set).

        Raises:
            DecisionParseError: When every attempt is malformed (an empty body raises
                `EmptyResponseError` from `handle`, propagating as a transport failure).
        """
        trace: list[DecisionRetryNote] = []
        tally = _UsageTally()
        last_error: DecisionParseError | None = None
        for _ in range(self.max_parse_retries + 1):
            message, usage_carrier = fetch(client, messages, **create_kwargs)
            tally.add(usage_carrier)
            decision, messages, last_error = handle(message, messages, trace, tally)
            if decision is not None:
                decision.retry_trace = trace
                decision.usage = tally.total()
                return decision
        raise DecisionParseError(
            str(last_error) if last_error else "model returned no valid decision", usage=tally.total()
        ) from last_error

    async def _aparse_retry(
        self,
        messages: list[dict],
        fetch: Callable[..., Any],
        handle: Callable[..., Any],
        client: Any,
        create_kwargs: dict[str, Any],
    ) -> ModelDecision:
        """Async twin of `_parse_retry` (only the fetch is awaited).

        Args:
            messages: The initial conversation (rebuilt on each malformed attempt).
            fetch: Awaitable `fetch(client, messages, **create_kwargs) -> (message, usage_carrier)`.
            handle: `handle(message, messages, trace, tally) -> (decision|None, messages, error)`.
            client: The async OpenAI-compatible client passed to `fetch`.
            create_kwargs: `tools`/`response_format` passed to `fetch`.

        Returns:
            The validated decision (with `retry_trace`/`usage` set).

        Raises:
            DecisionParseError: When every attempt is malformed (an empty body raises
                `EmptyResponseError` from `handle`, propagating as a transport failure).
        """
        trace: list[DecisionRetryNote] = []
        tally = _UsageTally()
        last_error: DecisionParseError | None = None
        for _ in range(self.max_parse_retries + 1):
            message, usage_carrier = await fetch(client, messages, **create_kwargs)
            tally.add(usage_carrier)
            decision, messages, last_error = handle(message, messages, trace, tally)
            if decision is not None:
                decision.retry_trace = trace
                decision.usage = tally.total()
                return decision
        raise DecisionParseError(
            str(last_error) if last_error else "model returned no valid decision", usage=tally.total()
        ) from last_error

    async def _adecide_native_stream(self, client: Any, context: ContextPacket) -> ModelDecision:
        """One streamed native decision (ADR-0029 R5): reassemble, then reuse the §6 parse path.

        Args:
            client: The async OpenAI-compatible client.
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.
        """
        decision = await self._aparse_retry(
            list(build_messages(context, native_tools=True)),
            self._afetch_stream,
            self._native_decision_from_message,
            client,
            {"tools": build_tool_schemas(context)},
        )
        # Distinguish a STREAMED native decision from a non-streamed one so the journal (which
        # records `transport` per turn) shows whether streaming was actually exercised (R5).
        if decision.transport == "native":
            decision.transport = "native_stream"
        return decision

    async def _adecide_native_async(self, client: Any, context: ContextPacket) -> ModelDecision:
        """One non-streaming native decision over the async client (the D4 streaming fallback).

        Args:
            client: The async OpenAI-compatible client.
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.
        """
        return await self._aparse_retry(
            list(build_messages(context, native_tools=True)),
            self._afetch,
            self._native_decision_from_message,
            client,
            {"tools": build_tool_schemas(context)},
        )

    async def _adecide_json_async(self, client: Any, context: ContextPacket) -> ModelDecision:
        """One non-streaming legacy-JSON decision over the async client (native disabled).

        Args:
            client: The async OpenAI-compatible client.
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.
        """
        return await self._aparse_retry(
            build_messages(context),
            self._afetch,
            self._json_decision_from_message,
            client,
            {"response_format": {"type": "json_object"}},
        )

    def _create(self, client: Any, **kwargs: Any) -> Any:
        """Issue one `chat.completions.create`, mapping a call failure to `TransportError`.

        A timeout, connection reset, or 5xx surfaces from the SDK as an exception (its own retries
        are disabled — `_ensure_client` sets `max_retries=0`); we re-raise it as a `TransportError`
        so the transport-retry loop handles it uniformly with the empty-body case (ADR-0028 R3).

        Args:
            client: The OpenAI-compatible client.
            **kwargs: Call arguments (`messages`, `tools`/`response_format`).

        Returns:
            The raw provider response.

        Raises:
            TransportError: When the underlying call raises (network/timeout/5xx).
        """
        try:
            return client.chat.completions.create(
                model=self.config.model, temperature=self.config.temperature, **kwargs
            )
        except Exception as exc:  # any create() failure is a transport failure (§16)
            raise TransportError(f"model request failed: {type(exc).__name__}: {exc}") from exc

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
        """
        return self._parse_retry(
            list(build_messages(context, native_tools=True)),
            self._fetch_sync,
            self._native_decision_from_message,
            client,
            {"tools": build_tool_schemas(context)},
        )

    def _decide_json(self, client: Any, context: ContextPacket) -> ModelDecision:
        """One decision over the legacy single-JSON-object protocol (the escape hatch).

        Args:
            client: The OpenAI-compatible client.
            context: The assembled context packet.

        Returns:
            The validated decision for the current turn.
        """
        return self._parse_retry(
            build_messages(context),
            self._fetch_sync,
            self._json_decision_from_message,
            client,
            {"response_format": {"type": "json_object"}},
        )
