from types import SimpleNamespace

import pytest

from avatar_harness.config import HarnessConfig
from avatar_harness.context import ContextPacket, ToolSummary
from avatar_harness.model_client import (
    DecisionParseError,
    FinalAnswer,
    OpenAIModelClient,
    ToolCall,
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
