"""Mid-run `investigate → edit` escalation + execution in investigation (ADR-0048).

The verifier steers (ADR-0046), but only for tasks that *reach* verification. A fix goal
misrouted to `investigate` never does — it edits blind (no execution) and can't keep its
changes (net-zero-diff), so it thrashes (the `tetris_grok3` spiral). This suite pins the
recovery path: `run_command` admitted in `investigating`; a consented `switch_to_editing`
that flips the kind; the baseline-clean freeze; and the harness thrash nudge.
"""

import subprocess

from conftest import ScriptedModel

from avatar.config import HarnessConfig
from avatar.context import ContextBuilder
from avatar.deps import CancellationToken, RunDeps
from avatar.event_types import TaskEscalated
from avatar.events import Emitter
from avatar.model_client import FinalAnswer, ModelDecision, ToolCall
from avatar.runner import AgentRunner
from avatar.session import UNGRANTABLE_TOOLS, Session
from avatar.state import TaskState
from avatar.tools import default_registry
from avatar.tools.base import ToolRegistry, phase_admits_tool
from avatar.tools.commands import run_command, run_linter, run_tests
from avatar.tools.edit import str_replace, write_file
from avatar.tools.filesystem import read_file
from avatar.tools.verification import switch_to_editing
from avatar.verifier import Verifier
from avatar.workspace import Workspace


def _runner(tmp_path, decisions=None, *, registry=None, emitter=None, **cfg) -> AgentRunner:
    config = HarnessConfig(**cfg)
    reg = registry or default_registry()
    deps = RunDeps(workspace=Workspace(tmp_path), config=config, cancellation=CancellationToken())
    return AgentRunner(
        model_client=ScriptedModel(decisions or []),
        registry=reg,
        deps=deps,
        context_builder=ContextBuilder(),
        verifier=Verifier(config),
        emitter=emitter or Emitter(),
        config=config,
    )


# --- D1: execution admitted in investigation (run_command only) ----------------------------


def test_run_command_admitted_in_investigating_but_not_run_tests():
    # ADR-0048: run_command joins `investigating` (it attributes its side effects); run_tests /
    # run_linter do not (no accounting, no pre-escalation plan).
    assert phase_admits_tool("investigating", "investigate", run_command)
    assert not phase_admits_tool("investigating", "investigate", run_tests)
    assert not phase_admits_tool("investigating", "investigate", run_linter)
    # Still available where they always were.
    assert phase_admits_tool("editing", "edit", run_command)
    assert phase_admits_tool("verifying", "edit", run_tests)


# --- the switch_to_editing control tool ----------------------------------------------------


def test_switch_to_editing_is_gated_investigating_control_tool():
    assert switch_to_editing.permission_tier == 3  # gated: attended asks, unattended per policy
    assert switch_to_editing.phases == frozenset({"investigating"})  # reachable where it's needed
    assert default_registry().get("switch_to_editing") is switch_to_editing
    assert "switch_to_editing" in UNGRANTABLE_TOOLS  # every escalation needs fresh consent


# --- _escalate_to_edit: the one-directional, once-only kind flip ----------------------------


def test_escalate_flips_kind_and_emits_event(tmp_path):
    events: list = []
    emitter = Emitter()
    emitter.subscribe(events.append)
    runner = _runner(tmp_path, emitter=emitter)
    state = TaskState(goal="fix the import error", task_kind="investigate")

    runner._escalate_to_edit(state, trigger="model")

    assert state.task_kind == "edit"
    assert state.escalated is True
    escalated = [e for e in events if e["type"] == "task_escalated"]
    assert escalated and escalated[-1]["trigger"] == "model"
    assert any(ev.kind == "escalation" for ev in state.evidence)


def test_escalate_is_one_directional_and_once(tmp_path):
    runner = _runner(tmp_path)
    # An edit task never escalates (there is nothing to escalate to).
    edit = TaskState(goal="x", task_kind="edit")
    runner._escalate_to_edit(edit, trigger="model")
    assert edit.escalated is False and edit.task_kind == "edit"
    # A second escalation is a no-op (once-only).
    inv = TaskState(goal="x", task_kind="investigate")
    runner._escalate_to_edit(inv, trigger="model")
    inv.task_kind = "investigate"  # pretend something tried to flip it back
    runner._escalate_to_edit(inv, trigger="thrash")
    assert inv.task_kind == "investigate"  # guarded by `escalated`, not re-flipped


