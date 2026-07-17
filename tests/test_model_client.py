import asyncio
import importlib
import importlib.util
import sys
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError

from avatar.config import HarnessConfig
from avatar.context import ContextBuilder, ContextPacket, ToolSummary
from avatar.deps import CancellationToken, RunDeps
from avatar.events import Emitter
from avatar.model_client import (
    DecisionParseError,
    EmptyResponseError,
    FinalAnswer,
    ModelClient,
    ModelDecision,
    OpenAIModelClient,
    StreamingUnsupportedError,
    ToolCall,
    TransportError,
    _is_empty_body,
    build_messages,
    parse_decision,
)
from avatar.runner import AgentRunner
from avatar.state import ConversationTurn, TaskState
from avatar.verifier import Verifier
from avatar.workspace import Workspace


def test_parses_tool_call_decision():
    raw = (
        '{"thought_summary": "inspect first",'
        ' "action": {"type": "tool_call", "name": "search_repo",'
        ' "input": {"query": "test_auth"}}}'
    )
    decision = parse_decision(raw)
    assert isinstance(decision.action, ToolCall)
    assert decision.action.name == "search_repo"
    assert decision.action.input == {"query": "test_auth"}


def test_parses_final_answer_decision():
    raw = '{"thought_summary": "done", "action": {"type": "final_answer", "answer": "the bug is X"}}'
    decision = parse_decision(raw)
    assert isinstance(decision.action, FinalAnswer)
    assert decision.action.answer == "the bug is X"


def test_malformed_decision_is_recoverable():
    with pytest.raises(DecisionParseError):
        parse_decision("{not valid json")
    with pytest.raises(DecisionParseError):
        parse_decision('{"action": {"type": "bogus_action"}}')  # unknown discriminator


def _fake_openai(content: str, captured: dict):
    def create(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _fake_openai_seq(contents: list[str], captured: list[dict] | None = None):
    """A transport returning each reply in `contents` in turn (for retry-path tests)."""
    replies = iter(contents)

    def create(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        message = SimpleNamespace(content=next(replies))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _msg(content: str | None = None, tool_calls: list | None = None):
    """One provider reply message (native tool-calling shape)."""
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _tc(name: str, arguments: str, call_id: str = "call_1"):
    """One provider tool call."""
    return SimpleNamespace(
        id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments)
    )


def _fake_openai_messages(messages: list, captured: list[dict] | None = None):
    """A transport returning each prebuilt reply *message* in turn."""
    replies = iter(messages)

    def create(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=next(replies))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def _no_asleep(_seconds: float) -> None:
    """An async no-op sleeper so transport-retry backoff doesn't actually wait in tests."""


def _afake_openai_messages(messages: list, captured: list[dict] | None = None):
    """Async non-streaming transport: returns each prebuilt reply *message* in turn (ADR-0029)."""
    replies = iter(messages)

    async def create(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=next(replies))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class _AsyncStream:
    """A minimal async stand-in for the OpenAI `AsyncStream`: `async for` chunks + `aclose`/`close`."""

    def __init__(self, chunks: list, *, fail=None) -> None:
        self._chunks = chunks
        self._fail = fail  # an exception instance to raise mid-iteration (an idle stall, say)
        self.closed = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for chunk in self._chunks:
            yield chunk
        if self._fail is not None:
            raise self._fail

    async def close(self) -> None:
        self.closed = True


def _chunk(*, content: str | None = None, tool_calls: list | None = None, usage=None):
    """One streamed completion chunk (a `choices[0].delta` + optional usage-only tail)."""
    if content is None and tool_calls is None:
        return SimpleNamespace(choices=[], usage=usage)  # usage-only final chunk
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=usage)


def _tc_delta(index: int, *, call_id=None, name=None, arguments=None):
    """One streamed tool-call delta fragment (id/name once, arguments in pieces)."""
    fn = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=fn)


