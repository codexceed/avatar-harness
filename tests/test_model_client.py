import importlib
import importlib.util
import sys
from types import SimpleNamespace

import pytest

from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextBuilder, ContextPacket, ToolSummary
from avatar_harness.deps import CancellationToken, RunDeps
from avatar_harness.events import Emitter
from avatar_harness.model_client import (
    DecisionParseError,
    FinalAnswer,
    ModelClient,
    ModelDecision,
    OpenAIModelClient,
    ToolCall,
    build_messages,
    parse_decision,
)
from avatar_harness.runner import AgentRunner
from avatar_harness.state import TaskState
from avatar_harness.verifier import Verifier
from avatar_harness.workspace import Workspace


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
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments))


def _fake_openai_messages(messages: list, captured: list[dict] | None = None):
    """A transport returning each prebuilt reply *message* in turn."""
    replies = iter(messages)

    def create(**kwargs):
        if captured is not None:
            captured.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=next(replies))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _packet(**overrides) -> ContextPacket:
    base = dict(
        goal="add a retry to the client",
        phase="investigating",
        task_kind="edit",
        allowed_tools=[
            ToolSummary(
                name="read_file",
                description="read a file",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ],
    )
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
    read_file_schema = next(t["function"]["parameters"] for t in sent["tools"] if t["function"]["name"] == "read_file")
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


def test_native_plain_content_falls_back_to_json_decision():
    # An "OpenAI-compatible" endpoint that ignores `tools=` and answers in prose still
    # works: valid legacy-JSON content parses through parse_decision unchanged.
    content = '{"thought_summary": "ok", "action": {"type": "final_answer", "answer": "done"}}'
    client = OpenAIModelClient(HarnessConfig(model="m"), client=_fake_openai_messages([_msg(content=content)]))

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
    native_sys = next(m["content"] for m in build_messages(_packet(), native_tools=True) if m["role"] == "system")
    legacy_sys = next(m["content"] for m in build_messages(_packet()) if m["role"] == "system")
    assert "JSON object" in legacy_sys  # legacy contract untouched
    assert "JSON object" not in native_sys
    assert "WORKING code change" in native_sys  # still kind-aware (edit framing)


def test_openai_client_records_parse_retry_trace():
    # The in-client retry loop must leave a trace: the dogfood run showed apply_patch
    # attempts dying invisibly inside decide(), the model downgrading to reads, and no
    # record anywhere (state, journal, or context) that a patch was ever attempted.
    malformed = '{"thought_summary": "patching", "action": {"type": "apply_patch", '  # truncated JSON
    valid = '{"thought_summary": "ok", "action": {"type": "final_answer", "answer": "done"}}'
    client = OpenAIModelClient(HarnessConfig(model="m"), client=_fake_openai_seq([malformed, valid]))
    packet = ContextPacket(goal="g", phase="investigating", allowed_tools=[])

    decision = client.decide(packet)

    assert isinstance(decision.action, FinalAnswer)  # the run still recovered
    assert len(decision.retry_trace) == 1  # ...but the failed attempt is on the record
    note = decision.retry_trace[0]
    assert "JSON" in note.error  # what was wrong
    assert "apply_patch" in note.raw  # and the raw attempt itself, for debugging


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
    # told NOT to edit. An edit task must never be re-locked to READ-ONLY framing.
    edit = ContextPacket(
        goal="fix the off-by-one in app.py",
        phase="editing",
        task_kind="edit",
        allowed_tools=[ToolSummary(name="apply_patch", description="apply a patch")],
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
    assert "without editing" in inv_sys.lower()  # investigate explicitly forbids mutation
    assert "JSON" in edit_sys and "apply_patch" in edit_sys  # still schema-bearing


def test_core_imports_without_openai(monkeypatch):
    # Simulate openai being absent: importing the package and building messages must
    # still work — `openai` is an optional extra, only needed by OpenAIModelClient.
    # Setting sys.modules["openai"] = None makes `import openai` raise ImportError,
    # exactly as if the extra were not installed (no real uninstall).
    monkeypatch.setitem(sys.modules, "openai", None)

    # A fresh import of the module under the simulated absence must not fail at import
    # time — the openai dependency must be lazy. Load it under an alias so we never
    # rebind the canonical `avatar_harness.model_client` (whose classes the runner and
    # other tests share — reloading it would break discriminated-union identity).
    spec = importlib.util.find_spec("avatar_harness.model_client")
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
