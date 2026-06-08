import pytest

from avatar_harness.cli import run_agent, run_echo
from avatar_harness.config import HarnessConfig
from avatar_harness.events import Emitter
from avatar_harness.model_client import FinalAnswer, ModelDecision
from avatar_harness.workspace import DirtyWorkspaceError


class _OneShotModel:
    """A ModelClient that answers immediately — enough to exercise CLI wiring."""

    def decide(self, context: object) -> ModelDecision:
        return ModelDecision(action=FinalAnswer(answer="done"))


def test_run_agent_rejects_dirty_workspace_by_default(git_repo):
    (git_repo / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    config = HarnessConfig(workspace_root=str(git_repo))
    with pytest.raises(DirtyWorkspaceError):
        run_agent("anything", config=config, emitter=Emitter(), model_client=_OneShotModel())


def test_run_agent_allow_dirty_opens_dirty_workspace(git_repo):
    # `--allow-dirty` threads through to Workspace(allow_dirty=True), so a tracked
    # modification no longer blocks the run (§15 acknowledged-dirty escape).
    (git_repo / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    config = HarnessConfig(workspace_root=str(git_repo))
    state = run_agent(
        "anything", config=config, emitter=Emitter(), model_client=_OneShotModel(), allow_dirty=True
    )
    assert state.iterations >= 1  # the loop ran instead of raising at open


def test_run_emits_start_and_end():
    emitter = Emitter()
    events = []
    emitter.subscribe(events.append)

    run_echo("hello", emitter=emitter)

    types = [e["type"] for e in events]
    assert types[0] == "agent_start"
    assert types[-1] == "agent_end"


def test_echo_roundtrip():
    emitter = Emitter()
    result = run_echo("do the thing", emitter=emitter)
    assert result.answer == "do the thing"
    assert result.outcome == "success"