def _afake_openai_streams(streams: list, captured: list[dict] | None = None):
    """Async streaming transport: returns each `_AsyncStream` in turn from a `stream=True` create."""
    it = iter(streams)

    async def create(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        return next(it)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _packet(**overrides) -> ContextPacket:
    base = {
        "goal": "add a retry to the client",
        "phase": "investigating",
        "task_kind": "edit",
        "allowed_tools": [
            ToolSummary(
                name="read_file",
                description="read a file",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ],
    }
    base.update(overrides)
    return ContextPacket(**base)


def test_openai_client_builds_request_and_parses():
    captured: dict = {}
    content = (
        '{"thought_summary": "found it",'
        ' "action": {"type": "final_answer", "answer": "the bug is in app.py"}}'
    )
    client = _fake_openai(content, captured)
    config = HarnessConfig(model="unit-test-model")
    packet = ContextPacket(
        goal="where is the bug?",
        phase="investigating",
        allowed_tools=[ToolSummary(name="read_file", description="read a file")],
    )

    decision = OpenAIModelClient(config, client=client).decide(packet)

    assert isinstance(decision.action, FinalAnswer)
    assert decision.action.answer == "the bug is in app.py"
    assert captured["model"] == "unit-test-model"
    blob = " ".join(m["content"] for m in captured["messages"])
    assert "where is the bug?" in blob  # the goal reached the prompt
    # The allowed tool is advertised as a function schema (native mode, ADR-0003 A) —
    # no longer as prose inside the system message.
    assert "read_file" in [t["function"]["name"] for t in captured["tools"]]


# --- native tool-calling (ADR-0003 Option A) ----------------------------------------------
#
# The decision rides the provider's function-calling channel: tool schemas go up as
# `tools=`, the chosen action comes back as a structured tool call — the provider owns
# the JSON envelope/escaping the model used to hand-write (the dogfood failure mode).
# `final_answer`/`ask_user` are functions too; a content-only reply (an endpoint that
# ignores `tools=`) falls back to `parse_decision`; AVATAR_NATIVE_TOOL_CALLS=false
# restores the legacy json_object protocol verbatim.


def test_native_mode_sends_tool_schemas():
    captured: list[dict] = []
    reply = _msg(tool_calls=[_tc("read_file", '{"path": "app.py"}')])
    client = OpenAIModelClient(HarnessConfig(model="m"), client=_fake_openai_messages([reply], captured))

    client.decide(_packet())

    sent = captured[0]
    assert "response_format" not in sent  # the JSON-envelope protocol is gone in native mode
    names = [t["function"]["name"] for t in sent["tools"]]
    assert "read_file" in names  # registry tools advertised as functions...
    assert "final_answer" in names and "ask_user" in names  # ...and the decision actions too
    read_file_schema = next(
        t["function"]["parameters"] for t in sent["tools"] if t["function"]["name"] == "read_file"
    )
    assert read_file_schema["properties"] == {"path": {"type": "string"}}  # real input schema, not prose


def test_native_tool_call_reply_parses_to_tool_call():
    reply = _msg(content="inspecting first", tool_calls=[_tc("read_file", '{"path": "app.py"}')])
    client = OpenAIModelClient(HarnessConfig(model="m"), client=_fake_openai_messages([reply]))

    decision = client.decide(_packet())

    assert isinstance(decision.action, ToolCall)
    assert decision.action.name == "read_file"
    assert decision.action.input == {"path": "app.py"}
    assert decision.thought_summary == "inspecting first"  # prose alongside the call is the thought


def test_native_final_answer_function_parses():
    reply = _msg(tool_calls=[_tc("final_answer", '{"answer": "the bug is in app.py:3"}')])
    client = OpenAIModelClient(HarnessConfig(model="m"), client=_fake_openai_messages([reply]))

    decision = client.decide(_packet())

    assert isinstance(decision.action, FinalAnswer)
    assert decision.action.answer == "the bug is in app.py:3"


def test_native_malformed_arguments_retry_pairs_tool_messages():
    # Bad arguments are retried in-conversation with valid §18 pairing — the assistant's
    # tool call answered by a role="tool" message with the matching tool_call_id — and
    # the failed attempt lands on the retry trace like any other malformed decision.
    bad = _msg(tool_calls=[_tc("read_file", '{"path": ', call_id="c9")])  # truncated args JSON
    good = _msg(tool_calls=[_tc("read_file", '{"path": "app.py"}')])
    captured: list[dict] = []
    client = OpenAIModelClient(HarnessConfig(model="m"), client=_fake_openai_messages([bad, good], captured))

    decision = client.decide(_packet())

    assert isinstance(decision.action, ToolCall)
    assert len(decision.retry_trace) == 1
    assert "read_file" in decision.retry_trace[0].raw or "path" in decision.retry_trace[0].raw
    retry_messages = captured[1]["messages"]
    tool_replies = [m for m in retry_messages if m.get("role") == "tool"]
    assert tool_replies and tool_replies[0]["tool_call_id"] == "c9"  # §18: every call answered
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in retry_messages)


# --- transport-layer retry: NUL/hung provider replies (ADR-0028) --------------------------
#
# A provider hang returns a NUL / empty body (a 200 with no content). That is a TRANSPORT
# failure, not a malformed *decision*: it must be re-ISSUED (same request, backoff), never
# re-prompted through the model parse-retry, and on exhaustion surfaced as a system failure
# (§16) — never a silent one-turn `incomplete`. Raw: eval_run_20260620T142752Z.


def _nul_reply():
    """A provider hang's signature: a NUL-byte body with no tool call."""
    return _msg(content="\x00")


def test_empty_nul_body_is_transport_retried_not_reprompted():
    captured: list[dict] = []
    good = _msg(tool_calls=[_tc("read_file", '{"path": "app.py"}')])
    client = OpenAIModelClient(
        HarnessConfig(model="m"),
        client=_fake_openai_messages([_nul_reply(), good], captured),
        sleep=lambda _s: None,
    )

    decision = client.decide(_packet())

    assert isinstance(decision.action, ToolCall)  # recovered to the valid reply
    assert decision.action.name == "read_file"
    assert len(captured) == 2  # exactly one retry
    # Re-ISSUE, not re-prompt: the retry's messages are identical to the first request — no
    # appended assistant/user correction turn (that is the parse-retry's behavior, asserted below).
    assert captured[0]["messages"] == captured[1]["messages"]
    assert decision.retry_trace == []  # a transport retry leaves no parse trace


def test_nonempty_malformed_still_uses_parse_retry_not_transport():
    # The OTHER branch must stay intact: a non-empty but malformed body is model-correctable,
    # so it RE-PROMPTS (appends a correction turn) — the opposite of a transport re-issue.
    captured: list[dict] = []
    bad = _msg(content="not a json decision")  # non-empty, malformed -> DecisionParseError path
    good = _msg(content='{"thought_summary": "ok", "action": {"type": "final_answer", "answer": "done"}}')
    client = OpenAIModelClient(
        HarnessConfig(model="m"),
        client=_fake_openai_messages([bad, good], captured),
        sleep=lambda _s: None,
    )

    decision = client.decide(_packet())

    assert isinstance(decision.action, FinalAnswer)
    assert len(captured[1]["messages"]) > len(captured[0]["messages"])  # correction appended
    assert len(decision.retry_trace) == 1  # the malformed attempt is traced as a parse error


def test_transport_retries_exhausted_raise_transport_error():
    calls: list[int] = []

    def create(**kwargs):
        calls.append(1)
        return SimpleNamespace(choices=[SimpleNamespace(message=_nul_reply())])

    client_obj = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = OpenAIModelClient(
        HarnessConfig(model="m"), client=client_obj, transport_max_retries=2, sleep=lambda _s: None
    )

    with pytest.raises(TransportError):
        client.decide(_packet())
    assert len(calls) == 3  # initial attempt + 2 retries; NOT re-prompted into the model


def test_request_call_failure_is_wrapped_as_transport_error():
    # A timeout / connection reset surfaces from the SDK as an exception (SDK retries disabled);
    # it must be retried at the transport layer and surface as TransportError, not crash raw.
    calls: list[int] = []

    def create(**kwargs):
        calls.append(1)
        raise RuntimeError("connection reset by peer")

    client_obj = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = OpenAIModelClient(
        HarnessConfig(model="m"), client=client_obj, transport_max_retries=1, sleep=lambda _s: None
    )

    with pytest.raises(TransportError):
        client.decide(_packet())
    assert len(calls) == 2  # initial + 1 retry


def test_is_empty_body_classifies_nul_and_whitespace():
    assert issubclass(EmptyResponseError, TransportError)  # NUL/empty is a transport failure
    assert _is_empty_body("")
    assert _is_empty_body("\x00")
    assert _is_empty_body("  \n\x00\t ")
    assert not _is_empty_body('{"action": ...}')


def test_request_timeout_default_is_calibrated():
    cfg = HarnessConfig()
    wall = cfg.max_wall_clock_seconds
    assert wall is not None  # the batch default keeps a real cap (only the cockpit disables it)
    # Under the run budget (one call can't eat the whole run)...
    assert cfg.request_timeout_seconds < wall
    # ...but comfortably ABOVE the longest legitimate generation observed in the 2026-06-20 data
    # (~203s on secret-safety) so a flat timeout never kills real work (the 90s default did).
    assert cfg.request_timeout_seconds >= 210
    assert cfg.transport_max_retries >= 0
    # Worst-case dead-endpoint cost stays a bounded overrun, not many wall clocks.
    assert cfg.request_timeout_seconds * (cfg.transport_max_retries + 1) <= 2 * wall


def test_runner_surfaces_transport_error_not_incomplete(tmp_path, read_registry):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=_nul_reply())])

    nul_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    # The runner now drives the async path (ADR-0029 R5); non-streaming keeps this transport test
    # focused on the retry/surface semantics (which streaming and non-streaming share).
    config = HarnessConfig(stream_model_calls=False)
    model_client = OpenAIModelClient(config, aclient=nul_client, transport_max_retries=1, asleep=_no_asleep)
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    emitter = Emitter()
    seen: list[str] = []
    emitter.subscribe(lambda event: seen.append(str(event["type"])))
    runner = AgentRunner(
        model_client=model_client,
        registry=read_registry,
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=emitter,
        config=config,
    )
    state = TaskState(goal="do it", task_kind="investigate")

    with pytest.raises(TransportError):
        runner.run(state)

    assert "transport_error" in seen  # journaled distinctly...
    assert "decision_error" not in seen  # ...NOT mislabeled as a model parse error
    assert state.outcome != "incomplete"  # surfaced as a system failure, not a silent budget stop


