"""Phase 3.1 Lane 2a — multi-turn SessionState + submit() + visible-mode routing (§23).

`SessionState` is the scope *above* `TaskState`: conversation history, the sequence of
per-goal tasks, session-scoped grants, and the current mode. A `ReplSession` driver runs
each goal as one fresh `TaskState` through the existing single-task `Session` (one code
path — batch is the degenerate one-submit case), seeding the new task with prior history
and carrying grants forward. No TUI here; this is the pure-logic scope the cockpit renders.
"""

import asyncio

import pytest
from conftest import ScriptedModel
from pydantic import BaseModel

from avatar.config import HarnessConfig
from avatar.harness import Harness
from avatar.model_client import AskUser, FinalAnswer, ModelDecision, ToolCall
from avatar.session_state import ReplSession, default_mode
from avatar.tools.base import ToolDefinition, ToolRegistry, ToolResult
from avatar.tools.edit import write_file
from avatar.tools.filesystem import read_file
from avatar.workspace import DirtyWorkspaceError


class _Empty(BaseModel):
    pass


def _read_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(read_file)
    return reg


def _gated_registry() -> ToolRegistry:
    """read_file + a tier-3 `risky` tool (reachable in `investigating`, no file mutation)."""

    def _risky(args, deps) -> ToolResult:
        return ToolResult(tool_name="risky", success=True, summary="did the thing")

    risky = ToolDefinition(
        name="risky",
        description="needs approval",
        input_model=_Empty,
        handler=_risky,
        phases=frozenset({"investigating"}),
        permission_tier=3,
    )
    reg = _read_registry()
    reg.register(risky)
    return reg


def _harness(tmp_path, decisions, *, registry=None, **cfg) -> Harness:
    config = HarnessConfig(workspace_root=str(tmp_path), **cfg)
    return Harness(config=config, model=ScriptedModel(decisions), tools=registry or _read_registry())


def _repl(tmp_path, decisions, *, registry=None, **cfg) -> ReplSession:
    return ReplSession(_harness(tmp_path, decisions, registry=registry, **cfg))


async def _drive(session, *, remember_first: bool):
    """Run a per-goal session to completion, approving every prompt; return (state, prompts)."""
    requested: list = []
    stream = session.events()
    run_task = asyncio.create_task(session.run())
    async for ev in stream:
        if ev.type == "approval_requested":
            first = not requested
            requested.append(ev)
            await session.resolve_approval(ev.approval_id, allow=True, remember=(remember_first and first))
    state = await run_task
    return state, requested


# --- goal → task → session ---------------------------------------------------------------


async def test_submit_runs_goal_and_appends_turns(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="x is set in app.py")),
    ]
    repl = _repl(tmp_path, decisions)
    state = await repl.submit("explain where x is set in app.py")
    assert state.outcome == "success"
    assert len(repl.state.tasks) == 1  # one goal → one task
    assert [t.role for t in repl.state.history] == ["user", "agent"]
    assert repl.state.history[0].text == "explain where x is set in app.py"
    assert repl.state.history[1].task_id == state.task_id  # agent turn links to its task


async def test_each_submit_spins_a_fresh_taskstate(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="x is set in app.py")),
    ] * 2  # enough for two goals
    repl = _repl(tmp_path, decisions)
    s1 = await repl.submit("explain x in app.py")
    s2 = await repl.submit("explain x again in app.py")
    assert s1.task_id != s2.task_id  # a fresh TaskState per goal (invariant #1)
    assert len(repl.state.tasks) == 2


async def test_history_seeds_next_task_context(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="x is set in app.py")),
    ] * 2
    repl = _repl(tmp_path, decisions)
    await repl.submit("explain the widget in app.py")
    # the next task carries the prior conversation as real chat turns (ADR-0017), not evidence
    session2 = repl.start("and what about the button?")
    convo = session2.state.conversation
    assert convo  # prior turns seeded, not forgotten
    assert convo[0].role == "user" and "explain the widget" in convo[0].content
    assert any(t.role == "assistant" for t in convo)  # the prior reply rides as an assistant turn
    assert not any(e.kind == "history" for e in session2.state.evidence)  # no longer evidence bullets
    st = await session2.run()  # finish it so the session closes cleanly
    repl.record(st)


async def test_blocked_question_recorded_as_agent_turn_seeds_next_goal(tmp_path):
    # A goal that ends by asking the user must record the QUESTION as its agent turn, so
    # the next goal (the user's answer) carries the question in history — not "blocked".
    # Regression: events/f0957ed4… re-asked the same question every turn because the
    # model only ever saw `agent: blocked`, never its own question.
    question = "What features should the chatbot include?"
    repl = _repl(tmp_path, [ModelDecision(action=AskUser(question=question))])
    state = await repl.submit("write a chatbot")
    assert state.outcome == "blocked"
    assert repl.state.history[-1].role == "agent"
    assert repl.state.history[-1].text == question  # the question, not "blocked"
    # the next goal (the answer) seeds the question as an assistant chat turn, not the outcome
    session2 = repl.start("basic streaming, minimal")
    convo = session2.state.conversation
    assert any(t.role == "assistant" and t.content == question for t in convo)
    assert not any(t.content.strip() == "blocked" for t in convo)


# --- visible-mode routing ----------------------------------------------------------------


def test_default_mode_is_heuristic_and_visible():
    assert default_mode("explain how the loop works") == "investigate"
    assert default_mode("why does it hang?") == "investigate"
    assert default_mode("fix the failing auth test") == "edit"
    assert default_mode("add a retry to the client") == "edit"


