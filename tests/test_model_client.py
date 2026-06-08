import importlib
import importlib.util
import sys
from types import SimpleNamespace

import pytest

from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextPacket, ToolSummary
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
    # The default system prompt must NOT be hard-locked to a READ-ONLY investigation
    # task: an edit task framed as READ-ONLY would forbid the very mutation it needs.
    # task_kind is not (yet) carried on the ContextPacket, so the prompt is kind-NEUTRAL:
    # it must not assert READ-ONLY framing for any packet.
    packet = ContextPacket(
        goal="fix the off-by-one in app.py",
        phase="editing",
        allowed_tools=[ToolSummary(name="apply_patch", description="apply a patch")],
    )
    system = next(m["content"] for m in build_messages(packet) if m["role"] == "system")
    assert "READ-ONLY" not in system
    assert "read-only" not in system.lower()
    # Still a usable, schema-bearing harness prompt.
    assert "JSON" in system
    assert "apply_patch" in system


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

    # Using OpenAIModelClient without the extra raises a clear, actionable error.
    with pytest.raises(ImportError):
        isolated.OpenAIModelClient(HarnessConfig(model="x"))


class _ScriptedModel(ModelClient):
    """A non-OpenAI ModelClient that replays decisions — no `openai` import path touched."""

    def __init__(self, decisions: list[ModelDecision]) -> None:
        self._decisions = decisions
        self._i = 0

    def decide(self, context: ContextPacket) -> ModelDecision:  # noqa: ARG002
        decision = self._decisions[min(self._i, len(self._decisions) - 1)]
        self._i += 1
        return decision


def test_custom_model_client_runs_end_to_end(tmp_path, read_registry):
    from avatar_harness.context import ContextBuilder
    from avatar_harness.deps import CancellationToken, RunDeps
    from avatar_harness.events import Emitter
    from avatar_harness.runner import AgentRunner
    from avatar_harness.state import TaskState
    from avatar_harness.verifier import Verifier
    from avatar_harness.workspace import Workspace

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