def test_transport_retry_sums_usage_and_traces_recovery():
    # Usage must accumulate across attempts (the failed-but-billed ones are not discarded), and
    # the recovered transport failure is surfaced on the decision for the runner to journal.
    nul = _msg_with_usage(content="\x00", prompt=100, completion=0)
    good = _msg_with_usage(tool_calls=[_tc("read_file", '{"path": "a.py"}')], prompt=120, completion=20)
    client = OpenAIModelClient(
        HarnessConfig(model="m"), client=_fake_openai_usage([nul, good]), sleep=lambda _s: None
    )

    decision = client.decide(_packet())

    assert decision.usage is not None
    assert decision.usage.prompt_tokens == 220  # 100 (lost NUL attempt) + 120 (winning attempt)
    assert decision.usage.completion_tokens == 20
    assert len(decision.transport_trace) == 1  # one recovered transport failure


def test_exhausted_transport_error_sums_usage_across_attempts():
    nul = _msg_with_usage(content="\x00", prompt=100, completion=0)
    client = OpenAIModelClient(
        HarnessConfig(model="m"),
        client=_fake_openai_usage([nul, nul, nul]),
        transport_max_retries=2,
        sleep=lambda _s: None,
    )

    with pytest.raises(TransportError) as excinfo:
        client.decide(_packet())
    assert excinfo.value.usage is not None
    assert excinfo.value.usage.prompt_tokens == 300  # 100 x 3 attempts, all billed


def test_runner_journals_recovered_transport_retry(tmp_path, read_registry):
    # Turn 1's first model call returns a NUL (transport failure); the transport retry re-issues
    # and the model concludes. The fake settles on final_answer so verification/repair can't
    # exhaust it (the run reaches a terminal outcome regardless of how many turns that takes).
    nul = _msg(content="\x00")
    final = _msg(tool_calls=[_tc("final_answer", '{"answer": "found it"}')])
    replies = iter([nul])

    async def create(**kwargs):
        try:
            message = next(replies)
        except StopIteration:
            message = final
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client_obj = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    config = HarnessConfig(stream_model_calls=False)
    model_client = OpenAIModelClient(config, aclient=client_obj, transport_max_retries=2, asleep=_no_asleep)
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    emitter = Emitter()
    seen: list[str] = []
    emitter.subscribe(lambda event: seen.append(str(event["type"])))
    runner = AgentRunner(
        model_client=model_client,
        registry=read_registry,
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=emitter,
        config=config,
    )

    result = runner.run(TaskState(goal="where is it?", task_kind="investigate"))

    assert "transport_retry" in seen  # the recovered NUL is journaled...
    assert "decision_error" not in seen  # ...not mislabeled as a parse error
    # ...and a transport failure NEVER enters the model's context (no feedback evidence for it).
    assert not any(ev.kind.startswith("transport") for ev in result.evidence)


