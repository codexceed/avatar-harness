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
    assert "read_file" in blob  # the allowed tool was advertised


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