def test_explicit_mode_overrides_heuristic(tmp_path):
    # No override → heuristic picks investigate for a question-shaped prompt.
    assert _repl(tmp_path, []).start("explain how X works").state.task_kind == "investigate"
    # Explicit edit mode forces the kind even for a question-shaped prompt (visible, correctable).
    repl = _repl(tmp_path, [])
    repl.set_mode("edit")
    assert repl.start("explain how X works").state.task_kind == "edit"


# --- grants & event stream across the session --------------------------------------------


async def test_session_grant_persists_across_tasks(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    read = ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"}))
    risky = ModelDecision(action=ToolCall(name="risky", input={}))
    answer = ModelDecision(action=FinalAnswer(answer="x is set in app.py"))
    repl = _repl(tmp_path, [read, risky, answer, read, risky, answer], registry=_gated_registry())

    s1 = repl.start("inspect and run the risky thing")
    st1, req1 = await _drive(s1, remember_first=True)
    repl.record(st1)
    assert len(req1) == 1  # task 1 prompted the human once
    assert repl.state.grants  # the grant was recorded at session scope

    s2 = repl.start("do the risky thing again")
    st2, req2 = await _drive(s2, remember_first=False)
    repl.record(st2)
    assert len(req2) == 0  # task 2 auto-allowed by the persisted grant — no re-prompt


async def test_session_exposes_task_event_stream(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer="x is set in app.py")),
    ]
    repl = _repl(tmp_path, decisions)
    session = repl.start("explain x in app.py")
    seen: list = []
    stream = session.events()
    run_task = asyncio.create_task(session.run())
    async for ev in stream:
        seen.append(ev.type)
    repl.record(await run_task)
    assert {"agent_start", "tool_end", "agent_end"} <= set(seen)  # cockpit can render the run


# --- session-owned dirt (multi-turn §15) ---------------------------------------------------
#
# The clean-start check protects the FIRST goal of a sitting (don't conflate pre-existing
# user changes with agent changes). After that, uncommitted changes are the session's own
# work product — a follow-up goal must not be refused because the previous goal succeeded
# (the dogfood crash: goal 1 staged `scripts/chatbot.py`, goal 2 died on DirtyWorkspaceError).


def _edit_then_read_registry() -> ToolRegistry:
    reg = _read_registry()
    reg.register(write_file)
    return reg


async def test_follow_up_goal_tolerates_session_owned_dirt(git_repo):
    decisions = [
        # goal 1 (edit): create a file — leaves the tree dirty (staged, uncommitted)
        ModelDecision(action=ToolCall(name="write_file", input={"path": "util.py", "content": "x = 1\n"})),
        ModelDecision(action=FinalAnswer(answer="created util.py")),
        # goal 2 (investigate): a follow-up against the session's own dirt
        ModelDecision(action=ToolCall(name="read_file", input={"path": "util.py"})),
        ModelDecision(action=FinalAnswer(answer="util.py sets x")),
    ]
    repl = _repl(git_repo, decisions, registry=_edit_then_read_registry())
    first = await repl.submit("create util.py with x = 1")
    assert first.outcome == "success"
    follow_up = await repl.submit("explain x in util.py")  # must NOT raise DirtyWorkspaceError
    assert follow_up.outcome is not None
    assert len(repl.state.tasks) == 2


async def test_first_goal_still_refuses_preexisting_dirt(git_repo):
    (git_repo / "calc.py").write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")
    repl = _repl(git_repo, [])
    with pytest.raises(DirtyWorkspaceError):  # §15 intact: the sitting opens on a dirty tree
        await repl.submit("explain calc.py")


async def test_failed_first_goal_does_not_pollute_history(git_repo):
    # A first goal that can't open the workspace (dirty tree, §15) must leave NO phantom user
    # turn: ADR-0017 replays history as real chat turns, so a never-run prompt would otherwise
    # haunt the next goal's conversation. Regression for the L380→below-open reordering.
    committed = "def add(a, b):\n    return a - b\n"  # the git_repo fixture's committed calc.py
    (git_repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")  # dirty
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer="ok")),
    ]
    repl = _repl(git_repo, decisions)
    with pytest.raises(DirtyWorkspaceError):
        repl.start("phantom goal")  # builds the session; raises before the turn is kept
    assert repl.state.history == []  # no phantom user turn recorded

    (git_repo / "calc.py").write_text(committed, encoding="utf-8")  # clean the tree, then start fresh
    session2 = repl.start("real goal")
    assert all("phantom" not in t.content for t in session2.state.conversation)


async def test_repl_allow_dirty_opt_in(git_repo):
    (git_repo / "calc.py").write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer="calc.py multiplies")),
    ]
    config = HarnessConfig(workspace_root=str(git_repo))
    harness = Harness(config=config, model=ScriptedModel(decisions), tools=_read_registry())
    repl = ReplSession(harness, allow_dirty=True)  # the --allow-dirty acknowledgement
    state = await repl.submit("explain calc.py")
    assert state.outcome is not None  # ran; the dirty tree was deliberately acknowledged


async def test_batch_is_degenerate_session(tmp_path):
    # One submit with no further input == the one-shot engine run (§23.2 one code path).
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    answer = "x is set in app.py"
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "app.py"})),
        ModelDecision(action=FinalAnswer(answer=answer)),
    ]
    direct = await _harness(tmp_path, list(decisions)).arun("explain x in app.py")
    repl = _repl(tmp_path, list(decisions))
    via_repl = await repl.submit("explain x in app.py")
    assert direct.outcome == via_repl.outcome == "success"
    assert len(repl.state.tasks) == 1