def test_runner_journals_streaming_fallback(tmp_path, read_registry):
    # A provider that rejects stream=True flips the session to non-streaming; the runner journals a
    # `streaming_fallback` event (R5 observability) so an eval can tell streaming was attempted.
    final = _msg(tool_calls=[_tc("final_answer", '{"answer": "found it"}')])

    async def create(**kwargs):
        if kwargs.get("stream"):
            raise _api_status_error(400, "streaming is not supported for this model")
        return SimpleNamespace(choices=[SimpleNamespace(message=final)])

    client_obj = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    config = HarnessConfig()  # stream_model_calls=True default → streaming attempted, then falls back
    model_client = OpenAIModelClient(config, aclient=client_obj, transport_max_retries=2, asleep=_no_asleep)
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    emitter = Emitter()
    seen: list[str] = []
    emitter.subscribe(lambda event: seen.append(str(event["type"])))
    runner = AgentRunner(
        model_client=model_client,
        registry=read_registry,
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=emitter,
        config=config,
    )

    runner.run(TaskState(goal="where is it?", task_kind="investigate"))

    assert "streaming_fallback" in seen  # the capability fallback is visible in the journal


def test_native_plain_content_falls_back_to_json_decision():
    # An "OpenAI-compatible" endpoint that ignores `tools=` and answers in prose still
    # works: valid legacy-JSON content parses through parse_decision unchanged.
    content = '{"thought_summary": "ok", "action": {"type": "final_answer", "answer": "done"}}'
    client = OpenAIModelClient(
        HarnessConfig(model="m"), client=_fake_openai_messages([_msg(content=content)])
    )

    decision = client.decide(_packet())

    assert isinstance(decision.action, FinalAnswer)
    assert decision.retry_trace == []


def test_legacy_mode_preserved_when_disabled():
    captured: list[dict] = []
    content = '{"thought_summary": "ok", "action": {"type": "final_answer", "answer": "done"}}'
    config = HarnessConfig(model="m", native_tool_calls=False)
    client = OpenAIModelClient(config, client=_fake_openai_messages([_msg(content=content)], captured))

    decision = client.decide(_packet())

    assert isinstance(decision.action, FinalAnswer)
    sent = captured[0]
    assert sent["response_format"] == {"type": "json_object"}  # the escape hatch is verbatim legacy
    assert "tools" not in sent


def test_native_system_prompt_drops_json_envelope():
    # In native mode the provider carries the schemas, so the prompt must not demand a
    # hand-written JSON envelope (the instruction that conflicted with tool-calling) —
    # while staying kind-aware (the 2.6 contract).
    native_sys = next(
        m["content"] for m in build_messages(_packet(), native_tools=True) if m["role"] == "system"
    )
    legacy_sys = next(m["content"] for m in build_messages(_packet()) if m["role"] == "system")
    assert "JSON object" in legacy_sys  # legacy contract untouched
    assert "JSON object" not in native_sys
    assert "WORKING code change" in native_sys  # still kind-aware (edit framing)


def test_build_messages_replays_conversation_as_real_turns():
    # Cross-goal history rides as REAL user/assistant messages between the system message
    # and the working packet (ADR-0017) — not flattened into the packet's evidence bullets,
    # which the model under-weighted (it re-asked answered questions).
    packet = _packet(
        conversation=[
            ConversationTurn(role="user", content="explain the widget"),
            ConversationTurn(role="assistant", content="the widget lives in app.py"),
        ]
    )
    messages = build_messages(packet)
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "explain the widget"}
    assert messages[2] == {"role": "assistant", "content": "the widget lives in app.py"}
    assert messages[-1]["role"] == "user"  # the working packet is last
    assert packet.goal in messages[-1]["content"]  # ...and carries the current goal


def test_openai_client_records_parse_retry_trace():
    # The in-client retry loop must leave a trace: the dogfood run showed str_replace
    # attempts dying invisibly inside decide(), the model downgrading to reads, and no
    # record anywhere (state, journal, or context) that an edit was ever attempted.
    malformed = '{"thought_summary": "patching", "action": {"type": "str_replace", '  # truncated JSON
    valid = '{"thought_summary": "ok", "action": {"type": "final_answer", "answer": "done"}}'
    client = OpenAIModelClient(HarnessConfig(model="m"), client=_fake_openai_seq([malformed, valid]))
    packet = ContextPacket(goal="g", phase="investigating", allowed_tools=[])

    decision = client.decide(packet)

    assert isinstance(decision.action, FinalAnswer)  # the run still recovered
    assert len(decision.retry_trace) == 1  # ...but the failed attempt is on the record
    note = decision.retry_trace[0]
    assert "JSON" in note.error  # what was wrong
    assert "str_replace" in note.raw  # and the raw attempt itself, for debugging


def test_parse_decision_ignores_model_supplied_retry_trace():
    # `retry_trace` is a harness-owned diagnostics channel: a model emitting the field
    # in its JSON must not be able to plant fake retry history.
    raw = (
        '{"thought_summary": "t", "action": {"type": "final_answer", "answer": "a"},'
        ' "retry_trace": [{"error": "fake", "raw": "fake"}]}'
    )
    assert parse_decision(raw).retry_trace == []


def test_default_prompt_is_kind_aware():
    # task_kind is now carried on the ContextPacket, so the prompt frames the mission
    # per kind: an edit task is told to make a working change; an investigate task is
    # told the repo must net to zero diff when it answers (ADR-0005: transient
    # instrumentation is legal, leaving it is not). An edit task must never be
    # re-locked to READ-ONLY framing.
    edit = ContextPacket(
        goal="fix the off-by-one in app.py",
        phase="editing",
        task_kind="edit",
        allowed_tools=[ToolSummary(name="str_replace", description="replace exact text in a file")],
    )
    inv = ContextPacket(
        goal="explain the loop",
        phase="investigating",
        task_kind="investigate",
        allowed_tools=[ToolSummary(name="read_file", description="read a file")],
    )
    edit_sys = next(m["content"] for m in build_messages(edit) if m["role"] == "system")
    inv_sys = next(m["content"] for m in build_messages(inv) if m["role"] == "system")
    assert edit_sys != inv_sys  # framing genuinely varies by kind
    assert "READ-ONLY" not in edit_sys and "read-only" not in edit_sys.lower()
    # ADR-0005 framing: net-zero diff at the end, not "no writes ever".
    assert "must be unchanged when you answer" in inv_sys.lower()
    assert "revert any instrumentation" in inv_sys.lower()
    assert "without editing" not in inv_sys.lower()  # the old blanket prohibition is gone
    assert "JSON" in edit_sys and "str_replace" in edit_sys  # still schema-bearing


