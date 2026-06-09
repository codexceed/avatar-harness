"""Phase 3.1 · 3.2a — meta commands (§23.2, ADR-0002 J).

Input starting with `/` is handled *locally* in `ReplSession` — it never spins a
`TaskState` or hits the model. Each command returns a typed `MetaResult(kind, text)` the
cockpit interprets (`message`/`mode_set`/`state` → show text, `diff` → pop the diff modal,
`quit` → exit). `@path` grounding and the `/plan`/`/undo` flows are later tail increments.
"""

import asyncio

from avatar_harness.config import HarnessConfig
from avatar_harness.harness import Harness
from avatar_harness.model_client import FinalAnswer, ModelClient, ModelDecision, ToolCall
from avatar_harness.session import ApprovalGrant
from avatar_harness.session_state import ReplSession
from avatar_harness.tools.base import ToolRegistry
from avatar_harness.tools.filesystem import read_file


class ScriptedModel(ModelClient):
    """Replays pre-built decisions; repeats the last when exhausted."""

    def __init__(self, decisions: list[ModelDecision]) -> None:
        self._decisions = decisions
        self._i = 0

    def decide(self, context: object) -> ModelDecision:
        decision = self._decisions[min(self._i, len(self._decisions) - 1)]
        self._i += 1
        return decision


def _repl(root, decisions=None) -> ReplSession:
    reg = ToolRegistry()
    reg.register(read_file)
    config = HarnessConfig(workspace_root=str(root))
    return ReplSession(Harness(config=config, model=ScriptedModel(decisions or []), tools=reg))


def test_non_slash_input_is_not_meta(tmp_path):
    repl = _repl(tmp_path)
    assert repl.is_meta("/help") is True
    assert repl.is_meta("explain x") is False  # a normal goal, not a meta command


def test_mode_command_sets_mode(tmp_path):
    repl = _repl(tmp_path)
    result = repl.run_meta("/mode edit")
    assert result.kind == "mode_set"
    assert repl.resolve_mode("explain how X works") == "edit"  # override now in effect


def test_mode_command_rejects_unknown_kind(tmp_path):
    repl = _repl(tmp_path)
    result = repl.run_meta("/mode bogus")
    assert result.kind == "message" and "bogus" in result.text
    assert repl.mode is None  # unchanged


def test_help_lists_commands(tmp_path):
    result = _repl(tmp_path).run_meta("/help")
    assert result.kind == "message"
    assert "/mode" in result.text and "/diff" in result.text


def test_state_summarizes_session(git_repo):
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer="add is defined in calc.py")),
    ]
    repl = _repl(git_repo, decisions)
    asyncio.run(repl.submit("explain calc.py"))
    result = repl.run_meta("/state")
    assert "1" in result.text  # one task run
    assert "investigate" in result.text  # current mode


def test_diff_returns_workspace_diff(git_repo):
    (git_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    result = _repl(git_repo).run_meta("/diff")
    assert result.kind == "diff"
    assert "calc.py" in result.text  # the uncommitted change is shown


def test_permissions_lists_grants(tmp_path):
    repl = _repl(tmp_path)
    repl.state.grants.append(ApprovalGrant(tool="run_command", prefix="pytest", tier=3))
    result = repl.run_meta("/permissions")
    assert result.kind == "message" and "pytest" in result.text


def test_unknown_command_is_reported(tmp_path):
    result = _repl(tmp_path).run_meta("/frobnicate")
    assert result.kind == "message" and "frobnicate" in result.text  # reported, not run as a goal