# --- D2: escalation freezes a baseline-clean contract, not a planted one --------------------


def test_baseline_resolution_ignores_a_contract_planted_mid_investigation(tmp_path):
    # The hole: an agent writes a Makefile during investigation, escalates, and has it frozen as
    # its own passing rubric (self-certification). The eager baseline resolution closes it.
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
        check=True,
    )
    runner = _runner(tmp_path, test_command="", lint_command="")
    ws = runner.deps.workspace
    baseline = runner._resolve_plan(ws)  # resolved at run open, tree pristine → nothing detected
    assert baseline == []

    # The agent now plants a Makefile that would "pass" trivially.
    (tmp_path / "Makefile").write_text("test:\n\techo planted\n", encoding="utf-8")
    # Escalation-time resolution returns the cached baseline, NOT the planted contract.
    assert runner._resolve_plan(ws) == []


# --- D4b: the harness thrash nudge (surface the signal; do not auto-escalate) ---------------


def test_thrash_nudge_fires_after_repeats_with_a_persistent_diff(tmp_path):
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / "scratch.py").write_text("print(1)\n", encoding="utf-8")  # untracked → status non-empty
    runner = _runner(tmp_path, escalation_thrash_repeats=3)
    ws = runner.deps.workspace
    state = TaskState(goal="fix it", task_kind="investigate")

    for _ in range(2):
        runner._maybe_nudge_escalation(state, ws)
    assert not any(e.kind == "escalation_nudge" for e in state.evidence)  # not yet
    runner._maybe_nudge_escalation(state, ws)  # third repeat-with-diff
    assert any(e.kind == "escalation_nudge" for e in state.evidence)
    assert state.escalated is False  # a nudge, NOT an auto-escalation — the model must consent


def test_thrash_nudge_silent_without_a_diff(tmp_path):
    # A clean tree (no persisted diff) is a legitimate read-only investigation — never nudged.
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    runner = _runner(tmp_path, escalation_thrash_repeats=1)
    state = TaskState(goal="explain it", task_kind="investigate")
    for _ in range(5):
        runner._maybe_nudge_escalation(state, runner.deps.workspace)
    assert not any(e.kind == "escalation_nudge" for e in state.evidence)


# --- D4: consent — escalation is a proposal (attended asks; unattended per policy) ----------


async def test_unattended_auto_approves_scoped_escalation(tmp_path):
    session = Session(
        _runner(tmp_path),
        TaskState(goal="x", task_kind="investigate"),
        unattended=True,
        escalation_policy="auto",
    )
    assert await session.request_approval("a1", "switch_to_editing", "gated", {}) is True
    # Scoped by tool NAME, not tier: run_command (also tier 3) still auto-denies.
    assert await session.request_approval("a2", "run_command", "gated", {}) is False


async def test_unattended_denies_escalation_by_default(tmp_path):
    session = Session(
        _runner(tmp_path),
        TaskState(goal="x", task_kind="investigate"),
        unattended=True,
    )
    assert await session.request_approval("a1", "switch_to_editing", "gated", {}) is False


# --- end-to-end: a consented switch_to_editing turns the run into an edit task ---------------


async def test_model_requested_escalation_end_to_end(tmp_path):
    reg = ToolRegistry()
    for tool in (read_file, str_replace, write_file, switch_to_editing):
        reg.register(tool)
    decisions = [
        ModelDecision(action=ToolCall(name="switch_to_editing", input={"reason": "this is a fix"})),
        ModelDecision(action=FinalAnswer(answer="fixed")),
    ]
    session = Session(
        _runner(tmp_path, decisions, registry=reg, test_command="true", lint_command="true"),
        TaskState(goal="the import is broken", task_kind="investigate"),
        unattended=True,
        escalation_policy="auto",
    )
    events: list = []
    queue = session.bus.subscribe()
    state = await session.run()
    while not queue.empty():
        events.append(queue.get_nowait())

    assert state.task_kind == "edit"  # the run is now an edit task
    assert state.escalated is True
    assert any(isinstance(e, TaskEscalated) for e in events)