def test_core_imports_without_openai(monkeypatch):
    # Simulate openai being absent: importing the package and building messages must
    # still work — `openai` is an optional extra, only needed by OpenAIModelClient.
    # Setting sys.modules["openai"] = None makes `import openai` raise ImportError,
    # exactly as if the extra were not installed (no real uninstall).
    monkeypatch.setitem(sys.modules, "openai", None)

    # A fresh import of the module under the simulated absence must not fail at import
    # time — the openai dependency must be lazy. Load it under an alias so we never
    # rebind the canonical `avatar.model_client` (whose classes the runner and
    # other tests share — reloading it would break discriminated-union identity).
    spec = importlib.util.find_spec("avatar.model_client")
    assert spec is not None and spec.loader is not None
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)  # would raise here if `import openai` were eager

    packet = ContextPacket(
        goal="where is the bug?",
        phase="investigating",
        allowed_tools=[ToolSummary(name="read_file", description="read a file")],
    )
    messages = isolated.build_messages(packet)
    assert any("where is the bug?" in m["content"] for m in messages)

    # Construction is lazy (no credentials/extra needed); the clear, actionable error
    # surfaces when the client is first actually used (decide → _ensure_client).
    client = isolated.OpenAIModelClient(HarnessConfig(model="x"))
    with pytest.raises(ImportError):
        client.decide(packet)


def test_openai_client_constructs_without_credentials():
    # Regression: a Harness with the default model must be constructible without an API
    # key — credentials are inference-time only. Eager OpenAI(...) in __init__ raised
    # OpenAIError in CI (no key), masking the dirty-workspace error. Client is now lazy.
    client = OpenAIModelClient(HarnessConfig(api_key=None))
    assert client._client is None  # not built at construction time


class _ScriptedModel(ModelClient):
    """A non-OpenAI ModelClient that replays decisions — no `openai` import path touched."""

    def __init__(self, decisions: list[ModelDecision]) -> None:
        self._decisions = decisions
        self._i = 0

    def decide(self, context: ContextPacket) -> ModelDecision:
        decision = self._decisions[min(self._i, len(self._decisions) - 1)]
        self._i += 1
        return decision


def test_custom_model_client_runs_end_to_end(tmp_path, read_registry):
    (tmp_path / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="the handler lives in app.py")),
    ]
    config = HarnessConfig()
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    runner = AgentRunner(
        model_client=_ScriptedModel(decisions),
        registry=read_registry,
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=Emitter(),
        config=config,
    )
    result = runner.run(TaskState(goal="where is the handler?", task_kind="investigate"))
    assert result.outcome == "success"
    assert result.final_answer == "the handler lives in app.py"


# --- token-usage capture (eval prerequisite; ADR-0004) -------------------------------------


def _msg_with_usage(content=None, tool_calls=None, prompt=0, completion=0):
    """A reply message namespace whose response carries provider usage."""
    return SimpleNamespace(content=content, tool_calls=tool_calls), SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion
    )


