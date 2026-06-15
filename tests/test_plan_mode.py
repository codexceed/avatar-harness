"""Phase 3.2c — plan mode (§23, ADR-0002 Decision 5).

Plan mode is a session *interaction mode*, not a `task_kind`. It rides the existing
contract: a **plan task** (`task_kind="investigate"`, so the verifier's net-zero-diff
contract requires the tree unchanged at verification — ADR-0005 admits transient
instrumentation) proposes a plan, the human **approves or revises**, and the approved plan
**seeds the edit task as a constraint** (which `model_client` surfaces). Revise re-runs the
plan task so the model refines it (ADR mermaid: revise → PLAN). No new control plane.

Pure `ReplSession` logic here (mirroring 3.2a meta + 3.2b grounding); the cockpit renders
the flow through `PlanModal` in 3.2e. The `decide` callback stands in for that modal — in
tests it is injected, returning a `PlanDecision(approved, text)`.
"""

import pytest
from conftest import CyclingModel, ScriptedModel

from avatar_harness.config import HarnessConfig
from avatar_harness.harness import Harness
from avatar_harness.model_client import AskUser, FinalAnswer, ModelDecision, ToolCall
from avatar_harness.session_state import PlanDecision, ReplSession, default_mode
from avatar_harness.tools.base import ToolRegistry
from avatar_harness.tools.edit import str_replace
from avatar_harness.tools.filesystem import read_file

# An exact-text edit against the `git_repo` fixture's calc.py (fixes the `-` bug).
_FIX = {"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"}


def _repl(root, decisions=None, *, edit=False, model=None, **cfg) -> ReplSession:
    """A ReplSession whose registry holds the read tools (+ str_replace when `edit`)."""
    reg = ToolRegistry()
    reg.register(read_file)
    if edit:
        reg.register(str_replace)
    config = HarnessConfig(workspace_root=str(root), **cfg)
    client = model if model is not None else ScriptedModel(decisions or [])
    return ReplSession(Harness(config=config, model=client, tools=reg))


def _evidence(session, kind: str) -> list:
    return [e for e in session.state.evidence if e.kind == kind]


class _Decider:
    """Records calls and replays a script of `PlanDecision`s — stands in for the PlanModal."""

    def __init__(self, *responses: PlanDecision) -> None:
        self._responses = responses
        self.proposals: list[str] = []

    def __call__(self, proposed: str) -> PlanDecision:
        self.proposals.append(proposed)
        i = min(len(self.proposals) - 1, len(self._responses) - 1)
        resp = self._responses[i]
        # An approval with no edited text keeps the proposed plan.
        return resp if resp.text else PlanDecision(approved=resp.approved, text=proposed)


# --- the read-only plan task -------------------------------------------------------------


def test_plan_mode_task_is_read_only(tmp_path):
    repl = _repl(tmp_path)
    repl.set_mode("plan")
    session = repl.start("rework the auth flow")
    # The plan task investigates — any transient instrumentation must net to zero diff
    # at verification (the investigate contract; ADR-0005).
    assert session.state.task_kind == "investigate"
    assert session.state.phase == "investigating"
    # A planning directive is seeded so the model proposes a plan rather than answering.
    assert any("plan" in c.lower() for c in session.state.constraints)


# --- the approved plan seeds the edit task -----------------------------------------------


def test_approved_plan_seeds_edit_task_constraints(tmp_path):
    plan = "1. flip the sign in add()\n2. keep the signature"
    session = _repl(tmp_path).start_build("fix the add bug", plan)
    assert session.state.task_kind == "edit"  # the build task can reach the edit tools
    assert any(plan in c for c in session.state.constraints)  # approved plan rides as a constraint


# --- the full plan → approve → build flow ------------------------------------------------


async def test_plan_flow_runs_plan_then_build(git_repo):
    the_plan = "PLAN: in calc.py, change `-` to `+` in add()"
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer=the_plan)),  # plan task proposes the plan
        ModelDecision(action=ToolCall(name="str_replace", input=_FIX)),
        ModelDecision(action=FinalAnswer(answer="fixed add()")),  # build task
    ]
    repl = _repl(git_repo, decisions, edit=True, test_command="true", lint_command="true")
    decide = _Decider(PlanDecision(approved=True, text=""))  # approve the proposed plan as-is
    state = await repl.submit_plan("fix the add bug", decide)

    assert state.task_kind == "edit"
    assert state.outcome == "success"  # plan → approve → edit → verified
    assert len(repl.state.tasks) == 1  # only the build task is the recorded goal task
    assert any(the_plan in c for c in state.constraints)  # the approved plan seeded the build
    assert len(decide.proposals) == 1  # approved on the first proposal
    assert [t.role for t in repl.state.history] == ["user", "agent"]  # one turn pair, not doubled