def _fake_openai_usage(replies: list, captured: list[dict] | None = None):
    """A transport returning (message, usage) pairs in turn."""
    seq = iter(replies)

    def create(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        message, usage = next(seq)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_usage_captured_from_provider_reply():
    """`response.usage` rides the decision as a harness-owned annotation.

    Until now usage was dropped on the floor — no token counts reached state or the
    journal, so cost-per-solve (the eval harness's key metric) was unmeasurable.
    """
    reply = _msg_with_usage(tool_calls=[_tc("read_file", '{"path": "a.py"}')], prompt=1200, completion=45)
    client = OpenAIModelClient(HarnessConfig(model="m"), client=_fake_openai_usage([reply]))
    decision = client.decide(_packet())
    assert decision.usage is not None
    assert decision.usage.prompt_tokens == 1200
    assert decision.usage.completion_tokens == 45


def test_usage_summed_across_in_client_retries():
    """Every attempt costs tokens — a retried turn reports the SUM, not the last call."""
    bad = _msg_with_usage(content="{not json", prompt=1000, completion=30)
    good = _msg_with_usage(tool_calls=[_tc("read_file", '{"path": "a.py"}')], prompt=1100, completion=40)
    client = OpenAIModelClient(HarnessConfig(model="m"), client=_fake_openai_usage([bad, good]))
    decision = client.decide(_packet())
    assert decision.usage is not None
    assert decision.usage.prompt_tokens == 2100  # both attempts paid for
    assert decision.usage.completion_tokens == 70


def test_missing_usage_tolerated():
    """Compat endpoints that omit `usage` yield `None` — never a crash."""
    reply = _msg(tool_calls=[_tc("read_file", '{"path": "a.py"}')])  # transport without usage attr
    client = OpenAIModelClient(HarnessConfig(model="m"), client=_fake_openai_messages([reply]))
    decision = client.decide(_packet())
    assert decision.usage is None


def test_parse_decision_ignores_model_supplied_usage():
    """`usage` is harness-owned, like `retry_trace` — a model can't bill itself kindly."""
    raw = (
        '{"thought_summary": "t", "action": {"type": "final_answer", "answer": "a"},'
        ' "usage": {"prompt_tokens": 1, "completion_tokens": 1}}'
    )
    assert parse_decision(raw).usage is None


def test_exhausted_parse_failure_carries_usage():
    """A turn lost to malformed output still reports what its attempts cost.

    The expensive failure mode (3 paid attempts, no decision) was exactly the one
    being silently undercounted (PR-#31 review): the raised error now carries the
    summed tally for the runner to bill.
    """
    bad = _msg_with_usage(content="{not json", prompt=1000, completion=30)
    client = OpenAIModelClient(
        HarnessConfig(model="m"), client=_fake_openai_usage([bad, bad, bad]), max_parse_retries=2
    )
    with pytest.raises(DecisionParseError) as exc_info:
        client.decide(_packet())
    usage = exc_info.value.usage
    assert usage is not None
    assert usage.prompt_tokens == 3000 and usage.completion_tokens == 90


# --- decision transport recording (loop-determinism hardening) ------------------------------
#
# The silent native -> JSON-envelope fallback (model_client._decide_native) means two runs of
# the same task can ride different transports — with different system prompts — depending on
# provider behavior. The fix is observability first: every decision records which transport
# produced it, the runner journals it, and parse_decision clears a model-claimed value (the
# field is harness-owned, like usage/retry_trace).


def test_decision_records_native_transport():
    reply = _msg(tool_calls=[_tc("read_file", '{"path": "app.py"}')])
    client = OpenAIModelClient(HarnessConfig(model="m"), client=_fake_openai_messages([reply]))

    decision = client.decide(_packet())

    assert decision.transport == "native"


def test_decision_records_json_fallback_transport():
    # An endpoint that ignores `tools=` and answers in prose: the decision still parses,
    # but the transport flip is RECORDED, never silent.
    content = '{"thought_summary": "ok", "action": {"type": "final_answer", "answer": "done"}}'
    client = OpenAIModelClient(
        HarnessConfig(model="m"), client=_fake_openai_messages([_msg(content=content)])
    )

    decision = client.decide(_packet())

    assert decision.transport == "json_fallback"


def test_decision_records_json_transport_when_native_disabled():
    content = '{"thought_summary": "ok", "action": {"type": "final_answer", "answer": "done"}}'
    config = HarnessConfig(model="m", native_tool_calls=False)
    client = OpenAIModelClient(config, client=_fake_openai_messages([_msg(content=content)]))

    decision = client.decide(_packet())

    assert decision.transport == "json"


def test_parse_decision_clears_model_claimed_transport():
    # `transport` is a harness-owned channel: a model emitting the field must not
    # impersonate a transport (same rule as retry_trace/usage).
    raw = (
        '{"thought_summary": "ok", "transport": "native",'
        ' "action": {"type": "final_answer", "answer": "done"}}'
    )
    decision = parse_decision(raw)
    assert decision.transport == ""


# --- per-action retry excerpt cap (loop-determinism hardening) ------------------------------
#
# A failed str_replace/write_file attempt is most useful WITH its edit payload: the flat
# 2000-char excerpt cut real edits mid-body, so the model retried blind and re-emitted the
# same error. Patch-bearing actions get a higher cap; any cut is marked loudly (same rule as
# context compaction: never cut silently).


def test_patch_retry_excerpt_keeps_long_edit():
    # 5800-char old_string: over the 2000 raw cap, under the 12000 patch cap — TAIL_MARKER
    # survives ONLY because str_replace rides the higher edit-bearing cap.
    bad_args = '{"path": "app.py", "old_string": "' + ("x" * 5800) + "TAIL_MARKER"  # truncated JSON
    bad = _msg(tool_calls=[_tc("str_replace", bad_args, call_id="c1")])
    good = _msg(tool_calls=[_tc("read_file", '{"path": "app.py"}')])
    client = OpenAIModelClient(HarnessConfig(model="m"), client=_fake_openai_messages([bad, good]))

    decision = client.decide(_packet())

    assert len(decision.retry_trace) == 1
    assert "TAIL_MARKER" in decision.retry_trace[0].raw  # the tail survived the cap


def test_truncated_retry_excerpt_is_marked():
    # Non-patch raw past the cap is still cut — but the cut is explicit, never silent.
    long_invalid = "not json " + ("y" * 4000)
    bad = _msg(content=long_invalid)
    good = _msg(tool_calls=[_tc("read_file", '{"path": "app.py"}')])
    client = OpenAIModelClient(HarnessConfig(model="m"), client=_fake_openai_messages([bad, good]))

    decision = client.decide(_packet())

    assert len(decision.retry_trace) == 1
    raw = decision.retry_trace[0].raw
    assert len(raw) < len(long_invalid)  # still capped
    assert "[truncated" in raw  # but loudly


def _fake_async_openai_messages(messages: list, captured: list[dict] | None = None):
    """An async transport returning each prebuilt reply *message* in turn (non-streaming path)."""
    replies = iter(messages)

    async def create(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=next(replies))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def test_adecide_parses_native_decision():
    # The non-streaming async path (`stream_model_calls=False` → `_adecide_native_async`).
    reply = _msg(tool_calls=[_tc("read_file", '{"path": "app.py"}')])
    client = OpenAIModelClient(
        HarnessConfig(model="m", stream_model_calls=False), aclient=_fake_async_openai_messages([reply])
    )

    decision = await client.adecide(_packet())

    assert isinstance(decision.action, ToolCall)
    assert decision.action.name == "read_file"
    assert decision.transport == "native"


async def test_adecide_retries_then_succeeds():
    # Same malformed-then-valid retry path as the sync driver, sharing the parse-retry loop.
    bad = _msg(tool_calls=[_tc("read_file", "{not json")])
    good = _msg(tool_calls=[_tc("read_file", '{"path": "app.py"}')])
    captured: list[dict] = []
    client = OpenAIModelClient(
        HarnessConfig(model="m", stream_model_calls=False),
        aclient=_fake_async_openai_messages([bad, good], captured),
    )

    decision = await client.adecide(_packet())

    assert isinstance(decision.action, ToolCall)
    assert len(decision.retry_trace) == 1  # the malformed attempt is recorded, not swallowed
    assert len(captured) == 2  # it retried in-conversation


async def test_adecide_json_transport():
    content = '{"thought_summary": "done", "action": {"type": "final_answer", "answer": "x"}}'
    client = OpenAIModelClient(
        HarnessConfig(model="m", native_tool_calls=False, stream_model_calls=False),
        aclient=_fake_async_openai_messages([_msg(content=content)]),
    )

    decision = await client.adecide(_packet())

    assert isinstance(decision.action, FinalAnswer)
    assert decision.transport == "json"


async def test_adecide_cancellation_aborts_in_flight_call():
    # Cancelling the awaiting task raises CancelledError *at the in-flight call* (httpx aborts
    # the socket there) and propagates out of adecide — it is never swallowed (ADR-0030, the
    # guarantee the runner's cancel-race relies on). Simulated with a create() that blocks.
    started = asyncio.Event()
    saw_cancel: dict = {}

    async def create(**kwargs):
        started.set()
        try:
            await asyncio.sleep(30)  # a slow in-flight request
        except asyncio.CancelledError:
            saw_cancel["v"] = True  # httpx would abort the socket at this point
            raise
        return SimpleNamespace(choices=[SimpleNamespace(message=_msg(content="never"))])  # pragma: no cover

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    mc = OpenAIModelClient(HarnessConfig(model="m", stream_model_calls=False), aclient=client)

    task = asyncio.create_task(mc.adecide(_packet()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert saw_cancel.get("v")  # the cancel reached the in-flight call, not just the wrapper


def test_request_uses_config_temperature():
    # Eval reliability (pass^k) needs temperature>0 so each seed is an independent sample;
    # the request temperature must come from config (default 0.0 keeps the loop deterministic).
    assert HarnessConfig(model="m").temperature == 0.0
    captured: list[dict] = []
    reply = _msg(tool_calls=[_tc("read_file", '{"path": "a.py"}')])
    client = OpenAIModelClient(
        HarnessConfig(model="m", temperature=0.5), client=_fake_openai_messages([reply], captured)
    )
    client.decide(_packet())
    assert captured[0]["temperature"] == 0.5


# --- ADR-0029 R5: async streaming, idle timeout, session-scoped fallback ---


def _api_status_error(status: int, message: str):
    """A real `openai.APIStatusError` carrying `status_code`/`message` for the discrimination rule."""
    response = httpx.Response(status, request=httpx.Request("POST", "https://x"))
    return APIStatusError(message, response=response, body=None)


async def test_streaming_reassembles_tool_call_matching_nonstream():
    # The reassembly anchor: id+name arrive once, arguments stream in fragments, usage in the tail.
    # The reassembled decision must equal the one the non-streaming path produces for the same call.
    stream = _AsyncStream(
        [
            _chunk(tool_calls=[_tc_delta(0, call_id="call_1", name="read_file", arguments='{"pa')]),
            _chunk(tool_calls=[_tc_delta(0, arguments='th": "app.py"}')]),
            _chunk(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5)),
        ]
    )
    client = OpenAIModelClient(
        HarnessConfig(model="m"), aclient=_afake_openai_streams([stream]), asleep=_no_asleep
    )

    decision = await client.adecide(_packet())

    assert isinstance(decision.action, ToolCall)
    assert decision.action.name == "read_file"
    assert decision.action.input == {"path": "app.py"}
    assert decision.transport == "native_stream"  # streamed → tagged distinctly (R5 observability)
    assert decision.usage is not None
    assert decision.usage.prompt_tokens == 10  # usage read from the stream's final chunk
    assert decision.usage.completion_tokens == 5
    assert stream.closed is True  # the stream is always closed

    # Equivalence anchor: the same call over the non-streaming async path yields the same action.
    nonstream = OpenAIModelClient(
        HarnessConfig(model="m", stream_model_calls=False),
        aclient=_afake_openai_messages([_msg(tool_calls=[_tc("read_file", '{"path": "app.py"}')])]),
        asleep=_no_asleep,
    )
    nd = await nonstream.adecide(_packet())
    assert nd.action == decision.action


async def test_streaming_idle_timeout_reaches_sdk_as_per_call_timeout():
    # The core R5 mechanism end-to-end: request_idle_timeout_seconds is handed to the SDK as the
    # per-call httpx read timeout (the idle watchdog). Asserted on the captured create() kwargs, so
    # the *wiring* is covered, not just the behaviour-on-injected-ReadTimeout the other tests use.
    captured: list[dict] = []
    stream = _AsyncStream(
        [
            _chunk(tool_calls=[_tc_delta(0, call_id="c", name="read_file", arguments='{"path": "a.py"}')]),
            _chunk(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)),
        ]
    )
    client = OpenAIModelClient(
        HarnessConfig(model="m", request_idle_timeout_seconds=12.5),
        aclient=_afake_openai_streams([stream], captured),
        asleep=_no_asleep,
    )

    await client.adecide(_packet())

    assert captured[0]["stream"] is True
    assert captured[0]["timeout"] == 12.5  # the idle bound reaches the SDK as the per-call timeout


async def test_streaming_multi_tool_call_reassembles_index_zero():
    # _areassemble accumulates every index but uses only index 0 (one action per turn, §6). A
    # provider that streams two parallel tool calls must still yield the index-0 action cleanly,
    # with its arguments joined across fragments and the index-1 call ignored.
    stream = _AsyncStream(
        [
            _chunk(tool_calls=[_tc_delta(0, call_id="c0", name="read_file", arguments='{"path": ')]),
            _chunk(tool_calls=[_tc_delta(1, call_id="c1", name="search_repo", arguments='{"query": "x"}')]),
            _chunk(tool_calls=[_tc_delta(0, arguments='"app.py"}')]),
            _chunk(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)),
        ]
    )
    client = OpenAIModelClient(
        HarnessConfig(model="m"), aclient=_afake_openai_streams([stream]), asleep=_no_asleep
    )

    decision = await client.adecide(_packet())

    assert isinstance(decision.action, ToolCall)
    assert decision.action.name == "read_file"  # index 0, not the index-1 search_repo
    assert decision.action.input == {"path": "app.py"}  # index-0 args joined across fragments


async def test_streaming_unsupported_flips_flag_and_falls_back_session_scoped():
    # MANDATED: a streaming-rejection 4xx flips the per-instance flag once; the SAME request then
    # succeeds non-streaming, and every later call skips streaming entirely (no `stream=True`).
    captured: list[dict] = []
    final = _msg(tool_calls=[_tc("final_answer", '{"answer": "done"}')])
    reject = _api_status_error(400, "streaming is not supported for this model")

    async def create(**kwargs):
        captured.append(kwargs)
        if kwargs.get("stream"):
            raise reject
        return SimpleNamespace(choices=[SimpleNamespace(message=final)])

    aclient = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = OpenAIModelClient(HarnessConfig(model="m"), aclient=aclient, asleep=_no_asleep)

    d1 = await client.adecide(_packet())
    assert isinstance(d1.action, FinalAnswer)  # recovered via the non-streaming fallback
    assert client._streaming_unsupported is True
    assert captured[0]["stream"] is True and "stream" not in captured[1]  # streamed once, then fell back
    assert "streaming is not supported" in d1.streaming_fallback  # the flip reason is surfaced (R5 obs.)
    assert d1.transport == "native"  # the fallback turn ran non-streamed

    captured.clear()
    d2 = await client.adecide(_packet())
    assert isinstance(d2.action, FinalAnswer)
    assert all(not c.get("stream") for c in captured)  # session-scoped: never streams again
    assert d2.streaming_fallback == ""  # the signal fires only on the flip turn, not every later turn