async def test_revise_reruns_plan_before_build(git_repo):
    the_plan = "PLAN: in calc.py, change `-` to `+` in add()"
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer=the_plan)),  # first plan attempt
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer=the_plan)),  # re-plan after revise
        ModelDecision(action=ToolCall(name="str_replace", input=_FIX)),
        ModelDecision(action=FinalAnswer(answer="fixed add()")),  # build task
    ]
    repl = _repl(git_repo, decisions, edit=True, test_command="true", lint_command="true")
    decide = _Decider(
        PlanDecision(approved=False, text="also keep the docstring"),  # revise → re-plan
        PlanDecision(approved=True, text=""),  # then approve
    )
    state = await repl.submit_plan("fix the add bug", decide)

    assert len(decide.proposals) == 2  # the plan task re-ran on revise before approval
    assert state.outcome == "success"
    assert len(repl.state.tasks) == 1  # no build task ran until the plan was approved
    assert any(the_plan in c for c in state.constraints)


# --- mode plumbing -----------------------------------------------------------------------


def test_plan_meta_command_enters_plan_mode(tmp_path):
    repl = _repl(tmp_path)
    result = repl.run_meta("/plan")
    assert result.kind == "mode_set"
    assert repl.mode == "plan"

    repl2 = _repl(tmp_path)
    assert repl2.run_meta("/mode plan").kind == "mode_set"  # /mode plan is also valid
    assert repl2.mode == "plan"


def test_heuristic_never_selects_plan(tmp_path):
    # The heuristic only ever returns edit/investigate — plan is opt-in (visible), never inferred.
    assert default_mode("fix the failing test") == "edit"
    assert default_mode("explain how the loop works") == "investigate"
    repl = _repl(tmp_path)  # no explicit override
    assert repl.resolve_mode("fix the failing test") != "plan"
    assert repl.resolve_mode("explain how the loop works") != "plan"


# --- plan mode composes with history + grounding -----------------------------------------


async def test_plan_task_seeds_history_and_grounding(git_repo):
    decisions = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer="calc.py adds two numbers")),
    ]
    repl = _repl(git_repo, decisions)
    await repl.submit("explain calc.py")  # a prior turn populates history

    repl.set_mode("plan")
    session = repl.start("rework @calc.py")  # plan task with an @path reference
    assert session.state.task_kind == "investigate"  # still the read-only plan task
    # prior conversation rides as real chat turns (ADR-0017); @path grounding stays evidence
    assert any("explain calc.py" in t.content for t in session.state.conversation)
    assert any("calc.py" in e.summary for e in _evidence(session, "grounding"))


# --- guards: an abnormal plan never reaches approval/build (PR #17 review) ----------------


async def test_blocked_plan_is_surfaced_not_approved(tmp_path):
    # A plan run that blocks on input produces no plan — it must not flow into decide()/build.
    repl = _repl(tmp_path, [ModelDecision(action=AskUser(question="which module?"))])
    asked: list[str] = []

    def decide(proposed: str) -> PlanDecision:
        asked.append(proposed)  # would mean "approve an empty plan" — must never happen
        return PlanDecision(approved=True, text="")

    state = await repl.submit_plan("rework the auth flow", decide)
    assert asked == []  # the human was never asked to approve an empty/blocked plan
    assert state.outcome == "blocked"  # the terminal planning state is surfaced instead
    assert len(repl.state.tasks) == 1
    assert [t.role for t in repl.state.history] == ["user", "agent"]  # still recorded as one goal


async def test_never_approving_decider_stops_at_revision_budget(git_repo):
    # A programmatic decide() that never approves must not spin forever (budget discipline).
    cycle = [
        ModelDecision(action=ToolCall(name="read_file", input={"path": "calc.py"})),
        ModelDecision(action=FinalAnswer(answer="PLAN: in calc.py, flip the sign")),
    ]  # one full cycle = one valid plan task; the model keeps producing fresh plans
    repl = _repl(git_repo, model=CyclingModel(cycle))
    calls = 0

    def never_approve(proposed: str) -> PlanDecision:
        nonlocal calls
        calls += 1
        return PlanDecision(approved=False, text="needs more detail")

    state = await repl.submit_plan("fix the add bug", never_approve)
    assert state.outcome == "incomplete"  # the revision budget was hit; no build ran
    assert calls >= 1  # the loop terminated (did not hang) rather than approving


async def test_submit_in_plan_mode_requires_submit_plan(tmp_path):
    # plan mode has no run-to-completion path — submit() must refuse rather than silently
    # running a directive-laden read-only task as a goal.
    repl = _repl(tmp_path)
    repl.set_mode("plan")
    with pytest.raises(ValueError, match="submit_plan"):
        await repl.submit("rework the auth flow")