async def test_streaming_idle_stall_retries_as_transport_not_fallback():
    # An inter-chunk idle stall surfaces as httpx.ReadTimeout → TransportError → R3 retry (re-issue),
    # NOT a capability fallback: the flag stays down and the backoff sleeper is used.
    slept: list[float] = []

    async def sleeper(seconds: float) -> None:
        slept.append(seconds)

    stall = _AsyncStream(
        [_chunk(tool_calls=[_tc_delta(0, call_id="c", name="read_file", arguments='{"pa')])],
        fail=httpx.ReadTimeout("idle"),
    )
    good = _AsyncStream(
        [
            _chunk(tool_calls=[_tc_delta(0, call_id="c", name="read_file", arguments='{"path": "a.py"}')]),
            _chunk(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)),
        ]
    )
    client = OpenAIModelClient(
        HarnessConfig(model="m"),
        aclient=_afake_openai_streams([stall, good]),
        transport_max_retries=2,
        asleep=sleeper,
    )

    decision = await client.adecide(_packet())

    assert isinstance(decision.action, ToolCall)
    assert len(decision.transport_trace) == 1  # one recovered idle stall
    assert client._streaming_unsupported is False  # a stall is transient, not a capability verdict
    assert len(slept) == 1  # backoff slept once before the re-issue
    assert stall.closed is True  # the stalled stream was still closed


async def test_empty_stream_is_transport_retried():
    # An empty reassembled body (no tool calls, blank content) is EmptyResponseError → R3, exactly
    # like the non-streaming NUL case (ADR-0028 R2).
    empty = _AsyncStream(
        [_chunk(content=""), _chunk(usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0))]
    )
    good = _AsyncStream(
        [_chunk(tool_calls=[_tc_delta(0, call_id="c", name="final_answer", arguments='{"answer": "x"}')])]
    )
    client = OpenAIModelClient(
        HarnessConfig(model="m"),
        aclient=_afake_openai_streams([empty, good]),
        transport_max_retries=2,
        asleep=_no_asleep,
    )

    decision = await client.adecide(_packet())

    assert isinstance(decision.action, FinalAnswer)
    assert len(decision.transport_trace) == 1  # the empty stream was a recovered transport failure


async def test_streaming_broken_framing_falls_back_no_index_zero():
    # Tool-call deltas that never use index 0 are unusable framing → StreamingUnsupportedError →
    # non-streaming fallback (a capability problem, not a parse one).
    bad = _AsyncStream([_chunk(tool_calls=[_tc_delta(1, call_id="c", name="read_file", arguments="{}")])])
    final = _msg(tool_calls=[_tc("final_answer", '{"answer": "done"}')])

    async def create(**kwargs):
        if kwargs.get("stream"):
            return bad
        return SimpleNamespace(choices=[SimpleNamespace(message=final)])

    aclient = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = OpenAIModelClient(HarnessConfig(model="m"), aclient=aclient, asleep=_no_asleep)

    decision = await client.adecide(_packet())

    assert isinstance(decision.action, FinalAnswer)
    assert client._streaming_unsupported is True  # framing failure flipped the flag


async def test_streaming_invalid_args_is_parse_error_not_fallback():
    # Well-framed call (index 0, name + id) but invalid-JSON arguments is model-correctable:
    # it surfaces as DecisionParseError (the runner re-prompts next turn) and does NOT flip the flag.
    def _bad():
        frag = _tc_delta(0, call_id="c", name="read_file", arguments="{not json")
        return _AsyncStream([_chunk(tool_calls=[frag])])

    client = OpenAIModelClient(
        HarnessConfig(model="m"),
        aclient=_afake_openai_streams([_bad(), _bad(), _bad()]),
        max_parse_retries=2,
        asleep=_no_asleep,
    )

    with pytest.raises(DecisionParseError):
        await client.adecide(_packet())
    assert client._streaming_unsupported is False  # a bad-args reply is not a capability verdict


async def test_adecide_bridge_offloads_sync_decide_for_fakes():
    # A plain ModelClient fake (no adecide override) is driven through the async entry point via the
    # to_thread bridge — the 8 suite fakes keep working unchanged under the runner's async path.
    class _Fake(ModelClient):
        def decide(self, context):
            return ModelDecision(action=FinalAnswer(answer="bridged"))

    decision = await _Fake().adecide(_packet())
    assert isinstance(decision.action, FinalAnswer)
    assert decision.action.answer == "bridged"


def test_streaming_unsupported_is_not_a_transport_error():
    # It must NOT be retried at the transport layer (re-issuing the same stream fails identically).
    assert not issubclass(StreamingUnsupportedError, TransportError)
